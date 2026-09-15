"""Live WRITE gate: real fills, passive de-risk, and emergency exit through HB.

Functional-plan step 3, second half (see `perp-bot/status.md`), matching the
Phase 2 live-gate scenario (`clmm-animation/docs/perp-phase2-implementation-plan.md`
§7.1). `run_hb_mainnet_quote_gate.py` proved quote create/refresh/cancel; this
gate proves the non-quoting execution body on a real micro position:

  1. open    — a MARKET OrderExecutor buys `--open-size`: a real fill reaches
               the FillObserver, and the controller's position (venue truth)
               reconciles to it.
  2. de-risk — with caps below the position, the keeper's own RiskPolicy says
               DE_RISK; the controller runs a reduce-only
               PassiveAggressiveExecutor at real control-cycle cadence, which
               rests, refreshes, and flattens the position (COMPLETED).
  3. emergency — re-open, inject a drawdown into the real RiskPolicy so the
               keeper says EMERGENCY_EXIT; the short-cycle PA must flatten
               within `--emergency-deadline` seconds.

Quote-creating actions are dropped (and counted) throughout: the gate is trying
to get flat and must never open fresh exposure. Teardown force-cancels and
market-closes through the raw HL SDK if the HB path left anything behind —
that fallback is itself a gate failure.

Usage:
  OPMS_HB_MAINNET=confirm OPMS_HB_PLACE_ORDERS=confirm \
  python scripts/run_hb_mainnet_derisk_gate.py --account-id e2_mm1 --dry-run
"""

import argparse
import asyncio
import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_hb_mainnet_quote_gate import (  # noqa: E402  (shared gate plumbing)
    _DryRunComplete,
    _GateStrategy,
    _account_addresses,
    _create_executors,
    _hl_exchange,
    _hl_info,
    _jsonable,
    _log_to,
    _pump,
    _resting_oids,
    _stop_executors,
    _venue_position,
)
from run_hb_mainnet_smoke import CONNECTOR_NAME, PAIR, _build_connector, _resolve_account, _wait_ready  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
SIZE_EPS = 1e-9


class _RecordingStrategy(_GateStrategy):
    """Records every order the executors send, so the gate can prove *how* a
    position was closed (resting limit vs aggressive market)."""

    def __init__(self, connectors):
        super().__init__(connectors)
        self.orders: list[dict] = []

    def _record(self, side, order_type, amount, price, position_action):
        self.orders.append({
            "ts": time.time(), "side": side, "order_type": order_type.name,
            "amount": float(amount), "price": None if price is None or price.is_nan() else float(price),
            "position_action": position_action.name,
        })

    def buy(self, connector_name, trading_pair, amount, order_type, price, position_action):
        self._record("buy", order_type, amount, price, position_action)
        return super().buy(connector_name, trading_pair, amount, order_type, price, position_action)

    def sell(self, connector_name, trading_pair, amount, order_type, price, position_action):
        self._record("sell", order_type, amount, price, position_action)
        return super().sell(connector_name, trading_pair, amount, order_type, price, position_action)


def _reduce_only_orders(info, address: str, coin: str) -> list[dict]:
    return [
        {"oid": o["oid"], "side": o["side"], "px": o["limitPx"], "sz": o["sz"], "type": o.get("orderType")}
        for o in info.frontend_open_orders(address)
        if o["coin"] == coin and o.get("reduceOnly")
    ]


def _account_indicators(info, address: str, controller) -> dict:
    """Every account-level number HL publishes, next to what HB feeds the
    keeper as equity — raw material for choosing the drawdown indicator on
    unified accounts, where HB's balance excludes unrealized PnL."""
    perp = info.user_state(address)
    spot = info.spot_user_state(address)
    usdc = next((b for b in spot["balances"] if b["coin"] == "USDC"), {})
    avail = dict((int(t), v) for t, v in spot.get("tokenToAvailableAfterMaintenance", []))
    return {
        "hb_equity": float(controller._current_equity()),
        "spot_usdc_total": float(usdc.get("total", 0)),
        "spot_usdc_hold": float(usdc.get("hold", 0)),
        "spot_available_after_maintenance": float(avail.get(0, 0)),
        "perp_account_value": float(perp["marginSummary"]["accountValue"]),
        "perp_total_margin_used": float(perp["marginSummary"]["totalMarginUsed"]),
        "perp_total_ntl_pos": float(perp["marginSummary"]["totalNtlPos"]),
        "perp_cross_maintenance_margin_used": float(perp["crossMaintenanceMarginUsed"]),
        "perp_withdrawable": float(perp["withdrawable"]),
        "perp_unrealized_pnl": sum(float(e["position"]["unrealizedPnl"]) for e in perp["assetPositions"]),
    }


async def _wait_for(predicate, timeout_s: float, interval: float = 1.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


async def _sleep_with_order_poll(seconds: float, info, address: str, coin: str,
                                 order_log: list[dict] | None, poll_s: float = 1.0) -> None:
    """Sleep in slices, recording venue reduce-only orders on each slice.

    The pump cadence alone (~9.4 s: 5 s sleep + HL REST latency) missed a
    reduce-only order that rested less than 8 s before filling (2026-09-15),
    so the gate's "did it ever rest" check must not depend on it."""
    if order_log is None:
        await asyncio.sleep(seconds)
        return
    remaining = seconds
    while remaining > 0:
        for order in _reduce_only_orders(info, address, coin):
            order_log.append({"ts": round(time.time(), 1), **order})
        slice_s = min(poll_s, remaining)
        await asyncio.sleep(slice_s)
        remaining -= slice_s


async def _open_position(controller, strategy, executors, size: Decimal, info, address, coin, timeout_s):
    """MARKET buy through a real OrderExecutor; returns the open-phase evidence."""
    from hummingbot.core.data_type.common import PositionAction, TradeType
    from hummingbot.strategy_v2.executors.order_executor.data_types import ExecutionStrategy, OrderExecutorConfig
    from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction

    await controller.update_processed_data()  # FillObserver needs a mid before the fill
    fills_before = controller._fill_observer.n_fills
    config = OrderExecutorConfig(
        timestamp=time.time(), trading_pair=PAIR, connector_name=CONNECTOR_NAME,
        side=TradeType.BUY, amount=size, price=None,
        execution_strategy=ExecutionStrategy.MARKET, position_action=PositionAction.OPEN, leverage=1,
    )
    opened = _create_executors([CreateExecutorAction(controller_id=controller.config.id, executor_config=config)],
                               strategy)
    executors.extend(opened)
    started = time.time()
    venue_ok = await _wait_for(lambda: abs(_venue_position(info, address, coin) - float(size)) < SIZE_EPS,
                               timeout_s, interval=2.0)
    reconciled = await _wait_for(
        lambda: abs(float(controller._current_base_position()) - float(size)) < SIZE_EPS, timeout_s)
    fill_seen = await _wait_for(lambda: controller._fill_observer.n_fills > fills_before, 10.0)
    return {
        "venue_position": _venue_position(info, address, coin),
        "controller_position": float(controller._current_base_position()),
        "venue_ok": venue_ok, "reconciled": reconciled,
        "reconcile_s": round(time.time() - started, 1),
        "fill_seen": fill_seen, "n_fills": controller._fill_observer.n_fills,
        "executor_close_type": str(opened[0].close_type),
    }


async def _run_cycles(controller, strategy, executors, info, address, coin, *, seconds, cycle_s, samples,
                      order_log: list[dict] | None = None, poll_s: float = 1.0):
    """Drive the controller like ControllerBase.control_task does
    (update_processed_data -> determine_executor_actions -> execute), until the
    venue position is flat. Quote creates are dropped, not executed."""
    from hummingbot.strategy_v2.executors.order_executor.data_types import ExecutionStrategy
    from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, StopExecutorAction

    created: list = []
    suppressed_quotes = 0
    deadline = time.time() + seconds
    while time.time() < deadline:
        controller.executors_info = [e.executor_info for e in executors]
        await controller.update_processed_data()
        intent = controller._client.last_intent
        actions = controller.determine_executor_actions()
        by_id = {e.config.id: e for e in executors}
        creates = []
        for action in actions:
            if isinstance(action, StopExecutorAction):
                executor = by_id.get(action.executor_id)
                if executor is not None:
                    executor.early_stop()
            elif isinstance(action, CreateExecutorAction):
                cfg = action.executor_config
                if getattr(cfg, "execution_strategy", None) == ExecutionStrategy.LIMIT_MAKER:
                    suppressed_quotes += 1
                else:
                    creates.append(action)
        new = _create_executors(creates, strategy)
        executors.extend(new)
        created.extend(new)
        position = _venue_position(info, address, coin)
        samples.append({
            "ts": round(time.time(), 1), "venue_position": position,
            "controller_position": float(controller._current_base_position()),
            "urgency": None if intent is None else intent.urgency,
            "quoting": bool(intent is not None and intent.quote is not None),
            "actions": [type(a).__name__ for a in actions],
            "reduce_only_resting": _reduce_only_orders(info, address, coin),
            "account": _account_indicators(info, address, controller),
        })
        if abs(position) < SIZE_EPS:
            return created, suppressed_quotes, True
        await _sleep_with_order_poll(cycle_s, info, address, coin, order_log, poll_s)
    return created, suppressed_quotes, abs(_venue_position(info, address, coin)) < SIZE_EPS


def _equity_marks_unrealized_pnl(samples: list) -> str | None:
    """The keeper's drawdown stop is only sound if the equity HB reports moves
    with open-position PnL. On HL unified accounts that equity is the spot USDC
    total, which HL marks to market: while the position is unchanged,
    `total - unrealized_pnl` (cash) must stay constant. Verified 2026-09-15;
    fail loudly if HL ever changes that."""
    open_samples = [s for s in samples if abs(s["venue_position"]) > SIZE_EPS]
    by_size: dict = {}
    for s in open_samples:
        a = s["account"]
        by_size.setdefault(s["venue_position"], []).append(a["spot_usdc_total"] - a["perp_unrealized_pnl"])
    for size, cash in by_size.items():
        if len(cash) > 1 and max(cash) - min(cash) > 1e-3:
            return f"spot USDC total no longer marks unrealized PnL at position {size}: cash drifted {min(cash):.4f}..{max(cash):.4f}"
    return None


def _pa_executed(e) -> float:
    """Authoritative executed size for a PA executor (base units)."""
    try:
        return float(e.executor_info.custom_info.get("cumulative_filled", 0.0))
    except Exception:  # noqa: BLE001 — executor_info can fail pre-start
        return float(getattr(e, "_cumulative_filled", 0.0) or 0.0)


def _pa_outcome_true(e) -> bool:
    """COMPLETED only counts when the full size actually executed.

    A PA that skipped a child (observed live 2026-09-15) used to close
    COMPLETED with nothing executed; the close type is no longer trusted
    on its own — the executed size is.
    """
    total = float(getattr(e.config, "total_amount_base", 0) or 0)
    return "COMPLETED" in str(e.close_type) and total > 0 and _pa_executed(e) >= total - 1e-9


def _pa_summary(executors) -> list[dict]:
    return [
        {
            "id": e.config.id, "type": e.config.type,
            "child_order_time_limit": getattr(e.config, "child_order_time_limit", None),
            "position_action": getattr(getattr(e.config, "position_action", None), "name", None),
            "total_amount_base": float(getattr(e.config, "total_amount_base", 0) or 0),
            "cumulative_filled": _pa_executed(e),
            "children": [float(c.target) for c in getattr(e, "_children", [])],
            "close_type": str(e.close_type), "closed": e.is_closed,
        }
        for e in executors
    ]


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--account-id", default="e2_mm1")
    ap.add_argument("--use-vault", choices=["yes", "no"], default=None)
    ap.add_argument("--open-size", type=float, default=0.012, help="ETH per opened position (~$30)")
    ap.add_argument("--max-notional", type=float, default=60.0)
    ap.add_argument("--cycle-s", type=float, default=5.0, help="controller update_interval")
    ap.add_argument("--derisk-timeout", type=float, default=300.0)
    ap.add_argument("--emergency-deadline", type=float, default=30.0)
    ap.add_argument("--timeout", type=float, default=90.0)
    ap.add_argument("--artifact-dir", default=None)
    ap.add_argument("--dry-run", action="store_true", help="plan only; place nothing")
    args = ap.parse_args()

    if os.environ.get("OPMS_HB_MAINNET") != "confirm":
        print("refusing: set OPMS_HB_MAINNET=confirm", file=sys.stderr)
        return 2
    if os.environ.get("OPMS_HB_PLACE_ORDERS") != "confirm":
        print("refusing: set OPMS_HB_PLACE_ORDERS=confirm (this gate opens real positions)", file=sys.stderr)
        return 2

    from hummingbot.data_feed.market_data_provider import MarketDataProvider

    from opms.controllers.generic.perp_mm_bridge import ExecutionRequest
    from opms.controllers.generic.perp_mm_controller import PerpMMController, PerpMMControllerConfig

    stamp = time.strftime("%Y%m%dT%H%M%S")
    artifact_dir = Path(args.artifact_dir or (REPO_ROOT / "hb-enhanced-opms" / "logs" / f"derisk_gate_{stamp}"))
    artifact_dir.mkdir(parents=True, exist_ok=True)
    _log_to(artifact_dir / "gate.log")

    address, private_key, use_vault = _resolve_account(args.account_id, args.use_vault)
    coin = PAIR.split("-")[0]
    size = Decimal(str(args.open_size))
    info = _hl_info()
    others = {aid: addr for aid, addr in _account_addresses().items() if addr != address}

    report: dict = {"ts": stamp, "account_id": args.account_id, "address": address,
                    "use_vault": use_vault, "pair": PAIR, "open_size": args.open_size, "phases": []}
    failures: list[str] = []
    executors: list = []
    stop_pump = asyncio.Event()
    pump_task = None
    controller = None
    strategy = None
    connector = _build_connector(address, private_key, use_vault)
    print(f"account={args.account_id} use_vault={use_vault} address={address[:8]}..{address[-4:]}")
    print(f"artifact_dir={artifact_dir}")

    try:
        await connector._initialize_trading_pair_symbol_map()
        await connector.start_network()
        mid = await _wait_ready(connector, args.timeout)
        pump_task = asyncio.create_task(_pump(connector, stop_pump))

        start_position = _venue_position(info, address, coin)
        preexisting = _resting_oids(info, address, coin)
        notional = float(size) * mid
        print(f"mid={mid} position={start_position} resting={len(preexisting)} open_notional=${notional:.2f}")
        if start_position != 0.0 or preexisting:
            failures.append(f"account not clean (position={start_position}, resting={sorted(preexisting)})")
            raise RuntimeError("dirty account")
        if notional > args.max_notional:
            failures.append(f"open notional ${notional:.2f} exceeds --max-notional ${args.max_notional}")
            raise RuntimeError("notional guard")

        provider = MarketDataProvider(connectors={CONNECTOR_NAME: connector})
        config = PerpMMControllerConfig(
            id=f"hb-derisk-gate-{stamp}", controller_name="perp_mm", connector_name=CONNECTOR_NAME,
            trading_pair=PAIR, venue="hyperliquid", account_id=args.account_id, leverage=1,
            # caps below the opened size: the keeper's own RiskPolicy must say DE_RISK
            max_position=args.open_size / 4, critical_position=args.open_size / 2,
            decision_log_path=str(artifact_dir / "decisions.jsonl"),
        )
        controller = PerpMMController(config, provider, asyncio.Queue(), update_interval=args.cycle_s)
        await controller.on_start()
        strategy = _RecordingStrategy({CONNECTOR_NAME: connector})

        planned = controller._execution_actions(
            ExecutionRequest(side="sell", amount=args.open_size, urgency="normal", reduce_only=True)
        )[0].executor_config
        planned_emergency = controller._execution_actions(
            ExecutionRequest(side="sell", amount=args.open_size, urgency="emergency", reduce_only=True)
        )[0].executor_config
        report["phases"].append({"phase": "plan", "mid": mid, "de_risk": _jsonable(planned.model_dump()),
                                 "emergency": _jsonable(planned_emergency.model_dump())})
        print(f"plan: de-risk {planned.type} child={planned.child_order_quantity} "
              f"action={planned.position_action.name}; emergency child={planned_emergency.child_order_quantity} "
              f"limit={planned_emergency.child_order_time_limit}s")
        if args.dry_run:
            raise _DryRunComplete()

        # --- phase 1: real fill + position reconciliation -------------------
        opened = await _open_position(controller, strategy, executors, size, info, address, coin, args.timeout)
        opened["account"] = _account_indicators(info, address, controller)
        report["phases"].append({"phase": "open", **opened})
        print(f"open: {opened}")
        if not opened["venue_ok"]:
            failures.append(f"open: venue position {opened['venue_position']} != {args.open_size}")
            raise RuntimeError("open failed")
        if not opened["reconciled"]:
            failures.append(f"open: controller position {opened['controller_position']} never reconciled")
        if not opened["fill_seen"]:
            failures.append("open: FillObserver saw no fill event")
        leaked = {aid: _venue_position(info, addr, coin) for aid, addr in others.items()}
        if any(abs(v) > SIZE_EPS for v in leaked.values()):
            failures.append(f"open: position leaked onto another account {leaked}")

        # --- phase 2: passive de-risk to flat --------------------------------
        fills_before = controller._fill_observer.n_fills
        orders_before = len(strategy.orders)
        samples: list = []
        order_log: list = []
        started = time.time()
        created, suppressed, flat = await _run_cycles(
            controller, strategy, executors, info, address, coin,
            seconds=args.derisk_timeout, cycle_s=args.cycle_s, samples=samples,
            order_log=order_log,
        )
        await _wait_for(lambda: all(e.is_closed for e in created), 15.0)
        pa = [e for e in created if e.config.type == "passive_aggressive_executor"]
        resting_oids = {o["oid"] for s in samples for o in s["reduce_only_resting"]} | {
            o["oid"] for o in order_log
        }
        phase = {
            "phase": "de_risk", "flat": flat, "seconds": round(time.time() - started, 1),
            "executors": _pa_summary(created), "suppressed_quote_creates": suppressed,
            "orders": strategy.orders[orders_before:], "distinct_reduce_only_oids": sorted(resting_oids),
            "fills": controller._fill_observer.n_fills - fills_before,
            "order_failures": sum(e._current_retries for e in created),
            "samples": samples, "venue_order_poll": order_log,
        }
        report["phases"].append(phase)
        print(f"de-risk: flat={flat} in {phase['seconds']}s, executors={[(x['type'], x['close_type']) for x in phase['executors']]}, "
              f"reduce-only oids={len(resting_oids)}, fills={phase['fills']}")
        if not pa:
            failures.append("de-risk: no PassiveAggressiveExecutor was created")
        elif pa[0].config.child_order_time_limit != 60.0 or pa[0].config.position_action.name != "CLOSE":
            failures.append(f"de-risk: first PA is not the reduce-only 60s cycle: {_pa_summary(pa[:1])}")
        if len(pa) > 1:
            failures.append(f"de-risk: {len(pa)} PA executors created — churned or re-issued on a stale position")
        if phase["order_failures"]:
            failures.append(f"de-risk: {phase['order_failures']} order submission(s) rejected")
        if not flat:
            failures.append(f"de-risk: not flat after {args.derisk_timeout}s")
        if not resting_oids:
            failures.append("de-risk: never observed a resting reduce-only order on the venue")
        if pa and not any(_pa_outcome_true(e) for e in pa):
            failures.append(
                f"de-risk: no PA fully executed and closed COMPLETED "
                f"({[(str(e.close_type), _pa_executed(e)) for e in pa]})"
            )
        if phase["fills"] < 1:
            failures.append("de-risk: FillObserver recorded no de-risk fill")
        equity_problem = _equity_marks_unrealized_pnl(samples)
        if equity_problem:
            failures.append(f"equity: {equity_problem}")
        if any(o["position_action"] != "CLOSE" for o in phase["orders"]):
            failures.append("de-risk: a non-reduce-only order was sent")

        # --- phase 3: emergency exit -----------------------------------------
        if flat:
            await _stop_executors([e for e in executors if not e.is_closed], timeout_s=15.0)
            reopened = await _open_position(controller, strategy, executors, size, info, address, coin, args.timeout)
            report["phases"].append({"phase": "reopen", **reopened})
            if not (reopened["venue_ok"] and reopened["reconciled"]):
                failures.append(f"reopen failed: {reopened}")
                raise RuntimeError("reopen failed")
            equity = float(controller._current_equity())
            controller.keeper._risk._peak_equity = equity / 0.8  # 20% drawdown > 10% stop
            orders_before = len(strategy.orders)
            samples = []
            started = time.time()
            created, suppressed, flat = await _run_cycles(
                controller, strategy, executors, info, address, coin,
                seconds=args.emergency_deadline * 2, cycle_s=args.cycle_s, samples=samples,
                order_log=order_log,
            )
            elapsed = round(time.time() - started, 1)
            await _wait_for(lambda: all(e.is_closed for e in created), 15.0)
            orders = strategy.orders[orders_before:]
            market = [o for o in orders if o["order_type"] == "MARKET"]
            phase = {
                "phase": "emergency", "flat": flat, "seconds": elapsed,
                "executors": _pa_summary(created), "orders": orders,
                "market_order_after_s": round(market[0]["ts"] - started, 1) if market else None,
                "suppressed_quote_creates": suppressed,
                "order_failures": sum(e._current_retries for e in created), "samples": samples,
            }
            report["phases"].append(phase)
            print(f"emergency: flat={flat} in {elapsed}s, market_order_after_s={phase['market_order_after_s']}, "
                  f"executors={[(x['type'], x['child_order_time_limit'], x['close_type']) for x in phase['executors']]}")
            if not samples or samples[0]["urgency"] != "emergency":
                failures.append(f"emergency: keeper did not escalate (first urgency {samples[0]['urgency'] if samples else None})")
            if len(created) > 1:
                failures.append(f"emergency: {len(created)} executors created — re-issued on a stale position")
            if phase["order_failures"]:
                failures.append(f"emergency: {phase['order_failures']} order submission(s) rejected")
            if market and created:
                phase["market_order_after_decision_s"] = round(market[0]["ts"] - created[0].config.timestamp, 1)
                if phase["market_order_after_decision_s"] > 10.0:
                    failures.append(f"emergency: market order {phase['market_order_after_decision_s']}s after the "
                                    f"controller's decision (plan \u00a77.1: within 10 s)")
            elif not flat:
                failures.append("emergency: no market order fired and not flat")
            if not created or getattr(created[0].config, "child_order_time_limit", None) != 5.0:
                failures.append(f"emergency: short-cycle PA not created: {_pa_summary(created[:1])}")
            if not flat or elapsed > args.emergency_deadline:
                failures.append(f"emergency: flat={flat} after {elapsed}s (deadline {args.emergency_deadline}s)")
            if any(o["position_action"] != "CLOSE" for o in orders):
                failures.append("emergency: a non-reduce-only order was sent")
    except _DryRunComplete:
        pass
    except Exception as exc:  # noqa: BLE001 — the artifact must record why
        failures.append(f"{type(exc).__name__}: {exc}")
        import traceback

        traceback.print_exc()
    finally:
        forced = await _stop_executors([e for e in executors if not e.is_closed], timeout_s=20.0) if executors else []
        if forced:
            failures.append(f"teardown: executor(s) {forced} did not terminate on early_stop()")
        if not args.dry_run:
            await asyncio.sleep(2.0)
            leftover = _resting_oids(info, address, coin)
            position = _venue_position(info, address, coin)
            if leftover or position:
                failures.append(f"teardown: HB path left orders={sorted(leftover)} position={position} — raw-SDK cleanup")
                exchange = _hl_exchange(args.account_id)
                for oid in leftover:
                    print(f"force-cancel {oid}: {exchange.cancel(coin, oid)}")
                if position:
                    print(f"market_close: {exchange.market_close(coin)}")
                await asyncio.sleep(3.0)
                leftover, position = _resting_oids(info, address, coin), _venue_position(info, address, coin)
                if leftover or position:
                    failures.append(f"teardown: STILL orders={sorted(leftover)} position={position} — check manually")
            report["teardown"] = {"leftover_orders": sorted(leftover), "position": position}
            print(f"teardown: orders={sorted(leftover)} position={position}")
        if controller is not None:
            report["fill_observer"] = _jsonable(controller.get_custom_info())
            controller.on_stop()
        stop_pump.set()
        if pump_task is not None:
            pump_task.cancel()
        await connector.stop_network()

    report["failures"] = failures
    report["passed"] = not failures
    (artifact_dir / "gate.json").write_text(json.dumps(_jsonable(report), indent=2))
    if failures:
        print("\nDE-RISK GATE FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nDRY RUN OK (nothing placed)" if args.dry_run else "\nDE-RISK GATE PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

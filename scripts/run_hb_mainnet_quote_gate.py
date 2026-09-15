"""Live WRITE gate: real PerpMMController quoting through real HB executors on HL.

Functional-plan step 3 (see `perp-bot/status.md`): the read-only smoke
(`run_hb_mainnet_smoke.py`) proved the connector + controller start, read
mid/funding/equity and register the FillObserver. This gate closes the other
half — that a keeper QUOTE intent actually becomes a resting maker order on
the intended subaccount, refreshes, and cancels clean.

What it proves, in order:
  1. keeper -> intent -> OrderExecutor -> real HL order (LIMIT_MAKER = ALO)
  2. the order rests on the *target* subaccount and on no other account
  3. a refresh cycle stops the old executor and replaces the resting order
  4. teardown leaves zero resting orders and zero position

Safety: orders are tiny and forced strictly passive (priced through the touch
by `--passive-bps`), notional is hard-capped by `--max-notional`, and teardown
force-cancels through the raw HL SDK if the HB path fails to clean up. A
leftover position (an unintended fill) is market-closed and reported.

Standalone (not pytest) for the same reason as the smoke: HB connectors spawn
background tasks that outlive pytest-asyncio's per-test loop.

Usage:
  OPMS_HB_MAINNET=confirm OPMS_HB_PLACE_ORDERS=confirm \
  python scripts/run_hb_mainnet_quote_gate.py --account-id e2_mm1
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import asdict, is_dataclass
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_hb_mainnet_smoke import (  # noqa: E402  (shared connector/credential plumbing)
    CONNECTOR_NAME,
    PAIR,
    _build_connector,
    _resolve_account,
    _wait_ready,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MASTER_ID = "e2_main"
OTHER_ACCOUNT_IDS = ("e2_main", "e2_mm1", "e2_mm2")


class _DryRunComplete(Exception):
    """--dry-run reached its stopping point; not a failure."""


class _GateStrategy:
    """The slice of StrategyV2Base that ExecutorBase actually calls.

    ponytail: a real StrategyV2Base pulls in Clock + MarketsRecorder + a DB
    session just to place one order. These five methods are the whole contract
    (`grep _strategy\\. hummingbot/strategy_v2/executors/`). Swap in the real
    strategy when the gate grows to a full HB script run.
    """

    def __init__(self, connectors):
        self.connectors = connectors

    @property
    def current_timestamp(self) -> float:
        return time.time()

    def buy(self, connector_name, trading_pair, amount, order_type, price, position_action):
        return self.connectors[connector_name].buy(
            trading_pair, amount, order_type, price, position_action=position_action
        )

    def sell(self, connector_name, trading_pair, amount, order_type, price, position_action):
        return self.connectors[connector_name].sell(
            trading_pair, amount, order_type, price, position_action=position_action
        )

    def cancel(self, connector_name, trading_pair, order_id):
        return self.connectors[connector_name].cancel(trading_pair, order_id)

    def get_active_orders(self, connector_name):
        return self.connectors[connector_name].limit_orders


def _log_to(path: Path) -> None:
    """Capture HB + controller logs as a gate artifact.

    HB configures the root logger at import, which turns logging.basicConfig
    into a silent no-op; attach a file handler instead and keep the console at
    its previous verbosity.
    """
    root = logging.getLogger()
    for handler in root.handlers:
        handler.setLevel(max(handler.level, root.level))
    file_handler = logging.FileHandler(path)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(file_handler)
    root.setLevel(logging.INFO)


async def _pump(connector, stop: asyncio.Event, interval: float = 1.0):
    """Drive the connector's clock-fed loops (status polling, funding poll).

    Without a Clock nothing calls tick(), so `_poll_notifier` never fires and
    order/balance state would only ever refresh from the user websocket.
    """
    while not stop.is_set():
        now = time.time()
        connector._set_current_timestamp(now)
        connector.tick(now)
        await asyncio.sleep(interval)


def _hl_info():
    from hyperliquid.info import Info
    from hyperliquid.utils import constants

    return Info(constants.MAINNET_API_URL, skip_ws=True)


def _hl_exchange(account_id: str):
    from eth_account import Account
    from hyperliquid.exchange import Exchange
    from hyperliquid.utils import constants

    prefix = f"HYPERLIQUID_{account_id.upper()}"
    wallet = Account.from_key(os.environ[f"{prefix}_PRIVATE_KEY"])
    master = os.environ[f"HYPERLIQUID_{MASTER_ID.upper()}_ACCOUNT_ADDRESS"].lower()
    address = os.environ[f"{prefix}_ACCOUNT_ADDRESS"].lower()
    if address == master:
        return Exchange(wallet=wallet, base_url=constants.MAINNET_API_URL)
    return Exchange(
        wallet=wallet,
        base_url=constants.MAINNET_API_URL,
        account_address=master,
        vault_address=address,
    )


def _account_addresses() -> dict[str, str]:
    out = {}
    for account_id in OTHER_ACCOUNT_IDS:
        addr = os.environ.get(f"HYPERLIQUID_{account_id.upper()}_ACCOUNT_ADDRESS")
        if addr:
            out[account_id] = addr.lower()
    return out


def _resting_oids(info, address: str, coin: str) -> set[int]:
    return {int(o["oid"]) for o in info.open_orders(address) if o["coin"] == coin}


def _venue_position(info, address: str, coin: str) -> float:
    for entry in info.user_state(address).get("assetPositions", []):
        position = entry.get("position", {})
        if position.get("coin") == coin:
            return float(position.get("szi", 0.0))
    return 0.0


def _executor_oids(executors) -> set[int]:
    oids = set()
    for executor in executors:
        order = getattr(executor, "_order", None)
        exchange_id = order.order.exchange_order_id if order and order.order else None
        if exchange_id:
            oids.add(int(exchange_id))
    return oids


async def _await_oids(executors, timeout_s: float) -> set[int]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        oids = _executor_oids(executors)
        if len(oids) == len(executors):
            return oids
        await asyncio.sleep(1.0)
    return _executor_oids(executors)


def _clamp_passive(intent, best_bid: float, best_ask: float, passive_bps: float) -> dict:
    """Push the keeper's quote strictly through the touch.

    HB's OrderExecutor already clamps a LIMIT_MAKER to the touch
    (`min(price, best_bid)` / `max(price, best_ask)`), so an ALO can't be
    rejected — but *at* the touch it can fill. The gate wants a resting order,
    not a position, so it prices `passive_bps` beyond the touch and records
    whether the keeper's own price was already there.
    """
    edge = passive_bps / 1e4
    bid_cap = best_bid * (1.0 - edge)
    ask_floor = best_ask * (1.0 + edge)
    original = {"bid": intent.quote.bid_price, "ask": intent.quote.ask_price}
    intent.quote.bid_price = min(intent.quote.bid_price, bid_cap)
    intent.quote.ask_price = max(intent.quote.ask_price, ask_floor)
    return {
        "keeper_bid": original["bid"],
        "keeper_ask": original["ask"],
        "gate_bid": intent.quote.bid_price,
        "gate_ask": intent.quote.ask_price,
        "clamped": (original["bid"] != intent.quote.bid_price
                    or original["ask"] != intent.quote.ask_price),
    }


def _synthetic_quote(coin: str, account_id: str, best_bid: float, best_ask: float, size: float):
    from mm_core.contracts import ExecIntent, QuoteSpec

    return ExecIntent(
        venue="hyperliquid", coin=coin, account_id=account_id,
        target_inventory=0.0, current_inventory=0.0,
        quote=QuoteSpec(bid_price=best_bid, ask_price=best_ask, bid_size=size, ask_size=size),
        urgency="passive",
    )


def _create_executors(actions, strategy) -> list:
    from hummingbot.strategy_v2.executors.executor_orchestrator import ExecutorOrchestrator

    executors = []
    for action in actions:
        config = action.executor_config
        config.controller_id = action.controller_id  # ExecutorOrchestrator.create_executor does this
        executor_class = ExecutorOrchestrator._executor_mapping.get(config.type)
        if executor_class is None:
            raise RuntimeError(f"no HB executor registered for type={config.type!r}")
        executor = executor_class(strategy=strategy, config=config, update_interval=1.0)
        executor.start()
        executors.append(executor)
    return executors


async def _stop_executors(executors, timeout_s: float = 30.0) -> list[str]:
    """early_stop() and wait for each executor to terminate *itself*.

    Waiting on `is_active` would be wrong: early_stop() sets SHUTTING_DOWN,
    which is already not "active", so the wait returns instantly and a forced
    stop() kills the control loop before `control_shutdown_process` ever
    cancels the resting order. TERMINATED is the only state that means the
    executor is done. A forced stop after the timeout is a gate failure, not
    normal teardown.
    """
    for executor in executors:
        executor.early_stop()
    deadline = time.time() + timeout_s
    while time.time() < deadline and not all(e.is_closed for e in executors):
        await asyncio.sleep(1.0)
    forced = [e.config.id for e in executors if not e.is_closed]
    for executor in executors:
        executor.stop()
    return forced


def _jsonable(value):
    if is_dataclass(value):
        return {k: _jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--account-id", default="e2_mm1")
    ap.add_argument("--use-vault", choices=["yes", "no"], default=None)
    ap.add_argument("--max-position", type=float, default=0.05,
                    help="keeper cap; quote size is 10%% of it (0.05 -> 0.005 ETH)")
    ap.add_argument("--max-notional", type=float, default=60.0,
                    help="hard abort if a single quote's notional exceeds this (USD)")
    ap.add_argument("--passive-bps", type=float, default=8.0,
                    help="price each quote this far beyond the touch")
    ap.add_argument("--warmup-ticks", type=int, default=30)
    ap.add_argument("--tick-interval", type=float, default=2.0)
    ap.add_argument("--rest-seconds", type=float, default=20.0,
                    help="how long to leave each quote resting before refresh/teardown")
    ap.add_argument("--timeout", type=float, default=90.0)
    ap.add_argument("--artifact-dir", default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="stop after building the executor configs; place nothing")
    args = ap.parse_args()

    if os.environ.get("OPMS_HB_MAINNET") != "confirm":
        print("refusing: set OPMS_HB_MAINNET=confirm", file=sys.stderr)
        return 2
    if os.environ.get("OPMS_HB_PLACE_ORDERS") != "confirm":
        print("refusing: set OPMS_HB_PLACE_ORDERS=confirm (this gate places real orders)",
              file=sys.stderr)
        return 2

    from hummingbot.core.data_type.common import PriceType
    from hummingbot.data_feed.market_data_provider import MarketDataProvider
    from hummingbot.strategy_v2.models.executor_actions import StopExecutorAction

    from opms.controllers.generic.perp_mm_bridge import intent_is_quoting
    from opms.controllers.generic.perp_mm_controller import PerpMMController, PerpMMControllerConfig

    stamp = time.strftime("%Y%m%dT%H%M%S")
    artifact_dir = Path(args.artifact_dir or (REPO_ROOT / "hb-enhanced-opms" / "logs" / f"quote_gate_{stamp}"))
    artifact_dir.mkdir(parents=True, exist_ok=True)
    _log_to(artifact_dir / "gate.log")

    address, private_key, use_vault = _resolve_account(args.account_id, args.use_vault)
    coin = PAIR.split("-")[0]
    info = _hl_info()
    addresses = _account_addresses()
    others = {aid: addr for aid, addr in addresses.items() if addr != address}

    report: dict = {
        "ts": stamp, "account_id": args.account_id, "address": address,
        "use_vault": use_vault, "pair": PAIR, "phases": [],
    }
    failures: list[str] = []
    executors: list = []
    stop_pump = asyncio.Event()
    pump_task = None
    connector = _build_connector(address, private_key, use_vault)

    print(f"account={args.account_id} use_vault={use_vault} address={address[:8]}..{address[-4:]}")
    print(f"artifact_dir={artifact_dir}")

    try:
        await connector._initialize_trading_pair_symbol_map()
        await connector.start_network()
        mid = await _wait_ready(connector, args.timeout)
        pump_task = asyncio.create_task(_pump(connector, stop_pump))

        preexisting = _resting_oids(info, address, coin)
        start_position = _venue_position(info, address, coin)
        report["preexisting_oids"] = sorted(preexisting)
        report["start_position"] = start_position
        print(f"mid={mid} preexisting_orders={len(preexisting)} position={start_position}")
        if start_position != 0.0:
            failures.append(f"account already holds a {coin} position ({start_position}) — refusing")
            raise RuntimeError("dirty account")

        provider = MarketDataProvider(connectors={CONNECTOR_NAME: connector})
        config = PerpMMControllerConfig(
            id=f"hb-quote-gate-{stamp}",
            controller_name="perp_mm",
            connector_name=CONNECTOR_NAME,
            trading_pair=PAIR,
            venue="hyperliquid",
            account_id=args.account_id,
            leverage=1,
            max_position=args.max_position,
            critical_position=args.max_position * 2,
            decision_log_path=str(artifact_dir / "decisions.jsonl"),
        )
        controller = PerpMMController(config, provider, asyncio.Queue(), update_interval=5.0)
        await controller.on_start()

        # --- phase 1: warm the keeper up until its own risk policy says QUOTE
        decisions = []
        for i in range(args.warmup_ticks):
            await controller.update_processed_data()
            intent = controller._client.last_intent
            decisions.append(None if intent is None else intent.urgency)
            if intent_is_quoting(intent):
                break
            await asyncio.sleep(args.tick_interval)
        organic = intent_is_quoting(controller._client.last_intent)
        print(f"warmup ticks={len(decisions)} organic_quote={organic}")

        best_bid = float(connector.get_price_by_type(PAIR, PriceType.BestBid))
        best_ask = float(connector.get_price_by_type(PAIR, PriceType.BestAsk))
        if not organic:
            # The regime gate (mean-reversion/Hurst/trend) legitimately refuses
            # to quote sometimes; that is a keeper decision, not an HB-path
            # result. Force a synthetic quote so the order path is still
            # exercised — and say so in the artifact.
            controller._client.last_intent = _synthetic_quote(
                coin, args.account_id, best_bid, best_ask, args.max_position * 0.1
            )
        prices = _clamp_passive(controller._client.last_intent, best_bid, best_ask, args.passive_bps)
        size = controller._client.last_intent.quote.bid_size
        notional = size * best_ask
        report["phases"].append({
            "phase": "warmup", "ticks": len(decisions), "organic_quote": organic,
            "best_bid": best_bid, "best_ask": best_ask, "size": size,
            "notional": notional, **prices,
        })
        print(f"quote size={size} notional=${notional:.2f} bid={prices['gate_bid']} ask={prices['gate_ask']}")
        if notional > args.max_notional:
            failures.append(f"quote notional ${notional:.2f} exceeds --max-notional ${args.max_notional}")
            raise RuntimeError("notional guard")

        strategy = _GateStrategy({CONNECTOR_NAME: connector})

        # --- phase 2: intent -> executors -> resting orders on the right account
        actions = controller.determine_executor_actions()
        if args.dry_run:
            planned = [_jsonable(a.executor_config) for a in actions]
            print(f"dry-run: would create {len(actions)} executor(s):")
            for plan in planned:
                print(f"  {plan}")
            report["phases"].append({"phase": "dry_run", "planned": planned})
            controller.on_stop()
            raise _DryRunComplete()
        executors = _create_executors(actions, strategy)
        oids = await _await_oids(executors, args.timeout)
        resting = _resting_oids(info, address, coin) - preexisting
        leaked = {aid: sorted(_resting_oids(info, addr, coin) & oids) for aid, addr in others.items()}
        print(f"placed oids={sorted(oids)} resting_on_target={sorted(resting)} leaked={leaked}")
        report["phases"].append({
            "phase": "place", "actions": len(actions), "oids": sorted(oids),
            "resting_on_target": sorted(resting), "leaked": leaked,
        })
        if len(oids) != len(actions):
            failures.append(f"{len(actions)} executor(s) created but only {len(oids)} exchange order id(s)")
        if not oids or not oids <= resting:
            failures.append(f"orders {sorted(oids - resting)} are not resting on {args.account_id}")
        for account_id, found in leaked.items():
            if found:
                failures.append(f"order(s) {found} leaked onto {account_id} — vault routing broken")

        await asyncio.sleep(args.rest_seconds)
        still_resting = _resting_oids(info, address, coin) & oids
        report["phases"].append({"phase": "rest", "seconds": args.rest_seconds,
                                 "still_resting": sorted(still_resting)})
        if still_resting != oids:
            print(f"note: {sorted(oids - still_resting)} no longer resting (filled or cancelled)")

        # --- phase 3: refresh cycle replaces the resting quotes
        controller.executors_info = [e.executor_info for e in executors]
        await controller.update_processed_data()
        if not intent_is_quoting(controller._client.last_intent):
            controller._client.last_intent = _synthetic_quote(
                coin, args.account_id, best_bid, best_ask, args.max_position * 0.1
            )
        best_bid = float(connector.get_price_by_type(PAIR, PriceType.BestBid))
        best_ask = float(connector.get_price_by_type(PAIR, PriceType.BestAsk))
        _clamp_passive(controller._client.last_intent, best_bid, best_ask, args.passive_bps)
        refresh_actions = controller.determine_executor_actions()
        stops = [a for a in refresh_actions if isinstance(a, StopExecutorAction)]
        creates = [a for a in refresh_actions if not isinstance(a, StopExecutorAction)]
        print(f"refresh: {len(stops)} stop action(s), {len(creates)} create action(s)")
        if not stops:
            failures.append("refresh cycle emitted no StopExecutorAction for the resting quotes")
        stop_ids = {a.executor_id for a in stops}
        live_ids = {e.config.id for e in executors}
        if stop_ids != live_ids:
            failures.append(f"stop actions target {stop_ids}, live executors are {live_ids}")

        forced = await _stop_executors(executors)
        if forced:
            failures.append(f"executor(s) {forced} did not terminate on early_stop() — forced")
        executors = _create_executors(creates, strategy)
        new_oids = await _await_oids(executors, args.timeout)
        after = _resting_oids(info, address, coin)
        print(f"refresh oids={sorted(new_oids)} old_still_resting={sorted(oids & after)}")
        report["phases"].append({
            "phase": "refresh", "stops": len(stops), "creates": len(creates),
            "new_oids": sorted(new_oids), "old_still_resting": sorted(oids & after),
        })
        if oids & after:
            failures.append(f"refresh left old order(s) {sorted(oids & after)} resting")
        if not new_oids or not new_oids <= after:
            failures.append(f"refreshed order(s) {sorted(new_oids - after)} are not resting")

        # --- phase 4: teardown leaves nothing behind
        forced = await _stop_executors(executors)
        if forced:
            failures.append(f"executor(s) {forced} did not terminate on early_stop() — forced")
        await asyncio.sleep(3.0)
        leftover = _resting_oids(info, address, coin) - preexisting
        if leftover:
            failures.append(f"HB teardown left order(s) {sorted(leftover)} resting — force-cancelling")
            exchange = _hl_exchange(args.account_id)
            for oid in leftover:
                print(f"force-cancel oid={oid}: {exchange.cancel(coin, oid)}")
            await asyncio.sleep(2.0)
            leftover = _resting_oids(info, address, coin) - preexisting
        end_position = _venue_position(info, address, coin)
        if end_position != 0.0:
            failures.append(f"gate left a {coin} position ({end_position}) — market-closing it")
            exchange = _hl_exchange(args.account_id)
            print(f"market_close: {exchange.market_close(coin)}")
            await asyncio.sleep(2.0)
            end_position = _venue_position(info, address, coin)
        print(f"teardown leftover_orders={sorted(leftover)} position={end_position}")
        report["phases"].append({"phase": "teardown", "leftover_orders": sorted(leftover),
                                 "end_position": end_position})
        if leftover:
            failures.append(f"orders still resting after force-cancel: {sorted(leftover)}")
        if end_position != 0.0:
            failures.append(f"position still open after market_close: {end_position}")

        report["custom_info"] = _jsonable(controller.get_custom_info())
        print(f"custom_info={report['custom_info']}")
        controller.on_stop()
    except _DryRunComplete:
        pass
    except Exception as exc:  # noqa: BLE001 — the artifact must record why
        failures.append(f"{type(exc).__name__}: {exc}")
        import traceback

        traceback.print_exc()
    finally:
        if executors:
            try:
                await _stop_executors(executors, timeout_s=15.0)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"teardown stop failed: {exc}")
        stop_pump.set()
        if pump_task is not None:
            pump_task.cancel()
        await connector.stop_network()

    report["failures"] = failures
    report["passed"] = not failures
    (artifact_dir / "gate.json").write_text(json.dumps(_jsonable(report), indent=2))

    if failures:
        print("\nQUOTE GATE FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nDRY RUN OK (nothing placed)" if args.dry_run else "\nQUOTE GATE PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

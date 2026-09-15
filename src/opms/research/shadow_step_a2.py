"""Step A.2 shadow rollout driver — phase-3 plan §4.6.

Drives the real ``dlmm_bot.Keeper`` through the three gate phases and
records evidence for each:

  1. ``initial_deposit`` — the regime gate opens, the keeper builds a ladder
     and deposits it through the bridge; ``position_id`` is adopted.
  2. ``drift_refresh``  — the price leaves the deploy-time anchor by
     ``drift_threshold_bins``, the keeper refreshes (withdraw + redeposit)
     exactly once, and adopts the position PDA the executor returns.
  3. ``tvl_emergency``  — a TVL breach fires ``_emergency_exit``: the position
     is withdrawn, inventory swapped to the safe leg, keeper halted.

Bridge modes:
  ``fake``        in-memory ``FakeExecBridge``: offline, no keys, no txs
                  (default; the harness injects pool state).
  ``subprocess``  the TS executor (``node dist/bridge.js``): live reads and,
                  only with ``CONFIRM=yes``, real signing. Without CONFIRM the
                  bridge runs with ``DRY_RUN=true`` (preview only). Live writes
                  also need the executor's M4/M5 + signing gate
                  (``solana-clmm-executor`` test plan §5).

Usage:
  python -m opms.research.shadow_step_a2 --fake
  POOL=... WALLET=... python -m opms.research.shadow_step_a2 \
      --bridge subprocess --executor-cwd ../solana-clmm-executor
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import sys
from dataclasses import dataclass, field

from dlmm_bot.clock import FrozenClock
from dlmm_bot.config import DLMMConfig
from dlmm_bot.exec_bridge import ExecBridge, FakeExecBridge
from dlmm_bot.grid import VenueGrid
from dlmm_bot.keeper import Keeper, KeeperConfig
from dlmm_bot.risk_dlmm import PairType

logger = logging.getLogger(__name__)


class A2StepError(RuntimeError):
    """A Step A.2 gate phase never reached its expected keeper state."""


@dataclass
class PhaseEvidence:
    name: str
    cycles: int
    decision: str
    action: str
    position_id: str | None = None
    detail: str = ""


@dataclass
class A2Report:
    phases: list[PhaseEvidence] = field(default_factory=list)
    bridge_calls: list[str] = field(default_factory=list)
    ok: bool = True

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "phases": [vars(p) for p in self.phases],
            "bridge_calls": self.bridge_calls,
        }


def _bridge_call_names(bridge) -> list[str]:
    calls = getattr(bridge, "calls", None)
    return [c["method"] for c in calls] if calls is not None else []


class _A2Runner:
    def __init__(
        self,
        keeper: Keeper,
        bridge,
        *,
        pool: str,
        base_bin: int = 100,
        amplitude: int = 5,
        period: int = 12,
        tvl_usd: float = 50_000.0,
        kill_tvl_usd: float = 100.0,
        base_balance: float = 1.0,
        quote_balance: float = 500.0,
        max_cycles_per_phase: int = 200,
        pace_s: float = 0.0,
        clock: FrozenClock | None = None,
    ):
        self.keeper = keeper
        self.bridge = bridge
        self.pool = pool
        self.base_bin = base_bin
        self.amplitude = amplitude
        self.period = period
        self.tvl_usd = tvl_usd
        self.kill_tvl_usd = kill_tvl_usd
        self.base_balance = base_balance
        self.quote_balance = quote_balance
        self.max_cycles = max_cycles_per_phase
        self.pace_s = pace_s
        self.clock = clock
        self.cycle_no = 0
        self.is_fake = isinstance(bridge, FakeExecBridge)

    # -- driving -------------------------------------------------------

    async def _cycle(self):
        self.cycle_no += 1
        record = await self.keeper._cycle()
        if self.clock is not None:
            self.clock.advance()
        if self.pace_s > 0:
            await asyncio.sleep(self.pace_s)
        return record

    def _inject_state(self, position_exists: bool, tvl_usd: float | None = None) -> None:
        if not self.is_fake:
            return  # live state comes from the chain
        active = self.base_bin + round(
            self.amplitude * math.sin(2 * math.pi * self.cycle_no / self.period)
        )
        self.bridge.set_state(
            self.pool,
            active_bin=active,
            balances=(
                {"base": self.base_balance, "quote": self.quote_balance}
                if position_exists else {"base": 0.0, "quote": 0.0}
            ),
            tvl_usd=self.tvl_usd if tvl_usd is None else tvl_usd,
        )

    def _refresh_calls(self) -> int:
        return _bridge_call_names(self.bridge).count("refresh_bundle")

    def _arm_live_tvl_kill(self) -> None:
        """Live variant of the TVL injection: raise the minimum above the
        pool's last observed TVL instead of fabricating chain state."""
        policy = self.keeper.dlmm_risk
        last_tvl = getattr(policy.state, "last_tvl", None)
        if last_tvl is None:
            raise A2StepError("tvl_emergency: no TVL observed yet to arm the kill")
        policy.cfg.min_tvl_usd = float(last_tvl) + 1.0
        logger.warning(
            "Armed live TVL kill: min_tvl_usd=%.2f (last observed %.2f)",
            policy.cfg.min_tvl_usd, last_tvl,
        )

    # -- phases --------------------------------------------------------

    async def _phase_initial_deposit(self) -> PhaseEvidence:
        if self.is_fake:
            self.bridge.set_receipt(position_id="shadow-pos-1")
        for _ in range(self.max_cycles):
            self._inject_state(position_exists=False)
            record = await self._cycle()
            if record.action == "initial_deposit" and self.keeper._current_position_id:
                return PhaseEvidence(
                    "initial_deposit", self.cycle_no, record.decision, record.action,
                    position_id=self.keeper._current_position_id,
                    detail=f"active_bin={self.keeper._active_bin}",
                )
        raise A2StepError(
            f"initial_deposit: not reached in {self.max_cycles} cycles "
            f"(last action {self.keeper.decision_log[-1].action if self.keeper.decision_log else 'none'})"
        )

    async def _phase_drift_refresh(self) -> PhaseEvidence:
        if self.is_fake:
            self.bridge.set_receipt(position_id="shadow-pos-2")
        before = self._refresh_calls()
        for _ in range(self.max_cycles):
            self._inject_state(position_exists=self.keeper._current_position_id is not None)
            record = await self._cycle()
            if record.action != "refresh":
                continue
            if self.is_fake and self._refresh_calls() != before + 1:
                raise A2StepError("drift_refresh: refresh action without exactly one refresh_bundle")
            if self.is_fake and self.keeper._current_position_id != "shadow-pos-2":
                raise A2StepError(
                    "drift_refresh: keeper did not adopt the returned position_id "
                    f"({self.keeper._current_position_id!r})"
                )
            if not self.keeper._current_position_id:
                raise A2StepError("drift_refresh: no position_id after refresh")
            return PhaseEvidence(
                "drift_refresh", self.cycle_no, record.decision, record.action,
                position_id=self.keeper._current_position_id,
                detail=record.refresh_reason,
            )
        raise A2StepError(f"drift_refresh: not reached in {self.max_cycles} cycles")

    async def _phase_tvl_emergency(self) -> PhaseEvidence:
        armed = False
        for _ in range(self.max_cycles):
            if self.is_fake:
                self._inject_state(position_exists=True, tvl_usd=self.kill_tvl_usd)
            elif not armed:
                self._arm_live_tvl_kill()
                armed = True
            record = await self._cycle()
            if record.decision != "emergency_exit":
                continue
            if not self.keeper._halted:
                raise A2StepError("tvl_emergency: decision fired but keeper not halted")
            if self.keeper._current_position_id is not None:
                raise A2StepError("tvl_emergency: position not withdrawn")
            return PhaseEvidence(
                "tvl_emergency", self.cycle_no, record.decision, record.action,
                detail=record.refresh_reason,
            )
        raise A2StepError(f"tvl_emergency: not reached in {self.max_cycles} cycles")

    async def run(self) -> A2Report:
        report = A2Report()
        report.phases.append(await self._phase_initial_deposit())
        report.phases.append(await self._phase_drift_refresh())
        report.phases.append(await self._phase_tvl_emergency())
        report.bridge_calls = _bridge_call_names(self.bridge)
        logger.info(
            "Step A.2 sequence complete: %s",
            " -> ".join(f"{p.name}({p.cycles})" for p in report.phases),
        )
        return report


async def run_step_a2(keeper: Keeper, bridge, *, pool: str, **kwargs) -> A2Report:
    """Run the three Step A.2 phases against an already-constructed keeper."""
    return await _A2Runner(keeper, bridge, pool=pool, **kwargs).run()


def _build_keeper(args, bridge) -> Keeper:
    grid = VenueGrid(
        ref_price=args.ref_price,
        bin_step_bps=args.bin_step_bps,
        base_decimals=args.base_decimals,
        quote_decimals=args.quote_decimals,
    )
    cfg = KeeperConfig(
        dlmm=DLMMConfig(
            gamma=args.gamma, kappa=args.kappa,
            bin_step_bps=args.bin_step_bps, ref_price=args.ref_price,
            levels=args.levels, inner_offset=args.inner_offset,
            capital=args.capital, level_weight=args.level_weight,
        ),
        grid=grid,
        pool_address=args.pool,
        drift_threshold_bins=args.drift_threshold_bins,
        refresh_interval=args.refresh_interval,
        pair_type=PairType(args.pair_type),
        dry_run=False,
        base_mint=args.base_mint,
        quote_mint=args.quote_mint,
    )
    return Keeper(cfg=cfg, exec_bridge=bridge)


def build_args(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--bridge", choices=("fake", "subprocess"), default="fake")
    ap.add_argument("--fake", action="store_true", help="alias for --bridge fake")
    ap.add_argument("--executor-cwd", default="../solana-clmm-executor")
    ap.add_argument("--pool", default="SOL-USDC")
    ap.add_argument("--wallet", default="")
    ap.add_argument("--ref-price", type=float, default=150.0)
    ap.add_argument("--bin-step-bps", type=int, default=20)
    ap.add_argument("--base-decimals", type=int, default=6)
    ap.add_argument("--quote-decimals", type=int, default=9)
    ap.add_argument("--gamma", type=float, default=1.0)
    ap.add_argument("--kappa", type=float, default=0.5)
    ap.add_argument("--levels", type=int, default=5)
    ap.add_argument("--inner-offset", type=int, default=2)
    ap.add_argument("--capital", type=float, default=1000.0)
    ap.add_argument("--level-weight", type=float, default=0.2)
    ap.add_argument("--drift-threshold-bins", type=int, default=3)
    ap.add_argument("--refresh-interval", type=float, default=5.0)
    ap.add_argument("--pair-type", default="bluechip")
    ap.add_argument("--base-mint", default="")
    ap.add_argument("--quote-mint", default="")
    ap.add_argument("--base-bin", type=int, default=100)
    ap.add_argument("--amplitude", type=int, default=5)
    ap.add_argument("--period", type=int, default=12)
    ap.add_argument("--tvl-usd", type=float, default=50_000.0)
    ap.add_argument("--kill-tvl-usd", type=float, default=100.0)
    ap.add_argument("--max-cycles-per-phase", type=int, default=200)
    ap.add_argument("--pace-s", type=float, default=0.0)
    ap.add_argument("--out", default="shadow-a2-report.json")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = build_args(argv)
    if args.fake:
        args.bridge = "fake"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    clock = None
    if args.bridge == "fake":
        bridge = FakeExecBridge()
        clock = FrozenClock(start=2_000_000.0, step=5.0)
    else:
        if not args.pool or not args.wallet:
            logger.error("subprocess bridge needs --pool and --wallet")
            return 2
        if os.environ.get("CONFIRM") != "yes":
            os.environ.setdefault("DRY_RUN", "true")
            logger.warning("CONFIRM != yes: executor runs DRY_RUN=true (no signing)")
        bridge = ExecBridge(cmd=["node", "dist/bridge.js"], cwd=args.executor_cwd)

    bridge.start()
    keeper = _build_keeper(args, bridge)
    try:
        if clock is not None:
            with clock:
                report = asyncio.run(run_step_a2(
                    keeper, bridge, pool=args.pool, clock=clock,
                    base_bin=args.base_bin, amplitude=args.amplitude, period=args.period,
                    tvl_usd=args.tvl_usd, kill_tvl_usd=args.kill_tvl_usd,
                    max_cycles_per_phase=args.max_cycles_per_phase, pace_s=args.pace_s,
                ))
        else:
            report = asyncio.run(run_step_a2(
                keeper, bridge, pool=args.pool,
                base_bin=args.base_bin, amplitude=args.amplitude, period=args.period,
                tvl_usd=args.tvl_usd, kill_tvl_usd=args.kill_tvl_usd,
                max_cycles_per_phase=args.max_cycles_per_phase, pace_s=args.pace_s,
            ))
    except A2StepError as e:
        logger.error("Step A.2 gate failed: %s", e)
        return 1
    finally:
        keeper.stop()
        bridge.stop()

    payload = report.to_dict()
    with open(args.out, "w") as fh:
        json.dump(payload, fh, indent=2, default=str)
    print(json.dumps(payload, indent=2, default=str))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())

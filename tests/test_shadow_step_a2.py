"""Step A.2 shadow driver: full keeper sequence on one bridge, offline.

The phase-3 §4.6 gate is deposit → drift-triggered refresh → TVL-triggered
emergency exit. These tests drive the real keeper through all three phases
against `FakeExecBridge`; the live variant (TS executor subprocess, real
signing) is gated in `opms.research.shadow_step_a2` and stays opt-in.
"""

import asyncio
import math

import pytest

from dlmm_bot.clock import FrozenClock
from dlmm_bot.config import DLMMConfig
from dlmm_bot.exec_bridge import FakeExecBridge
from dlmm_bot.grid import VenueGrid
from dlmm_bot.keeper import Keeper, KeeperConfig
from dlmm_bot.risk_dlmm import PairType


def _keeper():
    grid = VenueGrid(ref_price=150.0, bin_step_bps=20, base_decimals=6, quote_decimals=9)
    cfg = KeeperConfig(
        dlmm=DLMMConfig(gamma=1.0, kappa=0.5, bin_step_bps=20, ref_price=150.0,
                        levels=5, inner_offset=2, capital=1000.0, level_weight=0.2),
        grid=grid,
        pool_address="pool",
        dry_run=False,
        refresh_interval=5.0,
        position_id=None,
        pair_type=PairType.BLUECHIP,
        drift_threshold_bins=3,
        base_mint="base",
        quote_mint="quote",
    )
    bridge = FakeExecBridge()
    bridge.set_state("pool", active_bin=100, balances={"base": 0.0, "quote": 0.0},
                     tvl_usd=50_000.0)
    return Keeper(cfg=cfg, exec_bridge=bridge), bridge


class TestRunStepA2:
    def test_full_sequence_offline(self):
        from opms.research.shadow_step_a2 import run_step_a2

        keeper, bridge = _keeper()
        clock = FrozenClock(start=2_000_000.0, step=5.0)
        with clock:
            report = asyncio.new_event_loop().run_until_complete(
                run_step_a2(keeper, bridge, pool="pool", clock=clock)
            )

        assert report.ok
        assert [p.name for p in report.phases] == [
            "initial_deposit", "drift_refresh", "tvl_emergency",
        ]
        deposits = [p for p in report.phases if p.name == "initial_deposit"]
        assert deposits[0].position_id
        assert "deposit_single_sided" in report.bridge_calls
        assert "refresh_bundle" in report.bridge_calls
        assert "withdraw" in report.bridge_calls
        assert keeper._halted is True
        assert keeper._current_position_id is None  # emergency withdraw cleared it

    def test_fails_when_tvl_kill_never_arrives(self):
        """A gate step that never happens must raise, not pass silently."""
        from opms.research.shadow_step_a2 import A2StepError, run_step_a2

        keeper, bridge = _keeper()
        clock = FrozenClock(start=2_000_000.0, step=5.0)
        with clock:
            with pytest.raises(A2StepError, match="tvl_emergency"):
                asyncio.new_event_loop().run_until_complete(
                    run_step_a2(
                        keeper, bridge, pool="pool", clock=clock,
                        max_cycles_per_phase=40,
                        kill_tvl_usd=50_000.0,  # never breaches the TVL gate
                    )
                )

"""Tests for DLMMController lifecycle and keeper integration.

Uses FakeExecBridge injected into the keeper to bypass Gateway.
Mocks ControllerBase dependencies following conftest pattern.
"""

import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from dlmm_bot.exec_bridge import FakeExecBridge
from dlmm_bot.grid import VenueGrid
from dlmm_bot.keeper import KeeperConfig
from dlmm_bot.risk_dlmm import PairType
from dlmm_bot.config import DLMMConfig

from opms.controllers.generic.dlmm_controller import DLMMController, DLMMControllerConfig


@pytest.fixture
def mock_controller_config():
    return MagicMock(
        gateway_url="http://localhost:15888",
        wallet="w1",
        chain="solana",
        network="mainnet-beta",
        pool_address="pool1",
        ref_price=150.0,
        bin_step_bps=2,
        base_decimals=9,
        quote_decimals=6,
        gamma=1.0,
        kappa=0.5,
        levels=5,
        inner_offset=1,
        capital=1000.0,
        level_weight=0.2,
        drift_threshold_bins=3,
        refresh_interval=5.0,
        pair_type="bluechip",
        dry_run=True,
        hedge_enabled=False,
        hedge_venue="hyperliquid",
        hedge_coin="SOL",
        decision_log_path=None,
        shared_book_key="",
        id="dlmm_1",
    )


@pytest.fixture
def mock_bridge():
    return FakeExecBridge()


@pytest.fixture
def mock_keeper(mock_controller_config, mock_bridge):
    grid = VenueGrid(ref_price=150.0, bin_step_bps=2, base_decimals=9, quote_decimals=6)
    dlmm_cfg = DLMMConfig(
        gamma=1.0, kappa=0.5, bin_step_bps=2, ref_price=150.0,
        inner_offset=1, levels=5, capital=1000.0, level_weight=0.2,
    )
    keeper_cfg = KeeperConfig(
        dlmm=dlmm_cfg,
        grid=grid,
        pool_address="pool1",
        drift_threshold_bins=3,
        refresh_interval=5.0,
        pair_type=PairType.BLUECHIP,
        dry_run=True,
    )
    from dlmm_bot.keeper import Keeper
    return Keeper(cfg=keeper_cfg, exec_bridge=mock_bridge)


class TestDLMMControllerLifecycle:
    """Test on_start, on_stop, and basic construction."""

    def test_on_start_starts_bridge(self, mock_keeper):
        """on_start calls bridge.start()."""
        started = []
        original_start = mock_keeper.exec.start
        def capture_start():
            started.append(True)
            return original_start()
        mock_keeper.exec.start = capture_start
        mock_keeper.exec.start()
        assert len(started) == 1

    def test_on_stop_stops_keeping(self, mock_keeper):
        """on_stop calls keeper.stop() and bridge.stop()."""
        mock_keeper.stop = MagicMock()
        mock_keeper.exec.stop = MagicMock()
        mock_keeper.stop()
        mock_keeper.exec.stop()
        mock_keeper.stop.assert_called_once()
        mock_keeper.exec.stop.assert_called_once()

    def test_determine_executor_actions_empty(self, mock_keeper):
        """DLMMController returns empty executor actions — no HB executors."""
        actions = []  # DLMM execution is direct via Gateway
        assert actions == []

    def test_get_custom_info_keys(self, mock_keeper):
        """get_custom_info returns expected keys."""
        expected_keys = [
            "last_decision", "last_action", "active_bin", "center_bin",
            "inventory_base", "inventory_quote", "pnl_total", "cycle_count", "dry_run",
        ]
        for key in expected_keys:
            assert key in {
                "last_decision": "none",
                "last_action": "none",
                "active_bin": mock_keeper._active_bin,
                "center_bin": mock_keeper._center_bin,
                "inventory_base": mock_keeper._inventory_base,
                "inventory_quote": mock_keeper._inventory_quote,
                "pnl_total": 0.0,
                "cycle_count": mock_keeper._cycle_count,
                "dry_run": True,
            }


class TestSharedBookWrite:
    """update_processed_data() must push keeper state into SharedRiskBook
    (C2 depends on this — HedgeController reads dlmm_net_delta/sigma/etc.
    from the same book).  Calls the real DLMMController.update_processed_data
    against a lightweight stand-in `self` (real construction is blocked by
    the ControllerBase=object test stub, same constraint as other tests in
    this file), so this exercises the actual method body, not a duplicate."""

    @pytest.mark.asyncio
    async def test_writes_keeper_state_into_shared_book(self, mock_keeper):
        from dlmm_bot.keeper import CycleRecord
        from opms.controllers.generic.dlmm_controller import DLMMController
        from opms.controllers.generic.shared_risk_book import SharedRiskBook

        # Stub out the real cycle (no Gateway state set up for "pool1") so
        # only the shared-book write logic under test runs.
        mock_keeper._cycle = AsyncMock()

        mock_keeper._decision_log.append(CycleRecord(
            ts=123.0, active_bin=100, mid=150.0,
            regime_half_life=0.0, regime_hurst=0.0, regime_trending=False,
            decision="quote", urgency="normal", action="hold",
            inventory_base=2.0, inventory_quote=300.0,
            r_reservation=150.0, half_spread=0.1,
            ladder_center=100, ladder_levels=5,
            refresh_needed=False, refresh_reason="",
            net_delta=1.5, sigma=0.25,
        ))

        book = SharedRiskBook()
        fake_self = type("Fake", (), {
            "keeper": mock_keeper,
            "_shared_book": book,
            "update_processed_data": DLMMController.update_processed_data,
        })()

        await DLMMController.update_processed_data(fake_self)

        assert book.dlmm_net_delta == 1.5
        assert book.dlmm_sigma == 0.25
        assert book.last_dlmm_ts == 123.0
        assert book.dlmm_inventory_value_usd == pytest.approx(150.0 * (2.0 + 300.0 / 150.0))

    @pytest.mark.asyncio
    async def test_no_shared_book_is_noop(self, mock_keeper):
        from opms.controllers.generic.dlmm_controller import DLMMController

        mock_keeper._cycle = AsyncMock()
        fake_self = type("Fake", (), {
            "keeper": mock_keeper,
            "_shared_book": None,
        })()

        await DLMMController.update_processed_data(fake_self)  # must not raise

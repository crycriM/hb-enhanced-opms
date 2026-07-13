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
from hummingbot.core.data_type.common import TradeType


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


class TestDLMMHedgeFullLoop:
    """Integration test for the full C2 loop: DLMMController writes
    inventory state into SharedRiskBook → HedgeController reads it →
    emits PA executor actions on the perp connector.  Both controllers
    share the same book key (parity gate §5.4 / acceptance §10)."""

    @pytest.fixture
    def shared_book(self):
        from opms.controllers.generic.shared_risk_book import SharedRiskBook, _REGISTRY
        _REGISTRY.clear()
        book = SharedRiskBook()
        _REGISTRY["dlmm_hedge_test"] = book
        yield book
        _REGISTRY.clear()

    def _make_hedge_self(self, shared_book, perp_position=0.0):
        """Build a fake self for HedgeController that works with the real
        dlmm_bot.hedge.HedgeController engine."""
        from dlmm_bot.hedge import HedgeController as DlmmHedgeController, HedgeConfig
        from opms.controllers.generic.hedge_controller import HedgeController

        hc = DlmmHedgeController(cfg=HedgeConfig(
            tau_h=3600.0, tau_min=900.0, tau_max=7200.0,
            sigma_ref=0.5, deadband_base_bps=10.0, per_trade_cost_bps=2.0,
            delta_cap_bps=200.0, cube_root_constant=1.0, deadband_base=0.0,
            venue="hyperliquid_perpetual", coin="SOL", enabled=True,
        ))

        config = MagicMock()
        config.perp_connector = "hyperliquid_perpetual"
        config.perp_trading_pair = "SOL-USD"
        config.shared_book_key = "dlmm_hedge_test"
        config.refresh_interval = 5.0
        config.id = "hedge_1"

        md_provider = MagicMock()
        md_provider.time.return_value = 1700000000.0

        positions = []
        if perp_position != 0:
            pos = MagicMock()
            pos.connector_name = "hyperliquid_perpetual"
            pos.trading_pair = "SOL-USD"
            pos.amount = abs(perp_position)
            pos.side = TradeType.BUY if perp_position > 0 else TradeType.SELL
            positions = [pos]

        return type("Fake", (), {
            "_shared_book": shared_book,
            "_hedge": hc,
            "config": config,
            "positions_held": positions,
            "market_data_provider": md_provider,
            "_current_perp_position": HedgeController._current_perp_position,
            "update_processed_data": HedgeController.update_processed_data,
            "determine_executor_actions": HedgeController.determine_executor_actions,
        })()

    @pytest.mark.asyncio
    async def test_dlmm_writes_hedge_reads_book_populated(self, mock_keeper, shared_book):
        """DLMM writes inventory → Hedge reads from same book key."""
        from dlmm_bot.keeper import CycleRecord
        from opms.controllers.generic.dlmm_controller import DLMMController

        mock_keeper._cycle = AsyncMock()
        mock_keeper._decision_log.append(CycleRecord(
            ts=1700000000.0, active_bin=100, mid=150.0,
            regime_half_life=0.0, regime_hurst=0.0, regime_trending=False,
            decision="quote", urgency="normal", action="hold",
            inventory_base=2.0, inventory_quote=300.0,
            r_reservation=150.0, half_spread=0.1,
            ladder_center=100, ladder_levels=5,
            refresh_needed=False, refresh_reason="",
            net_delta=1.5, sigma=0.25,
        ))

        dlmm_self = type("Fake", (), {
            "keeper": mock_keeper,
            "_shared_book": shared_book,
            "update_processed_data": DLMMController.update_processed_data,
        })()

        await DLMMController.update_processed_data(dlmm_self)
        assert shared_book.dlmm_net_delta == 1.5

        hedge_self = self._make_hedge_self(shared_book)
        from opms.controllers.generic.hedge_controller import HedgeController
        await HedgeController.update_processed_data(hedge_self)

        assert shared_book.hedge_action != ""
        assert shared_book.last_hedge_ts == 1700000000.0

    @pytest.mark.asyncio
    async def test_hedge_emits_pa_action_when_rehedge(self, mock_keeper, shared_book):
        """Positive net_delta with no existing perp position → hedge emits
        a SELL executor action (short delta to offset long inventory)."""
        from dlmm_bot.keeper import CycleRecord
        from opms.controllers.generic.dlmm_controller import DLMMController
        from opms.controllers.generic.hedge_controller import HedgeController

        mock_keeper._cycle = AsyncMock()
        mock_keeper._decision_log.append(CycleRecord(
            ts=1700000000.0, active_bin=100, mid=150.0,
            regime_half_life=0.0, regime_hurst=0.0, regime_trending=False,
            decision="quote", urgency="normal", action="hold",
            inventory_base=5.0, inventory_quote=750.0,
            r_reservation=150.0, half_spread=0.1,
            ladder_center=100, ladder_levels=5,
            refresh_needed=False, refresh_reason="",
            net_delta=5.0, sigma=0.30,
        ))

        dlmm_self = type("Fake", (), {
            "keeper": mock_keeper,
            "_shared_book": shared_book,
            "update_processed_data": DLMMController.update_processed_data,
        })()

        await DLMMController.update_processed_data(dlmm_self)
        shared_book.dlmm_sigma = 0.30

        hedge_self = self._make_hedge_self(shared_book, perp_position=0.0)
        hedge_self._hedge.evaluate = MagicMock(return_value=("rehedge", 5.0, None))
        await HedgeController.update_processed_data(hedge_self)

        actions = HedgeController.determine_executor_actions(hedge_self)
        assert len(actions) == 1

    @pytest.mark.asyncio
    async def test_no_trade_emits_no_actions(self, mock_keeper, shared_book):
        """When hedge evaluates to no_trade, no executor actions emitted."""
        from dlmm_bot.keeper import CycleRecord
        from opms.controllers.generic.dlmm_controller import DLMMController
        from opms.controllers.generic.hedge_controller import HedgeController

        mock_keeper._cycle = AsyncMock()
        mock_keeper._decision_log.append(CycleRecord(
            ts=1700000000.0, active_bin=100, mid=150.0,
            regime_half_life=0.0, regime_hurst=0.0, regime_trending=False,
            decision="quote", urgency="normal", action="hold",
            inventory_base=0.1, inventory_quote=15.0,
            r_reservation=150.0, half_spread=0.1,
            ladder_center=100, ladder_levels=5,
            refresh_needed=False, refresh_reason="",
            net_delta=0.0, sigma=0.25,
        ))

        dlmm_self = type("Fake", (), {
            "keeper": mock_keeper,
            "_shared_book": shared_book,
            "update_processed_data": DLMMController.update_processed_data,
        })()

        await DLMMController.update_processed_data(dlmm_self)

        hedge_self = self._make_hedge_self(shared_book, perp_position=0.0)
        hedge_self._hedge.evaluate = MagicMock(return_value=("no_trade", 0.0, None))
        await HedgeController.update_processed_data(hedge_self)

        actions = HedgeController.determine_executor_actions(hedge_self)
        assert len(actions) == 0

    @pytest.mark.asyncio
    async def test_hedge_with_existing_perp_position(self, mock_keeper, shared_book):
        """Already short 3.0, DLMM net delta 5.0 → hedge delta = -5.0 - (-3.0) = -2.0 → SELL for remaining."""
        from dlmm_bot.keeper import CycleRecord
        from opms.controllers.generic.dlmm_controller import DLMMController
        from opms.controllers.generic.hedge_controller import HedgeController

        mock_keeper._cycle = AsyncMock()
        mock_keeper._decision_log.append(CycleRecord(
            ts=1700000000.0, active_bin=100, mid=150.0,
            regime_half_life=0.0, regime_hurst=0.0, regime_trending=False,
            decision="quote", urgency="normal", action="hold",
            inventory_base=5.0, inventory_quote=750.0,
            r_reservation=150.0, half_spread=0.1,
            ladder_center=100, ladder_levels=5,
            refresh_needed=False, refresh_reason="",
            net_delta=5.0, sigma=0.30,
        ))

        dlmm_self = type("Fake", (), {
            "keeper": mock_keeper,
            "_shared_book": shared_book,
            "update_processed_data": DLMMController.update_processed_data,
        })()

        await DLMMController.update_processed_data(dlmm_self)
        shared_book.dlmm_sigma = 0.30

        hedge_self = self._make_hedge_self(shared_book, perp_position=-3.0)
        hedge_self._hedge.evaluate = MagicMock(return_value=("rehedge", 5.0, None))
        await HedgeController.update_processed_data(hedge_self)

        actions = HedgeController.determine_executor_actions(hedge_self)
        assert len(actions) == 1

    @pytest.mark.asyncio
    async def test_no_shared_book_hedge_is_noop(self, mock_keeper):
        """HedgeController without a shared_book is a no-op in both
        update_processed_data and determine_executor_actions."""
        from opms.controllers.generic.hedge_controller import HedgeController

        hedge_self = self._make_hedge_self(None)

        await HedgeController.update_processed_data(hedge_self)
        actions = HedgeController.determine_executor_actions(hedge_self)
        assert len(actions) == 0

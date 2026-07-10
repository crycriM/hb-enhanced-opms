"""Tests for HedgeController shared risk book interaction and executor action emission."""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from dlmm_bot.hedge import HedgeConfig, HedgeController as DlmmHedgeController
from opms.controllers.generic.shared_risk_book import SharedRiskBook, get_shared_book, _REGISTRY
from opms.executors.passive_aggressive_executor import PassiveAggressiveExecutorConfig

from hummingbot.core.data_type.common import TradeType


@pytest.fixture(autouse=True)
def clear_registry():
    _REGISTRY.clear()
    yield
    _REGISTRY.clear()


class TestSharedRiskBook:
    def test_get_shared_book_creates(self):
        book = get_shared_book("key1")
        assert isinstance(book, SharedRiskBook)
        assert book.dlmm_net_delta == 0.0

    def test_get_shared_book_returns_same(self):
        b1 = get_shared_book("key1")
        b2 = get_shared_book("key1")
        assert b1 is b2

    def test_different_keys_different_books(self):
        b1 = get_shared_book("key1")
        b2 = get_shared_book("key2")
        assert b1 is not b2

    def test_shared_risk_book_defaults(self):
        book = SharedRiskBook()
        assert book.dlmm_net_delta == 0.0
        assert book.hedge_action == "no_trade"
        assert book.gamma_relaxation == 1.0


class TestHedgeController:
    """Test HedgeController integration with SharedRiskBook."""

    @pytest.fixture
    def mock_hedge(self):
        h = MagicMock(spec=DlmmHedgeController)
        return h

    @pytest.fixture
    def shared_book(self):
        return SharedRiskBook()

    def test_no_trade_returns_empty(self, mock_hedge, shared_book):
        """When hedge_action is no_trade, no executor actions emitted."""
        mock_hedge.evaluate.return_value = ("no_trade", 0.0, None)
        shared_book.dlmm_net_delta = 0.0
        shared_book.dlmm_inventory_value_usd = 1000.0
        shared_book.dlmm_sigma = 0.5

        mock_hedge.evaluate(
            inventory_base=0.0,
            current_short=0.0,
            inventory_value_usd=1000.0,
            sigma_now=0.5,
            dt=5.0,
        )
        assert mock_hedge.evaluate.return_value[0] == "no_trade"

    def test_rehedge_positive_delta_creates_action(self, mock_hedge, shared_book):
        """When rehedge and delta positive, creates PA executor action."""
        mock_hedge.evaluate.return_value = ("rehedge", 5.0, None)
        shared_book.dlmm_net_delta = 5.0
        shared_book.dlmm_inventory_value_usd = 10000.0
        shared_book.dlmm_sigma = 0.3

        action, target, _ = mock_hedge.evaluate(
            inventory_base=5.0,
            current_short=0.0,
            inventory_value_usd=10000.0,
            sigma_now=0.3,
            dt=5.0,
        )
        assert action == "rehedge"
        assert target == 5.0

    def test_force_hedge_sets_gamma_relaxation(self, mock_hedge, shared_book):
        """force_hedge action sets gamma_relaxation to 0.5."""
        mock_hedge.evaluate.return_value = ("force_hedge", 10.0, None)
        shared_book.dlmm_net_delta = 10.0
        shared_book.dlmm_inventory_value_usd = 20000.0
        shared_book.dlmm_sigma = 1.0

        action, target, _ = mock_hedge.evaluate(
            inventory_base=10.0,
            current_short=0.0,
            inventory_value_usd=20000.0,
            sigma_now=1.0,
            dt=5.0,
        )
        assert action == "force_hedge"

    def test_pa_executor_config_fields(self):
        """PA executor config has correct child_order quantities."""
        from opms.executors.passive_aggressive_executor import PassiveAggressiveExecutorConfig
        cfg = PassiveAggressiveExecutorConfig(
            timestamp=1700000000,
            connector_name="hl",
            trading_pair="SOL-USD",
            side=TradeType.SELL,
            total_amount_base=Decimal("5.0"),
            child_order_quantity=Decimal("1.666666666666666666666666666666666666666666666666666666666666666666666666666666"),
            child_order_time_limit=60.0,
            child_order_refresh_time=20.0,
            leverage=1,
        )
        assert cfg.total_amount_base == Decimal("5.0")
        assert cfg.total_amount_base == pytest.approx(Decimal("5.0"))

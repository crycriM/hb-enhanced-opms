"""
Tests for FillObserver.

HB imports are mocked via conftest.py sys.modules injection.
"""

import time
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

# conftest.py already injected HB stubs before this file is loaded.
from conftest import TradeType, MarketEvent  # shared stubs
from opms.analytics.fill_observer import FillObserver


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fill_event(order_id: str, trading_pair: str, side: str, price: float, amount: float):
    """Build a fake OrderFilledEvent-like object."""
    event = MagicMock()
    event.order_id = order_id
    event.trading_pair = trading_pair
    event.trade_type = TradeType.BUY if side == "buy" else TradeType.SELL
    event.price = Decimal(str(price))
    event.amount = Decimal(str(amount))
    # flat_fees empty → fee = 0
    event.trade_fee = MagicMock()
    event.trade_fee.flat_fees = []
    return event


def _make_connector(name="hyperliquid_perpetual"):
    connector = MagicMock()
    connector.name = name
    return connector


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestFillObserverLifecycle:
    def test_register_and_unregister(self):
        observer = FillObserver(venue="hl", symbol="SOL-PERP")
        connector = _make_connector()
        observer.register(connector)
        connector.add_listener.assert_called_once_with(MarketEvent.OrderFilled, observer._fill_forwarder)
        observer.unregister(connector)
        connector.remove_listener.assert_called_once()

    def test_double_register_replaces(self):
        """Re-registering removes the old listener before adding the new one."""
        observer = FillObserver(venue="hl", symbol="SOL-PERP")
        c1 = _make_connector()
        c2 = _make_connector()
        observer.register(c1)
        observer.register(c2)
        c1.remove_listener.assert_called_once()
        c2.add_listener.assert_called_once()


class TestFillObserverFillAccounting:
    def _observer_with_fill(self, side: str, price: float, size: float, mid: float):
        observer = FillObserver(venue="hl", symbol="SOL-PERP")
        # Prime the mid so spread_capture is computed.
        observer.update_mid(mid)
        event = _make_fill_event("ord1", "SOL-PERP", side, price, size)
        observer._on_fill_event(0, None, event)
        return observer

    def test_position_after_buy(self):
        observer = self._observer_with_fill("buy", price=100.0, size=2.0, mid=100.5)
        assert observer.position == pytest.approx(2.0)

    def test_position_after_sell(self):
        observer = self._observer_with_fill("sell", price=100.0, size=1.0, mid=100.5)
        assert observer.position == pytest.approx(-1.0)

    def test_n_fills(self):
        observer = FillObserver(venue="hl", symbol="SOL-PERP")
        observer.update_mid(100.0)
        for i in range(3):
            event = _make_fill_event(f"ord{i}", "SOL-PERP", "buy", 100.0, 1.0)
            observer._on_fill_event(0, None, event)
        assert observer.n_fills == 3

    def test_wrong_symbol_ignored(self):
        observer = FillObserver(venue="hl", symbol="SOL-PERP")
        event = _make_fill_event("ord1", "BTC-PERP", "buy", 50000.0, 0.1)
        observer._on_fill_event(0, None, event)
        assert observer.n_fills == 0

    def test_realized_pnl_round_trip(self):
        """Buy at 100, sell at 110 → realized PnL = +10."""
        observer = FillObserver(venue="hl", symbol="SOL-PERP")
        observer.update_mid(100.5)
        buy = _make_fill_event("b1", "SOL-PERP", "buy", 100.0, 1.0)
        observer._on_fill_event(0, None, buy)
        sell = _make_fill_event("s1", "SOL-PERP", "sell", 110.0, 1.0)
        observer._on_fill_event(0, None, sell)
        assert observer.realized_pnl == pytest.approx(10.0)

    def test_explain_returns_dict(self):
        observer = FillObserver(venue="hl", symbol="SOL-PERP")
        observer.update_mid(100.0)
        event = _make_fill_event("o1", "SOL-PERP", "buy", 99.5, 1.0)
        observer._on_fill_event(0, None, event)
        report = observer.explain(mid=100.0)
        assert isinstance(report, dict)
        assert "trading_pnl" in report
        assert "spread_capture" in report


class TestFillObserverMarkout:
    def test_markout_resolves_after_mid_update(self):
        """A fill at t=0, then mid at t=11 s should resolve the 10 s horizon."""
        observer = FillObserver(venue="hl", symbol="SOL-PERP", markout_horizons=(10.0,))
        t0 = time.time()
        observer.update_mid(100.0, ts=t0)
        event = _make_fill_event("f1", "SOL-PERP", "buy", 100.0, 1.0)
        observer._on_fill_event(0, None, event)
        # Advance mid by 11 s
        observer.update_mid(102.0, ts=t0 + 11.0)

        stats = observer.markout_stats()
        # Buy filled at 100, mid at 102 → +200 bps markout ((102-100)/100 * 10000)
        assert stats[10.0] == pytest.approx(200.0, abs=0.01)

    def test_markout_not_yet_resolved(self):
        """Markout horizon not yet reached → None."""
        observer = FillObserver(venue="hl", symbol="SOL-PERP", markout_horizons=(60.0,))
        observer.update_mid(100.0)
        event = _make_fill_event("f1", "SOL-PERP", "buy", 100.0, 1.0)
        observer._on_fill_event(0, None, event)
        assert observer.markout_stats()[60.0] is None


class TestFillObserverSlippage:
    def test_buy_above_mid_is_positive_slippage(self):
        """Buy at 101 with mid at 100 → +100 bps adverse slippage."""
        observer = FillObserver(venue="hl", symbol="SOL-PERP")
        observer.update_mid(100.0)
        event = _make_fill_event("o1", "SOL-PERP", "buy", 101.0, 1.0)
        observer._on_fill_event(0, None, event)
        stats = observer.slippage_stats()
        assert stats["n"] == 1
        assert stats["mean_bps"] == pytest.approx(100.0, abs=0.1)

    def test_sell_above_mid_is_negative_slippage(self):
        """Sell at 101 with mid at 100 → negative slippage (seller got more than mid)."""
        observer = FillObserver(venue="hl", symbol="SOL-PERP")
        observer.update_mid(100.0)
        event = _make_fill_event("o1", "SOL-PERP", "sell", 101.0, 1.0)
        observer._on_fill_event(0, None, event)
        stats = observer.slippage_stats()
        assert stats["mean_bps"] == pytest.approx(-100.0, abs=0.1)

    def test_no_fills_slippage_stats(self):
        observer = FillObserver(venue="hl", symbol="SOL-PERP")
        stats = observer.slippage_stats()
        assert stats["n"] == 0
        assert stats["mean_bps"] is None

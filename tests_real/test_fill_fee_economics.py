from decimal import Decimal
from types import SimpleNamespace

import pytest
from hummingbot.core.data_type.common import TradeType
from hummingbot.core.data_type.trade_fee import AddedToCostTradeFee, TokenAmount
from opms.analytics.fill_observer import FillObserver


def test_observer_sums_percent_and_all_flat_fees_and_uses_event_timestamp():
    observer = FillObserver(venue="hl", symbol="SOL-USD")
    observer.update_mid(100, ts=999)
    event = SimpleNamespace(timestamp=1000, order_id="one", trading_pair="SOL-USD",
        trade_type=TradeType.BUY, price=Decimal("100"), amount=Decimal("2"),
        trade_fee=AddedToCostTradeFee(percent=Decimal(".001"), flat_fees=[
            TokenAmount("USD", Decimal(".03")), TokenAmount("SOL", Decimal(".002"))]))
    observer._on_fill_event(0, None, event)
    fill = observer._ledger.fills[0]
    assert fill.ts == 1000
    assert fill.fee == pytest.approx(.43)


def _event(fee, *, ts=1000, pair="SOL-USDC", side=TradeType.BUY,
           price=Decimal("100"), amount=Decimal("1")):
    return SimpleNamespace(timestamp=ts, order_id=f"o{ts}", trading_pair=pair,
                           trade_type=side, price=price, amount=amount,
                           trade_fee=fee)


def test_percentage_fee_is_applied_over_notional():
    observer = FillObserver(venue="hl", symbol="SOL-USDC")
    observer._on_fill_event(0, None, _event(
        AddedToCostTradeFee(percent=Decimal("0.0002")),
        amount=Decimal("3"), ts=1001))
    assert observer._ledger.fills[0].fee == pytest.approx(0.06)


def test_base_token_flat_fee_converts_at_fill_price():
    observer = FillObserver(venue="hl", symbol="SOL-USDC")
    observer._on_fill_event(0, None, _event(
        AddedToCostTradeFee(flat_fees=[TokenAmount("SOL", Decimal("0.005"))]),
        ts=1002))
    assert observer._ledger.fills[0].fee == pytest.approx(0.5)


def test_multiple_quote_flat_fees_all_count():
    observer = FillObserver(venue="hl", symbol="SOL-USDC")
    observer._on_fill_event(0, None, _event(
        AddedToCostTradeFee(flat_fees=[TokenAmount("USDC", Decimal("0.02")),
                                       TokenAmount("USDC", Decimal("0.03"))]),
        ts=1003))
    assert observer._ledger.fills[0].fee == pytest.approx(0.05)


def test_rebate_stays_negative_and_is_not_clamped():
    observer = FillObserver(venue="hl", symbol="SOL-USDC")
    observer._on_fill_event(0, None, _event(
        AddedToCostTradeFee(flat_fees=[TokenAmount("USDC", Decimal("-0.05"))]),
        ts=1004))
    fill = observer._ledger.fills[0]
    assert fill.fee == pytest.approx(-0.05)


def test_unconvertible_third_token_fee_keeps_fill_with_zero_fee(monkeypatch):
    from hummingbot.core.rate_oracle.rate_oracle import RateOracle

    class _NoRates:
        def get_pair_rate(self, pair):
            return None

    monkeypatch.setattr(RateOracle, "get_instance", classmethod(lambda cls: _NoRates()))
    observer = FillObserver(venue="hl", symbol="SOL-USDC")
    observer.update_mid(100, ts=900)
    observer._on_fill_event(0, None, _event(
        AddedToCostTradeFee(flat_fees=[TokenAmount("ARB", Decimal("0.1"))]),
        ts=1005))
    fills = observer._ledger.fills
    assert len(fills) == 1
    assert fills[0].ts == 1005
    assert fills[0].fee == pytest.approx(0.0)
    assert observer.last_fill_ts is not None
    assert len(observer._slippage_bps) == 1

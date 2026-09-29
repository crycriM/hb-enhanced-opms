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

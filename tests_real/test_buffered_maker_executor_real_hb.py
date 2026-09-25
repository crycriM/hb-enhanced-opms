"""Buffered maker quote executor against the real Hummingbot runtime."""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

pytest.importorskip("hummingbot.strategy_v2.executors.order_executor.order_executor")

from hummingbot.core.data_type.common import OrderType, PositionAction, PriceType, TradeType  # noqa: E402
from hummingbot.strategy_v2.executors.order_executor.data_types import ExecutionStrategy  # noqa: E402

from opms.executors.buffered_maker_executor import (  # noqa: E402
    BufferedMakerExecutor,
    BufferedMakerExecutorConfig,
)


def _strategy():
    strategy = MagicMock()
    strategy.current_timestamp = 100.0
    connector = MagicMock()
    connector.get_price_by_type.side_effect = lambda pair, price_type: {
        PriceType.BestBid: Decimal("100"),
        PriceType.BestAsk: Decimal("101"),
    }[price_type]
    strategy.connectors = {"hyperliquid_perpetual": connector}
    strategy.buy.return_value = "oid-1"
    return strategy, connector


def _config():
    return BufferedMakerExecutorConfig(
        timestamp=100.0,
        connector_name="hyperliquid_perpetual",
        trading_pair="ETH-USD",
        side=TradeType.BUY,
        amount=Decimal("0.1"),
        price=Decimal("102"),
        execution_strategy=ExecutionStrategy.LIMIT_MAKER,
        position_action=PositionAction.OPEN,
        leverage=6,
        maker_buffer_bps=Decimal("8"),
    )


def test_price_is_buffered_beyond_live_touch():
    strategy, _ = _strategy()
    executor = BufferedMakerExecutor(strategy, _config())

    assert executor.get_order_price() == Decimal("99.9200")


def test_rejected_quote_backs_off_then_reprices_from_fresh_touch():
    strategy, connector = _strategy()
    executor = BufferedMakerExecutor(strategy, _config())
    executor.control_order()
    event = MagicMock(
        order_id="oid-1",
        error_message="Post only order would have immediately matched",
        order_type=OrderType.LIMIT_MAKER,
    )

    executor.process_order_failed_event(0, connector, event)
    executor.control_order()
    assert strategy.buy.call_count == 1

    strategy.current_timestamp += 2.0
    connector.get_price_by_type.side_effect = lambda pair, price_type: {
        PriceType.BestBid: Decimal("99"),
        PriceType.BestAsk: Decimal("100"),
    }[price_type]
    strategy.buy.return_value = "oid-2"
    executor.control_order()

    assert strategy.buy.call_count == 2
    assert strategy.buy.call_args.args[4] == Decimal("98.9208")

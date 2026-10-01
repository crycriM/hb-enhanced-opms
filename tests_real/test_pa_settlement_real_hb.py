"""PA settlement assumptions against real Hummingbot order tracking (no stubs).

HB's ClientOrderTracker moves a cancelled order into a fillable cache, so an
OrderFilled event can follow OrderCancelled; it de-duplicates by trade id; a
completion carries the order's cumulative executed amount. These tests drive
real tracker/InFlightOrder updates into a real PassiveAggressiveExecutor.
"""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

pytest.importorskip("hummingbot.connector.client_order_tracker")

from hummingbot.connector.client_order_tracker import ClientOrderTracker  # noqa: E402
from hummingbot.core.data_type.common import OrderType, PositionAction, TradeType  # noqa: E402
from hummingbot.core.data_type.in_flight_order import (  # noqa: E402
    InFlightOrder,
    OrderState,
    OrderUpdate,
    TradeUpdate,
)
from hummingbot.core.data_type.trade_fee import AddedToCostTradeFee, TokenAmount  # noqa: E402
from hummingbot.core.event.events import MarketEvent  # noqa: E402
from hummingbot.strategy_v2.models.executors import CloseType  # noqa: E402

from opms.executors.passive_aggressive_executor import (  # noqa: E402
    PassiveAggressiveExecutor,
    PassiveAggressiveExecutorConfig,
)

PAIR = "ETH-USD"


class _Connector:
    """Delivers the tracker's real events to the executor, like HB's pubsub."""

    current_timestamp = 100.0

    def __init__(self):
        self.executor = None
        self.events = []

    def trigger_event(self, tag, event):
        self.events.append(tag)
        handler = {
            MarketEvent.OrderFilled: "process_order_filled_event",
            MarketEvent.OrderCancelled: "process_order_canceled_event",
            MarketEvent.BuyOrderCompleted: "process_order_completed_event",
            MarketEvent.SellOrderCompleted: "process_order_completed_event",
            MarketEvent.OrderFailure: "process_order_failed_event",
        }.get(tag)
        if handler and self.executor is not None:
            getattr(self.executor, handler)(tag, self, event)


def _setup(position_action):
    connector = _Connector()
    tracker = ClientOrderTracker(connector)
    strategy = MagicMock()
    strategy.current_timestamp = 100.0
    market = MagicMock()
    market.get_price_by_type.return_value = Decimal("3000")
    strategy.connectors = {"hyperliquid_perpetual": market}
    ids = iter(f"oid-{i}" for i in range(1, 10))

    def _sell(connector_name, trading_pair, amount, order_type, price, position):
        order_id = next(ids)
        tracker.start_tracking_order(InFlightOrder(
            client_order_id=order_id, trading_pair=PAIR, order_type=order_type,
            trade_type=TradeType.SELL, amount=amount, creation_timestamp=100.0,
            price=price, exchange_order_id=f"x-{order_id}", initial_state=OrderState.OPEN,
            position=position,
        ))
        return order_id

    strategy.sell.side_effect = _sell
    config = PassiveAggressiveExecutorConfig(
        timestamp=100.0, connector_name="hyperliquid_perpetual", trading_pair=PAIR,
        side=TradeType.SELL, total_amount_base=Decimal("1"), child_order_quantity=Decimal("1"),
        child_order_time_limit=5.0, child_order_refresh_time=60.0, position_action=position_action,
    )
    executor = PassiveAggressiveExecutor(strategy, config)
    connector.executor = executor
    return executor, tracker, strategy, connector


def _trade(order_id, amount, trade_id):
    return TradeUpdate(
        trade_id=trade_id, client_order_id=order_id, exchange_order_id=f"x-{order_id}",
        trading_pair=PAIR, fill_timestamp=100.0, fill_price=Decimal("3000"),
        fill_base_amount=amount, fill_quote_amount=amount * Decimal("3000"),
        fee=AddedToCostTradeFee(flat_fees=[TokenAmount("USD", Decimal("0.01"))]),
    )


async def _cancel_ack(tracker, order_id):
    await tracker._process_order_update(OrderUpdate(
        trading_pair=PAIR, update_timestamp=100.0, new_state=OrderState.CANCELED,
        client_order_id=order_id,
    ))


async def _expire_first_limit(executor, tracker, strategy):
    executor._step()
    limit = executor._children[0].tracked_order.order_id
    strategy.current_timestamp += 6.0
    executor._step()
    await _cancel_ack(tracker, limit)
    return limit


async def test_hb_delivers_fills_after_cancel_and_executor_credits_them_once():
    executor, tracker, strategy, connector = _setup(PositionAction.CLOSE)
    limit = await _expire_first_limit(executor, tracker, strategy)
    market = executor._children[0].tracked_order.order_id
    assert market != limit and strategy.sell.call_args.args[3] == OrderType.MARKET

    # The assumption the executor must survive: a fill after the cancel ack.
    tracker.process_trade_update(_trade(limit, Decimal("0.3"), "t-1"))
    tracker.process_trade_update(_trade(limit, Decimal("0.3"), "t-1"))  # replayed
    assert connector.events.count(MarketEvent.OrderFilled) == 1
    assert connector.events.index(MarketEvent.OrderCancelled) < connector.events.index(MarketEvent.OrderFilled)
    assert executor._children[0].filled == Decimal("0.3")

    tracker.process_trade_update(_trade(market, Decimal("0.7"), "t-2"))
    await tracker._process_order_update(OrderUpdate(
        trading_pair=PAIR, update_timestamp=100.0, new_state=OrderState.FILLED,
        client_order_id=market,
    ))
    # Completion carries the market order's cumulative 1.0 (it was sized
    # before the late 0.3 arrived): credited once per order, 1.3 overall.
    assert executor._cumulative_filled == Decimal("1.3")
    executor._step()
    assert executor.close_type == CloseType.COMPLETED


async def test_open_does_not_cross_after_a_real_cancel_ack():
    executor, tracker, strategy, _ = _setup(PositionAction.OPEN)
    await _expire_first_limit(executor, tracker, strategy)
    assert strategy.sell.call_count == 1
    executor._step()
    executor._step()
    assert executor.close_type == CloseType.TIME_LIMIT

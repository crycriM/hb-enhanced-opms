"""
Tests for PassiveAggressiveExecutor state machine.

Hummingbot is mocked via conftest.py sys.modules injection (before any
import here).  The executor logic is driven directly without a live runtime.
"""

import sys
import time
from decimal import Decimal
from enum import Enum, auto
from typing import Optional
from unittest.mock import MagicMock

import pytest

# conftest.py has already injected HB stubs.
from conftest import (
    CloseType,
    ExecutorBase,
    OrderType,
    PositionAction,
    PriceType,
    RunnableStatus,
    TrackedOrder,
    TradeType,
)

from opms.executors.passive_aggressive_executor import (
    PassiveAggressiveExecutor,
    PassiveAggressiveExecutorConfig,
    _ChildStatus,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ORDER_COUNTER = [0]


def _make_strategy(connector_name="hyperliquid_perpetual", mid=100.0):
    strategy = MagicMock()
    strategy.current_timestamp = time.time()

    connector = MagicMock()
    connector.get_price_by_type = MagicMock(return_value=Decimal(str(mid)))
    connector.trading_rules = {
        "SOL-PERP": MagicMock(min_order_size=Decimal("0.1"))
    }
    strategy.connectors = {connector_name: connector}

    # buy/sell return auto-incrementing order IDs
    def _place(*args, **kwargs):
        _ORDER_COUNTER[0] += 1
        return f"oid-{_ORDER_COUNTER[0]}"

    strategy.buy = MagicMock(side_effect=_place)
    strategy.sell = MagicMock(side_effect=_place)
    strategy.cancel = MagicMock()
    return strategy


def _make_config(total=Decimal("3"), child_q=Decimal("1"),
                 time_limit=60.0, refresh_time=20.0):
    return PassiveAggressiveExecutorConfig(
        connector_name="hyperliquid_perpetual",
        trading_pair="SOL-PERP",
        side=TradeType.BUY,
        total_amount_base=total,
        child_order_quantity=child_q,
        child_order_time_limit=time_limit,
        child_order_refresh_time=refresh_time,
    )


def _make_executor(total=Decimal("3"), child_q=Decimal("1"), **cfg_kwargs):
    strategy = _make_strategy()
    config = _make_config(total=total, child_q=child_q, **cfg_kwargs)
    return PassiveAggressiveExecutor(strategy, config), strategy


def _fill_event(order_id, trading_pair="SOL-PERP", side=TradeType.BUY, amount=Decimal("1")):
    event = MagicMock()
    event.order_id = order_id
    event.trading_pair = trading_pair
    event.trade_type = side
    event.amount = amount
    event.price = Decimal("100")
    event.trade_fee = MagicMock()
    event.trade_fee.flat_fees = []
    return event


def _cancel_event(order_id):
    event = MagicMock()
    event.order_id = order_id
    return event


def _completed_event(order_id, amount=Decimal("1")):
    event = MagicMock()
    event.order_id = order_id
    event.base_asset_amount = amount
    return event


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestChildSlotBuilding:
    def test_exact_division(self):
        exe, _ = _make_executor(total=Decimal("6"), child_q=Decimal("2"))
        assert len(exe._children) == 3

    def test_with_remainder(self):
        exe, _ = _make_executor(total=Decimal("2.5"), child_q=Decimal("1"))
        assert len(exe._children) == 3
        assert exe._children[-1].target == Decimal("0.5")

    def test_all_children_idle(self):
        exe, _ = _make_executor()
        assert all(c.status == _ChildStatus.IDLE for c in exe._children)


class TestLimitOrderPlacement:
    def test_first_step_places_limit(self):
        exe, strategy = _make_executor()
        exe._step()
        strategy.buy.assert_called_once()
        child = exe._children[0]
        assert child.status == _ChildStatus.ACTIVE
        assert child.tracked_order is not None

    def test_active_child_not_replaced_before_refresh(self):
        exe, strategy = _make_executor(refresh_time=60.0)
        exe._step()
        # On next step (before refresh), same order stays active.
        exe._step()
        assert strategy.buy.call_count == 1

    def test_refresh_triggers_cancel(self):
        exe, strategy = _make_executor(refresh_time=1.0)
        exe._step()
        order_id = exe._children[0].tracked_order.order_id
        # Advance time past refresh window
        exe._strategy.current_timestamp += 2.0
        exe._step()
        strategy.cancel.assert_called_once_with(
            "hyperliquid_perpetual", "SOL-PERP", order_id
        )
        assert exe._children[0].status == _ChildStatus.CANCELING

    def test_cancel_confirmed_refresh_restores_idle(self):
        exe, strategy = _make_executor(refresh_time=1.0)
        exe._step()
        order_id = exe._children[0].tracked_order.order_id
        exe._strategy.current_timestamp += 2.0
        exe._step()  # triggers cancel
        # Simulate cancel event
        exe.process_order_canceled_event(0, None, _cancel_event(order_id))
        assert exe._children[0].status == _ChildStatus.IDLE


class TestCycleExpiry:
    def test_cycle_expiry_triggers_aggressive(self):
        exe, strategy = _make_executor(time_limit=5.0, refresh_time=60.0)
        exe._step()  # places limit
        order_id = exe._children[0].tracked_order.order_id
        # Advance past cycle limit
        exe._strategy.current_timestamp += 6.0
        exe._step()  # triggers cancel
        strategy.cancel.assert_called_once()
        # Simulate cancel event → should place aggressive (market)
        exe.process_order_canceled_event(0, None, _cancel_event(order_id))
        assert exe._children[0].status == _ChildStatus.AGGRESSIVE
        # buy should have been called twice: once limit, once market
        assert strategy.buy.call_count == 2


class TestFillAccounting:
    def test_partial_fill_updates_child_filled(self):
        exe, strategy = _make_executor(child_q=Decimal("2"))
        exe._step()
        order_id = exe._children[0].tracked_order.order_id
        event = _fill_event(order_id, amount=Decimal("1"))
        exe.process_order_filled_event(0, None, event)
        assert exe._children[0].filled == Decimal("1")
        assert exe._cumulative_filled == Decimal("1")

    def test_completed_event_marks_child_done(self):
        exe, strategy = _make_executor()
        exe._step()
        order_id = exe._children[0].tracked_order.order_id
        exe.process_order_completed_event(0, None, _completed_event(order_id, Decimal("1")))
        assert exe._children[0].status == _ChildStatus.DONE

    def test_fill_for_wrong_order_ignored(self):
        exe, strategy = _make_executor()
        exe._step()
        event = _fill_event("wrong-order-id", amount=Decimal("1"))
        exe.process_order_filled_event(0, None, event)
        assert exe._cumulative_filled == Decimal("0")

    def test_all_children_done_closes_executor(self):
        exe, strategy = _make_executor(total=Decimal("1"), child_q=Decimal("1"))
        exe._step()
        order_id = exe._children[0].tracked_order.order_id
        exe.process_order_completed_event(0, None, _completed_event(order_id, Decimal("1")))
        # Next step should detect done and close.
        exe._step()
        assert exe._status == RunnableStatus.TERMINATED
        assert exe.close_type == CloseType.COMPLETED


class TestOrderFailure:
    def test_failed_order_resets_to_idle(self):
        exe, strategy = _make_executor()
        exe._step()
        order_id = exe._children[0].tracked_order.order_id
        failed_event = MagicMock()
        failed_event.order_id = order_id
        exe.process_order_failed_event(0, None, failed_event)
        assert exe._children[0].status == _ChildStatus.IDLE
        assert exe._current_retries == 1

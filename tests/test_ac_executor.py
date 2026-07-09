"""
Tests for ACScheduleExecutor.

Hummingbot is mocked via conftest.py sys.modules injection.
"""

import time
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

# conftest.py has already injected HB stubs.
from conftest import CloseType, RunnableStatus, TradeType, TrackedOrder

from opms.executors.ac_schedule_executor import ACScheduleExecutor, ACScheduleExecutorConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_counter = [0]


def _make_strategy():
    strategy = MagicMock()
    strategy.current_timestamp = 0.0
    connector = MagicMock()
    connector.get_price_by_type = MagicMock(return_value=Decimal("100"))
    strategy.connectors = {"hyperliquid_perpetual": connector}
    return strategy


def _make_config(**overrides):
    params = dict(
        connector_name="hyperliquid_perpetual",
        trading_pair="SOL-PERP",
        side=TradeType.BUY,
        total_amount_base=Decimal("10"),
        duration_seconds=100.0,
        num_intervals=10,
        risk_aversion=1e-5,
        volatility=0.03,
        eta=0.01,
        gamma=0.0,
        volume_forecast=False,
    )
    params.update(overrides)
    return ACScheduleExecutorConfig(**params)


def _make_executor(**cfg_overrides):
    strategy = _make_strategy()
    config = _make_config(**cfg_overrides)
    return ACScheduleExecutor(strategy, config), strategy


def _completed_event(order_id, amount=Decimal("1")):
    event = MagicMock()
    event.order_id = order_id
    event.base_asset_amount = amount
    return event


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestScheduleInitialization:
    def test_schedule_sums_to_total(self):
        exe, _ = _make_executor()
        assert sum(exe._schedule) == Decimal("10")

    def test_schedule_length_matches_intervals(self):
        exe, _ = _make_executor(num_intervals=8)
        assert len(exe._schedule) == 8

    def test_risk_neutral_schedule_is_uniform(self):
        exe, _ = _make_executor(risk_aversion=1e-12, num_intervals=5)
        first = exe._schedule[0]
        assert all(abs(s - first) < Decimal("0.001") for s in exe._schedule)

    def test_high_risk_aversion_is_front_loaded(self):
        exe, _ = _make_executor(risk_aversion=1e-3, num_intervals=10)
        assert exe._schedule[0] > exe._schedule[-1]

    def test_invalid_params_raise(self):
        with pytest.raises(ValueError):
            _make_executor(num_intervals=-1)


class TestSlicePacing:
    async def test_first_tick_submits_slice(self):
        exe, strategy = _make_executor(num_intervals=5)
        await exe._tick()
        assert exe._slice_idx == 1
        assert len(exe._submitted) == 1

    async def test_no_slice_before_interval_elapses(self):
        exe, strategy = _make_executor(num_intervals=5, duration_seconds=100.0)
        await exe._tick()  # first slice at t=0
        # Don't advance time — second tick should NOT submit
        await exe._tick()
        assert exe._slice_idx == 1

    async def test_slice_submitted_after_interval(self):
        exe, strategy = _make_executor(num_intervals=5, duration_seconds=50.0)
        # interval = 50/5 = 10 s
        await exe._tick()  # slice 1 at t=0
        exe._strategy.current_timestamp += 11.0   # past 10 s interval
        await exe._tick()  # slice 2
        assert exe._slice_idx == 2

    async def test_all_slices_submitted_in_sequence(self):
        exe, strategy = _make_executor(num_intervals=4, duration_seconds=40.0)
        for i in range(4):
            exe._strategy.current_timestamp = float(i) * 11.0
            await exe._tick()
        assert exe._slice_idx == 4


class TestFillAccounting:
    async def test_completed_event_updates_filled(self):
        exe, _ = _make_executor(num_intervals=3, duration_seconds=30.0)
        await exe._tick()
        order_id = exe._submitted[0].order_id
        exe.process_order_completed_event(0, None, _completed_event(order_id, Decimal("3.33")))
        assert exe._cumulative_filled == pytest.approx(Decimal("3.33"))

    async def test_wrong_order_ignored(self):
        exe, _ = _make_executor(num_intervals=2, duration_seconds=20.0)
        await exe._tick()
        exe.process_order_completed_event(0, None, _completed_event("wrong-id", Decimal("5")))
        assert exe._cumulative_filled == Decimal("0")

    async def test_all_slices_and_fills_complete_executor(self):
        exe, _ = _make_executor(num_intervals=2, duration_seconds=20.0)
        # Submit both slices
        for i in range(2):
            exe._strategy.current_timestamp = float(i) * 11.0
            await exe._tick()
        # Simulate fills for both slices
        for tracked in exe._submitted:
            tracked.is_done = True
            exe.process_order_completed_event(0, None, _completed_event(tracked.order_id, Decimal("5")))
        # Next tick should close the executor
        await exe._tick()
        assert exe._status == RunnableStatus.TERMINATED


class TestFailedOrder:
    async def test_failed_order_rewinds_slice(self):
        exe, _ = _make_executor(num_intervals=5, duration_seconds=50.0)
        await exe._tick()
        assert exe._slice_idx == 1
        failed_evt = MagicMock()
        failed_evt.order_id = exe._submitted[0].order_id
        exe.process_order_failed_event(0, None, failed_evt)
        assert exe._slice_idx == 0  # rewind
        assert exe._current_retries == 1

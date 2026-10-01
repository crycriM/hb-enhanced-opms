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
        assert exe.close_type == CloseType.COMPLETED


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


class TestShutdownAndOutcome:
    async def test_early_stop_does_not_report_completed(self):
        exe, _ = _make_executor(num_intervals=2, duration_seconds=20.0)
        await exe._tick()
        exe.early_stop()
        await exe.control_task()
        assert exe._status == RunnableStatus.TERMINATED
        assert exe.close_type == CloseType.EARLY_STOP

    async def test_under_filled_completion_closes_time_limit(self):
        exe, _ = _make_executor(num_intervals=2, duration_seconds=20.0)
        for i in range(2):
            exe._strategy.current_timestamp = float(i) * 11.0
            await exe._tick()
        for tracked in exe._submitted:
            tracked.is_done = True
            exe.process_order_completed_event(0, None, _completed_event(tracked.order_id, Decimal("2")))
        await exe.control_task()
        assert exe._cumulative_filled < exe.config.total_amount_base
        assert exe._status == RunnableStatus.TERMINATED
        assert exe.close_type == CloseType.TIME_LIMIT

    async def test_evaluate_max_retries_closes_failed(self):
        exe, _ = _make_executor()
        exe._current_retries = exe._max_retries + 1
        exe.evaluate_max_retries()
        assert exe._status == RunnableStatus.TERMINATED
        assert exe.close_type == CloseType.FAILED


# ---------------------------------------------------------------------------
# T3 (2026-10-01): OPEN-only and bounded schedules, rejected before allocation
# ---------------------------------------------------------------------------

from conftest import PositionAction  # noqa: E402


class TestOpenOnlyAndScheduleLimits:
    @pytest.mark.parametrize("action", [PositionAction.CLOSE, PositionAction.NIL])
    def test_construction_rejects_non_open(self, action):
        with pytest.raises(ValueError, match="OPEN"):
            _make_executor(position_action=action)

    async def test_default_open_reaches_order_submission(self):
        exe, strategy = _make_executor()
        assert exe.config.position_action == PositionAction.OPEN
        await exe._tick()
        assert strategy.buy.call_args.args[-1] == PositionAction.OPEN

    @pytest.mark.parametrize("num_intervals, duration", [(0, 100.0), (201, 1000.0), (-1, 100.0)])
    def test_interval_count_outside_1_to_200_rejects_before_allocation(
            self, monkeypatch, num_intervals, duration):
        import opms.executors.ac_schedule_executor as module

        def _no_allocation(**_):
            raise AssertionError("schedule allocated before validation")

        monkeypatch.setattr(module, "build_schedule", _no_allocation)
        with pytest.raises(ValueError, match="num_intervals"):
            _make_executor(num_intervals=num_intervals, duration_seconds=duration)

    @pytest.mark.parametrize("num_intervals, duration", [(1, 1.0), (200, 200.0)])
    def test_interval_count_bounds_are_valid(self, num_intervals, duration):
        exe, _ = _make_executor(num_intervals=num_intervals, duration_seconds=duration)
        assert len(exe._schedule) == num_intervals
        assert exe._interval_seconds >= 1.0

    @pytest.mark.parametrize("duration", [0.0, -5.0, float("nan"), float("inf"), 199.0])
    def test_invalid_or_subsecond_durations_reject(self, duration):
        with pytest.raises(ValueError, match="duration"):
            _make_executor(num_intervals=200, duration_seconds=duration)

    async def test_volume_aware_schedule_keeps_the_limits(self, monkeypatch):
        import opms.forecasting.historical_profile as profile

        class _Forecaster:
            def __init__(self, provider):
                pass

            async def forecast(self, symbol, num_buckets, bucket_seconds, start_time):
                return [Decimal(i + 1) for i in range(num_buckets)]

        monkeypatch.setattr(profile, "HistoricalProfileForecaster", _Forecaster)
        monkeypatch.setattr(profile, "HBCandlesProvider", lambda **_: None)
        exe, _ = _make_executor(num_intervals=200, duration_seconds=200.0, volume_forecast=True)
        clock = list(exe._schedule)
        await exe._tick()
        assert exe._schedule != clock  # the volume path actually ran
        assert len(exe._schedule) <= 200
        assert exe._interval_seconds >= 1.0
        assert sum(exe._schedule) == Decimal("10")

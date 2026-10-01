"""ACScheduleExecutorConfig validation against real Hummingbot/pydantic (no stubs)."""

from decimal import Decimal

import pytest

pytest.importorskip("hummingbot.strategy_v2.executors.data_types")

from hummingbot.core.data_type.common import PositionAction, TradeType  # noqa: E402

from opms.executors.ac_schedule_executor import ACScheduleExecutorConfig  # noqa: E402


def _config(**overrides):
    params = dict(
        timestamp=100.0, connector_name="hyperliquid_perpetual", trading_pair="ETH-USD",
        side=TradeType.BUY, total_amount_base=Decimal("1"),
        duration_seconds=100.0, num_intervals=10,
    )
    params.update(overrides)
    return ACScheduleExecutorConfig(**params)


def test_default_is_open():
    assert _config().position_action == PositionAction.OPEN


@pytest.mark.parametrize("action", [PositionAction.CLOSE, PositionAction.NIL])
def test_config_rejects_non_open(action):
    with pytest.raises(ValueError, match="OPEN"):
        _config(position_action=action)


@pytest.mark.parametrize("num_intervals, duration", [(0, 100.0), (201, 1000.0)])
def test_config_rejects_interval_count_outside_1_to_200(num_intervals, duration):
    with pytest.raises(ValueError, match="num_intervals"):
        _config(num_intervals=num_intervals, duration_seconds=duration)


@pytest.mark.parametrize("num_intervals, duration", [(1, 1.0), (200, 200.0)])
def test_config_accepts_interval_bounds(num_intervals, duration):
    assert _config(num_intervals=num_intervals, duration_seconds=duration).num_intervals == num_intervals


@pytest.mark.parametrize("duration", [0.0, -5.0, float("nan"), float("inf"), 199.0])
def test_config_rejects_invalid_or_subsecond_durations(duration):
    with pytest.raises(ValueError, match="duration"):
        _config(num_intervals=200, duration_seconds=duration)

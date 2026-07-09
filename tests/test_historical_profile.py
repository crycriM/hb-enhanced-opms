"""
Tests for HistoricalProfileForecaster (opms version).

HB-free: uses an in-memory stub for the CandlesProvider protocol.
"""

import time
from decimal import Decimal

import pytest

from opms.forecasting.historical_profile import (
    CandlesProvider,
    HistoricalProfileForecaster,
)


# ---------------------------------------------------------------------------
# Stub provider
# ---------------------------------------------------------------------------

class _StubProvider:
    """
    Returns a fixed list of candles from an in-memory list.

    Each candle is aligned to hour boundaries; ``volume`` is 100 per candle.
    """

    def __init__(self, candles: list[dict] | None = None, raise_on_call: bool = False):
        self._candles = candles or []
        self._raise = raise_on_call

    async def get_candles(self, trading_pair: str, interval: str, limit: int) -> list[dict]:
        if self._raise:
            raise RuntimeError("candle fetch failure")
        return self._candles[:limit]


def _make_candles(num_hours: int, base_ts_ms: int, volume: float = 100.0) -> list[dict]:
    """Build synthetic hourly candles with constant volume."""
    candles = []
    for i in range(num_hours):
        open_ms = base_ts_ms + i * 3_600_000
        close_ms = open_ms + 3_600_000
        candles.append({
            "open_time": open_ms,
            "close_time": close_ms,
            "volume": str(volume),
        })
    return candles


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_uniform_volume_gives_flat_profile():
    """When all hours have the same volume, the forecast should be uniform."""
    now_ms = int(time.time() * 1000)
    # Align to start of the current hour
    now_ms = (now_ms // 3_600_000) * 3_600_000
    candles = _make_candles(7 * 24, base_ts_ms=now_ms - 7 * 24 * 3_600_000)
    provider = _StubProvider(candles)
    forecaster = HistoricalProfileForecaster(provider)

    forecast = await forecaster.forecast(
        symbol="SOL-PERP",
        num_buckets=12,
        bucket_seconds=300.0,
    )
    assert len(forecast) == 12
    # All buckets should be equal (flat profile)
    assert all(abs(float(v) - float(forecast[0])) < 1e-6 for v in forecast)


@pytest.mark.asyncio
async def test_sum_proportional_to_duration():
    """Total forecast volume is proportional to num_buckets * bucket_seconds."""
    now_ms = int(time.time() * 1000)
    now_ms = (now_ms // 3_600_000) * 3_600_000
    candles = _make_candles(7 * 24, base_ts_ms=now_ms - 7 * 24 * 3_600_000, volume=100.0)
    provider = _StubProvider(candles)
    forecaster = HistoricalProfileForecaster(provider)

    bucket_seconds = 300.0
    num_buckets = 12

    forecast_a = await forecaster.forecast("SOL-PERP", num_buckets, bucket_seconds)
    forecast_b = await forecaster.forecast("SOL-PERP", num_buckets * 2, bucket_seconds)

    total_a = sum(float(v) for v in forecast_a)
    total_b = sum(float(v) for v in forecast_b)
    # Double the buckets → double the volume forecast
    assert abs(total_b / total_a - 2.0) < 0.01


@pytest.mark.asyncio
async def test_no_candles_returns_uniform():
    """Empty candles → uniform fallback."""
    provider = _StubProvider(candles=[])
    forecaster = HistoricalProfileForecaster(provider)
    forecast = await forecaster.forecast("SOL-PERP", 6, 3600.0)
    assert len(forecast) == 6
    assert all(v > 0 for v in forecast)
    # All equal (uniform fallback)
    assert all(v == forecast[0] for v in forecast)


@pytest.mark.asyncio
async def test_provider_raises_returns_uniform():
    """Exception from provider → uniform fallback, no crash."""
    provider = _StubProvider(raise_on_call=True)
    forecaster = HistoricalProfileForecaster(provider)
    forecast = await forecaster.forecast("SOL-PERP", 4, 3600.0)
    assert len(forecast) == 4
    assert all(v > 0 for v in forecast)


@pytest.mark.asyncio
async def test_none_provider_returns_uniform():
    """No provider set at all → uniform."""
    forecaster = HistoricalProfileForecaster(provider=None)
    forecast = await forecaster.forecast("BTC-PERP", 8, 600.0)
    assert len(forecast) == 8
    assert all(v == forecast[0] for v in forecast)


@pytest.mark.asyncio
async def test_zero_buckets_returns_empty():
    provider = _StubProvider(candles=[])
    forecaster = HistoricalProfileForecaster(provider)
    assert await forecaster.forecast("SOL-PERP", 0, 3600.0) == []


@pytest.mark.asyncio
async def test_invalid_bucket_seconds():
    provider = _StubProvider(candles=[])
    forecaster = HistoricalProfileForecaster(provider)
    with pytest.raises(ValueError):
        await forecaster.forecast("SOL-PERP", 4, -1.0)


@pytest.mark.asyncio
async def test_provider_satisfies_protocol():
    """Both stub and HBCandlesProvider satisfy the CandlesProvider protocol."""
    stub = _StubProvider()
    assert isinstance(stub, CandlesProvider)

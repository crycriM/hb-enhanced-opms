"""
Historical intraday volume-profile forecaster — opms / HB version.

Ported from dex_executor/forecasting/historical_profile.py.  The only
change is the data-source interface: instead of an OPMS adapter this
module accepts a ``CandlesProvider`` protocol, keeping the math HB-agnostic
and unit-testable without a live connector.

Concrete HB integration: ``HBCandlesProvider`` wraps a HB connector and
fetches 1 h candles via the connector's ``get_candles`` helper.  When
candles are unavailable (connector lacks the endpoint, or no data yet) it
falls back to a uniform profile so callers always receive a usable result.
"""

import logging
import time
from decimal import Decimal
from typing import Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

_MS_PER_HOUR = 3_600_000
_HOURS_PER_DAY = 24


# ---------------------------------------------------------------------------
# Candles-provider protocol (HB-agnostic)
# ---------------------------------------------------------------------------

@runtime_checkable
class CandlesProvider(Protocol):
    """
    Minimal interface for supplying OHLCV candles to the forecaster.

    Each candle dict must contain at least:
        open_time  (int, epoch-ms)
        close_time (int, epoch-ms)
        volume     (str or numeric)
    """

    async def get_candles(
        self,
        trading_pair: str,
        interval: str,
        limit: int,
    ) -> list[dict]:
        ...


# ---------------------------------------------------------------------------
# HB concrete provider
# ---------------------------------------------------------------------------

class HBCandlesProvider:
    """
    Wraps a Hummingbot connector to satisfy the CandlesProvider protocol.

    HB connectors do not expose a ``get_candles`` REST endpoint directly; the
    canonical way is the ``CandlesFactory`` component.  For simplicity this
    implementation issues a REST request through the connector's REST
    assistant if available, or falls back to an empty list so the forecaster
    degrades gracefully to a uniform profile.

    In the short term, users can subclass / swap this with a CandlesFactory-
    backed implementation without changing the forecaster at all.
    """

    def __init__(self, connector, trading_pair: str, lookback_days: int = 7):
        self._connector = connector
        self._trading_pair = trading_pair
        self._lookback_days = lookback_days

    async def get_candles(
        self,
        trading_pair: str,
        interval: str = "1h",
        limit: int = 168,
    ) -> list[dict]:
        try:
            if hasattr(self._connector, "get_candles"):
                return await self._connector.get_candles(trading_pair, interval=interval, limit=limit)
            # Hyperliquid perpetual connectors expose candles via get_historical_candles
            if hasattr(self._connector, "get_historical_candles"):
                raw = await self._connector.get_historical_candles(trading_pair, interval, limit=limit)
                return raw or []
        except Exception as e:
            logger.debug("HBCandlesProvider: candles unavailable for %s: %s", trading_pair, e)
        return []


# ---------------------------------------------------------------------------
# Forecaster
# ---------------------------------------------------------------------------

def _uniform_profile(num_buckets: int, total: Decimal = Decimal("1")) -> list[Decimal]:
    if num_buckets <= 0:
        return []
    share = total / Decimal(num_buckets)
    return [share] * num_buckets


class HistoricalProfileForecaster:
    """
    Forecast intraday volume from a per-hour-of-day average built from
    recent candles.  Falls back to a uniform profile when data is absent.

    Args:
        provider: A ``CandlesProvider``-compatible object.
        lookback_days: Days of hourly candles to average over.
        candle_interval: Candle interval string (e.g. "1h").
    """

    def __init__(
        self,
        provider: Optional[CandlesProvider] = None,
        *,
        lookback_days: int = 7,
        candle_interval: str = "1h",
    ):
        self._provider = provider
        self.lookback_days = lookback_days
        self.candle_interval = candle_interval

    def set_provider(self, provider: CandlesProvider) -> None:
        self._provider = provider

    async def forecast(
        self,
        symbol: str,
        num_buckets: int,
        bucket_seconds: float,
        *,
        start_time: Optional[int] = None,
    ) -> list[Decimal]:
        if num_buckets <= 0:
            return []
        if bucket_seconds <= 0:
            raise ValueError("bucket_seconds must be positive")

        if start_time is None:
            start_time = int(time.time() * 1000)

        hourly_rate = await self._build_hourly_rate_profile(symbol)
        if hourly_rate is None:
            logger.info(
                "HistoricalProfileForecaster: no usable candle data for %s — "
                "falling back to uniform profile",
                symbol,
            )
            return _uniform_profile(num_buckets)

        bucket_ms = bucket_seconds * 1000
        forecast: list[Decimal] = []
        for j in range(num_buckets):
            midpoint_ms = start_time + int((j + 0.5) * bucket_ms)
            hour = (midpoint_ms // _MS_PER_HOUR) % _HOURS_PER_DAY
            rate = hourly_rate[hour]
            forecast.append(rate * Decimal(str(bucket_seconds)))

        if sum(forecast) <= 0:
            return _uniform_profile(num_buckets)

        return forecast

    async def _build_hourly_rate_profile(
        self, symbol: str
    ) -> Optional[list[Decimal]]:
        if self._provider is None:
            return None

        limit = self.lookback_days * _HOURS_PER_DAY
        try:
            candles = await self._provider.get_candles(
                symbol, interval=self.candle_interval, limit=limit
            )
        except Exception as e:
            logger.warning("HistoricalProfileForecaster: failed to fetch candles for %s: %s", symbol, e)
            return None

        if not candles:
            return None

        rate_sums = [Decimal("0")] * _HOURS_PER_DAY
        rate_counts = [0] * _HOURS_PER_DAY
        for c in candles:
            duration_s = (int(c["close_time"]) - int(c["open_time"])) / 1000
            if duration_s <= 0:
                continue
            hour = (int(c["open_time"]) // _MS_PER_HOUR) % _HOURS_PER_DAY
            rate_sums[hour] += Decimal(str(c["volume"])) / Decimal(str(duration_s))
            rate_counts[hour] += 1

        if not any(rate_counts):
            return None

        populated = [
            rate_sums[h] / Decimal(rate_counts[h])
            for h in range(_HOURS_PER_DAY)
            if rate_counts[h]
        ]
        global_mean = sum(populated) / Decimal(len(populated))
        return [
            rate_sums[h] / Decimal(rate_counts[h]) if rate_counts[h] else global_mean
            for h in range(_HOURS_PER_DAY)
        ]


__all__ = [
    "CandlesProvider",
    "HBCandlesProvider",
    "HistoricalProfileForecaster",
]

"""
ACScheduleExecutor — Almgren-Chriss optimal execution as a HB ExecutorBase.

The closed-form AC trajectory is computed once at startup (optionally in
volume-time using HistoricalProfileForecaster, resolved on the first tick).
Each slice is then submitted as a MARKET order on HB's timer.

Schedule math lives in _ac_math.py (HB-free, independently testable).
Volume forecasting uses opms.forecasting.HistoricalProfileForecaster, which
accepts any CandlesProvider — tested without a real HB runtime.
"""

import asyncio
import logging
import time
from decimal import Decimal
from typing import Literal, Optional, Union

from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.core.data_type.common import OrderType, PositionAction, PriceType, TradeType
from hummingbot.core.data_type.order_candidate import OrderCandidate, PerpetualOrderCandidate
from hummingbot.core.event.events import (
    BuyOrderCompletedEvent,
    MarketOrderFailureEvent,
    OrderCancelledEvent,
    SellOrderCompletedEvent,
)
from hummingbot.strategy.strategy_v2_base import StrategyV2Base
from hummingbot.strategy_v2.executors.data_types import ExecutorConfigBase
from hummingbot.strategy_v2.executors.executor_base import ExecutorBase
from hummingbot.strategy_v2.models.base import RunnableStatus
from hummingbot.strategy_v2.models.executors import CloseType, TrackedOrder

from opms.executors._ac_math import build_schedule

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class ACScheduleExecutorConfig(ExecutorConfigBase):
    type: Literal["ac_schedule_executor"] = "ac_schedule_executor"
    connector_name: str
    trading_pair: str
    side: TradeType
    total_amount_base: Decimal
    # Almgren-Chriss parameters
    duration_seconds: float = 3600.0
    num_intervals: int = 10
    risk_aversion: float = 1e-6
    volatility: float = 0.02
    eta: float = 0.01
    gamma: float = 0.0
    min_order_size: Optional[Decimal] = None
    # Volume-aware scheduling
    volume_forecast: bool = False
    # For perp connectors
    leverage: int = 1


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

class ACScheduleExecutor(ExecutorBase):
    """
    Submits AC-optimal slices as market orders on HB's scheduler.

    The schedule is (optionally) re-computed in volume time on the first tick
    using HistoricalProfileForecaster fed by HB connector candles.  With
    ``volume_forecast=False`` (default) it is a pure AC clock-time schedule.
    """

    def __init__(
        self,
        strategy: StrategyV2Base,
        config: ACScheduleExecutorConfig,
        update_interval: float = 1.0,
        max_retries: int = 10,
    ):
        super().__init__(
            strategy=strategy,
            connectors=[config.connector_name],
            config=config,
            update_interval=update_interval,
            max_retries=max_retries,
        )
        self.config: ACScheduleExecutorConfig = config

        # Build clock-time schedule immediately; may be replaced by volume-aware
        # schedule on first control_task if volume_forecast is enabled.
        self._schedule: list[Decimal] = build_schedule(
            total_quantity=config.total_amount_base,
            duration_seconds=config.duration_seconds,
            num_intervals=config.num_intervals,
            risk_aversion=config.risk_aversion,
            volatility=config.volatility,
            eta=config.eta,
            gamma=config.gamma,
            min_order_size=config.min_order_size,
        )
        self._interval_seconds: float = config.duration_seconds / len(self._schedule)
        self._schedule_resolved: bool = not config.volume_forecast

        # Execution state
        self._slice_idx: int = 0
        self._submitted: list[TrackedOrder] = []
        self._failed: list[TrackedOrder] = []
        self._cumulative_filled: Decimal = Decimal("0")
        self._cum_fees_quote: Decimal = Decimal("0")
        self._start_timestamp: Optional[float] = None
        self._last_slice_timestamp: Optional[float] = None

    # ------------------------------------------------------------------
    # Volume-aware schedule (resolved lazily on first tick)
    # ------------------------------------------------------------------

    async def _maybe_resolve_volume_schedule(self):
        """Replace the clock-time schedule with a volume-aware one (once)."""
        if self._schedule_resolved:
            return
        self._schedule_resolved = True  # don't retry on failure

        try:
            from opms.forecasting.historical_profile import HistoricalProfileForecaster, HBCandlesProvider

            provider = HBCandlesProvider(
                connector=self.connectors[self.config.connector_name],
                trading_pair=self.config.trading_pair,
            )
            forecaster = HistoricalProfileForecaster(provider=provider)
            n = len(self._schedule)
            volumes = await forecaster.forecast(
                symbol=self.config.trading_pair,
                num_buckets=n,
                bucket_seconds=self._interval_seconds,
                start_time=int(time.time() * 1000),
            )
            total_v = sum(float(v) for v in volumes)
            if not volumes or total_v <= 0:
                logger.warning("AC: volume forecast empty — keeping clock-time schedule")
                return

            # Cumulative volume fractions V_0=0 .. V_N=1
            cum = 0.0
            fractions = [0.0]
            for v in volumes:
                cum += float(v)
                fractions.append(cum / total_v)
            fractions[-1] = 1.0

            new_schedule = build_schedule(
                total_quantity=self.config.total_amount_base,
                duration_seconds=self.config.duration_seconds,
                num_intervals=self.config.num_intervals,
                risk_aversion=self.config.risk_aversion,
                volatility=self.config.volatility,
                eta=self.config.eta,
                gamma=self.config.gamma,
                min_order_size=self.config.min_order_size,
                cumulative_volume_fractions=fractions,
            )
            self._schedule = new_schedule
            self._interval_seconds = self.config.duration_seconds / len(self._schedule)
            logger.info(
                f"AC: volume-aware schedule applied — "
                f"{len(self._schedule)} slices, sizes={[float(s) for s in self._schedule]}"
            )
        except Exception as e:
            logger.warning(f"AC: volume forecast failed ({e}) — keeping clock-time schedule")

    # ------------------------------------------------------------------
    # ExecutorBase hooks
    # ------------------------------------------------------------------

    async def validate_sufficient_balance(self):
        mid = self.get_price(self.config.connector_name, self.config.trading_pair, PriceType.MidPrice)
        if self.is_perpetual_connector(self.config.connector_name):
            candidate = PerpetualOrderCandidate(
                trading_pair=self.config.trading_pair,
                is_maker=False,
                order_type=OrderType.MARKET,
                order_side=self.config.side,
                amount=self.config.total_amount_base,
                price=mid,
                leverage=Decimal(self.config.leverage),
            )
        else:
            candidate = OrderCandidate(
                trading_pair=self.config.trading_pair,
                is_maker=False,
                order_type=OrderType.MARKET,
                order_side=self.config.side,
                amount=self.config.total_amount_base,
                price=mid,
            )
        adjusted = self.adjust_order_candidates(self.config.connector_name, [candidate])
        if adjusted[0].amount == Decimal("0"):
            self.close_type = CloseType.INSUFFICIENT_BALANCE
            logger.error("ACScheduleExecutor: insufficient balance.")
            self.stop()

    async def control_task(self):
        if self.status == RunnableStatus.RUNNING:
            await self._tick()
        elif self.status == RunnableStatus.SHUTTING_DOWN:
            # early_stop() already chose EARLY_STOP / POSITION_HOLD; a run that
            # exhausted its schedule closes with the outcome-true type instead.
            self.close_execution_by(self.close_type or self._final_close_type())

    # ------------------------------------------------------------------
    # Main logic
    # ------------------------------------------------------------------

    async def _tick(self):
        # Resolve volume-aware schedule once (no-op if already done).
        await self._maybe_resolve_volume_schedule()

        now = self._strategy.current_timestamp
        if self._start_timestamp is None:
            self._start_timestamp = now

        if self._slice_idx >= len(self._schedule):
            # All slices submitted; wait for outstanding fills.
            if self._all_done():
                self.close_execution_by(self._final_close_type())
            return

        # Pace slices: submit when the current interval has elapsed.
        if self._last_slice_timestamp is not None:
            if now - self._last_slice_timestamp < self._interval_seconds:
                return  # not yet time for the next slice

        self._submit_next_slice()

    def _submit_next_slice(self):
        if self._slice_idx >= len(self._schedule):
            return

        is_last = self._slice_idx == len(self._schedule) - 1
        if is_last:
            # Final slice: trade exact remainder to avoid floating-point drift.
            amount = self.config.total_amount_base - self._cumulative_filled - sum(
                t.executed_amount_base for t in self._submitted if not t.is_done
            )
            if amount <= 0:
                self._slice_idx += 1
                return
        else:
            amount = self._schedule[self._slice_idx]

        order_id = self.place_order(
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            order_type=OrderType.MARKET,
            side=self.config.side,
            amount=amount,
            position_action=PositionAction.OPEN,
        )
        tracked = TrackedOrder(order_id=order_id)
        self._submitted.append(tracked)
        self._last_slice_timestamp = self._strategy.current_timestamp
        self._slice_idx += 1
        logger.info(
            f"AC slice {self._slice_idx}/{len(self._schedule)}: "
            f"{amount} {self.config.trading_pair} [{order_id}]"
        )

    def _all_done(self) -> bool:
        return (
            self._slice_idx >= len(self._schedule)
            and all(t.is_done for t in self._submitted)
        )

    # ------------------------------------------------------------------
    # HB event callbacks
    # ------------------------------------------------------------------

    def _find_tracked(self, order_id: str) -> Optional[TrackedOrder]:
        return next((t for t in self._submitted if t.order_id == order_id), None)

    def process_order_completed_event(
        self,
        event_tag: int,
        market: ConnectorBase,
        event: Union[BuyOrderCompletedEvent, SellOrderCompletedEvent],
    ):
        tracked = self._find_tracked(event.order_id)
        if tracked:
            executed = event.base_asset_amount
            self._cumulative_filled += executed
            logger.info(
                f"AC slice complete: {executed} filled "
                f"(total {self._cumulative_filled}/{self.config.total_amount_base})"
            )
            if self._all_done():
                self._status = RunnableStatus.SHUTTING_DOWN

    def process_order_failed_event(
        self,
        event_tag: int,
        market: ConnectorBase,
        event: MarketOrderFailureEvent,
    ):
        tracked = self._find_tracked(event.order_id)
        if tracked:
            self._failed.append(tracked)
            self._submitted.remove(tracked)
            # Rewind slice index so the slice is retried on the next tick.
            self._slice_idx = max(0, self._slice_idx - 1)
            self._current_retries += 1
            logger.warning(
                f"AC: slice failed [{event.order_id}], retry {self._current_retries}"
            )

    def process_order_canceled_event(
        self,
        event_tag: int,
        market: ConnectorBase,
        event: OrderCancelledEvent,
    ):
        # AC uses market orders; cancels should not occur in normal operation.
        logger.warning(f"AC: unexpected order cancel [{event.order_id}]")

    # ------------------------------------------------------------------
    # ExecutorBase required properties
    # ------------------------------------------------------------------

    def get_net_pnl_quote(self) -> Decimal:
        try:
            mid = self.get_price(self.config.connector_name, self.config.trading_pair, PriceType.MidPrice)
            avg = self.filled_amount_quote / self._cumulative_filled if self._cumulative_filled else mid
            sign = Decimal("1") if self.config.side == TradeType.BUY else Decimal("-1")
            return sign * (mid - avg) * self._cumulative_filled - self._cum_fees_quote
        except Exception:
            return Decimal("0")

    def get_net_pnl_pct(self) -> Decimal:
        faq = self.filled_amount_quote
        if faq == 0:
            return Decimal("0")
        return self.get_net_pnl_quote() / faq

    def get_cum_fees_quote(self) -> Decimal:
        return self._cum_fees_quote

    @property
    def filled_amount_quote(self) -> Decimal:
        try:
            mid = self.get_price(self.config.connector_name, self.config.trading_pair, PriceType.MidPrice)
            return self._cumulative_filled * mid
        except Exception:
            return Decimal("0")

    def get_custom_info(self) -> dict:
        return {
            "slice_idx": self._slice_idx,
            "total_slices": len(self._schedule),
            "cumulative_filled": float(self._cumulative_filled),
            "total_amount_base": float(self.config.total_amount_base),
            "volume_forecast": self.config.volume_forecast,
        }

    def early_stop(self, keep_position: bool = False):
        self.close_type = CloseType.POSITION_HOLD if keep_position else CloseType.EARLY_STOP
        self._status = RunnableStatus.SHUTTING_DOWN

    def close_execution_by(self, close_type: CloseType):
        self.close_type = close_type
        self.close_timestamp = self._strategy.current_timestamp
        self._status = RunnableStatus.TERMINATED
        self.stop()

    def _final_close_type(self) -> CloseType:
        """Close type must describe what happened, not merely that the run ended."""
        if self._current_retries > self._max_retries:
            return CloseType.FAILED
        if self._cumulative_filled >= self.config.total_amount_base:
            return CloseType.COMPLETED
        logger.warning(
            f"AC: closing under-filled ({self._cumulative_filled}/{self.config.total_amount_base}) — TIME_LIMIT"
        )
        return CloseType.TIME_LIMIT

    def evaluate_max_retries(self) -> None:
        if self._current_retries > self._max_retries:
            self.close_execution_by(CloseType.FAILED)


__all__ = ["ACScheduleExecutorConfig", "ACScheduleExecutor"]

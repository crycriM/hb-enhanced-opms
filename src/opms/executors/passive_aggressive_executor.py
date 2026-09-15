"""
PassiveAggressiveExecutor — HB ExecutorBase port of PA-V2.

Execution model (preserved from dex_executor/algorithms/passive_aggressive_v2.py):
  For each child order:
    - Place limit order at best bid (buy) / best ask (sell).
    - Every ``child_order_refresh_time`` seconds, cancel and re-place at fresh L1.
    - When ``child_order_time_limit`` seconds elapse without a full fill, cancel
      any resting order and fire an aggressive (market) order for the remainder.
  Once all children are terminal, the executor closes with an outcome-true
  CloseType: COMPLETED only when the full total executed, else TIME_LIMIT
  (or FAILED when retries are exhausted). A skipped child is never COMPLETED.

Key differences from PA-V2:
  - Order placement / cancellation via HB connector (strategy.buy/sell/cancel).
  - Fill events via HB's MarketEvent.OrderFilled / BuyOrderCompleted /
    SellOrderCompleted callbacks (synchronous, no asyncio.Event needed).
  - No OPMS service imports (database, event_bus) — analytics are emitted via
    the FillObserver that subscribes to the same HB events externally.
"""

import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum, auto
from typing import Literal, Optional, Union

from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.core.data_type.common import OrderType, PositionAction, PriceType, TradeType
from hummingbot.core.data_type.order_candidate import OrderCandidate, PerpetualOrderCandidate
from hummingbot.core.event.events import (
    BuyOrderCompletedEvent,
    MarketOrderFailureEvent,
    OrderCancelledEvent,
    OrderFilledEvent,
    SellOrderCompletedEvent,
)
from hummingbot.strategy.strategy_v2_base import StrategyV2Base
from hummingbot.strategy_v2.executors.data_types import ExecutorConfigBase
from hummingbot.strategy_v2.executors.executor_base import ExecutorBase
from hummingbot.strategy_v2.models.base import RunnableStatus
from hummingbot.strategy_v2.models.executors import CloseType, TrackedOrder

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class PassiveAggressiveExecutorConfig(ExecutorConfigBase):
    type: Literal["passive_aggressive_executor"] = "passive_aggressive_executor"
    connector_name: str
    trading_pair: str
    side: TradeType
    total_amount_base: Decimal
    child_order_quantity: Decimal
    child_order_time_limit: float = 60.0
    child_order_refresh_time: float = 20.0
    # For perp connectors: leverage multiplier applied when computing margin.
    leverage: int = 1
    # CLOSE makes every child reduce-only on venues that support it, so a
    # de-risk can shrink a position but never flip it.
    position_action: PositionAction = PositionAction.OPEN


# ---------------------------------------------------------------------------
# Internal child-order state machine
# ---------------------------------------------------------------------------

class _ChildStatus(Enum):
    IDLE = auto()       # no open order — will place limit on next tick
    ACTIVE = auto()     # limit order resting
    CANCELING = auto()  # cancel sent; waiting for OrderCancelled event
    AGGRESSIVE = auto() # market order placed after cycle expiry
    DONE = auto()       # child fully filled
    SKIPPED = auto()    # cycle expired without executing anything


_TERMINAL_STATUSES = (_ChildStatus.DONE, _ChildStatus.SKIPPED)


@dataclass
class _ChildSlot:
    target: Decimal
    status: _ChildStatus = _ChildStatus.IDLE
    tracked_order: Optional[TrackedOrder] = None
    filled: Decimal = Decimal("0")
    cycle_start: Optional[float] = None
    refresh_start: Optional[float] = None
    # Reason for last cancel, drives post-cancel action
    cancel_reason: str = ""  # "refresh" | "cycle_expired"


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

class PassiveAggressiveExecutor(ExecutorBase):
    """
    Passive-Aggressive V2 execution as a Hummingbot ExecutorBase.

    Submits child limit orders at L1 price, refreshes them on a timer, and
    falls back to aggressive (market) orders when the per-child cycle expires.
    """

    def __init__(
        self,
        strategy: StrategyV2Base,
        config: PassiveAggressiveExecutorConfig,
        update_interval: float = 0.5,
        max_retries: int = 10,
    ):
        super().__init__(
            strategy=strategy,
            connectors=[config.connector_name],
            config=config,
            update_interval=update_interval,
            max_retries=max_retries,
        )
        self.config: PassiveAggressiveExecutorConfig = config

        # Build child slots from total quantity / child size.
        self._children: list[_ChildSlot] = self._build_child_slots()
        self._child_idx: int = 0  # index of the currently active child
        self._cumulative_filled: Decimal = Decimal("0")
        self._cum_fees_quote: Decimal = Decimal("0")

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def _build_child_slots(self) -> list[_ChildSlot]:
        q = self.config.total_amount_base
        child_q = self.config.child_order_quantity
        if child_q <= 0:
            raise ValueError("child_order_quantity must be positive")
        n = int(q // child_q)
        remainder = q - n * child_q
        slots = [_ChildSlot(target=child_q) for _ in range(n)]
        if remainder > 0:
            # Fold the remainder into the last child rather than emitting a
            # sub-child-size order: venues reject orders under their minimum
            # notional (HL: $10), and a child sized at the minimum would leave
            # an unfillable dust child behind.
            if slots:
                slots[-1].target += remainder
            else:
                slots.append(_ChildSlot(target=remainder))
        return slots

    # ------------------------------------------------------------------
    # ExecutorBase hooks
    # ------------------------------------------------------------------

    async def validate_sufficient_balance(self):
        connector = self.connectors[self.config.connector_name]
        mid = self.get_price(self.config.connector_name, self.config.trading_pair, PriceType.MidPrice)
        if self.is_perpetual_connector(self.config.connector_name):
            candidate = PerpetualOrderCandidate(
                trading_pair=self.config.trading_pair,
                is_maker=True,
                order_type=OrderType.LIMIT,
                order_side=self.config.side,
                amount=self.config.total_amount_base,
                price=mid,
                leverage=Decimal(self.config.leverage),
                position_close=self.config.position_action == PositionAction.CLOSE,
            )
        else:
            candidate = OrderCandidate(
                trading_pair=self.config.trading_pair,
                is_maker=True,
                order_type=OrderType.LIMIT,
                order_side=self.config.side,
                amount=self.config.total_amount_base,
                price=mid,
            )
        adjusted = self.adjust_order_candidates(self.config.connector_name, [candidate])
        if adjusted[0].amount == Decimal("0"):
            self.close_type = CloseType.INSUFFICIENT_BALANCE
            logger.error("PassiveAggressiveExecutor: insufficient balance.")
            self.stop()

    async def control_task(self):
        if self.status == RunnableStatus.RUNNING:
            self._step()
        elif self.status == RunnableStatus.SHUTTING_DOWN:
            self._cancel_all_open()
            # early_stop() already chose EARLY_STOP / POSITION_HOLD; an
            # interrupted execution must not report itself as COMPLETED.
            self.close_execution_by(self.close_type or self._final_close_type())

    # ------------------------------------------------------------------
    # Main state machine
    # ------------------------------------------------------------------

    def _step(self):
        # Advance child index past terminal slots.
        while self._child_idx < len(self._children) and \
                self._children[self._child_idx].status in _TERMINAL_STATUSES:
            self._child_idx += 1

        if self._child_idx >= len(self._children):
            # All children terminal; only a fully executed run is COMPLETED.
            self.close_execution_by(self._final_close_type())
            return

        child = self._children[self._child_idx]
        now = self._strategy.current_timestamp

        # --- Initialise cycle clock on first visit ---
        if child.cycle_start is None:
            child.cycle_start = now

        cycle_elapsed = now - child.cycle_start

        if child.status == _ChildStatus.IDLE:
            if cycle_elapsed >= self.config.child_order_time_limit:
                # Cycle expired before a limit order was even placed; skip child.
                logger.warning(
                    f"PA child {self._child_idx}: cycle expired in IDLE — skipping "
                    f"(filled {child.filled}/{child.target})"
                )
                child.status = _ChildStatus.SKIPPED
            else:
                self._place_limit(child)

        elif child.status == _ChildStatus.ACTIVE:
            if cycle_elapsed >= self.config.child_order_time_limit:
                logger.info(
                    f"PA child {self._child_idx}: cycle expired — canceling limit, "
                    f"firing aggressive for remainder {child.target - child.filled}"
                )
                self._cancel_child(child, reason="cycle_expired")
            elif (child.refresh_start is not None and
                  now - child.refresh_start >= self.config.child_order_refresh_time):
                logger.debug(f"PA child {self._child_idx}: refresh timeout — re-placing")
                self._cancel_child(child, reason="refresh")

        elif child.status in (_ChildStatus.CANCELING, _ChildStatus.AGGRESSIVE):
            pass  # waiting for events

    # ------------------------------------------------------------------
    # Order helpers
    # ------------------------------------------------------------------

    def _place_limit(self, child: _ChildSlot):
        price_type = PriceType.BestBid if self.config.side == TradeType.BUY else PriceType.BestAsk
        price = self.get_price(self.config.connector_name, self.config.trading_pair, price_type)
        remaining = child.target - child.filled
        order_id = self.place_order(
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            order_type=OrderType.LIMIT,
            side=self.config.side,
            amount=remaining,
            price=price,
            position_action=self.config.position_action,
        )
        child.tracked_order = TrackedOrder(order_id=order_id)
        child.status = _ChildStatus.ACTIVE
        child.refresh_start = self._strategy.current_timestamp
        logger.info(
            f"PA child {self._child_idx}: limit {remaining} @ {price} "
            f"[{order_id}] (refresh in {self.config.child_order_refresh_time}s)"
        )

    def _place_aggressive(self, child: _ChildSlot):
        remaining = child.target - child.filled
        if remaining <= 0:
            child.status = _ChildStatus.DONE
            return
        # Market order: use NaN price so HB selects market price.
        order_id = self.place_order(
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            order_type=OrderType.MARKET,
            side=self.config.side,
            amount=remaining,
            position_action=self.config.position_action,
        )
        child.tracked_order = TrackedOrder(order_id=order_id)
        child.status = _ChildStatus.AGGRESSIVE
        logger.info(
            f"PA child {self._child_idx}: aggressive market {remaining} [{order_id}]"
        )

    def _cancel_child(self, child: _ChildSlot, reason: str):
        if child.tracked_order and child.tracked_order.order_id:
            self._strategy.cancel(
                self.config.connector_name,
                self.config.trading_pair,
                child.tracked_order.order_id,
            )
            child.cancel_reason = reason
            child.status = _ChildStatus.CANCELING

    def _cancel_all_open(self):
        for child in self._children:
            if child.status in (_ChildStatus.ACTIVE, _ChildStatus.CANCELING, _ChildStatus.AGGRESSIVE):
                if child.tracked_order and child.tracked_order.order_id:
                    try:
                        self._strategy.cancel(
                            self.config.connector_name,
                            self.config.trading_pair,
                            child.tracked_order.order_id,
                        )
                    except Exception as e:
                        logger.warning(f"PA: error canceling order during stop: {e}")

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
            f"PA: closing under-filled ({self._cumulative_filled}/{self.config.total_amount_base}) — TIME_LIMIT"
        )
        return CloseType.TIME_LIMIT

    def evaluate_max_retries(self) -> None:
        if self._current_retries > self._max_retries:
            self.close_execution_by(CloseType.FAILED)

    # ------------------------------------------------------------------
    # HB event callbacks (synchronous, called on HB event thread)
    # ------------------------------------------------------------------

    def _active_child(self) -> Optional[_ChildSlot]:
        if 0 <= self._child_idx < len(self._children):
            return self._children[self._child_idx]
        return None

    def _is_our_order(self, order_id: str) -> bool:
        child = self._active_child()
        return (child is not None and
                child.tracked_order is not None and
                child.tracked_order.order_id == order_id)

    def process_order_filled_event(
        self,
        event_tag: int,
        market: ConnectorBase,
        event: OrderFilledEvent,
    ):
        if not self._is_our_order(event.order_id):
            return
        child = self._active_child()
        incremental = event.trade_type == TradeType.BUY and event.amount or event.amount
        # Always use event.amount as the filled increment for this event.
        incremental = event.amount
        child.filled += incremental
        self._cumulative_filled += incremental
        fee = event.trade_fee.flat_fees[0].amount if event.trade_fee.flat_fees else Decimal("0")
        self._cum_fees_quote += fee
        logger.debug(
            f"PA child {self._child_idx}: fill +{incremental} "
            f"(child={child.filled}/{child.target}, total={self._cumulative_filled})"
        )

    def process_order_completed_event(
        self,
        event_tag: int,
        market: ConnectorBase,
        event: Union[BuyOrderCompletedEvent, SellOrderCompletedEvent],
    ):
        if not self._is_our_order(event.order_id):
            return
        child = self._active_child()
        # Ensure fill accounting is consistent with the completed event.
        # (process_order_filled_event may have already counted partial fills.)
        executed = event.base_asset_amount
        discrepancy = executed - child.filled
        if discrepancy > 0:
            child.filled = executed
            self._cumulative_filled += discrepancy

        child.status = _ChildStatus.DONE
        logger.info(
            f"PA child {self._child_idx}: completed (total filled {self._cumulative_filled})"
        )

    def process_order_canceled_event(
        self,
        event_tag: int,
        market: ConnectorBase,
        event: OrderCancelledEvent,
    ):
        if not self._is_our_order(event.order_id):
            return
        child = self._active_child()
        reason = child.cancel_reason
        child.tracked_order = None

        if reason == "cycle_expired":
            self._place_aggressive(child)
        else:
            # refresh — go back to IDLE, next control_task tick re-places
            child.status = _ChildStatus.IDLE

    def process_order_failed_event(
        self,
        event_tag: int,
        market: ConnectorBase,
        event: MarketOrderFailureEvent,
    ):
        if not self._is_our_order(event.order_id):
            return
        child = self._active_child()
        was_aggressive = child.status == _ChildStatus.AGGRESSIVE
        child.status = _ChildStatus.IDLE
        child.tracked_order = None
        self._current_retries += 1
        logger.warning(
            f"PA child {self._child_idx}: order failed [{event.order_id}], "
            f"retry {self._current_retries}/{self._max_retries}"
        )
        if (was_aggressive and self._status == RunnableStatus.RUNNING
                and self._current_retries <= self._max_retries):
            # A rejected market order must not become a silent skip; retry it
            # until evaluate_max_retries() closes the executor as FAILED.
            # Never after a stop — that would place an order post-termination.
            self._place_aggressive(child)

    # ------------------------------------------------------------------
    # ExecutorBase required properties
    # ------------------------------------------------------------------

    def get_net_pnl_quote(self) -> Decimal:
        """Unrealised mark-to-market PnL in quote; the FillObserver tracks real PnL."""
        try:
            mid = self.get_price(self.config.connector_name, self.config.trading_pair, PriceType.MidPrice)
            avg_fill = self.filled_amount_quote / self._cumulative_filled if self._cumulative_filled else mid
            sign = Decimal("1") if self.config.side == TradeType.BUY else Decimal("-1")
            return sign * (mid - avg_fill) * self._cumulative_filled - self._cum_fees_quote
        except Exception:
            return Decimal("0")

    def get_net_pnl_pct(self) -> Decimal:
        if self.filled_amount_quote == 0:
            return Decimal("0")
        return self.get_net_pnl_quote() / self.filled_amount_quote

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
            "child_idx": self._child_idx,
            "total_children": len(self._children),
            "cumulative_filled": float(self._cumulative_filled),
            "total_amount_base": float(self.config.total_amount_base),
        }

    def early_stop(self, keep_position: bool = False):
        self.close_type = CloseType.POSITION_HOLD if keep_position else CloseType.EARLY_STOP
        self._cancel_all_open()
        self._status = RunnableStatus.SHUTTING_DOWN


__all__ = ["PassiveAggressiveExecutorConfig", "PassiveAggressiveExecutor"]

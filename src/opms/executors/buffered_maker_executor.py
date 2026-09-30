"""Maker-only OrderExecutor that buffers and reprices every retry."""

from decimal import Decimal
from typing import Literal

from hummingbot.core.data_type.common import TradeType
from hummingbot.core.event.events import MarketOrderFailureEvent
from hummingbot.strategy_v2.executors.order_executor.data_types import OrderExecutorConfig
from hummingbot.strategy_v2.executors.order_executor.order_executor import OrderExecutor


class BufferedMakerExecutorConfig(OrderExecutorConfig):
    type: Literal["buffered_maker_executor"] = "buffered_maker_executor"
    maker_buffer_bps: Decimal = Decimal("8")
    retry_initial_s: float = 2.0
    retry_max_s: float = 16.0


class BufferedMakerExecutor(OrderExecutor):
    def __init__(self, strategy, config: BufferedMakerExecutorConfig, **kwargs):
        super().__init__(strategy, config, **kwargs)
        self.config: BufferedMakerExecutorConfig = config
        self._retry_at = 0.0

    def get_order_price(self) -> Decimal:
        touch = self.current_market_price
        if not touch.is_finite() or touch <= 0:  # NaN = no book; never price a maker order off it
            raise ValueError(f"no valid touch price ({touch}); refusing to price a maker order")
        edge = self.config.maker_buffer_bps / Decimal("10000")
        buffered = touch * (
            Decimal("1") - edge
            if self.config.side == TradeType.BUY
            else Decimal("1") + edge
        )
        if self.config.price is None:
            return buffered
        return (
            min(self.config.price, buffered)
            if self.config.side == TradeType.BUY
            else max(self.config.price, buffered)
        )

    def control_order(self):
        if self._order is None and self._strategy.current_timestamp < self._retry_at:
            return
        super().control_order()

    def get_custom_info(self):
        info = super().get_custom_info()
        info["order_price"] = self._order.price if self._order else None
        info["exchange_order_id"] = (self._order.order.exchange_order_id
                                     if self._order and self._order.order else None)
        return info

    def process_order_failed_event(self, _, market, event: MarketOrderFailureEvent):
        if self._order is None or event.order_id != self._order.order_id:
            return
        self._failed_orders.append(self._order)
        self._order = None
        self._current_retries += 1
        delay = min(
            self.config.retry_initial_s * 2 ** (self._current_retries - 1),
            self.config.retry_max_s,
        )
        self._retry_at = self._strategy.current_timestamp + delay
        self.logger().warning(
            "Buffered maker order failed %s; retry %s/%s in %.1fs",
            event.order_id, self._current_retries, self._max_retries, delay,
        )


__all__ = ["BufferedMakerExecutorConfig", "BufferedMakerExecutor"]

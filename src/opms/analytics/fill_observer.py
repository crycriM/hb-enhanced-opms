"""
FillObserver — subscribes to HB fill events and feeds mm_core analytics.

Registers ``SourceInfoEventForwarder`` listeners on a Hummingbot connector
for ``MarketEvent.OrderFilled``.  On each fill it:

  1. Records the fill in ``mm_core.pnl.PnLLedger`` (WAC position, spread
     capture, realized PnL).
  2. Feeds ``mm_core.markout.MarkoutTracker`` (adverse-selection diagnostic
     at configurable horizons).
  3. Accumulates per-fill slippage vs the mid at the moment of the fill.

Call ``update_mid(mid)`` from the controller's ``update_processed_data``
loop (every tick) so that MarkoutTracker can resolve pending horizons.

Lifecycle::

    observer = FillObserver(venue="hyperliquid_perpetual", symbol="SOL-PERP")
    observer.register(connector)   # call once after connector is ready
    # ... trading ...
    report = observer.explain(mid=current_mid)
    observer.unregister(connector) # on shutdown

The observer is HB-aware only for event registration; the analytics objects
(PnLLedger, MarkoutTracker) are pure mm_core and import no hummingbot code.
"""

import logging
import time
from collections import deque
from typing import Optional

from hummingbot.core.event.event_forwarder import SourceInfoEventForwarder
from hummingbot.core.event.events import MarketEvent, OrderFilledEvent
from hummingbot.core.data_type.common import TradeType

from mm_core.pnl import Fill, PnLLedger
from mm_core.markout import MarkoutTracker

logger = logging.getLogger(__name__)


class FillObserver:
    """
    Cross-cutting fill analytics attached to a single (venue, symbol) book.

    Designed to be created by the controller and shared between the
    PerpMMController and any active executors — a single source of truth for
    execution quality metrics on one trading pair.
    """

    MID_BUFFER_MAXLEN = 4096

    def __init__(
        self,
        venue: str,
        symbol: str,
        markout_horizons: tuple[float, ...] = (10.0, 30.0, 60.0),
        ledger: PnLLedger | None = None,
        markout: MarkoutTracker | None = None,
    ):
        self.venue = venue
        self.symbol = symbol

        self._ledger = ledger if ledger is not None else PnLLedger(venue=venue, symbol=symbol)
        self._markout = markout if markout is not None else MarkoutTracker(horizons=markout_horizons)

        # Slippage tracking: fill_price vs mid_at_fill (bps, signed)
        self._slippage_bps: list[float] = []
        self._last_mid: Optional[float] = None
        self._last_mid_ts: Optional[float] = None
        self._mid_buffer: deque[tuple[float, float]] = deque(maxlen=self.MID_BUFFER_MAXLEN)
        self.last_fill_ts: Optional[float] = None

        # HB event forwarder — registered against the connector
        self._fill_forwarder = SourceInfoEventForwarder(self._on_fill_event)
        self._connector = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def register(self, connector) -> None:
        """Subscribe to fill events on the given HB connector."""
        if self._connector is not None:
            self.unregister(self._connector)
        self._connector = connector
        connector.add_listener(MarketEvent.OrderFilled, self._fill_forwarder)
        logger.info(
            "FillObserver registered on %s for %s/%s",
            connector.name if hasattr(connector, "name") else repr(connector),
            self.venue,
            self.symbol,
        )

    def unregister(self, connector) -> None:
        """Remove fill event listener from the connector."""
        try:
            connector.remove_listener(MarketEvent.OrderFilled, self._fill_forwarder)
        except Exception as e:
            logger.debug("FillObserver.unregister: %s", e)
        if self._connector is connector:
            self._connector = None

    # ------------------------------------------------------------------
    # Mid-price feed (called each controller tick)
    # ------------------------------------------------------------------

    def update_mid(self, mid: float, ts: Optional[float] = None) -> None:
        """
        Advance the MarkoutTracker with a fresh mid price.

        Call from the controller's ``update_processed_data`` loop so that
        pending markout horizons resolve correctly.
        """
        if ts is None:
            ts = time.time()
        self._last_mid = mid
        self._last_mid_ts = ts
        self._mid_buffer.append((ts, mid))
        self._markout.on_mid(ts, mid)

    def _mid_at(self, ts: float) -> Optional[float]:
        """Most recent observed mid at or before the fill's event time."""
        for mid_ts, mid in reversed(self._mid_buffer):
            if mid_ts <= ts:
                return mid
        return self._last_mid  # fill older than the buffer; keep it, disclose staleness

    # ------------------------------------------------------------------
    # HB event callback
    # ------------------------------------------------------------------

    @staticmethod
    def _fee_in_quote_token(event: OrderFilledEvent) -> float:
        """Sum percent and all flat fees in quote terms; never lose a fill."""
        try:
            return float(event.trade_fee.fee_amount_in_token(
                trading_pair=event.trading_pair, price=event.price,
                order_amount=event.amount,
                token=event.trading_pair.split("-")[1],
            ))
        except Exception as exc:
            # A missing rate oracle must not silently erase real fill economics.
            logger.warning(
                "FillObserver: fee conversion failed for %s order %s (%s); "
                "recording fill with fee=0",
                event.trading_pair,
                event.order_id,
                exc,
            )
            return 0.0

    def _on_fill_event(
        self,
        event_tag: int,
        market,
        event: OrderFilledEvent,
    ) -> None:
        """Process one HB OrderFilledEvent synchronously."""
        # Filter to our symbol (connector may carry multiple trading pairs).
        if event.trading_pair != self.symbol:
            return

        ts = float(event.timestamp)
        self.last_fill_ts = time.time()  # receipt time drives position reconciliation
        side = "buy" if event.trade_type == TradeType.BUY else "sell"
        price = float(event.price)
        size = float(event.amount)
        fee = self._fee_in_quote_token(event)

        # Mid at fill — the mid in force at the event's own timestamp.
        mid_at_fill = self._mid_at(ts)

        fill = Fill(
            ts=ts,
            side=side,
            price=price,
            size=size,
            fee=fee,
            mid_at_fill=mid_at_fill,
            label=f"order_{event.order_id[:8]}" if event.order_id else "",
        )
        self._ledger.on_fill(fill)
        self._markout.on_fill(ts, side, price, size)

        # Slippage: (fill_price - mid_at_fill) × direction / mid, in bps.
        if mid_at_fill and mid_at_fill > 0:
            sign = 1.0 if side == "buy" else -1.0
            # A buy fill above mid is positive slippage (adverse for buyer).
            slippage_bps = sign * (price - mid_at_fill) / mid_at_fill * 1e4
            self._slippage_bps.append(slippage_bps)

        logger.debug(
            "FillObserver: %s fill %s @ %s (fee=%s, mid=%s)",
            side.upper(),
            size,
            price,
            fee,
            mid_at_fill,
        )

    # ------------------------------------------------------------------
    # Analytics surface
    # ------------------------------------------------------------------

    def explain(self, mid: Optional[float] = None) -> dict:
        """
        Return a full PnL breakdown identical to what the OPMS PnL endpoint
        served.  Accepts an optional current mid override (useful when called
        outside the controller's tick loop).
        """
        if mid is None:
            mid = self._last_mid or 0.0
        breakdown = self._ledger.explain(mid=mid)
        return breakdown.to_dict()

    def markout_stats(self) -> dict:
        """
        Per-horizon average markout in bps.

        Negative markout means your fills are, on average, toxic at that
        horizon — widen spreads or reduce quoting.
        """
        return self._markout.stats()

    def slippage_stats(self) -> dict:
        """
        Summary of fill slippage vs mid (in bps, signed).

        Positive = adverse (buy above mid / sell below mid).
        """
        if not self._slippage_bps:
            return {"n": 0, "mean_bps": None, "max_bps": None}
        n = len(self._slippage_bps)
        return {
            "n": n,
            "mean_bps": sum(self._slippage_bps) / n,
            "max_bps": max(self._slippage_bps),
        }

    @property
    def position(self) -> float:
        return self._ledger.position

    @property
    def realized_pnl(self) -> float:
        return self._ledger.realized_pnl

    @property
    def n_fills(self) -> int:
        return len(self._ledger.fills)


__all__ = ["FillObserver"]

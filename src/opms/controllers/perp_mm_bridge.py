"""HB-free bridge between Keeper (mm-core brain) and a Hummingbot controller.

Keeper (perp_bot.keeper) is reused unmodified as the decision engine: this
module only adapts its OpmsClient-shaped I/O boundary to something that can
be driven from inside a Hummingbot control_task, without importing
hummingbot at all. That keeps this file testable without a Hummingbot
runtime and keeps "one module speaks Hummingbot" (perp_mm_controller.py).
"""

from dataclasses import dataclass

from mm_core.contracts import ExecIntent

from perp_bot.opms_client import Position


@dataclass
class OrderSpec:
    """A venue-agnostic instruction derived from the keeper's last ExecIntent."""
    cancel_all: bool
    side: str | None = None            # "buy" | "sell" | None (no new order)
    price: float | None = None
    amount: float = 0.0
    reduce_only: bool = False
    urgency: str = "normal"


class InProcessClient:
    """Duck-types perp_bot.opms_client.OpmsClient's callback/query surface,
    but keeps everything in-process instead of doing HTTP/websocket I/O —
    the controller feeds it snapshots/positions directly each HB cycle."""

    def __init__(self):
        self._positions: dict[str, Position] = {}
        self.last_intent: ExecIntent | None = None
        self._on_snapshot_cb = None
        self._on_fill_cb = None
        self._on_error_cb = None

    def on_snapshot(self, cb):
        self._on_snapshot_cb = cb

    def on_fill(self, cb):
        self._on_fill_cb = cb

    def on_error(self, cb):
        self._on_error_cb = cb

    def set_positions(self, positions: dict[str, Position]) -> None:
        self._positions = positions

    async def start(self):
        return self

    async def stop(self):
        pass

    async def send_intent(self, intent: ExecIntent) -> dict:
        self.last_intent = intent
        return {}

    async def get_positions(self) -> dict[str, Position]:
        return self._positions

    async def resnapshot_positions(self) -> dict[str, Position]:
        return self._positions


def intent_to_order_specs(intent: ExecIntent | None) -> list[OrderSpec]:
    """Map the keeper's ExecIntent onto plain order instructions.

    Every non-QUOTE/WIDEN decision means "cancel resting quotes"; DE_RISK and
    EMERGENCY_EXIT additionally need a reduce-only order back to flat.
    """
    if intent is None:
        return []

    if intent.quote is not None:
        specs = []
        if intent.quote.bid_price is not None:
            specs.append(OrderSpec(cancel_all=True, side="buy",
                                    price=intent.quote.bid_price,
                                    amount=intent.quote.bid_size,
                                    urgency=intent.urgency))
        if intent.quote.ask_price is not None:
            specs.append(OrderSpec(cancel_all=len(specs) == 0, side="sell",
                                    price=intent.quote.ask_price,
                                    amount=intent.quote.ask_size,
                                    urgency=intent.urgency))
        return specs or [OrderSpec(cancel_all=True)]

    current = intent.current_inventory or 0.0
    target = intent.target_inventory
    gap = target - current
    if abs(gap) < 1e-12:
        return [OrderSpec(cancel_all=True)]

    return [OrderSpec(
        cancel_all=True,
        side="buy" if gap > 0 else "sell",
        price=None,  # market/executor-managed — de-risk and emergency exits chase, not rest
        amount=abs(gap),
        reduce_only=True,
        urgency=intent.urgency,
    )]

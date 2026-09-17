"""HB-free bridge between Keeper (mm-core brain) and a Hummingbot controller.

Keeper (perp_bot.keeper) is reused unmodified as the decision engine: this
module only adapts its OpmsClient-shaped I/O boundary to something that can
be driven from inside a Hummingbot control_task, without importing
hummingbot at all. That keeps this file testable without a Hummingbot
runtime and keeps "one module speaks Hummingbot" (perp_mm_controller.py).
"""

from dataclasses import dataclass
import time

from mm_core.contracts import ExecIntent, PortfolioExecIntent
from mm_core.portfolio_execution import AdmissionStatus, PortfolioIntentGate

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


@dataclass
class ExecutionRequest:
    """Routing request for the non-quoting execution path."""
    side: str                          # "buy" | "sell"
    amount: float
    urgency: str                       # "passive" | "normal" | "immediate" | "emergency"
    reduce_only: bool = True


class InProcessClient:
    """Duck-types perp_bot.opms_client.OpmsClient's callback/query surface,
    but keeps everything in-process instead of doing HTTP/websocket I/O —
    the controller feeds it snapshots/positions directly each HB cycle."""

    def __init__(self):
        self._positions: dict[str, Position] = {}
        self.last_intent: ExecIntent | None = None
        self.last_portfolio_intent: PortfolioExecIntent | None = None
        self._portfolio_gate = PortfolioIntentGate()
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

    async def send_intent(self, intent: ExecIntent | PortfolioExecIntent) -> dict:
        if isinstance(intent, PortfolioExecIntent):
            return await self.send_portfolio_intent(intent)
        self.last_intent = intent
        return {}

    async def send_portfolio_intent(self, intent: PortfolioExecIntent) -> dict:
        """Admit grouped generations without pretending HB can execute them.

        The current controller owns one configured account, while a grouped
        intent may contain several accounts.  We still apply the shared
        schema/generation gate so shadow tooling observes the same acceptance
        semantics as native OPMS; live child placement remains disabled until a
        portfolio coordinator is wired around the controller.
        """
        admission = self._portfolio_gate.admit(intent, now=time.time())
        if admission.status is AdmissionStatus.REJECTED:
            return {
                "status": "rejected",
                "portfolio_id": intent.portfolio_id,
                "generation": intent.generation,
                "errors": list(admission.errors),
            }
        if admission.status is AdmissionStatus.DUPLICATE:
            previous = admission.previous
            return {
                "status": "duplicate",
                "portfolio_id": intent.portfolio_id,
                "generation": intent.generation,
                "strategy_ids": list(previous.strategy_ids if previous else ()),
            }
        self.last_portfolio_intent = intent
        self._portfolio_gate.commit(admission, strategy_ids=())
        return {
            "status": "accepted",
            "execution": "shadow_only",
            "portfolio_id": intent.portfolio_id,
            "generation": intent.generation,
            "strategy_ids": [],
        }

    async def get_positions(self) -> dict[str, Position]:
        return self._positions

    async def resnapshot_positions(self) -> dict[str, Position]:
        return self._positions


def intent_is_quoting(intent: ExecIntent | None) -> bool:
    return intent is not None and intent.quote is not None


def intent_to_execution_request(intent: ExecIntent | None) -> ExecutionRequest | None:
    if intent is None:
        return None
    if intent.quote is not None:
        return None
    current = intent.current_inventory or 0.0
    target = intent.target_inventory
    gap = target - current
    if abs(gap) < 1e-12:
        return None
    return ExecutionRequest(
        side="buy" if gap > 0 else "sell",
        amount=abs(gap),
        urgency=intent.urgency,
        reduce_only=True,
    )


def intent_to_order_specs(intent: ExecIntent | None) -> list[OrderSpec]:
    """Map the keeper's ExecIntent onto plain order instructions.

    Only used by the quoting path (intent.quote present).  Non-quoting
    intents are routed through intent_to_execution_request instead.
    """
    if intent is None:
        return []

    if intent.quote is not None:
        specs = []
        current = intent.current_inventory or 0.0
        if intent.quote.bid_price is not None and intent.quote.bid_size > 0:
            specs.append(OrderSpec(cancel_all=True, side="buy",
                                    price=intent.quote.bid_price,
                                    amount=intent.quote.bid_size,
                                    reduce_only=current < 0,
                                    urgency=intent.urgency))
        if intent.quote.ask_price is not None and intent.quote.ask_size > 0:
            specs.append(OrderSpec(cancel_all=len(specs) == 0, side="sell",
                                    price=intent.quote.ask_price,
                                    amount=intent.quote.ask_size,
                                    reduce_only=current > 0,
                                    urgency=intent.urgency))
        return specs or [OrderSpec(cancel_all=True)]

    return [OrderSpec(cancel_all=True)]

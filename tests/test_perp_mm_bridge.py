"""Parity gate: the InProcessClient-driven Keeper (the path PerpMMController
drives every HB cycle) must produce byte-identical decision records to a
bare Keeper (the existing oracle) fed the same market data — this is the
"diff_decision_logs.py must show only explained deltas" gate from the
migration plan, run without Docker/hummingbot in the loop.
"""

import json
import time

import pytest

from mm_core.inventory import Caps
from mm_core.contracts import (
    LegQuoteSpec,
    PortfolioDecisionAudit,
    PortfolioExecIntent,
)

from perp_bot.config import PerpPairConfig
from perp_bot.keeper import Keeper
from perp_bot.opms_client import Position

from opms.controllers.generic.perp_mm_bridge import (
    ExecutionRequest,
    InProcessClient,
    OrderSpec,
    intent_is_quoting,
    intent_to_execution_request,
    intent_to_order_specs,
)


class _StaticPositionClient:
    """Minimal OpmsClient stand-in independent of InProcessClient, so the
    parity test isn't comparing InProcessClient against itself."""

    def __init__(self, position: Position):
        self._position = position

    def on_snapshot(self, cb):
        self._on_snapshot_cb = cb

    def on_fill(self, cb):
        pass

    def on_error(self, cb):
        pass

    async def get_positions(self):
        return {self._position.coin: self._position}

    async def send_intent(self, intent):
        pass


def snapshots(n=20, mid=50000.0, drift=0.0, funding_rate=None):
    t0 = time.time()
    return [
        {"ts": t0 + i, "mid": mid + i * drift, "funding_rate": funding_rate}
        for i in range(n)
    ]


def make_config():
    return PerpPairConfig(coin="BTC", gamma=1.0, kappa=0.5, exchange="hyperliquid",
                           caps=Caps(max_position=10.0, critical_position=20.0))


@pytest.mark.asyncio
async def test_bridge_matches_oracle_keeper(tmp_path):
    ticks = snapshots(n=15, drift=25.0)
    position = Position(
        coin="BTC",
        position=0.0,
        equity=1000.0,
        margin_available=1000.0,
    )

    oracle_log = tmp_path / "oracle.jsonl"
    oracle_client = _StaticPositionClient(position)
    oracle_keeper = Keeper(client=oracle_client, config=make_config(), tick_s=0.01,
                            decision_log_path=str(oracle_log))
    oracle_client.on_snapshot(oracle_keeper._on_snapshot)
    for snap in ticks:
        await oracle_keeper._on_snapshot(snap)
        await oracle_keeper._tick()

    bridge_log = tmp_path / "bridge.jsonl"
    client = InProcessClient()
    bridge_keeper = Keeper(client=client, config=make_config(), tick_s=0.01,
                            decision_log_path=str(bridge_log))
    client.on_snapshot(bridge_keeper._on_snapshot)
    client.on_fill(bridge_keeper._on_fill)
    client.on_error(bridge_keeper._on_error)
    for snap in ticks:
        client.set_positions({"BTC": position})
        await client._on_snapshot_cb(snap)
        await bridge_keeper._tick()

    oracle_records = [json.loads(line) for line in oracle_log.read_text().splitlines()]
    bridge_records = [json.loads(line) for line in bridge_log.read_text().splitlines()]

    assert len(oracle_records) == len(bridge_records) == len(ticks)
    for oracle_rec, bridge_rec in zip(oracle_records, bridge_records):
        oracle_rec["source"] = bridge_rec["source"] = None  # oracle=live, bridge=live too; not a parity signal
        assert oracle_rec == bridge_rec


def test_intent_to_order_specs_quote():
    from mm_core.contracts import ExecIntent, QuoteSpec

    intent = ExecIntent(venue="hyperliquid", coin="BTC", target_inventory=0.0,
                         current_inventory=0.0,
                         quote=QuoteSpec(bid_price=99.0, ask_price=101.0, bid_size=1.0, ask_size=1.0))
    specs = intent_to_order_specs(intent)
    assert specs == [
        OrderSpec(cancel_all=True, side="buy", price=99.0, amount=1.0, urgency="normal"),
        OrderSpec(cancel_all=False, side="sell", price=101.0, amount=1.0, urgency="normal"),
    ]


@pytest.mark.asyncio
async def test_in_process_client_admits_grouped_generation_and_dedupes():
    intent = PortfolioExecIntent(
        schema_version=2,
        venue="hyperliquid",
        coin="BTC",
        portfolio_id="btc-mirror",
        generation=1,
        as_of_ts=time.time() - 1.0,
        expires_at=time.time() + 10.0,
        quotes=(
            LegQuoteSpec("long", "buy", 99.0, 1.0, False),
            LegQuoteSpec("short", "sell", 101.0, 1.0, False),
        ),
        target_net_inventory_base=0.0,
        client_id="btc-mirror-1",
        cancel_previous=True,
        audit=PortfolioDecisionAudit(
            ts=time.time() - 1.0,
            net_base=0.0,
            gross_notional_usd=200.0,
        ),
    )
    client = InProcessClient()

    accepted = await client.send_intent(intent)
    duplicate = await client.send_intent(intent)

    assert accepted["status"] == "accepted"
    assert duplicate["status"] == "duplicate"
    assert client.last_portfolio_intent == intent


@pytest.mark.asyncio
async def test_in_process_client_rejects_stale_grouped_generation():
    now = time.time()
    base = dict(
        schema_version=2,
        venue="hyperliquid",
        coin="BTC",
        portfolio_id="btc-mirror",
        as_of_ts=now - 1.0,
        expires_at=now + 10.0,
        quotes=(LegQuoteSpec("long", "buy", 99.0, 1.0, False),),
        target_net_inventory_base=0.0,
        cancel_previous=True,
        audit=PortfolioDecisionAudit(ts=now - 1.0, net_base=0.0, gross_notional_usd=100.0),
    )
    client = InProcessClient()
    await client.send_intent(PortfolioExecIntent(generation=2, client_id="new", **base))

    stale = await client.send_intent(
        PortfolioExecIntent(generation=1, client_id="old", **base)
    )

    assert stale["status"] == "rejected"
    assert "generation_stale" in stale["errors"]


@pytest.mark.parametrize(
    ("position", "expected_buy_reduce_only", "expected_sell_reduce_only"),
    [
        (0.0769, False, True),
        (-2.0, True, False),
        (0.0, False, False),
    ],
)
def test_inventory_reducing_quote_side_is_reduce_only(
    position, expected_buy_reduce_only, expected_sell_reduce_only
):
    """Venue reduce-only is the last line of defence against stale quote
    overlap flipping a position through flat."""
    from mm_core.contracts import ExecIntent, QuoteSpec

    intent = ExecIntent(
        venue="hyperliquid", coin="BTC", target_inventory=0.4,
        current_inventory=position,
        quote=QuoteSpec(bid_price=99.0, ask_price=101.0, bid_size=1.0, ask_size=1.0),
    )
    specs = intent_to_order_specs(intent)

    assert specs[0].reduce_only is expected_buy_reduce_only
    assert specs[1].reduce_only is expected_sell_reduce_only


def test_zero_sized_quote_side_is_not_routed():
    from mm_core.contracts import ExecIntent, QuoteSpec

    intent = ExecIntent(
        venue="hyperliquid", coin="BTC", target_inventory=0.4,
        current_inventory=0.0,
        quote=QuoteSpec(bid_price=99.0, ask_price=None, bid_size=0.1, ask_size=0.0),
    )

    assert intent_to_order_specs(intent) == [
        OrderSpec(cancel_all=True, side="buy", price=99.0, amount=0.1, urgency="normal"),
    ]


def test_intent_to_order_specs_de_risk():
    from mm_core.contracts import ExecIntent

    # Non-quoting intents no longer produce OrderSpec with side/amount —
    # they are routed through intent_to_execution_request instead.
    intent = ExecIntent(venue="hyperliquid", coin="BTC", target_inventory=0.0,
                         current_inventory=5.0, quote=None, urgency="normal",
                         strategy_hint="passive_aggressive")
    specs = intent_to_order_specs(intent)
    assert specs == [OrderSpec(cancel_all=True)]


def test_intent_to_order_specs_none():
    assert intent_to_order_specs(None) == []


def test_intent_is_quoting_true():
    from mm_core.contracts import ExecIntent, QuoteSpec

    intent = ExecIntent(venue="hl", coin="BTC", target_inventory=0.0,
                         quote=QuoteSpec(bid_price=99.0, ask_price=101.0, bid_size=1.0, ask_size=1.0))
    assert intent_is_quoting(intent) is True


def test_intent_is_quoting_false():
    from mm_core.contracts import ExecIntent

    intent = ExecIntent(venue="hl", coin="BTC", target_inventory=0.0,
                         current_inventory=5.0, quote=None, urgency="passive")
    assert intent_is_quoting(intent) is False


def test_intent_is_quoting_none():
    assert intent_is_quoting(None) is False


def test_intent_to_execution_request_sell():
    from mm_core.contracts import ExecIntent

    intent = ExecIntent(venue="hl", coin="BTC", target_inventory=0.0,
                         current_inventory=5.0, quote=None, urgency="passive")
    req = intent_to_execution_request(intent)
    assert isinstance(req, ExecutionRequest)
    assert req.side == "sell"
    assert req.amount == pytest.approx(5.0)
    assert req.urgency == "passive"
    assert req.reduce_only is True


def test_intent_to_execution_request_buy():
    from mm_core.contracts import ExecIntent

    intent = ExecIntent(venue="hl", coin="BTC", target_inventory=10.0,
                         current_inventory=5.0, quote=None, urgency="normal")
    req = intent_to_execution_request(intent)
    assert req.side == "buy"
    assert req.amount == pytest.approx(5.0)


def test_intent_to_execution_request_zero_gap():
    from mm_core.contracts import ExecIntent

    intent = ExecIntent(venue="hl", coin="BTC", target_inventory=0.0,
                         current_inventory=0.0, quote=None, urgency="normal")
    assert intent_to_execution_request(intent) is None


def test_intent_to_execution_request_quote_present():
    from mm_core.contracts import ExecIntent, QuoteSpec

    intent = ExecIntent(venue="hl", coin="BTC", target_inventory=0.0,
                         quote=QuoteSpec(bid_price=99.0, ask_price=101.0, bid_size=1.0, ask_size=1.0))
    assert intent_to_execution_request(intent) is None


def test_flat_position_retires_stale_non_quoting_intent():
    from mm_core.contracts import ExecIntent

    client = InProcessClient()
    intent = ExecIntent(
        venue="hyperliquid", coin="ETH", target_inventory=0.0,
        current_inventory=0.4, quote=None, urgency="immediate",
    )
    client.last_intent = intent

    client.set_positions({
        "ETH": Position(coin="ETH", position=0.4, equity=700.0),
    })
    assert client.last_intent is intent

    client.set_positions({
        "ETH": Position(coin="ETH", position=0.0, equity=700.0),
    })
    assert client.last_intent is None

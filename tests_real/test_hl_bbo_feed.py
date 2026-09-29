"""HL's public l2Book is throttled to ~5.5 s, so Hummingbot's book (and every mid/touch
read from it) is up to 5.5 s stale. The patch adds the block-cadence `bbo` channel on
top of that depth. Real connector class, fake connector/ws; no network, no orders."""

import asyncio
import math
from types import SimpleNamespace

import pytest

ds_module = pytest.importorskip(
    "hummingbot.connector.derivative.hyperliquid_perpetual.hyperliquid_perpetual_api_order_book_data_source"
)
from hummingbot.core.data_type.order_book_message import OrderBookMessageType  # noqa: E402

from opms.connectors import hl_bbo  # noqa: E402
from test_perp_mm_controller_real_hb import _MarketDataWithBookMetrics, _controller  # noqa: E402
from perp_bot.hl_lob import BookOverlay  # noqa: E402

DS = ds_module.HyperliquidPerpetualAPIOrderBookDataSource


class _Ws:
    def __init__(self):
        self.sent = []

    async def send(self, request):
        self.sent.append(request.payload["subscription"])


def _source():
    async def symbol(trading_pair):
        return "ENA"

    async def pair(symbol):
        return "ENA-USD"

    connector = SimpleNamespace(exchange_symbol_associated_to_pair=symbol,
                                trading_pair_associated_to_exchange_symbol=pair)
    hl_bbo.apply()
    return DS(["ENA-USD"], connector, None)


def _l2(time, bid, ask):
    lvl = lambda px, sz: {"px": str(px), "sz": str(sz), "n": 1}
    return {"channel": "l2Book", "data": {"coin": "ENA", "time": time, "levels": [
        [lvl(bid, 100), lvl(round(bid - 0.00001, 5), 50)],
        [lvl(ask, 200), lvl(round(ask + 0.00001, 5), 60)]]}}


def _bbo(time, bid, ask):
    return {"channel": "bbo", "data": {"coin": "ENA", "time": time, "bbo": [
        {"px": str(bid), "sz": "7", "n": 1}, {"px": str(ask), "sz": "8", "n": 1}]}}


def _parse(source, *raws):
    queue = asyncio.Queue()
    for raw in raws:
        asyncio.run(source._parse_order_book_snapshot_message(raw, queue))
    return [queue.get_nowait() for _ in range(queue.qsize())]


def test_bbo_routes_like_a_snapshot_and_other_channels_are_unchanged():
    source = _source()
    assert source._channel_originating_message(_bbo(1, 1, 2)) == source._snapshot_messages_queue_key
    assert source._channel_originating_message(_l2(1, 1, 2)) == source._snapshot_messages_queue_key
    assert source._channel_originating_message({"channel": "trades", "data": []}) == source._trade_messages_queue_key
    assert source._channel_originating_message({"channel": "activeAssetCtx"}) == source._funding_info_messages_queue_key


def test_subscribes_to_bbo_next_to_the_original_channels_once():
    source, ws = _source(), _Ws()
    hl_bbo.apply()  # idempotent
    asyncio.run(source._subscribe_channels(ws))
    assert {"type": "bbo", "coin": "ENA"} in ws.sent
    assert {"type": "l2Book", "coin": "ENA"} in ws.sent
    assert sum(1 for s in ws.sent if s["type"] == "bbo") == 1


def test_dynamic_subscription_adds_bbo():
    source, ws = _source(), _Ws()
    source._ws_assistant = ws
    assert asyncio.run(source.subscribe_to_trading_pair("ENA-USD"))
    assert {"type": "bbo", "coin": "ENA"} in ws.sent


def test_bbo_puts_the_touch_on_top_of_the_latest_l2_depth():
    source = _source()
    first, second = _parse(source, _l2(1000, 0.25676, 0.25684), _bbo(1200, 0.25679, 0.25685))
    assert first.type is second.type is OrderBookMessageType.SNAPSHOT
    assert second.trading_pair == "ENA-USD"
    assert second.bids[0].price == pytest.approx(0.25679) and second.bids[0].amount == 7
    assert second.asks[0].price == pytest.approx(0.25685) and second.asks[0].amount == 8
    assert [b.price for b in second.bids[1:]] == pytest.approx([0.25676, 0.25675])


def test_bbo_before_any_l2_emits_nothing():
    assert _parse(_source(), _bbo(1200, 0.25679, 0.25685)) == []


def test_feed_health_reports_a_silent_bbo_stream():
    source = _source()
    connector = SimpleNamespace(order_book_tracker=SimpleNamespace(data_source=source))
    assert hl_bbo.feed_healthy(connector, "ENA-USD")  # nothing seen yet: age gate covers it
    _parse(source, _bbo(900, 0.25676, 0.25684), _l2(1000, 0.25676, 0.25684))
    assert hl_bbo.feed_healthy(connector, "ENA-USD")
    _parse(source, *[_l2(7000 + i * 5500, 0.2570, 0.2571) for i in range(3)])  # market moved, no bbo
    assert not hl_bbo.feed_healthy(connector, "ENA-USD")


def test_controller_treats_an_unhealthy_bbo_feed_as_stale(monkeypatch):
    import opms.controllers.generic.perp_mm_controller as controller_module

    md = _MarketDataWithBookMetrics(last_diff=99.9)
    overlay = BookOverlay()
    md.connector.order_book_tracker.data_source = SimpleNamespace(_opms_overlays={"ETH-USD": overlay})
    ctrl = _controller(md)
    monkeypatch.setattr(controller_module.time, "perf_counter", lambda: 100.0)
    overlay.on_bbo(900, (1.0, 1.0), (1.1, 1.0))
    overlay.on_l2(1000, ((1.0, 1.0),), ((1.1, 1.0),))
    assert ctrl._market_data_age_s() == pytest.approx(0.1)
    for i in range(3):
        overlay.on_l2(7000 + i * 5500, ((1.05, 1.0),), ((1.15, 1.0),))
    assert math.isinf(ctrl._market_data_age_s())

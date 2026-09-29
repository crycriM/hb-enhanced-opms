"""Runtime patch: give the pinned Hummingbot HL connector a block-cadence touch.

HL's public `l2Book` is throttled to ~5.5 s, so HB's book, and every mid/touch read
from it, is up to 5.5 s stale. This subscribes `bbo` too and routes it as a snapshot
through `perp_bot.hl_lob.BookOverlay` (l2 depth + latest bbo touch), the same merge the
replay loader uses. Applied by importing the controller; no edits to the HB checkout.
"""

from hummingbot.core.data_type.order_book_message import OrderBookMessage, OrderBookMessageType
from hummingbot.core.web_assistant.connections.data_types import WSJSONRequest

from perp_bot.hl_lob import BookOverlay


def _bbo_request(coin: str) -> WSJSONRequest:
    return WSJSONRequest(payload={"method": "subscribe", "subscription": {"type": "bbo", "coin": coin}})


def _level(row):
    return None if not row else (float(row["px"]), float(row["sz"]))


def apply() -> None:
    from hummingbot.connector.derivative.hyperliquid_perpetual.hyperliquid_perpetual_api_order_book_data_source import (
        HyperliquidPerpetualAPIOrderBookDataSource as Source,
    )

    if getattr(Source, "_opms_bbo_patched", False):
        return
    subscribe_channels = Source._subscribe_channels
    subscribe_pair = Source.subscribe_to_trading_pair
    route = Source._channel_originating_message
    parse_snapshot = Source._parse_order_book_snapshot_message

    async def _subscribe_channels(self, ws):
        await subscribe_channels(self, ws)
        for pair in self._trading_pairs:
            symbol = await self._connector.exchange_symbol_associated_to_pair(trading_pair=pair)
            await ws.send(_bbo_request(symbol))

    async def subscribe_to_trading_pair(self, trading_pair):
        ok = await subscribe_pair(self, trading_pair)
        if ok:
            symbol = await self._connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
            await self._ws_assistant.send(_bbo_request(symbol.split("-")[0]))
        return ok

    def _channel_originating_message(self, event_message):
        if event_message.get("channel") == "bbo":
            return self._snapshot_messages_queue_key
        return route(self, event_message)

    async def _parse_order_book_snapshot_message(self, raw_message, message_queue):
        data = raw_message["data"]
        if raw_message.get("channel") != "bbo" and not all(data["levels"]):
            return await parse_snapshot(self, raw_message, message_queue)  # one-sided book: unchanged path
        pair = await self._connector.trading_pair_associated_to_exchange_symbol(self.parse_symbol(raw_message))
        overlay = self.__dict__.setdefault("_opms_overlays", {}).setdefault(pair, BookOverlay())
        if raw_message["channel"] == "bbo":
            merged = overlay.on_bbo(data["time"], *(_level(r) for r in data["bbo"]))
        else:
            bids, asks = ([(float(r["px"]), float(r["sz"])) for r in side] for side in data["levels"])
            merged = overlay.on_l2(data["time"], bids, asks)
        if merged is None:
            return
        message_queue.put_nowait(OrderBookMessage(OrderBookMessageType.SNAPSHOT, {
            "trading_pair": pair, "update_id": data["time"],
            "bids": [list(x) for x in merged[0]], "asks": [list(x) for x in merged[1]],
        }, timestamp=data["time"] * 1e-3))

    Source._subscribe_channels = _subscribe_channels
    Source.subscribe_to_trading_pair = subscribe_to_trading_pair
    Source._channel_originating_message = _channel_originating_message
    Source._parse_order_book_snapshot_message = _parse_order_book_snapshot_message
    Source._opms_bbo_patched = True


def feed_healthy(connector, trading_pair: str) -> bool:
    """False once bbo has silently stopped tracking the venue (see BookOverlay)."""
    source = getattr(getattr(connector, "order_book_tracker", None), "data_source", None)
    overlay = getattr(source, "_opms_overlays", {}).get(trading_pair)
    return overlay is None or overlay.healthy

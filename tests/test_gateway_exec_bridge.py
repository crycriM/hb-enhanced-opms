"""Tests for GatewayExecBridge against httpx.MockTransport."""

import json

import httpx
import pytest
from opms.gateway.exec_bridge import GatewayConfig, GatewayExecBridge

_ANCHOR = {"activeBinId": 0, "price": 100.0, "binStep": 80}  # step = 1.008


def _transport(handlers: dict[str, dict], recorder: list | None = None) -> httpx.MockTransport:
    """MockTransport that routes by (method, url) to handlers, defaulting
    unmocked pool-info reads to _ANCHOR so deposit tests don't need to
    repeat it. Records (path, json_body) for POSTs when `recorder` is given."""
    def handler(request):
        key = request.url.path
        if recorder is not None and request.method == "POST":
            recorder.append((key, json.loads(request.content)))
        h = handlers.get(key)
        if h is None:
            if key == "/connectors/meteora/clmm/pool-info":
                return httpx.Response(200, json=_ANCHOR)
            return httpx.Response(500, json={"error": "not found"})
        code = h.get("status", 200)
        return httpx.Response(code, json=h["body"])
    return httpx.MockTransport(handler)


def test_get_state_success():
    t = _transport({
        "/connectors/meteora/clmm/pool-info": {
            "status": 200,
            "body": {"activeBinId": 42, "price": 2.0, "baseTokenAmount": 100.0, "quoteTokenAmount": 5000.0},
        },
    })
    cfg = GatewayConfig(wallet="w1")
    bridge = GatewayExecBridge(cfg)
    bridge._client = httpx.Client(transport=t, base_url="http://mock")
    r = bridge.get_state("pool1")
    assert r.ok
    assert r.data["active_bin"] == 42
    assert r.data["tvl_usd"] == 5200.0
    bridge._client.close()


def test_get_state_with_position():
    t = _transport({
        "/connectors/meteora/clmm/pool-info": {
            "status": 200,
            "body": {"activeBinId": 10, "price": 100.0, "baseTokenAmount": 0.0, "quoteTokenAmount": 0.0},
        },
        "/connectors/meteora/clmm/position-info": {
            "status": 200,
            "body": {"baseTokenAmount": 1.5, "quoteTokenAmount": 200.0},
        },
    })
    cfg = GatewayConfig(wallet="w1")
    bridge = GatewayExecBridge(cfg)
    bridge._client = httpx.Client(transport=t, base_url="http://mock")
    bridge._positions["pool1"] = "pos1"
    r = bridge.get_state("pool1")
    assert r.ok
    assert r.data["balances"]["base"] == 1.5
    assert r.data["balances"]["quote"] == 200.0
    bridge._client.close()


def test_get_state_pool_error():
    t = _transport({
        "/connectors/meteora/clmm/pool-info": {"status": 500, "body": {"error": "fail"}},
    })
    cfg = GatewayConfig(wallet="w1")
    bridge = GatewayExecBridge(cfg)
    bridge._client = httpx.Client(transport=t, base_url="http://mock")
    r = bridge.get_state("pool1")
    assert not r.ok
    bridge._client.close()


def test_deposit_first_converts_bins_to_price_range():
    recorder = []
    t = _transport({
        "/connectors/meteora/clmm/open-position": {
            "status": 200,
            "body": {"positionAddress": "pos_new", "signature": "sig1"},
        },
    }, recorder=recorder)
    cfg = GatewayConfig(wallet="w1")
    bridge = GatewayExecBridge(cfg)
    bridge._client = httpx.Client(transport=t, base_url="http://mock")
    r = bridge.deposit_single_sided("pool1", "bid", [1, 2], [10.0, 20.0])
    assert r.ok
    assert bridge._positions.get("pool1") == "pos_new"
    assert r.data["position_id"] == "pos_new"

    body = next(b for p, b in recorder if p == "/connectors/meteora/clmm/open-position")
    step = 1.008
    assert body["lowerPrice"] == pytest.approx(100.0 * step ** 1)
    assert body["upperPrice"] == pytest.approx(100.0 * step ** 2)
    assert body["quoteTokenAmount"] == 30.0   # bid side: quote only
    assert body["baseTokenAmount"] == 0
    assert body["strategyType"] == 0          # Spot
    assert "binIds" not in body and "amounts" not in body
    bridge._client.close()


def test_deposit_second_routes_to_add_liquidity():
    recorder = []
    t = _transport({
        "/connectors/meteora/clmm/add-liquidity": {
            "status": 200,
            "body": {"signature": "sig2"},
        },
    }, recorder=recorder)
    cfg = GatewayConfig(wallet="w1")
    bridge = GatewayExecBridge(cfg)
    bridge._client = httpx.Client(transport=t, base_url="http://mock")
    bridge._positions["pool1"] = "pos1"
    r = bridge.deposit_single_sided("pool1", "ask", [3], [15.0])
    assert r.ok

    body = next(b for p, b in recorder if p == "/connectors/meteora/clmm/add-liquidity")
    assert body["positionAddress"] == "pos1"
    assert body["baseTokenAmount"] == 15.0    # ask side: base only
    assert body["quoteTokenAmount"] == 0
    bridge._client.close()


def test_withdraw_full():
    t = _transport({
        "/connectors/meteora/clmm/close-position": {"status": 200, "body": {"signature": "sig_w"}},
    })
    cfg = GatewayConfig(wallet="w1")
    bridge = GatewayExecBridge(cfg)
    bridge._client = httpx.Client(transport=t, base_url="http://mock")
    bridge._positions["pool1"] = "pos1"
    r = bridge.withdraw("pos1", bps=100)
    assert r.ok
    assert "pos1" not in bridge._positions["pool1"] if "pool1" in bridge._positions else True
    bridge._client.close()


def test_withdraw_full_body_has_no_bps():
    recorder = []
    t = _transport({
        "/connectors/meteora/clmm/close-position": {"status": 200, "body": {"signature": "sig_w"}},
    }, recorder=recorder)
    cfg = GatewayConfig(wallet="w1")
    bridge = GatewayExecBridge(cfg)
    bridge._client = httpx.Client(transport=t, base_url="http://mock")
    bridge.withdraw("pos1", bps=100)
    body = next(b for p, b in recorder if p == "/connectors/meteora/clmm/close-position")
    assert body == {"network": "mainnet-beta", "walletAddress": "w1", "positionAddress": "pos1"}
    bridge._client.close()


def test_withdraw_partial():
    t = _transport({
        "/connectors/meteora/clmm/remove-liquidity": {"status": 200, "body": {"signature": "sig_r"}},
    })
    cfg = GatewayConfig(wallet="w1")
    bridge = GatewayExecBridge(cfg)
    bridge._client = httpx.Client(transport=t, base_url="http://mock")
    bridge._positions["pool1"] = "pos1"
    r = bridge.withdraw("pos1", bps=50)
    assert r.ok
    assert bridge._positions.get("pool1") == "pos1"
    bridge._client.close()


def test_swap():
    t = _transport({
        "/connectors/meteora/clmm/execute-swap": {"status": 200, "body": {"signature": "sig_s"}},
    })
    cfg = GatewayConfig(wallet="w1")
    bridge = GatewayExecBridge(cfg)
    bridge._client = httpx.Client(transport=t, base_url="http://mock")
    r = bridge.swap("USDC", "SOL", 1000.0, max_slippage_bps=50)
    assert r.ok
    bridge._client.close()


def test_refresh_bundle_success():
    call_log = []
    call_count = {"deposit": 0}
    def handler(request):
        path = request.url.path
        call_log.append(path)
        if path == "/connectors/meteora/clmm/pool-info":
            return httpx.Response(200, json=_ANCHOR)
        if path == "/connectors/meteora/clmm/close-position":
            return httpx.Response(200, json={"signature": "sig_w"})
        if path in ("/connectors/meteora/clmm/open-position", "/connectors/meteora/clmm/add-liquidity"):
            call_count["deposit"] += 1
            return httpx.Response(200, json={"positionAddress": f"pos_{call_count['deposit']}", "signature": f"sig_d{call_count['deposit']}"})
        return httpx.Response(500, json={})
    t = httpx.MockTransport(handler)
    cfg = GatewayConfig(wallet="w1")
    bridge = GatewayExecBridge(cfg)
    bridge._client = httpx.Client(transport=t, base_url="http://mock")
    r = bridge.refresh_bundle(
        "pos1",
        None,
        {"pool": "pool1", "bid_bins": [1], "bid_amounts": [10.0], "ask_bins": [3], "ask_amounts": [20.0]},
    )
    assert r.ok
    assert "/connectors/meteora/clmm/close-position" in call_log
    bridge._client.close()


def test_refresh_bundle_swap_fail_continues():
    call_log = []
    def handler(request):
        path = request.url.path
        call_log.append(path)
        if path == "/connectors/meteora/clmm/pool-info":
            return httpx.Response(200, json=_ANCHOR)
        if path == "/connectors/meteora/clmm/close-position":
            return httpx.Response(200, json={"signature": "sig_w"})
        if path == "/connectors/meteora/clmm/execute-swap":
            return httpx.Response(200, json={"error": "swap fail"})
        if path == "/connectors/meteora/clmm/open-position":
            return httpx.Response(200, json={"positionAddress": "pos_new", "signature": "sig_d"})
        return httpx.Response(500, json={})
    t = httpx.MockTransport(handler)
    cfg = GatewayConfig(wallet="w1")
    bridge = GatewayExecBridge(cfg)
    bridge._client = httpx.Client(transport=t, base_url="http://mock")
    r = bridge.refresh_bundle(
        "pos1",
        {"in_mint": "A", "out_mint": "B", "amount": 100.0},
        {"pool": "pool1", "bid_bins": [1], "bid_amounts": [10.0], "ask_bins": [], "ask_amounts": []},
    )
    assert r.ok
    assert "/connectors/meteora/clmm/execute-swap" in call_log
    bridge._client.close()


def test_refresh_bundle_withdraw_fail():
    t = _transport({
        "/connectors/meteora/clmm/close-position": {"status": 500, "body": {"error": "fail"}},
    })
    cfg = GatewayConfig(wallet="w1")
    bridge = GatewayExecBridge(cfg)
    bridge._client = httpx.Client(transport=t, base_url="http://mock")
    r = bridge.refresh_bundle("pos1", None, {"pool": "pool1", "bid_bins": [], "ask_bins": []})
    assert not r.ok
    bridge._client.close()


def test_http_error():
    t = _transport({
        "/connectors/meteora/clmm/pool-info": {"status": 500, "body": {"error": "server err"}},
    })
    cfg = GatewayConfig(wallet="w1")
    bridge = GatewayExecBridge(cfg)
    bridge._client = httpx.Client(transport=t, base_url="http://mock")
    r = bridge.get_state("pool1")
    assert not r.ok
    bridge._client.close()

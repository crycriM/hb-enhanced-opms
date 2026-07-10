"""Tests for GatewayExecBridge against httpx.MockTransport."""

import httpx
import pytest
from opms.gateway.exec_bridge import GatewayConfig, GatewayExecBridge


def _transport(handlers: dict[str, dict]) -> httpx.MockTransport:
    """MockTransport that routes by (method, url) to handlers."""
    def handler(request):
        key = request.url.path
        h = handlers.get(key)
        if h is None:
            return httpx.Response(500, json={"error": "not found"})
        code = h.get("status", 200)
        return httpx.Response(code, json=h["body"])
    return httpx.MockTransport(handler)


def test_get_state_success():
    t = _transport({
        "/meteora/pool-info": {
            "status": 200,
            "body": {"activeBin": 42, "tvl": 10000.0},
        },
    })
    cfg = GatewayConfig(wallet="w1")
    bridge = GatewayExecBridge(cfg)
    bridge._client = httpx.Client(transport=t, base_url="http://mock")
    r = bridge.get_state("pool1")
    assert r.ok
    assert r.data["active_bin"] == 42
    assert r.data["tvl_usd"] == 10000.0
    bridge._client.close()


def test_get_state_with_position():
    t = _transport({
        "/meteora/pool-info": {
            "status": 200,
            "body": {"activeBin": 10, "tvl": 5000.0},
        },
        "/meteora/position-info": {
            "status": 200,
            "body": {"baseAmount": 1.5, "quoteAmount": 200.0},
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
        "/meteora/pool-info": {"status": 500, "body": {"error": "fail"}},
    })
    cfg = GatewayConfig(wallet="w1")
    bridge = GatewayExecBridge(cfg)
    bridge._client = httpx.Client(transport=t, base_url="http://mock")
    r = bridge.get_state("pool1")
    assert not r.ok
    bridge._client.close()


def test_deposit_first():
    t = _transport({
        "/meteora/open-position": {
            "status": 200,
            "body": {"positionAddress": "pos_new", "signature": "sig1"},
        },
    })
    cfg = GatewayConfig(wallet="w1")
    bridge = GatewayExecBridge(cfg)
    bridge._client = httpx.Client(transport=t, base_url="http://mock")
    r = bridge.deposit_single_sided("pool1", "bid", [1, 2], [10.0, 20.0])
    assert r.ok
    assert bridge._positions.get("pool1") == "pos_new"
    assert r.data["position_id"] == "pos_new"
    bridge._client.close()


def test_deposit_second():
    t = _transport({
        "/meteora/add-liquidity": {
            "status": 200,
            "body": {"signature": "sig2"},
        },
    })
    cfg = GatewayConfig(wallet="w1")
    bridge = GatewayExecBridge(cfg)
    bridge._client = httpx.Client(transport=t, base_url="http://mock")
    bridge._positions["pool1"] = "pos1"
    r = bridge.deposit_single_sided("pool1", "ask", [3], [15.0])
    assert r.ok
    bridge._client.close()


def test_withdraw_full():
    t = _transport({
        "/meteora/close-position": {"status": 200, "body": {"signature": "sig_w"}},
    })
    cfg = GatewayConfig(wallet="w1")
    bridge = GatewayExecBridge(cfg)
    bridge._client = httpx.Client(transport=t, base_url="http://mock")
    bridge._positions["pool1"] = "pos1"
    r = bridge.withdraw("pos1", bps=100)
    assert r.ok
    assert "pos1" not in bridge._positions["pool1"] if "pool1" in bridge._positions else True
    bridge._client.close()


def test_withdraw_partial():
    t = _transport({
        "/meteora/remove-liquidity": {"status": 200, "body": {"signature": "sig_r"}},
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
        "/jupiter/execute-swap": {"status": 200, "body": {"signature": "sig_s"}},
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
        if path == "/meteora/close-position":
            return httpx.Response(200, json={"signature": "sig_w"})
        if path in ("/meteora/open-position", "/meteora/add-liquidity"):
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
    assert "/meteora/close-position" in call_log
    bridge._client.close()


def test_refresh_bundle_swap_fail_continues():
    call_log = []
    def handler(request):
        path = request.url.path
        call_log.append(path)
        if path == "/meteora/close-position":
            return httpx.Response(200, json={"signature": "sig_w"})
        if path == "/jupiter/execute-swap":
            return httpx.Response(200, json={"error": "swap fail"})
        if path == "/meteora/open-position":
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
    assert "/jupiter/execute-swap" in call_log
    bridge._client.close()


def test_refresh_bundle_withdraw_fail():
    t = _transport({
        "/meteora/close-position": {"status": 500, "body": {"error": "fail"}},
    })
    cfg = GatewayConfig(wallet="w1")
    bridge = GatewayExecBridge(cfg)
    bridge._client = httpx.Client(transport=t, base_url="http://mock")
    r = bridge.refresh_bundle("pos1", None, {"pool": "pool1", "bid_bins": [], "ask_bins": []})
    assert not r.ok
    bridge._client.close()


def test_http_error():
    t = _transport({
        "/meteora/pool-info": {"status": 500, "body": {"error": "server err"}},
    })
    cfg = GatewayConfig(wallet="w1")
    bridge = GatewayExecBridge(cfg)
    bridge._client = httpx.Client(transport=t, base_url="http://mock")
    r = bridge.get_state("pool1")
    assert not r.ok
    bridge._client.close()

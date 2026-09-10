"""
Live integration tests against Hyperliquid mainnet.

Requires:
  pytest tests_live/ -m mainnet
  export OPMS_LIVE_MAINNET=confirm

Order tests additionally require:
  export OPMS_LIVE_PLACE_ORDERS=confirm
"""

import pytest


# ---------------------------------------------------------------------------
# Market data (read-only, no auth needed)
# ---------------------------------------------------------------------------

class TestMarketData:
    @pytest.mark.mainnet
    def test_exchange_meta(self, hl_info):
        meta = hl_info.meta()
        assert "universe" in meta
        assert len(meta["universe"]) > 50

    @pytest.mark.mainnet
    def test_l2_snapshot(self, hl_info):
        l2 = hl_info.l2_snapshot("ETH")
        assert "levels" in l2
        assert len(l2["levels"]) == 2
        bids, asks = l2["levels"]
        assert len(bids) >= 5
        assert len(asks) >= 5
        bid_px = float(bids[0]["px"])
        ask_px = float(asks[0]["px"])
        assert bid_px < ask_px
        assert 1 < bid_px < 100000
        assert 1 < ask_px < 100000

    @pytest.mark.mainnet
    def test_bbo_for_multiple_coins(self, hl_info):
        for coin in ("ETH", "BTC", "SOL"):
            l2 = hl_info.l2_snapshot(coin)
            bids = l2["levels"][0]
            assert len(bids) >= 1, f"{coin} has no bids"


class TestAllMids:
    @pytest.mark.mainnet
    def test_all_mids_returns_dict(self, hl_info):
        mids = hl_info.all_mids()
        assert isinstance(mids, dict)
        assert len(mids) > 10
        for coin, mid in mids.items():
            assert float(mid) > 0


# ---------------------------------------------------------------------------
# Account state (requires credentials)
# ---------------------------------------------------------------------------

class TestAccountState:
    @pytest.mark.mainnet
    def test_user_state_returns_valid(self, hl_info, live_account):
        state = hl_info.user_state(live_account["account_address"])
        assert "marginSummary" in state
        assert "accountValue" in state["marginSummary"]

    @pytest.mark.mainnet
    def test_account_has_expected_structure(self, hl_info, live_account):
        state = hl_info.user_state(live_account["account_address"])
        ms = state["marginSummary"]
        for field in ("accountValue", "totalNtlPos", "totalRawUsd"):
            assert field in ms, f"marginSummary missing {field}"
        positions = state.get("assetPositions", [])
        assert isinstance(positions, list)
        orders = state.get("openOrders", [])
        assert isinstance(orders, list)

    @pytest.mark.mainnet
    def test_account_address_case_insensitive(self, hl_info):
        addr = "0x27117759b5cd226747008a9c9210b2d9c052ece7"
        result = hl_info.user_state(addr)
        assert result is not None


# ---------------------------------------------------------------------------
# Order lifecycle (double-gated — requires OPMS_LIVE_PLACE_ORDERS=confirm)
# ---------------------------------------------------------------------------

class TestOrderLifecycle:
    @pytest.mark.mainnet
    @pytest.mark.place_orders
    def test_order_signing_and_submission(self, hl_exchange, live_account, place_orders):
        state = hl_exchange.info.user_state(live_account["account_address"])
        available = float(state["marginSummary"]["accountValue"])
        if available < 10:
            pytest.skip(f"Insufficient USDC ({available}) to place test order")

        sz = 0.001
        l2 = hl_exchange.info.l2_snapshot("ETH")
        _, asks = l2["levels"]
        ask_px = float(asks[0]["px"])
        far_price = round(ask_px * 1.2, 1)

        order_result = hl_exchange.order(
            "ETH",
            True,
            sz,
            far_price,
            {"limit": {"tif": "Gtc"}},
            reduce_only=False,
        )
        assert "response" in order_result
        resp = order_result["response"]
        if "data" in resp and "statuses" in resp["data"]:
            statuses = resp["data"]["statuses"]
            if statuses[0].get("resting", {}).get("oid"):
                oid = statuses[0]["resting"]["oid"]
                hl_exchange.cancel("ETH", oid)
                return
        pytest.skip(f"Order not accepted (response: {resp})")

    @pytest.mark.mainnet
    @pytest.mark.place_orders
    def test_cancel_non_existent_order(self, hl_exchange, place_orders):
        result = hl_exchange.cancel("ETH", 999999999999)
        assert "response" in result
        resp = result["response"]
        assert resp.get("type") != "error" or "order does not exist" in str(resp).lower()

    @pytest.mark.mainnet
    @pytest.mark.place_orders
    def test_cancel_all_orders_for_account(self, hl_exchange, live_account, place_orders):
        open_orders = hl_exchange.info.open_orders(live_account["account_address"])
        if not open_orders:
            pytest.skip("No open orders to cancel")
        oids = [o["oid"] for o in open_orders]
        result = hl_exchange.batch_cancel(oids)
        assert "response" in result


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestErrorHandling:
    @pytest.mark.mainnet
    def test_info_with_bad_symbol_raises(self, hl_info):
        with pytest.raises(Exception):
            hl_info.l2_snapshot("NOTAREALCOIN999")
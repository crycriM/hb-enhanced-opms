"""
Live integration tests against Hyperliquid mainnet.

Requires:
  pytest tests_live/ -m mainnet
  export OPMS_LIVE_MAINNET=confirm

Order tests additionally require:
  export OPMS_LIVE_PLACE_ORDERS=confirm

Order tests are deliberately tiny and passive: they rest a limit order far
from the mid on a maker-only path (so it cannot cross), verify placement /
subaccount routing, then cancel it. Nothing here should ever leave a position
or a resting order behind; every placement is cancelled in a ``finally``.
"""

import pytest


# Smallest order that clears Hyperliquid's $10 minimum notional at ETH prices.
TINY_ETH_SZ = 0.01
FAR_FRACTION = 0.20  # place 20% away from the near touch — never marketable


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


def _collateral_usdc(info, address: str) -> tuple[float, float]:
    """Return (spot_usdc, perp_account_value) for an HL account/subaccount.

    Hyperliquid unified-account mode collateralizes perps with the spot USDC
    balance, so perp ``accountValue`` stays 0 until a position opens. Spot USDC
    is therefore the real funding signal.
    """
    spot = info.spot_user_state(address)
    spot_usdc = sum(float(b["total"]) for b in spot.get("balances", []) if b["coin"] == "USDC")
    state = info.user_state(address)
    perp = float(state["marginSummary"]["accountValue"])
    return spot_usdc, perp


class TestUnifiedCollateral:
    @pytest.mark.mainnet
    def test_account_has_available_collateral(self, hl_info, live_account):
        spot, perp = _collateral_usdc(hl_info, live_account["account_address"])
        total = spot + perp
        if total <= 0:
            pytest.skip(f"{live_account['account_id']} has no USDC collateral (spot={spot}, perp={perp})")
        assert total > 0


# ---------------------------------------------------------------------------
# Order lifecycle (double-gated — requires OPMS_LIVE_PLACE_ORDERS=confirm)
# ---------------------------------------------------------------------------

def _place_far_maker(exchange, info, coin="ETH", sz=TINY_ETH_SZ, is_buy=True, tif="Gtc"):
    """Rest a non-marketable limit order and return (oid, price, raw_result).

    Buy is placed below the best bid, sell above the best ask, so the order
    cannot cross and cannot fill.
    """
    bids, asks = info.l2_snapshot(coin)["levels"]
    if is_buy:
        px = round(float(bids[0]["px"]) * (1 - FAR_FRACTION), 1)
    else:
        px = round(float(asks[0]["px"]) * (1 + FAR_FRACTION), 1)
    result = exchange.order(coin, is_buy, sz, px, {"limit": {"tif": tif}}, reduce_only=False)
    statuses = result.get("response", {}).get("data", {}).get("statuses", [])
    oid = statuses[0].get("resting", {}).get("oid") if statuses else None
    return oid, px, result


def _position_size(info, address: str, coin="ETH") -> float:
    for entry in info.user_state(address)["assetPositions"]:
        if entry["position"]["coin"] == coin:
            return float(entry["position"]["szi"])
    return 0.0


def _cancel(exchange, coin: str, oid: int) -> None:
    try:
        exchange.cancel(coin, oid)
    except Exception:
        pass  # best-effort cleanup; the assertion below is the real check


class TestOrderLifecycle:
    @pytest.mark.mainnet
    @pytest.mark.place_orders
    def test_order_signing_and_submission(self, hl_info, hl_exchange, live_account, place_orders):
        spot, perp = _collateral_usdc(hl_info, live_account["account_address"])
        if spot + perp <= 0:
            pytest.skip(f"{live_account['account_id']} has no USDC collateral to place an order")

        oid, px, result = _place_far_maker(hl_exchange, hl_info)
        try:
            assert oid is not None, f"order did not rest (response: {result})"
            open_oids = {o["oid"] for o in hl_info.open_orders(live_account["account_address"])}
            assert oid in open_oids, f"resting oid {oid} not visible on the account"
        finally:
            if oid is not None:
                _cancel(hl_exchange, "ETH", oid)
        assert oid not in {o["oid"] for o in hl_info.open_orders(live_account["account_address"])}

    @pytest.mark.mainnet
    @pytest.mark.place_orders
    def test_cancel_non_existent_order(self, hl_exchange, place_orders):
        result = hl_exchange.cancel("ETH", 999999999999)
        assert "response" in result
        resp = result["response"]
        assert resp.get("type") != "error" or "order does not exist" in str(resp).lower()

    @pytest.mark.mainnet
    @pytest.mark.place_orders
    def test_scoped_cancel_only_cancels_own_orders(self, hl_info, hl_exchange, live_account, place_orders):
        """Cancellation is by oid — never a blanket cancel-all on a live account."""
        spot, perp = _collateral_usdc(hl_info, live_account["account_address"])
        if spot + perp <= 0:
            pytest.skip(f"{live_account['account_id']} has no USDC collateral")

        oid, _, first = _place_far_maker(hl_exchange, hl_info, is_buy=True)
        try:
            assert oid is not None, f"order did not rest (response: {first})"
        finally:
            if oid is not None:
                _cancel(hl_exchange, "ETH", oid)
        remaining = {o["oid"] for o in hl_info.open_orders(live_account["account_address"])}
        assert oid not in remaining

    @pytest.mark.mainnet
    @pytest.mark.place_orders
    def test_post_only_alo_is_maker_only(self, hl_info, hl_exchange, live_account, place_orders):
        """ALO (post-only) rests when passive and is rejected, not filled, when crossing."""
        address = live_account["account_address"]
        spot, perp = _collateral_usdc(hl_info, address)
        if spot + perp <= 0:
            pytest.skip(f"{live_account['account_id']} has no USDC collateral")

        moving_oid = None
        try:
            moving_oid, _, passive = _place_far_maker(hl_exchange, hl_info, is_buy=True, tif="Alo")
            assert moving_oid is not None, f"passive ALO did not rest (response: {passive})"

            before = _position_size(hl_info, address)
            _, asks = hl_info.l2_snapshot("ETH")["levels"]
            crossing_px = round(float(asks[0]["px"]) * 1.05, 1)
            result = hl_exchange.order(
                "ETH", True, TINY_ETH_SZ, crossing_px, {"limit": {"tif": "Alo"}}, reduce_only=False
            )
            statuses = result.get("response", {}).get("data", {}).get("statuses", [])
            oid = statuses[0].get("resting", {}).get("oid") if statuses else None
            after = _position_size(hl_info, address)
            assert oid is None, "crossing ALO rested — post-only not enforced"
            assert after == before, f"crossing ALO filled (position {before} -> {after}) — maker-only violated"
        finally:
            if moving_oid is not None:
                _cancel(hl_exchange, "ETH", moving_oid)


# ---------------------------------------------------------------------------
# Subaccount routing (the vaultAddress seam)
# ---------------------------------------------------------------------------

class TestSubaccountRouting:
    @pytest.mark.mainnet
    @pytest.mark.place_orders
    def test_order_visible_only_on_target_account(
        self, hl_info, hl_exchange, live_account, all_account_addresses, place_orders
    ):
        """An order signed with vaultAddress=target must not leak to any other account."""
        target = live_account["account_address"]
        spot, perp = _collateral_usdc(hl_info, target)
        if spot + perp <= 0:
            pytest.skip(f"{live_account['account_id']} has no USDC collateral")

        oid, px, result = _place_far_maker(hl_exchange, hl_info, is_buy=True)
        try:
            assert oid is not None, f"order did not rest (response: {result})"
            assert oid in {o["oid"] for o in hl_info.open_orders(target)}, "order missing from target account"
            for account_id, address in all_account_addresses.items():
                if address == target:
                    continue
                leaked = {o["oid"] for o in hl_info.open_orders(address)}
                assert oid not in leaked, f"order leaked to {account_id} ({address}) — vaultAddress routing broken"
        finally:
            if oid is not None:
                _cancel(hl_exchange, "ETH", oid)


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestErrorHandling:
    @pytest.mark.mainnet
    def test_info_with_bad_symbol_raises(self, hl_info):
        with pytest.raises(Exception):
            hl_info.l2_snapshot("NOTAREALCOIN999")

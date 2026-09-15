"""Read-only smoke test of Hummingbot's real Hyperliquid **mainnet** connector.

Opt-in (hits the network but places no orders)::

    OPMS_HB_MAINNET=confirm pytest tests_real/test_hyperliquid_mainnet_connector.py -v

Proves the two things the credential/routing work depends on:
  * a subaccount connector is built with ``use_vault=True`` and reads that
    subaccount's balance (unified account mode routes spot USDC to perps);
  * the master connector is built with ``use_vault=False``.

No orders are placed and no private key is ever printed.
"""

import os
from pathlib import Path

import pytest

pytest.importorskip("hummingbot.connector.derivative.hyperliquid_perpetual")

from dotenv import load_dotenv  # noqa: E402

_DOT_ENV = Path(__file__).resolve().parents[2] / ".env"
if _DOT_ENV.exists():
    load_dotenv(_DOT_ENV)

MASTER_ACCOUNT_ID = "e2_main"
SUBACCOUNT_ID = "e2_mm1"


def _require_mainnet_optin():
    if os.environ.get("OPMS_HB_MAINNET") != "confirm":
        pytest.skip("OPMS_HB_MAINNET=confirm not set — refusing mainnet connector test")


def _creds(account_id: str) -> tuple[str, str]:
    prefix = f"HYPERLIQUID_{account_id.upper()}"
    address = os.environ.get(f"{prefix}_ACCOUNT_ADDRESS")
    private_key = os.environ.get(f"{prefix}_PRIVATE_KEY")
    if not address or not private_key:
        pytest.skip(f"credentials not found for {prefix}")
    return address, private_key


def _connector(account_id: str, use_vault: bool):
    from hummingbot.connector.derivative.hyperliquid_perpetual.hyperliquid_perpetual_derivative import (
        HyperliquidPerpetualDerivative,
    )

    address, private_key = _creds(account_id)
    return HyperliquidPerpetualDerivative(
        hyperliquid_perpetual_secret_key=private_key,
        hyperliquid_perpetual_address=address,
        use_vault=use_vault,
        hyperliquid_perpetual_mode="api_wallet",
        trading_pairs=["ETH-USD"],
        trading_required=True,
    )


async def _read_balance(connector) -> dict:
    await connector._initialize_trading_pair_symbol_map()
    await connector._update_balances()
    return connector.get_all_balances()


@pytest.mark.asyncio
async def test_subaccount_connector_reads_unified_balance():
    _require_mainnet_optin()
    connector = _connector(SUBACCOUNT_ID, use_vault=True)
    try:
        assert connector._use_vault is True
        balances = await _read_balance(connector)
        assert balances.get("USD", 0) > 0, f"expected a funded unified balance, got {balances}"
        # Margin-health feed (mm_core RiskConfig.margin_health_*): read the
        # live spotClearinghouseState.tokenToAvailableAfterMaintenance through
        # the controller's method, on the same connector/loop — a second
        # connector in a later test would hit HB's shared rate-limiter state
        # from this loop ("Event loop is closed"). The figure is the balance
        # minus cross maintenance margin used, so on a healthy account it
        # can only sit in (0, balance].
        from opms.controllers.generic.perp_mm_controller import (
            PerpMMController,
            PerpMMControllerConfig,
        )

        ctrl = object.__new__(PerpMMController)
        ctrl.config = PerpMMControllerConfig(
            id="ctrl_margin_live",
            controller_name="perp_mm",
            connector_name="hyperliquid_perpetual",
            trading_pair="ETH-USD",
            venue="hyperliquid",
            account_id=SUBACCOUNT_ID,
        )

        class _MD:
            def get_connector(self, connector_name):
                return connector

        ctrl.market_data_provider = _MD()

        margin = await ctrl._current_margin_available()
        equity = float(balances.get("USD", 0))
        assert margin is not None, "spot clearinghouse read failed on a live funded account"
        assert 0 < margin <= equity, f"margin {margin} outside (0, {equity}]"
    finally:
        await connector.stop_network()


@pytest.mark.asyncio
async def test_master_connector_builds_without_vault():
    _require_mainnet_optin()
    connector = _connector(MASTER_ACCOUNT_ID, use_vault=False)
    try:
        assert connector._use_vault is False
    finally:
        await connector.stop_network()


# The full live smoke (start_network + funding rate + controller on_start +
# equity) lives in scripts/run_hb_mainnet_smoke.py, not here: HB connectors
# spawn background tasks that outlive pytest-asyncio's per-test event loop and
# error on loop close. Run that script for the network path.

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

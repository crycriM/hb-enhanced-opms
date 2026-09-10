"""
Live integration test fixtures for Hyperliquid mainnet.

SAFETY: these tests will NOT run without explicit opt-in:
  export OPMS_LIVE_MAINNET=confirm
  pytest tests_live/ -m mainnet
"""

import os
import pytest
from pathlib import Path
from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DOT_ENV = _REPO_ROOT / ".env"

if _DOT_ENV.exists():
    load_dotenv(_DOT_ENV)


def pytest_configure(config):
    config.addinivalue_line("markers", "mainnet: requires Hyperliquid mainnet connection (OPMS_LIVE_MAINNET=confirm)")
    config.addinivalue_line("markers", "place_orders: places real orders on mainnet (requires OPMS_LIVE_PLACE_ORDERS=confirm)")


def pytest_runtest_setup(item):
    if item.get_closest_marker("mainnet") and os.environ.get("OPMS_LIVE_MAINNET") != "confirm":
        pytest.skip("OPMS_LIVE_MAINNET=confirm not set — refusing mainnet tests")


def _check_order_gate():
    if os.environ.get("OPMS_LIVE_PLACE_ORDERS") != "confirm":
        pytest.skip("OPMS_LIVE_PLACE_ORDERS=confirm not set — refusing order placement")


@pytest.fixture(params=[
    pytest.param("e2_main", id="e2_main"),
    pytest.param("e2_mm1", id="e2_mm1"),
    pytest.param("e2_mm2", id="e2_mm2"),
])
def live_account(request):
    account_id = request.param
    prefix = f"HYPERLIQUID_{account_id.upper()}"
    pk = os.environ.get(f"{prefix}_PRIVATE_KEY")
    addr = os.environ.get(f"{prefix}_ACCOUNT_ADDRESS")
    if not pk or not addr:
        pytest.skip(f"Credentials not found for {prefix}_PRIVATE_KEY / _ACCOUNT_ADDRESS")
    return {
        "account_id": account_id,
        "private_key": pk,
        "account_address": addr.lower(),
        "is_testnet": False,
    }


@pytest.fixture
def hl_info():
    from hyperliquid.info import Info
    from hyperliquid.utils import constants
    return Info(constants.MAINNET_API_URL, skip_ws=True)


@pytest.fixture
def hl_exchange(live_account):
    from hyperliquid.exchange import Exchange
    from eth_account import Account
    wallet = Account.from_key(live_account["private_key"])
    from hyperliquid.utils import constants
    return Exchange(wallet=wallet, base_url=constants.MAINNET_API_URL)


@pytest.fixture
def place_orders():
    _check_order_gate()
    return True
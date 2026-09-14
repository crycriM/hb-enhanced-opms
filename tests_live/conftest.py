"""
Live integration test fixtures for Hyperliquid mainnet.

SAFETY: these tests will NOT run without explicit opt-in:
  export OPMS_LIVE_MAINNET=confirm
  pytest tests_live/ -m mainnet

Order tests additionally require:
  export OPMS_LIVE_PLACE_ORDERS=confirm

Hyperliquid subaccounts own an address but no private key. To sign on a
subaccount's behalf, the master's agent key must sign with the request's
``vaultAddress`` set to the subaccount address. ``Exchange(vault_address=...)``
does exactly that, so ``hl_exchange`` routes each subaccount's signed calls to
that subaccount. Without it, ``e2_mm1``/``e2_mm2`` order tests would sign for
the master wallet instead of the intended subaccount (see
`perp-bot/docs/account-naming.md`).
"""

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DOT_ENV = _REPO_ROOT / ".env"

# The master account whose agent key signs for every subaccount. Its address
# is the `account_address` the agent wallet is approved under.
MASTER_ACCOUNT_ID = "e2_main"
ACCOUNT_IDS = ("e2_main", "e2_mm1", "e2_mm2")

if _DOT_ENV.exists():
    load_dotenv(_DOT_ENV)


def _cred(account_id: str, suffix: str) -> str | None:
    return os.environ.get(f"HYPERLIQUID_{account_id.upper()}_{suffix}")


def _master_address() -> str | None:
    addr = _cred(MASTER_ACCOUNT_ID, "ACCOUNT_ADDRESS")
    return addr.lower() if addr else None


def pytest_configure(config):
    config.addinivalue_line("markers", "mainnet: requires Hyperliquid mainnet connection (OPMS_LIVE_MAINNET=confirm)")
    config.addinivalue_line("markers", "place_orders: places real orders on mainnet (requires OPMS_LIVE_PLACE_ORDERS=confirm)")


def pytest_runtest_setup(item):
    if item.get_closest_marker("mainnet") and os.environ.get("OPMS_LIVE_MAINNET") != "confirm":
        pytest.skip("OPMS_LIVE_MAINNET=confirm not set — refusing mainnet tests")


def _check_order_gate():
    if os.environ.get("OPMS_LIVE_PLACE_ORDERS") != "confirm":
        pytest.skip("OPMS_LIVE_PLACE_ORDERS=confirm not set — refusing order placement")


@pytest.fixture(params=[pytest.param(a, id=a) for a in ACCOUNT_IDS])
def live_account(request):
    account_id = request.param
    pk = _cred(account_id, "PRIVATE_KEY")
    addr = _cred(account_id, "ACCOUNT_ADDRESS")
    if not pk or not addr:
        pytest.skip(f"Credentials not found for HYPERLIQUID_{account_id.upper()}_PRIVATE_KEY / _ACCOUNT_ADDRESS")
    return {
        "account_id": account_id,
        "private_key": pk,
        "account_address": addr.lower(),
        "master_address": _master_address(),
        "is_master": account_id == MASTER_ACCOUNT_ID,
        "is_testnet": False,
    }


@pytest.fixture
def all_account_addresses():
    """Every configured account_id -> lowercased address, for isolation checks."""
    out = {}
    for account_id in ACCOUNT_IDS:
        addr = _cred(account_id, "ACCOUNT_ADDRESS")
        if addr:
            out[account_id] = addr.lower()
    return out


@pytest.fixture
def hl_info():
    from hyperliquid.info import Info
    from hyperliquid.utils import constants
    return Info(constants.MAINNET_API_URL, skip_ws=True)


@pytest.fixture
def hl_exchange(live_account):
    from eth_account import Account
    from hyperliquid.exchange import Exchange
    from hyperliquid.utils import constants

    wallet = Account.from_key(live_account["private_key"])
    if live_account["is_master"]:
        return Exchange(wallet=wallet, base_url=constants.MAINNET_API_URL)
    return Exchange(
        wallet=wallet,
        base_url=constants.MAINNET_API_URL,
        account_address=live_account["master_address"],
        vault_address=live_account["account_address"],
    )


@pytest.fixture
def place_orders():
    _check_order_gate()
    return True

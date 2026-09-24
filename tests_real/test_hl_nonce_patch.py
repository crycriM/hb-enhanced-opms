"""Locks in the local HB patch to HL nonce generation (see README "Local patches to the pinned checkout").

Unpatched HB signs with the raw ms timestamp, so two requests signed in the same
millisecond share a nonce and HL rejects the second. No network, no orders.
"""

import json

import pytest

hl = pytest.importorskip("hummingbot.connector.derivative.hyperliquid_perpetual.hyperliquid_perpetual_auth")

import eth_account  # noqa: E402
from hummingbot.connector.derivative.hyperliquid_perpetual import (  # noqa: E402
    hyperliquid_perpetual_constants as CONSTANTS,
)

CANCEL = json.dumps({"type": "cancel", "cancels": {"asset": 1, "cloid": "0x" + "00" * 16}})


def test_nonces_strictly_increase_on_the_slot_residue(monkeypatch):
    monkeypatch.setattr(hl, "_NONCE_STEP", 2)
    monkeypatch.setattr(hl, "_NONCE_SLOT", 1)
    monkeypatch.setattr(hl, "_last_nonce", 0)
    auth = hl.HyperliquidPerpetualAuth("0x" + "11" * 20, eth_account.Account.create().key.hex(), True)
    # Same ms three times, then the clock steps backwards.
    clock = iter([1_700_000_000.0, 1_700_000_000.0, 1_700_000_000.0, 1_699_999_999.99])
    auth._get_timestamp = lambda: next(clock)

    nonces = [json.loads(auth.add_auth_to_params_post(CANCEL, CONSTANTS.PERPETUAL_BASE_URL))["nonce"]
              for _ in range(4)]

    assert all(b > a for a, b in zip(nonces, nonces[1:])), nonces
    assert all(n % 2 == 1 for n in nonces), nonces

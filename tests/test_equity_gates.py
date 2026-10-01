"""Smoke/derisk/preflight gates fail on an unresolved or non-finite equity reading."""

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from check_hl_account_state import equity_check  # noqa: E402
from run_hb_mainnet_smoke import equity_gate_failure  # noqa: E402


@pytest.mark.parametrize("equity", [None, Decimal("NaN"), Decimal("Infinity")])
def test_controller_gate_fails_unresolved_equity_with_actionable_reason(equity):
    failure = equity_gate_failure(equity)
    assert failure and "unresolved" in failure and "collateral_asset" in failure


def test_controller_gate_checks_resolved_readings():
    assert "non-positive" in equity_gate_failure(Decimal("0"))
    assert equity_gate_failure(Decimal("300")) is None


@pytest.mark.parametrize("state", [
    {},
    {"spot_usdc_total": None, "account_value": None},
    {"spot_usdc_total": "nan"},
    {"spot_usdc_total": "garbage"},
])
def test_preflight_fails_unresolved_equity_even_without_a_minimum(state):
    equity, failure = equity_check(state, min_equity=0.0)
    assert equity is None
    assert "unresolved" in failure


def test_preflight_min_equity_applies_to_resolved_readings():
    assert equity_check({"spot_usdc_total": "0.0", "account_value": "700"}, 600.0) == (
        0.0, "equity $0.00 is below required $600.00")
    assert equity_check({"spot_usdc_total": None, "account_value": "700"}, 600.0) == (700.0, None)

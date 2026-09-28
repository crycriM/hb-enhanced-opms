"""The live soak's equity stop uses exchange collateral, not keeper PnL."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from monitor_hl_soak import (
    _account_sample,
    _decision_liveness_breach,
    _drawdown_breached,
    _oldest_order_age_s,
    _portfolio_drawdown_breached,
    _read_error_delay_s,
    _read_outage_breached,
)
from check_hl_account_state import make_read_only_info


def test_drawdown_stop_trips_only_beyond_limit():
    assert not _drawdown_breached(99.0, 100.0, 1.0)
    assert _drawdown_breached(98.99, 100.0, 1.0)
    assert not _drawdown_breached(98.0, 100.0, None)


def test_portfolio_drawdown_uses_combined_equity_not_worst_account():
    peak = 356.951051 + 328.448044
    equities = [355.051293, 324.904134]

    assert _drawdown_breached(equities[1], 328.448044, 1.0)
    assert not _portfolio_drawdown_breached(equities, peak, 1.0)


def test_decision_liveness_detects_missing_and_stale_logs(tmp_path):
    log = tmp_path / "eth.decisions.jsonl"
    assert _decision_liveness_breach(
        {"e3:ETH": log}, now=50, started_at=0,
        max_age_s=30, startup_grace_s=90,
    ) is None
    assert "missing" in _decision_liveness_breach(
        {"e3:ETH": log}, now=91, started_at=0,
        max_age_s=30, startup_grace_s=90,
    )

    log.write_text("{}\n")
    os.utime(log, (100, 100))
    assert _decision_liveness_breach(
        {"e3:ETH": log}, now=130, started_at=0,
        max_age_s=30, startup_grace_s=90,
    ) is None
    assert "stale" in _decision_liveness_breach(
        {"e3:ETH": log}, now=130.1, started_at=0,
        max_age_s=30, startup_grace_s=90,
    )


def test_oldest_order_age_uses_exchange_timestamp():
    orders = [{"timestamp": 100_000}, {"timestamp": 125_000}]
    assert _oldest_order_age_s(orders, now=161.0) == 61.0
    assert _oldest_order_age_s([], now=161.0) == 0.0


def test_read_errors_back_off_but_fail_closed_after_bounded_outage():
    assert [_read_error_delay_s(n, interval_s=10, max_delay_s=20) for n in range(1, 5)] == [10, 20, 20, 20]
    assert not _read_outage_breached(first_error_at=100, now=144.9, max_outage_s=45)
    assert _read_outage_breached(first_error_at=100, now=145, max_outage_s=45)


def test_read_only_info_skips_unused_metadata_requests():
    calls = []

    class FakeInfo:
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))

    result = make_read_only_info(FakeInfo, "https://example.invalid")

    assert isinstance(result, FakeInfo)
    assert calls == [(('https://example.invalid',), {
        "skip_ws": True,
        "meta": {"universe": []},
        "spot_meta": {"tokens": [], "universe": []},
    })]


def test_account_sample_scopes_positions_and_orders_to_requested_coins():
    class Info:
        def user_state(self, address):
            return {"assetPositions": [
                {"position": {"coin": "ENA", "szi": "39"}},
                {"position": {"coin": "ETH", "szi": "1"}},
            ]}

        def spot_user_state(self, address):
            return {
                "balances": [{"coin": "USDC", "total": "300"}],
                "tokenToAvailableAfterMaintenance": [[0, "250"]],
            }

        def open_orders(self, address):
            return [
                {"coin": "ENA", "timestamp": 99_000},
                {"coin": "ETH", "timestamp": 98_000},
            ]

    sample = _account_sample(
        Info(), "e2_mm1", "0xabc", {"ENA": "0.264"}, {"ENA"}, 3.0, 100.0,
    )

    assert sample["gross_notional"] == 39 * 0.264
    assert sample["open_orders"] == 1
    assert [position["coin"] for position in sample["positions"]] == ["ENA"]

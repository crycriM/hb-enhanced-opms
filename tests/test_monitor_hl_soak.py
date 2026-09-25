"""The live soak's equity stop uses exchange collateral, not keeper PnL."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from monitor_hl_soak import (
    _decision_liveness_breach,
    _drawdown_breached,
    _oldest_order_age_s,
    _portfolio_drawdown_breached,
)


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

"""The live soak's equity stop uses exchange collateral, not keeper PnL."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from monitor_hl_soak import _drawdown_breached, _portfolio_drawdown_breached


def test_drawdown_stop_trips_only_beyond_limit():
    assert not _drawdown_breached(99.0, 100.0, 1.0)
    assert _drawdown_breached(98.99, 100.0, 1.0)
    assert not _drawdown_breached(98.0, 100.0, None)


def test_portfolio_drawdown_uses_combined_equity_not_worst_account():
    peak = 356.951051 + 328.448044
    equities = [355.051293, 324.904134]

    assert _drawdown_breached(equities[1], 328.448044, 1.0)
    assert not _portfolio_drawdown_breached(equities, peak, 1.0)

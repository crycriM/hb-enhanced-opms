"""The live soak's equity stop uses exchange collateral, not keeper PnL."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from monitor_hl_soak import _drawdown_breached


def test_drawdown_stop_trips_only_beyond_limit():
    assert not _drawdown_breached(99.0, 100.0, 1.0)
    assert _drawdown_breached(98.99, 100.0, 1.0)
    assert not _drawdown_breached(98.0, 100.0, None)

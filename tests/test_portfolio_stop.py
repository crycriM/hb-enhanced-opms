import pytest

from mm_core.inventory import usdc_flat_targets
from opms.controllers.generic.portfolio_stop import PortfolioStopBook


MEMBERS = {("e2_mm1", "ETH"), ("e2_mm1", "SOL"),
           ("e2_mm2", "ETH"), ("e2_mm2", "SOL")}


def test_usdc_flat_targets_reduce_only_across_both_accounts():
    legs = {
        ("e2_mm1", "ETH"): (0.4, 3000.0),
        ("e2_mm1", "SOL"): (-4.0, 100.0),
        ("e2_mm2", "ETH"): (-0.3, 3000.0),
        ("e2_mm2", "SOL"): (2.0, 100.0),
    }
    targets = usdc_flat_targets(legs)
    assert sum(targets[key] * price for key, (_, price) in legs.items()) == pytest.approx(0)
    assert targets[("e2_mm1", "ETH")] == pytest.approx(0.4 - 100 / 3000)
    assert targets[("e2_mm2", "ETH")] == -0.3  # short side preserved
    assert all(abs(targets[key]) <= abs(pos) for key, (pos, _) in legs.items())
    assert usdc_flat_targets({("a", "ETH"): (1.0, 3000.0)}) == {("a", "ETH"): 0.0}
    with pytest.raises(ValueError):
        usdc_flat_targets({("a", "ETH"): (1.0, float("nan"))})


def test_stop_requires_fresh_four_leg_book_and_emergency_zeros_all(tmp_path):
    book1 = PortfolioStopBook(str(tmp_path / "portfolio.db"), MEMBERS)
    book2 = PortfolioStopBook(str(tmp_path / "portfolio.db"), MEMBERS)
    assert book1.update("e2_mm1", "ETH", 0.4, 3000, "stop", now=100) == ("hold", 0.4)
    book1.update("e2_mm1", "SOL", -4, 100, "quote", now=100)
    book2.update("e2_mm2", "ETH", -0.3, 3000, "quote", now=100)
    mode, target = book2.update("e2_mm2", "SOL", 2, 100, "quote", now=100)
    assert mode == "stop"
    assert target == pytest.approx(2.0)
    assert book1.update("e2_mm1", "ETH", 0.4, 3000, "stop", now=121) == ("hold", 0.4)
    assert book2.update("e2_mm2", "ETH", -0.3, 3000, "emergency", now=122) == ("emergency", 0.0)
    assert book1.update("e2_mm1", "SOL", -4, 100, "quote", now=122) == ("emergency", 0.0)

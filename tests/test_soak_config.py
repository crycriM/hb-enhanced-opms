from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "deploy/hummingbot/conf/controllers"


def _config(account: str, coin: str) -> dict:
    path = CONFIG_DIR / f"perp_mm_{account}_{coin.lower()}_soak.yml"
    return yaml.safe_load(path.read_text())


def test_dual_soak_profile_fits_account_margin_budget_at_critical_caps():
    conservative_prices = {"ETH": 3_000.0, "SOL": 150.0}
    expected = {
        "e2_mm1": {"ETH": (0.1, 0.1, 0.125), "SOL": (-1.0, 1.0, 1.25)},
        "e3_sub1": {"ETH": (-0.1, 0.1, 0.125), "SOL": (1.0, 1.0, 1.25)},
    }

    for account, coins in expected.items():
        critical_notional = 0.0
        for coin, (target, soft_cap, hard_cap) in coins.items():
            cfg = _config(account, coin)
            assert (cfg["target_inventory"], cfg["max_position"], cfg["critical_position"]) == (
                target, soft_cap, hard_cap,
            )
            assert hard_cap > abs(target)
            critical_notional += (abs(target) + hard_cap) * conservative_prices[coin]

        initial_margin_ratio = critical_notional / 6 / 300
        assert initial_margin_ratio <= 0.75

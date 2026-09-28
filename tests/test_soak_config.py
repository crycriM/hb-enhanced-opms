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


def test_single_ena_override_profile_is_minimum_size_and_flat():
    cfg = _config("e2_mm1", "ENA")

    assert cfg["trading_pair"] == "ENA-USD"
    assert cfg["account_id"] == "e2_mm1"
    assert cfg["target_inventory"] == 0.0
    assert cfg["quote_size"] == 39.0
    assert cfg["price_tick"] == 0.00001
    assert cfg["max_position"] == 195.0
    assert cfg["critical_position"] == 390.0
    assert cfg["gamma"] == 5.0
    assert cfg["kappa"] == 20_000.0
    assert cfg["leverage"] == 3
    assert cfg["regime_stop"] is True
    assert cfg["toxic_markout_bps"] == -1.0
    assert cfg["update_interval"] == 5.0
    assert cfg["quote_refresh_interval"] == 10.0

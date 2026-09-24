"""Validate both real-Hummingbot OPMS shadow configurations without network I/O.

Run from the Hummingbot checkout after ``deploy/install_into_hummingbot.sh``::

    python scripts/validate_hb_deploy_configs.py

This uses Hummingbot's own config loader, then applies the OPMS launcher's
topology and credential-routing checks with non-secret address values from the
monorepo environment. It does not start connectors or place orders.
"""

import os
import sys
from pathlib import Path

import yaml
from dotenv import dotenv_values

sys.path.insert(0, str(Path.cwd()))

from opms.connectors.topology import validate_controller_topology
from scripts.opms_perp_mm import OpmsPerpMMConfig, _check_account_routing


class _Connector:
    def __init__(self, address: str):
        self.hyperliquid_perpetual_address = address


def main() -> int:
    root = Path.cwd()
    env_file = Path(os.environ.get(
        "OPMS_ENV_FILE",
        str(Path(__file__).resolve().parents[4] / ".env"),
    ))
    env = {**dotenv_values(env_file), **os.environ}
    configs = (
        "opms_perp_mm_e2_mm1_shadow.yml",
        "opms_perp_mm_e3_sub1_shadow.yml",
    )
    expected_targets = {
        "e2_mm1": {"ETH-USD": 0.4, "SOL-USD": -4.0},
        "e3_sub1": {"ETH-USD": -0.4, "SOL-USD": 4.0},
    }
    expected_leverage = 6
    loaded = []
    account_addresses = {}
    for script_config in configs:
        path = root / "conf" / "scripts" / script_config
        data = yaml.safe_load(path.read_text())
        cfg = OpmsPerpMMConfig(**data)
        controllers = cfg.load_controller_configs()
        validate_controller_topology(controllers)
        if len(controllers) != 2:
            raise ValueError(f"{script_config}: expected two controllers")
        account = controllers[0].account_id
        targets = {c.trading_pair: c.target_inventory for c in controllers}
        if targets != expected_targets[account]:
            raise ValueError(f"{script_config}: targets {targets!r}")
        if not all(c.leverage == expected_leverage for c in controllers):
            raise ValueError(f"{script_config}: expected {expected_leverage}x leverage")
        if not all(c.shadow_mode and c.update_interval == 5.0 for c in controllers):
            raise ValueError(f"{script_config}: expected 5s shadow controllers")
        address = env.get(f"HYPERLIQUID_{account.upper()}_ACCOUNT_ADDRESS")
        if not address:
            raise ValueError(f"missing HYPERLIQUID_{account.upper()}_ACCOUNT_ADDRESS")
        account_addresses[account] = address.lower()
        for controller in controllers:
            _check_account_routing(controller, _Connector(address), env)
        loaded.extend(controllers)
        print(f"{script_config}: {account}, {len(controllers)} controllers, routing OK")

    if {c.id for c in loaded} != {
        "perp_mm_e2_mm1_eth", "perp_mm_e2_mm1_sol",
        "perp_mm_e3_sub1_eth", "perp_mm_e3_sub1_sol",
    }:
        raise ValueError("controller ids are not unique across the two instances")
    if len(account_addresses) != 2 or len(set(account_addresses.values())) != 2:
        raise ValueError("e2_mm1 and e3_sub1 must use distinct HL account addresses")
    print("dual deploy config validation passed (no network, no orders)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Validate the single-account live soak through Hummingbot's config loader.

This performs no network I/O and places no orders.  It is run from a disposable
Hummingbot runtime after the soak files have been copied into ``conf/``.
"""

from __future__ import annotations

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
    if str(env.get("HYPERLIQUID_E2_MM1_IS_TESTNET", "")).lower() != "false":
        raise ValueError("soak must target a mainnet credential (HYPERLIQUID_E2_MM1_IS_TESTNET=false)")
    address = env.get("HYPERLIQUID_E2_MM1_ACCOUNT_ADDRESS")
    if not address:
        raise ValueError("missing HYPERLIQUID_E2_MM1_ACCOUNT_ADDRESS")

    script_name = "opms_perp_mm_e2_mm1_soak.yml"
    path = root / "conf" / "scripts" / script_name
    cfg = OpmsPerpMMConfig(**yaml.safe_load(path.read_text()))
    controllers = cfg.load_controller_configs()
    validate_controller_topology(controllers)
    if len(controllers) != 2:
        raise ValueError(f"{script_name}: expected ETH and SOL controllers")
    if {c.id for c in controllers} != {
        "perp_mm_e2_mm1_eth_soak",
        "perp_mm_e2_mm1_sol_soak",
    }:
        raise ValueError("unexpected soak controller ids")
    expected = {"ETH-USD": 0.4, "SOL-USD": -4.0}
    targets = {c.trading_pair: c.target_inventory for c in controllers}
    if targets != expected:
        raise ValueError(f"unexpected soak targets: {targets!r}")
    if any(c.account_id != "e2_mm1" for c in controllers):
        raise ValueError("soak must use e2_mm1 for every controller")
    if any(c.leverage != 6 for c in controllers):
        raise ValueError("soak must request 6x leverage for every controller")
    if any(c.shadow_mode or c.update_interval != 5.0 for c in controllers):
        raise ValueError("soak controllers must be live at a 5s cadence")
    for controller in controllers:
        _check_account_routing(controller, _Connector(address), env)
    print(f"{script_name}: e2_mm1, 2 live controllers, routing OK, 6x")
    print("single-account soak config validation passed (no network, no orders)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

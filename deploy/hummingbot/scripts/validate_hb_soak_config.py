"""Validate a live soak account through Hummingbot's config loader.

This performs no network I/O and places no orders.  It is run from a disposable
Hummingbot runtime after the soak files have been copied into ``conf/``.
"""

from __future__ import annotations

import argparse
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", choices=("e2_mm1", "e3_sub1"), default="e2_mm1")
    args = parser.parse_args()
    account = args.account_id
    root = Path.cwd()
    env_file = Path(os.environ.get(
        "OPMS_ENV_FILE",
        str(Path(__file__).resolve().parents[4] / ".env"),
    ))
    env = {**dotenv_values(env_file), **os.environ}
    prefix = f"HYPERLIQUID_{account.upper()}"
    if str(env.get(f"{prefix}_IS_TESTNET", "")).lower() != "false":
        raise ValueError(f"soak must target a mainnet credential ({prefix}_IS_TESTNET=false)")
    address = env.get(f"{prefix}_ACCOUNT_ADDRESS")
    if not address:
        raise ValueError(f"missing {prefix}_ACCOUNT_ADDRESS")

    script_name = f"opms_perp_mm_{account}_soak.yml"
    path = root / "conf" / "scripts" / script_name
    cfg = OpmsPerpMMConfig(**yaml.safe_load(path.read_text()))
    controllers = cfg.load_controller_configs()
    validate_controller_topology(controllers)
    if len(controllers) != 2:
        raise ValueError(f"{script_name}: expected ETH and SOL controllers")
    if {c.id for c in controllers} != {
        f"perp_mm_{account}_eth_soak",
        f"perp_mm_{account}_sol_soak",
    }:
        raise ValueError("unexpected soak controller ids")
    expected = ({"ETH-USD": 0.4, "SOL-USD": -4.0} if account == "e2_mm1"
                else {"ETH-USD": -0.4, "SOL-USD": 4.0})
    targets = {c.trading_pair: c.target_inventory for c in controllers}
    if targets != expected:
        raise ValueError(f"unexpected soak targets: {targets!r}")
    if any(c.account_id != account for c in controllers):
        raise ValueError(f"soak must use {account} for every controller")
    if any(c.leverage != 6 for c in controllers):
        raise ValueError("soak must request 6x leverage for every controller")
    if any(c.regime_stop for c in controllers):
        raise ValueError("soak must have the regime gate disabled for every controller")
    if any(c.toxic_markout_bps is not None for c in controllers):
        raise ValueError("soak must have toxic-markout widening disabled")
    if any(c.shadow_mode or c.update_interval != 5.0 for c in controllers):
        raise ValueError("soak controllers must be live at a 5s cadence")
    for controller in controllers:
        _check_account_routing(controller, _Connector(address), env)
    print(f"{script_name}: {account}, 2 live controllers, routing OK, 6x")
    print("soak config validation passed (no network, no orders)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

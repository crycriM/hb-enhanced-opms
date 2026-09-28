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
    parser.add_argument("--script-config")
    parser.add_argument("--coins", nargs="+", default=["ETH", "SOL"])
    parser.add_argument("--leverage", type=int, default=6)
    parser.add_argument("--flat-targets", action="store_true")
    parser.add_argument("--risk-gates", choices=("enabled", "disabled"), default="disabled")
    args = parser.parse_args()
    account = args.account_id
    coins = [coin.upper() for coin in args.coins]
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

    script_name = args.script_config or f"opms_perp_mm_{account}_soak.yml"
    path = root / "conf" / "scripts" / script_name
    cfg = OpmsPerpMMConfig(**yaml.safe_load(path.read_text()))
    controllers = cfg.load_controller_configs()
    validate_controller_topology(controllers)
    if len(controllers) != len(coins):
        raise ValueError(f"{script_name}: expected {len(coins)} controllers")
    if {c.id for c in controllers} != {
        f"perp_mm_{account}_{coin.lower()}_soak" for coin in coins
    }:
        raise ValueError("unexpected soak controller ids")
    expected_pairs = {f"{coin}-USD" for coin in coins}
    if {c.trading_pair for c in controllers} != expected_pairs:
        raise ValueError("unexpected soak trading pairs")
    targets = {c.trading_pair: c.target_inventory for c in controllers}
    if args.flat_targets:
        if any(abs(target) > 1e-12 for target in targets.values()):
            raise ValueError(f"single-market soak targets must be flat: {targets!r}")
    else:
        expected = ({"ETH-USD": 0.1, "SOL-USD": -1.0} if account == "e2_mm1"
                    else {"ETH-USD": -0.1, "SOL-USD": 1.0})
        if targets != expected:
            raise ValueError(f"unexpected soak targets: {targets!r}")
    if any(c.account_id != account for c in controllers):
        raise ValueError(f"soak must use {account} for every controller")
    if any(c.leverage != args.leverage for c in controllers):
        raise ValueError(f"soak must request {args.leverage}x leverage for every controller")
    if args.risk_gates == "enabled":
        if any(not c.regime_stop or c.toxic_markout_bps is None for c in controllers):
            raise ValueError("soak must enable regime and toxic-markout gates")
    else:
        if any(c.regime_stop for c in controllers):
            raise ValueError("soak must have the regime gate disabled for every controller")
        if any(c.toxic_markout_bps is not None for c in controllers):
            raise ValueError("soak must have toxic-markout widening disabled")
    if any(c.shadow_mode or c.update_interval != 5.0 for c in controllers):
        raise ValueError("soak controllers must be live at a 5s cadence")
    for controller in controllers:
        _check_account_routing(controller, _Connector(address), env)
    print(f"{script_name}: {account}, {len(controllers)} live controller(s), "
          f"routing OK, {args.leverage}x")
    print("soak config validation passed (no network, no orders)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

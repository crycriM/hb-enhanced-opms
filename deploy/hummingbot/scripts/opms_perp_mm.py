"""OPMS perp market-making launcher: HB's V2WithControllers plus the deploy-time
checks Hummingbot does not do.

HB's `add_controller()` catches constructor errors and only logs them, so a
misconfigured controller would silently not run. This script validates every
controller config *before* any controller is created, and refuses to start on:

  * a topology violation — two controllers on one coin/account of a net venue
    (`opms.connectors.topology.validate_controller_topology`);
  * credential misrouting — HB has a single `hyperliquid_perpetual` credential
    slot (last import wins), so the connector's address must equal the
    controller account's `HYPERLIQUID_<ACCOUNT_ID>_ACCOUNT_ADDRESS`.

Launch (from the HB root, inside the hummingbot conda env):
  CONFIG_PASSWORD=... SCRIPT_CONFIG=opms_perp_mm_e2_mm1_shadow.yml \
  python <amm-solution>/hb-enhanced-opms/deploy/hummingbot/scripts/run_hummingbot_isolated.py

The isolated launcher intentionally bypasses the stock headless loop's MQTT
requirement; process signals remain the deployment control plane.
"""

import os
from pathlib import Path

from dotenv import dotenv_values

import opms
import scripts.v2_with_controllers as v2  # module alias: HB picks the strategy/config classes via inspect.getmembers
from opms.connectors.topology import validate_controller_topology

# The monorepo .env (AGENTS.md credential convention). Read, never exported:
# the HB process has no business holding other accounts' keys in os.environ.
_ENV_FILE = Path(os.environ.get("OPMS_ENV_FILE", Path(opms.__file__).resolve().parents[3] / ".env"))


class OpmsPerpMMConfig(v2.V2WithControllersConfig):
    script_file_name: str = os.path.basename(__file__)


class OpmsPerpMM(v2.V2WithControllers):
    def __init__(self, connectors, config: OpmsPerpMMConfig):
        controller_configs = config.load_controller_configs()
        validate_controller_topology(controller_configs)
        env = {**dotenv_values(_ENV_FILE), **os.environ}
        for cfg in controller_configs:
            _check_account_routing(cfg, connectors[cfg.connector_name], env)
        super().__init__(connectors, config)


def _check_account_routing(cfg, connector, env) -> None:
    var = f"HYPERLIQUID_{cfg.account_id.upper()}_ACCOUNT_ADDRESS"
    expected = env.get(var)
    # ponytail: HL connectors only; any other connector has no such attribute and fails closed until mapped
    actual = getattr(connector, "hyperliquid_perpetual_address", None)
    if not expected:
        raise ValueError(f"controller {cfg.id}: {var} not set — cannot verify credential routing")
    if not actual or actual.lower() != expected.lower():
        raise ValueError(
            f"controller {cfg.id} declares account {cfg.account_id} but the {cfg.connector_name} "
            f"credentials route to {actual!r}; re-run import_hl_mainnet_credentials.py --account-id {cfg.account_id}"
        )

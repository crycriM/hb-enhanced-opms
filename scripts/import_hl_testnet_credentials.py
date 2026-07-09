"""One-off import of Hyperliquid testnet credentials into Hummingbot's
encrypted connector store, so PerpMMController can trade L4TEST_MAIN without
an interactive `connect hyperliquid_perpetual_testnet` session.

Must run inside the Hummingbot container/conda env (imports hummingbot.*).
Reads secrets only from environment variables — never pass them as CLI args
(they'd land in shell history / process listings).

Required env vars:
  HYPERLIQUID_L4TEST_MAIN_PRIVATE_KEY
  HYPERLIQUID_L4TEST_MAIN_ACCOUNT_ADDRESS
  HB_PASSWORD          # encrypts the above at rest in conf/connectors/;
                        # operator-chosen, unrelated to the trading key.

Usage (inside the container):
  HB_PASSWORD=... HYPERLIQUID_L4TEST_MAIN_PRIVATE_KEY=... \\
  HYPERLIQUID_L4TEST_MAIN_ACCOUNT_ADDRESS=... \\
  python import_hl_testnet_credentials.py
"""

import argparse
import os
import sys

REQUIRED_VARS = (
    "HYPERLIQUID_L4TEST_MAIN_PRIVATE_KEY",
    "HYPERLIQUID_L4TEST_MAIN_ACCOUNT_ADDRESS",
    "HB_PASSWORD",
)


def _read_env() -> dict[str, str]:
    missing = [v for v in REQUIRED_VARS if not os.environ.get(v)]
    if missing:
        raise SystemExit(f"Missing required env vars: {', '.join(missing)}")
    return {v: os.environ[v] for v in REQUIRED_VARS}


def import_credentials(env: dict[str, str]) -> None:
    from hummingbot.client.config.config_crypt import ETHKeyFileSecretManger, store_password_verification
    from hummingbot.client.config.config_helpers import ClientConfigAdapter
    from hummingbot.client.config.security import Security
    from hummingbot.connector.derivative.hyperliquid_perpetual.hyperliquid_perpetual_utils import (
        HyperliquidPerpetualTestnetConfigMap,
    )

    secrets_manager = ETHKeyFileSecretManger(env["HB_PASSWORD"])
    if Security.new_password_required():
        store_password_verification(secrets_manager)
    if not Security.login(secrets_manager):
        raise SystemExit("HB_PASSWORD does not match the existing conf/.password_verification")

    config_map = ClientConfigAdapter(HyperliquidPerpetualTestnetConfigMap(
        hyperliquid_perpetual_testnet_mode="arb_wallet",
        use_vault=False,
        hyperliquid_perpetual_testnet_address=env["HYPERLIQUID_L4TEST_MAIN_ACCOUNT_ADDRESS"],
        hyperliquid_perpetual_testnet_secret_key=env["HYPERLIQUID_L4TEST_MAIN_PRIVATE_KEY"],
    ))
    Security.update_secure_config(config_map)
    print("hyperliquid_perpetual_testnet credentials imported for L4TEST_MAIN.")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                     help="Only check required env vars are set; don't touch hummingbot or the encrypted store")
    args = ap.parse_args()

    env = _read_env()
    if args.dry_run:
        print("dry-run OK: all required env vars are present.")
        return
    import_credentials(env)


if __name__ == "__main__":
    sys.exit(main())

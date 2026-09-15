"""Import Hyperliquid **mainnet** credentials into Hummingbot's encrypted store.

Unlike the testnet-only importer, this handles the two mainnet shapes that
matter after the subaccount work (see `perp-bot/docs/account-naming.md`):

  * **master** — the agent/API key plus the master address, ``use_vault=False``;
  * **subaccount** — the *same* master-approved agent key, but with
    ``use_vault=True`` and the address set to the subaccount, so every signed
    request carries ``vaultAddress=<subaccount>``.

Both are written to HB's single ``hyperliquid_perpetual`` connector slot, so a
subaccount import **overwrites** a previous master/subaccount import. Running
two subaccounts concurrently therefore needs two separate HB instances (or a
future connector-name split) — Hummingbot keys credentials by connector name,
not by account id.

Must run inside the Hummingbot conda env (imports ``hummingbot.*``). Secrets are
read only from environment variables, never CLI args (shell history / ps).

Required env vars:
  HB_PASSWORD
  HYPERLIQUID_<ACCOUNT_ID>_ACCOUNT_ADDRESS
  HYPERLIQUID_<ACCOUNT_ID>_PRIVATE_KEY

Optional:
  HYPERLIQUID_MASTER_ACCOUNT_ADDRESS   # auto-detects subaccount -> use_vault (falls back to HYPERLIQUID_E2_MAIN_ACCOUNT_ADDRESS)
  HB_USE_VAULT=yes|no                  # explicit override of the auto-detection

Usage (inside the HB env):
  HB_PASSWORD=... python import_hl_mainnet_credentials.py --account-id e2_mm1
  python import_hl_mainnet_credentials.py --account-id e2_mm1 --dry-run
"""

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")  # monorepo credential store; never overrides the shell

MASTER_ADDRESS_VAR = "HYPERLIQUID_MASTER_ACCOUNT_ADDRESS"


def _account_env(account_id: str, suffix: str) -> str | None:
    return os.environ.get(f"HYPERLIQUID_{account_id.upper()}_{suffix}")


def _resolve(account_id: str, use_vault_override: str | None) -> dict:
    address = _account_env(account_id, "ACCOUNT_ADDRESS")
    private_key = _account_env(account_id, "PRIVATE_KEY")
    if not address or not private_key:
        raise SystemExit(
            f"Missing HYPERLIQUID_{account_id.upper()}_ACCOUNT_ADDRESS / _PRIVATE_KEY"
        )
    if not os.environ.get("HB_PASSWORD"):
        raise SystemExit("Missing HB_PASSWORD (encrypts the credential store)")

    master = (os.environ.get(MASTER_ADDRESS_VAR) or _account_env("e2_main", "ACCOUNT_ADDRESS") or "").lower()
    if use_vault_override is not None:
        use_vault = use_vault_override.strip().lower() in {"yes", "y", "true", "1"}
    elif master:
        use_vault = address.lower() != master
    else:
        # No master to compare against: default to vault routing only if the
        # operator explicitly asks, so a bare run can't silently misroute.
        raise SystemExit(
            f"Set {MASTER_ADDRESS_VAR} (to auto-detect master vs subaccount) or "
            f"HB_USE_VAULT=yes|no explicitly."
        )
    return {"account_id": account_id, "address": address.lower(), "private_key": private_key,
            "use_vault": use_vault}


def import_credentials(env: dict) -> None:
    from hummingbot.client.config.config_crypt import ETHKeyFileSecretManger, store_password_verification
    from hummingbot.client.config.config_helpers import ClientConfigAdapter
    from hummingbot.client.config.security import Security
    from hummingbot.connector.derivative.hyperliquid_perpetual.hyperliquid_perpetual_utils import (
        HyperliquidPerpetualConfigMap,
    )

    secrets_manager = ETHKeyFileSecretManger(os.environ["HB_PASSWORD"])
    if Security.new_password_required():
        store_password_verification(secrets_manager)
    if not Security.login(secrets_manager):
        raise SystemExit("HB_PASSWORD does not match the existing conf/.password_verification")

    config_map = ClientConfigAdapter(HyperliquidPerpetualConfigMap(
        hyperliquid_perpetual_mode="api_wallet",
        use_vault=env["use_vault"],
        hyperliquid_perpetual_address=env["address"],
        hyperliquid_perpetual_secret_key=env["private_key"],
    ))
    Security.update_secure_config(config_map)
    role = "subaccount (vaultAddress)" if env["use_vault"] else "master"
    print(f"hyperliquid_perpetual credentials imported for {env['account_id']} as {role}.")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--account-id", required=True,
                    help="e2_main (master) or e2_mm1 / e2_mm2 (subaccounts)")
    ap.add_argument("--use-vault", choices=["yes", "no"], default=None,
                    help="Override the master/subaccount auto-detection")
    ap.add_argument("--dry-run", action="store_true",
                    help="Validate env + resolution only; do not touch hummingbot/store")
    args = ap.parse_args()

    env = _resolve(args.account_id, args.use_vault)
    if args.dry_run:
        print(f"dry-run OK: account_id={env['account_id']} use_vault={env['use_vault']} "
              f"address={env['address'][:8]}..{env['address'][-4:]}")
        return
    import_credentials(env)


if __name__ == "__main__":
    main()

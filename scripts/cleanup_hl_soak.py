"""Cancel and flatten only the ETH/SOL state created by a clean soak run."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from check_hl_account_state import REPO_ROOT, resolve_account, snapshot_account
from dotenv import load_dotenv

load_dotenv(REPO_ROOT / ".env")


def _exchange(account_id: str, address: str):
    from eth_account import Account
    from hyperliquid.exchange import Exchange
    from hyperliquid.utils import constants

    private_key = os.environ.get(f"HYPERLIQUID_{account_id.upper()}_PRIVATE_KEY")
    if not private_key:
        raise SystemExit(f"missing HYPERLIQUID_{account_id.upper()}_PRIVATE_KEY")
    master = (os.environ.get("HYPERLIQUID_MASTER_ACCOUNT_ADDRESS")
              or os.environ.get("HYPERLIQUID_E2_MAIN_ACCOUNT_ADDRESS") or "").lower()
    wallet = Account.from_key(private_key)
    if address == master:
        return Exchange(wallet=wallet, base_url=constants.MAINNET_API_URL)
    if not master:
        raise SystemExit("missing master address for vault cleanup")
    return Exchange(wallet=wallet, base_url=constants.MAINNET_API_URL,
                    account_address=master, vault_address=address)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--account-id", default="e2_mm1")
    ap.add_argument("--coins", nargs="+", default=["ETH", "SOL"])
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--output")
    ap.add_argument("--allow-cleanup", action="store_true",
                    help="required because this command signs cancellations/market closes")
    args = ap.parse_args(argv)
    if not args.allow_cleanup:
        raise SystemExit("refusing cleanup without --allow-cleanup")
    if os.environ.get("OPMS_HB_MAINNET") != "confirm":
        raise SystemExit("refusing cleanup without OPMS_HB_MAINNET=confirm")
    if os.environ.get("OPMS_HB_PLACE_ORDERS") != "confirm":
        raise SystemExit("refusing cleanup without OPMS_HB_PLACE_ORDERS=confirm")

    from hyperliquid.info import Info
    from hyperliquid.utils import constants

    address, is_testnet = resolve_account(args.account_id)
    if str(is_testnet).lower() != "false":
        raise SystemExit("cleanup refuses a non-mainnet credential environment")
    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    exchange = _exchange(args.account_id, address)
    coins = set(args.coins)
    report = {"account_id": args.account_id, "address": f"{address[:8]}..{address[-4:]}",
              "coins": sorted(coins), "actions": [], "failures": []}

    def cancel_open_orders() -> None:
        for order in info.open_orders(address):
            if order.get("coin") not in coins:
                continue
            oid = int(order["oid"])
            coin = order["coin"]
            try:
                response = exchange.cancel(coin, oid)
                report["actions"].append({"action": "cancel", "coin": coin,
                                          "oid": oid, "response": str(response)})
            except Exception as exc:  # continue to attempt other scoped cleanup
                report["failures"].append(f"cancel {coin}/{oid}: {type(exc).__name__}: {exc}")

    def close_positions() -> None:
        state = info.user_state(address)
        for entry in state.get("assetPositions", []):
            position = entry.get("position", {})
            coin = position.get("coin")
            if coin not in coins or abs(float(position.get("szi", 0.0))) <= 1e-12:
                continue
            try:
                response = exchange.market_close(coin)
                report["actions"].append({"action": "market_close", "coin": coin,
                                          "szi": position.get("szi"),
                                          "response": str(response)})
            except Exception as exc:
                report["failures"].append(f"market_close {coin}: {type(exc).__name__}: {exc}")

    deadline = time.time() + args.timeout
    while time.time() < deadline:
        cancel_open_orders()
        close_positions()
        current = snapshot_account(info, address, coins)
        if not current["orders"] and not current["positions"]:
            report["final_state"] = current
            report["passed"] = not report["failures"]
            break
        time.sleep(2.0)
    else:
        report["final_state"] = snapshot_account(info, address, coins)
        report["failures"].append("scoped orders or positions remained after cleanup timeout")
        report["passed"] = False

    encoded = json.dumps(report, indent=2, sort_keys=True)
    print(encoded)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(encoded + "\n")
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    sys.exit(main())

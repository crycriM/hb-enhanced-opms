"""Read and optionally validate the scoped Hyperliquid account state.

The command is read-only.  It reports only public account state and a redacted
address; private keys are never read or printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")


def resolve_account(account_id: str) -> tuple[str, str | None]:
    address = os.environ.get(f"HYPERLIQUID_{account_id.upper()}_ACCOUNT_ADDRESS")
    if not address:
        raise SystemExit(f"missing HYPERLIQUID_{account_id.upper()}_ACCOUNT_ADDRESS")
    return address.lower(), os.environ.get(f"HYPERLIQUID_{account_id.upper()}_IS_TESTNET")


def snapshot_account(info, address: str, coins: set[str]) -> dict:
    state = info.user_state(address)
    spot = info.spot_user_state(address)
    orders = [
        {
            "oid": int(order["oid"]),
            "coin": order.get("coin"),
            "side": order.get("side"),
            "sz": order.get("sz"),
            "limitPx": order.get("limitPx"),
            "orderType": order.get("orderType"),
            "reduceOnly": bool(order.get("reduceOnly", False)),
        }
        for order in info.open_orders(address)
        if order.get("coin") in coins
    ]
    positions = []
    for entry in state.get("assetPositions", []):
        position = entry.get("position", {})
        coin = position.get("coin")
        if coin not in coins:
            continue
        szi = float(position.get("szi", 0.0))
        if abs(szi) > 1e-12:
            positions.append({
                "coin": coin,
                "szi": position.get("szi"),
                "entryPx": position.get("entryPx"),
                "unrealizedPnl": position.get("unrealizedPnl"),
            })
    summary = state.get("marginSummary", {})
    spot_usdc = next((b for b in spot.get("balances", []) if b.get("coin") == "USDC"), {})
    available = dict((int(token), value)
                     for token, value in spot.get("tokenToAvailableAfterMaintenance", []))
    return {
        "address": f"{address[:8]}..{address[-4:]}",
        "account_value": summary.get("accountValue"),
        "total_margin_used": summary.get("totalMarginUsed"),
        "total_notional": summary.get("totalNtlPos"),
        "withdrawable": state.get("withdrawable"),
        "spot_usdc_total": spot_usdc.get("total"),
        "spot_usdc_hold": spot_usdc.get("hold"),
        "available_after_maintenance": available.get(0),
        "orders": orders,
        "positions": positions,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--account-id", default="e2_mm1")
    ap.add_argument("--coins", nargs="+", default=["ETH", "SOL"])
    ap.add_argument("--output")
    ap.add_argument("--require-clean", action="store_true")
    ap.add_argument("--min-equity", type=float, default=0.0)
    args = ap.parse_args(argv)

    from hyperliquid.info import Info
    from hyperliquid.utils import constants

    address, is_testnet = resolve_account(args.account_id)
    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    result = {
        "account_id": args.account_id,
        "is_testnet_env": is_testnet,
        "coins": args.coins,
        "state": snapshot_account(info, address, set(args.coins)),
    }
    state = result["state"]
    try:
        equity = float(state.get("spot_usdc_total") or state.get("account_value") or 0.0)
    except (TypeError, ValueError):
        equity = 0.0
    failures: list[str] = []
    if str(is_testnet).lower() != "false":
        failures.append("credential environment is not explicitly mainnet")
    if equity < args.min_equity:
        failures.append(f"equity ${equity:.2f} is below required ${args.min_equity:.2f}")
    if args.require_clean:
        if state["orders"]:
            failures.append(f"{len(state['orders'])} scoped open order(s) already exist")
        if state["positions"]:
            failures.append(f"scoped positions already exist: {state['positions']}")
    result["equity_used_for_check"] = equity
    result["failures"] = failures
    result["passed"] = not failures
    encoded = json.dumps(result, indent=2, sort_keys=True)
    print(encoded)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(encoded + "\n")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())

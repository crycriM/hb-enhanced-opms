"""Read-only risk monitor for a bounded Hyperliquid soak process."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

from check_hl_account_state import REPO_ROOT, resolve_account
from dotenv import load_dotenv

load_dotenv(REPO_ROOT / ".env")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _drawdown_breached(equity: float, peak_equity: float, limit_pct: float | None) -> bool:
    return (limit_pct is not None and peak_equity > 0
            and (peak_equity - equity) / peak_equity * 100 > limit_pct)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--account-id", default="e2_mm1")
    ap.add_argument("--pid", type=int, required=True)
    ap.add_argument("--duration", type=float, required=True)
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--leverage", type=float, default=6.0)
    ap.add_argument("--min-margin-health-ratio", type=float, default=0.15)
    ap.add_argument("--max-initial-margin-ratio", type=float, default=0.85)
    ap.add_argument("--max-drawdown-pct", type=float)
    ap.add_argument("--output", required=True)
    args = ap.parse_args(argv)
    if args.max_drawdown_pct is not None and not 0 < args.max_drawdown_pct <= 100:
        ap.error("--max-drawdown-pct must be in (0, 100]")

    from hyperliquid.info import Info
    from hyperliquid.utils import constants

    address, is_testnet = resolve_account(args.account_id)
    if str(is_testnet).lower() != "false":
        raise SystemExit("monitor refuses a non-mainnet credential environment")
    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    coins = {"ETH", "SOL"}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + args.duration + 90.0
    consecutive_errors = 0
    peak_equity = 0.0

    with output.open("w") as stream:
        while _alive(args.pid) and time.time() < deadline:
            try:
                state = info.user_state(address)
                spot = info.spot_user_state(address)
                mids = info.all_mids()
                spot_usdc = next((b for b in spot.get("balances", [])
                                  if b.get("coin") == "USDC"), {})
                equity = float(spot_usdc.get("total") or 0.0)
                peak_equity = max(peak_equity, equity)
                available = dict((int(token), value)
                                 for token, value in spot.get("tokenToAvailableAfterMaintenance", []))
                margin_available = float(available.get(0) or 0.0)
                positions = []
                gross_notional = 0.0
                for entry in state.get("assetPositions", []):
                    position = entry.get("position", {})
                    coin = position.get("coin")
                    if coin not in coins:
                        continue
                    szi = float(position.get("szi", 0.0))
                    if abs(szi) <= 1e-12:
                        continue
                    mid = float(mids.get(coin, 0.0) or 0.0)
                    gross_notional += abs(szi) * mid
                    positions.append({"coin": coin, "szi": szi, "mid": mid})
                open_orders = [o for o in info.open_orders(address) if o.get("coin") in coins]
                initial_margin = gross_notional / args.leverage
                health_ratio = margin_available / equity if equity > 0 else 0.0
                initial_margin_ratio = initial_margin / equity if equity > 0 else 1.0
                sample = {
                    "ts": time.time(),
                    "equity": equity,
                    "margin_available": margin_available,
                    "margin_health_ratio": health_ratio,
                    "gross_notional": gross_notional,
                    "initial_margin": initial_margin,
                    "initial_margin_ratio": initial_margin_ratio,
                    "open_orders": len(open_orders),
                    "positions": positions,
                }
                stream.write(json.dumps(sample) + "\n")
                stream.flush()
                print(json.dumps(sample), flush=True)
                consecutive_errors = 0
                breach = None
                if equity <= 0:
                    breach = "equity is zero or unavailable"
                elif health_ratio < args.min_margin_health_ratio:
                    breach = (f"margin health {health_ratio:.3f} < "
                              f"{args.min_margin_health_ratio:.3f}")
                elif initial_margin_ratio > args.max_initial_margin_ratio:
                    breach = (f"initial margin ratio {initial_margin_ratio:.3f} > "
                              f"{args.max_initial_margin_ratio:.3f}")
                elif _drawdown_breached(equity, peak_equity, args.max_drawdown_pct):
                    breach = (f"equity drawdown "
                              f"{(peak_equity - equity) / peak_equity * 100:.2f}% > "
                              f"{args.max_drawdown_pct:.2f}%")
                if breach:
                    event = {"ts": time.time(), "event": "risk_breach", "reason": breach}
                    stream.write(json.dumps(event) + "\n")
                    stream.flush()
                    print(json.dumps(event), flush=True)
                    try:
                        os.kill(args.pid, signal.SIGINT)
                    except ProcessLookupError:
                        pass
                    return 1
            except Exception as exc:  # fail closed after repeated read failures
                consecutive_errors += 1
                event = {"ts": time.time(), "event": "read_error",
                         "error": f"{type(exc).__name__}: {exc}",
                         "consecutive": consecutive_errors}
                stream.write(json.dumps(event) + "\n")
                stream.flush()
                print(json.dumps(event), flush=True)
                if consecutive_errors >= 3:
                    try:
                        os.kill(args.pid, signal.SIGINT)
                    except ProcessLookupError:
                        pass
                    return 1
            time.sleep(args.interval)
    if time.time() >= deadline and _alive(args.pid):
        try:
            os.kill(args.pid, signal.SIGINT)
        except ProcessLookupError:
            pass
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Read-only risk monitor for a bounded Hyperliquid soak process."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

from check_hl_account_state import REPO_ROOT, make_read_only_info, resolve_account
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


def _portfolio_drawdown_breached(
    equities: list[float], peak_equity: float, limit_pct: float | None,
) -> bool:
    return _drawdown_breached(sum(equities), peak_equity, limit_pct)


def _read_error_delay_s(consecutive: int, interval_s: float, max_delay_s: float) -> float:
    return min(interval_s * (2 ** max(consecutive - 1, 0)), max_delay_s)


def _read_outage_breached(first_error_at: float, now: float, max_outage_s: float) -> bool:
    return now - first_error_at >= max_outage_s


def _oldest_order_age_s(orders: list[dict], now: float) -> float:
    if not orders:
        return 0.0
    return max(max(now - float(order.get("timestamp", 0)) / 1000, 0.0) for order in orders)


def _decision_liveness_breach(
    logs: dict[str, Path], *, now: float, started_at: float,
    max_age_s: float, startup_grace_s: float,
) -> str | None:
    for label, path in logs.items():
        try:
            stat = path.stat()
        except FileNotFoundError:
            if now - started_at > startup_grace_s:
                return f"{label} decision log missing after {startup_grace_s:.0f}s startup grace"
            continue
        if stat.st_size == 0:
            if now - started_at > startup_grace_s:
                return f"{label} decision log empty after {startup_grace_s:.0f}s startup grace"
            continue
        age = max(now - stat.st_mtime, 0.0)
        if age > max_age_s:
            return f"{label} decision log stale for {age:.1f}s > {max_age_s:.1f}s"
    return None


def _signal_all(pids: list[int]) -> None:
    for pid in pids:
        try:
            os.kill(pid, signal.SIGINT)
        except ProcessLookupError:
            pass


def _account_sample(
    info, account_id: str, address: str, mids: dict, coins: set[str],
    leverage: float, now: float,
) -> dict:
    state = info.user_state(address)
    spot = info.spot_user_state(address)
    spot_usdc = next((b for b in spot.get("balances", []) if b.get("coin") == "USDC"), {})
    equity = float(spot_usdc.get("total") or 0.0)
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
    initial_margin = gross_notional / leverage
    return {
        "account_id": account_id,
        "equity": equity,
        "margin_available": margin_available,
        "margin_health_ratio": margin_available / equity if equity > 0 else 0.0,
        "gross_notional": gross_notional,
        "initial_margin": initial_margin,
        "initial_margin_ratio": initial_margin / equity if equity > 0 else 1.0,
        "open_orders": len(open_orders),
        "oldest_order_age_s": _oldest_order_age_s(open_orders, now),
        "positions": positions,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--account-id", dest="account_ids", action="append")
    ap.add_argument("--pid", dest="pids", action="append", type=int, required=True)
    ap.add_argument("--duration", type=float, required=True)
    ap.add_argument("--interval", type=float, default=10.0)
    ap.add_argument("--max-read-outage-s", type=float, default=45.0)
    ap.add_argument("--max-read-retry-delay-s", type=float, default=20.0)
    ap.add_argument("--leverage", type=float, default=6.0)
    ap.add_argument("--coins", nargs="+", default=["ETH", "SOL"])
    ap.add_argument("--min-margin-health-ratio", type=float, default=0.15)
    ap.add_argument("--max-initial-margin-ratio", type=float, default=0.85)
    ap.add_argument("--max-drawdown-pct", type=float)
    ap.add_argument("--max-order-age-s", type=float, default=90.0)
    ap.add_argument("--max-decision-age-s", type=float, default=30.0)
    ap.add_argument("--decision-startup-grace-s", type=float, default=90.0)
    ap.add_argument("--decision-log", action="append", default=[])
    ap.add_argument("--output", required=True)
    args = ap.parse_args(argv)
    if args.max_drawdown_pct is not None and not 0 < args.max_drawdown_pct <= 100:
        ap.error("--max-drawdown-pct must be in (0, 100]")
    if min(args.max_order_age_s, args.max_decision_age_s, args.decision_startup_grace_s) <= 0:
        ap.error("order age, decision age, and startup grace must be positive")
    if min(args.interval, args.max_read_outage_s, args.max_read_retry_delay_s) <= 0:
        ap.error("read interval, outage limit, and retry delay must be positive")
    account_ids = args.account_ids or ["e2_mm1"]
    coins = {coin.upper() for coin in args.coins}
    if len(account_ids) != len(args.pids):
        ap.error("repeat --account-id and --pid once per monitored account")
    if len(set(account_ids)) != len(account_ids):
        ap.error("--account-id values must be distinct")
    decision_logs = {}
    for value in args.decision_log:
        if "=" not in value:
            ap.error("--decision-log must be LABEL=/absolute/path")
        label, raw_path = value.split("=", 1)
        path = Path(raw_path)
        if not label or not path.is_absolute() or label in decision_logs:
            ap.error("decision-log labels must be unique and paths absolute")
        decision_logs[label] = path

    from hyperliquid.info import Info
    from hyperliquid.utils import constants

    accounts = []
    for account_id in account_ids:
        address, is_testnet = resolve_account(account_id)
        if str(is_testnet).lower() != "false":
            raise SystemExit("monitor refuses a non-mainnet credential environment")
        accounts.append((account_id, address))
    if len({str(address).lower() for _, address in accounts}) != len(accounts):
        raise SystemExit("monitor refuses duplicate account addresses")
    info = make_read_only_info(Info, constants.MAINNET_API_URL)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + args.duration + 90.0
    consecutive_errors = 0
    first_read_error_at = None
    peak_equity = 0.0
    started_at = time.time()

    with output.open("w") as stream:
        while all(_alive(pid) for pid in args.pids) and time.time() < deadline:
            now = time.time()
            liveness_breach = _decision_liveness_breach(
                decision_logs, now=now, started_at=started_at,
                max_age_s=args.max_decision_age_s,
                startup_grace_s=args.decision_startup_grace_s,
            )
            if liveness_breach:
                event = {"ts": now, "event": "risk_breach", "reason": liveness_breach}
                stream.write(json.dumps(event) + "\n")
                stream.flush()
                print(json.dumps(event), flush=True)
                _signal_all(args.pids)
                return 1
            try:
                mids = info.all_mids()
                account_samples = [
                    _account_sample(
                        info, account_id, address, mids, coins, args.leverage, now,
                    )
                    for account_id, address in accounts
                ]
                equities = [sample["equity"] for sample in account_samples]
                equity = sum(equities)
                peak_equity = max(peak_equity, equity)
                sample = {
                    "ts": time.time(),
                    "equity": equity,
                    "peak_equity": peak_equity,
                    "accounts": account_samples,
                }
                stream.write(json.dumps(sample) + "\n")
                stream.flush()
                print(json.dumps(sample), flush=True)
                consecutive_errors = 0
                first_read_error_at = None
                breach = None
                for account in account_samples:
                    account_id = account["account_id"]
                    if account["equity"] <= 0:
                        breach = f"{account_id} equity is zero or unavailable"
                    elif account["margin_health_ratio"] < args.min_margin_health_ratio:
                        breach = (f"{account_id} margin health "
                                  f"{account['margin_health_ratio']:.3f} < "
                                  f"{args.min_margin_health_ratio:.3f}")
                    elif account["initial_margin_ratio"] > args.max_initial_margin_ratio:
                        breach = (f"{account_id} initial margin ratio "
                                  f"{account['initial_margin_ratio']:.3f} > "
                                  f"{args.max_initial_margin_ratio:.3f}")
                    elif account["oldest_order_age_s"] > args.max_order_age_s:
                        breach = (f"{account_id} oldest order age "
                                  f"{account['oldest_order_age_s']:.1f}s > "
                                  f"{args.max_order_age_s:.1f}s")
                    if breach:
                        break
                if not breach and _portfolio_drawdown_breached(
                    equities, peak_equity, args.max_drawdown_pct,
                ):
                    breach = (f"portfolio equity drawdown "
                              f"{(peak_equity - equity) / peak_equity * 100:.2f}% > "
                              f"{args.max_drawdown_pct:.2f}%")
                if breach:
                    event = {"ts": time.time(), "event": "risk_breach", "reason": breach}
                    stream.write(json.dumps(event) + "\n")
                    stream.flush()
                    print(json.dumps(event), flush=True)
                    _signal_all(args.pids)
                    return 1
            except Exception as exc:  # fail closed after repeated read failures
                consecutive_errors += 1
                error_at = time.time()
                if first_read_error_at is None:
                    first_read_error_at = error_at
                retry_delay = _read_error_delay_s(
                    consecutive_errors, args.interval, args.max_read_retry_delay_s,
                )
                event = {"ts": error_at, "event": "read_error",
                         "error": f"{type(exc).__name__}: {exc}",
                         "consecutive": consecutive_errors,
                         "outage_s": error_at - first_read_error_at,
                         "retry_in_s": retry_delay}
                stream.write(json.dumps(event) + "\n")
                stream.flush()
                print(json.dumps(event), flush=True)
                if _read_outage_breached(
                    first_read_error_at, error_at, args.max_read_outage_s,
                ):
                    _signal_all(args.pids)
                    return 1
                time.sleep(retry_delay)
                continue
            time.sleep(args.interval)
    if time.time() >= deadline and any(_alive(pid) for pid in args.pids):
        _signal_all(args.pids)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

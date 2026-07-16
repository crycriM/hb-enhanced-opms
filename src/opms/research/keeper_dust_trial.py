"""
Tier-2 dust trial: the real Keeper._deposit_ladder / GatewayExecBridge.withdraw
code paths, signing a real dust-sized mainnet transaction.

Tier 1 (keeper_dryrun.py) proved the Keeper loop runs clean against live
Gateway but never reaches the deposit branch on a cold start (the regime
gate holds STOP_QUOTING until it sees a real mean-reversion signal — see
that script's findings). Rather than wait out the gate, this calls
Keeper._deposit_ladder() directly with a small hand-built single-sided
(ask/base-only) ladder placed a few bins above the active bin — the exact
method the keeper's own QUOTE/WIDEN branch would call, just invoked without
going through _cycle()'s risk-gate machinery. hedge_config=None +
pair_type=BLUECHIP keeps hedging out of it, same as tier 1.

SAFETY: prints the exact plan and computed price range first. Only signs
with CONFIRM=yes. MODE=open (default) deposits; MODE=close withdraws a
given POSITION.

Usage (dry preview, default):
  POOL=Cgqw... WALLET=1odAb... \
  PYTHONPATH=opms/src .venv-legacy/bin/python -m opms.research.keeper_dust_trial

Then, to actually sign:
  ... CONFIRM=yes ... (same command)

To close afterwards:
  MODE=close POSITION=<addr> CONFIRM=yes ... (same command)

Env: POOL, WALLET, GATEWAY_URL, NETWORK, BIN_OFFSET_LOW, BIN_OFFSET_HIGH,
BASE_AMOUNT_TOTAL (SOL, split across the 2 levels), MODE, POSITION, CONFIRM.
"""
from __future__ import annotations

import asyncio
import logging
import os

import httpx

from dlmm_bot.config import DLMMConfig
from dlmm_bot.grid import VenueGrid
from dlmm_bot.keeper import Keeper, KeeperConfig
from dlmm_bot.ladder import LadderLevel
from dlmm_bot.risk_dlmm import PairType
from opms.gateway.exec_bridge import GatewayConfig, GatewayExecBridge

logging.basicConfig(level=logging.INFO, format="%(message)s")


def _make_keeper(pool: str, gateway_url: str, network: str, wallet: str) -> Keeper:
    bridge = GatewayExecBridge(GatewayConfig(gateway_url=gateway_url, network=network, wallet=wallet))
    keeper_cfg = KeeperConfig(
        dlmm=DLMMConfig(), grid=VenueGrid(1000.0, 80, 9, 6),  # placeholder grid — only used by _cycle(), unused here
        pair_type=PairType.BLUECHIP, hedge_config=None,
        pool_address=pool, dry_run=False,
    )
    return Keeper(cfg=keeper_cfg, exec_bridge=bridge)


async def _open(pool: str, gateway_url: str, network: str, wallet: str) -> int:
    r = httpx.get(f"{gateway_url}/connectors/meteora/clmm/pool-info",
                   params={"network": network, "poolAddress": pool}, timeout=15.0)
    r.raise_for_status()
    d = r.json()
    active_bin, price = int(d["activeBinId"]), float(d["price"])

    lo = int(os.environ.get("BIN_OFFSET_LOW", "2"))
    hi = int(os.environ.get("BIN_OFFSET_HIGH", "4"))
    total = float(os.environ.get("BASE_AMOUNT_TOTAL", "0.014"))
    ladder = [
        LadderLevel(bin_id=active_bin + lo, side="ask", size=total / 2),
        LadderLevel(bin_id=active_bin + hi, side="ask", size=total / 2),
    ]

    print(f"Pool {pool}: active_bin={active_bin} price={price:.4f}")
    print(f"PLAN: ask-side single-sided deposit, {len(ladder)} levels, "
          f"bin_ids=[{active_bin + lo}, {active_bin + hi}], total base={total} SOL")

    confirm = os.environ.get("CONFIRM") == "yes"
    if not confirm:
        print("\nDRY — nothing signed. Set CONFIRM=yes to execute.")
        return 0

    print("\n>>> CONFIRM=yes — signing on mainnet via Keeper._deposit_ladder.")
    keeper = _make_keeper(pool, gateway_url, network, wallet)
    keeper.exec.start()
    try:
        await keeper._deposit_ladder(ladder)
    finally:
        keeper.exec.stop()

    if keeper._current_position_id:
        print(f"\nOpened position: {keeper._current_position_id}")
        print(f"Close it with: MODE=close POSITION={keeper._current_position_id} CONFIRM=yes ...")
        return 0
    print("\nDeposit did not report a position_id — check the log above for the error.")
    return 1


async def _close(gateway_url: str, network: str, wallet: str, position: str) -> int:
    print(f"PLAN: close-position {position}")
    confirm = os.environ.get("CONFIRM") == "yes"
    if not confirm:
        print("\nDRY — nothing signed. Set CONFIRM=yes to execute.")
        return 0

    print("\n>>> CONFIRM=yes — signing on mainnet via GatewayExecBridge.withdraw.")
    bridge = GatewayExecBridge(GatewayConfig(gateway_url=gateway_url, network=network, wallet=wallet))
    bridge.start()
    try:
        result = bridge.withdraw(position, bps=100)
    finally:
        bridge.stop()
    print(f"ok={result.ok} tx={result.tx_signatures} error={result.error}")
    return 0 if result.ok else 1


async def _run() -> int:
    pool = os.environ["POOL"]
    wallet = os.environ["WALLET"]
    gateway_url = os.environ.get("GATEWAY_URL", "http://localhost:15888")
    network = os.environ.get("NETWORK", "mainnet-beta")
    mode = os.environ.get("MODE", "open")

    if mode == "open":
        return await _open(pool, gateway_url, network, wallet)
    elif mode == "close":
        return await _close(gateway_url, network, wallet, os.environ["POSITION"])
    raise SystemExit(f"unknown MODE={mode!r}, expected open|close")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))

"""
Minimal live test of the real dlmm_bot Keeper (no hedging, dry-run only).

Unlike dryrun_dlmm.py (which pokes GatewayExecBridge.get_state directly) and
roundtrip_dlmm.py (which bypasses the keeper with hand-written Gateway calls),
this drives the actual Keeper._cycle() loop — grid, ladder, regime, risk —
against live Gateway reads. dry_run=True means every actuate branch
(_deposit_ladder/_refresh_ladder/_stop_quoting/_de_risk/_emergency_exit) logs
and returns before calling any exec-bridge write verb, so no transaction is
ever signed. hedge_config=None + pair_type=BLUECHIP keeps HedgeController out
of the loop entirely (see keeper.py: hedge only evaluates for PairType.EXOTIC).

The keeper computes mid price via VenueGrid.price_from_bin(active_bin), not
from Gateway's own "price" field — so the grid's ref_price/bin_step_bps must
be calibrated to the real pool or the AS math runs on a nonsense price. This
script derives ref_price from one live pool-info read instead of guessing.

Usage:
  POOL=CgqwPLSFfht89pF5RSKGUUMFj5zRxoUt4861w2SkXaqY \
  PYTHONPATH=hb-enhanced-opms/src .venv-legacy/bin/python -m opms.research.keeper_dryrun

Optional env: GATEWAY_URL, NETWORK, CYCLES, INTERVAL, CAPITAL, LEVELS.
"""
from __future__ import annotations

import asyncio
import logging
import os

import httpx

from dlmm_bot.config import DLMMConfig
from dlmm_bot.grid import VenueGrid
from dlmm_bot.keeper import Keeper, KeeperConfig
from dlmm_bot.risk_dlmm import PairType
from opms.gateway.exec_bridge import GatewayConfig, GatewayExecBridge

logging.basicConfig(level=logging.INFO, format="%(message)s")


def _calibrate_grid(gateway_url: str, network: str, pool: str) -> VenueGrid:
    """Derive ref_price from one live pool-info read so price_from_bin(active_bin)
    matches Gateway's own price — Meteora bin ids aren't anchored at ref=1.0."""
    r = httpx.get(
        f"{gateway_url}/connectors/meteora/clmm/pool-info",
        params={"network": network, "poolAddress": pool},
        timeout=15.0,
    )
    r.raise_for_status()
    d = r.json()
    price, active_bin, bin_step_bps = d["price"], d["activeBinId"], d["binStep"]
    step = 1.0 + bin_step_bps / 1e4
    ref_price = price / (step ** active_bin)
    print(f"Calibrated: price={price:.6f} active_bin={active_bin} bin_step_bps={bin_step_bps} "
          f"-> ref_price={ref_price:.6f}")
    return VenueGrid(ref_price=ref_price, bin_step_bps=bin_step_bps, base_decimals=9, quote_decimals=6)


async def _run() -> int:
    pool = os.environ["POOL"]
    gateway_url = os.environ.get("GATEWAY_URL", "http://localhost:15888")
    network = os.environ.get("NETWORK", "mainnet-beta")
    cycles = int(os.environ.get("CYCLES", "5"))
    interval = float(os.environ.get("INTERVAL", "5"))
    capital = float(os.environ.get("CAPITAL", "5.0"))
    levels = int(os.environ.get("LEVELS", "2"))

    grid = _calibrate_grid(gateway_url, network, pool)

    keeper_cfg = KeeperConfig(
        dlmm=DLMMConfig(gamma=1.0, kappa=0.5, bin_step_bps=grid.bin_step_bps,
                         ref_price=grid.ref_price, levels=levels, capital=capital),
        grid=grid,
        refresh_interval=interval,
        pair_type=PairType.BLUECHIP,
        hedge_config=None,
        pool_address=pool,
        dry_run=True,
    )
    bridge = GatewayExecBridge(GatewayConfig(gateway_url=gateway_url, network=network))
    keeper = Keeper(cfg=keeper_cfg, exec_bridge=bridge)

    bridge.start()
    try:
        await keeper.run(max_cycles=cycles)
    finally:
        bridge.stop()

    log = keeper.decision_log
    for rec in log:
        print(f"cycle: decision={rec.decision:<14} action={rec.action:<16} mid={rec.mid:8.4f} "
              f"active_bin={rec.active_bin:6} center={rec.ladder_center:6} "
              f"r={rec.r_reservation:8.4f} half_spread={rec.half_spread:.6f}")

    print(f"\n{len(log)}/{cycles} cycles produced a decision record (no uncaught _cycle() error).")
    return 0 if len(log) == cycles else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))

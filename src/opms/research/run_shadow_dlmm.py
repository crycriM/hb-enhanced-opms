"""Phase 3 parity harness: run the DLMM keeper against a live Gateway,
log decisions to JSONL, never submit transactions.

Usage:
  python -m opms.research.run_shadow_dlmm \
    --pool SOL-USDC --wallet <addr> --gateway-url http://<host>:15888 \
    --cycles 20 --output shadow-decisions.jsonl

For offline testing without a live Gateway:
  python -m opms.research.run_shadow_dlmm --fake --cycles 5
"""

import argparse
import asyncio
import logging
from pathlib import Path

from dlmm_bot.config import DLMMConfig
from dlmm_bot.event_log import EventLog
from dlmm_bot.exec_bridge import FakeExecBridge
from dlmm_bot.grid import VenueGrid
from dlmm_bot.keeper import Keeper, KeeperConfig, hash_keeper_config
from dlmm_bot.swap_observer import (
    JsonlSwapEventSource, SwapObserver, SwapStreamRunner,
)
from dlmm_bot.risk_dlmm import PairType
from opms.gateway.exec_bridge import GatewayConfig, GatewayExecBridge

logger = logging.getLogger(__name__)


def build_args():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pool", default="SOL-USDC")
    ap.add_argument("--gateway-url", default="http://localhost:15888")
    ap.add_argument("--wallet", default="")
    ap.add_argument("--chain", default="solana")
    ap.add_argument("--network", default="mainnet-beta")
    ap.add_argument("--ref-price", type=float, default=150.0)
    ap.add_argument("--bin-step-bps", type=int, default=20)
    ap.add_argument("--base-decimals", type=int, default=6)
    ap.add_argument("--quote-decimals", type=int, default=9)
    ap.add_argument("--gamma", type=float, default=1.0)
    ap.add_argument("--kappa", type=float, default=0.5)
    ap.add_argument("--levels", type=int, default=5)
    ap.add_argument("--inner-offset", type=int, default=2)
    ap.add_argument("--capital", type=float, default=1000.0)
    ap.add_argument("--level-weight", type=float, default=0.2)
    ap.add_argument("--drift-threshold-bins", type=int, default=3)
    ap.add_argument("--refresh-interval", type=float, default=5.0)
    ap.add_argument("--pair-type", default="bluechip")
    ap.add_argument("--cycles", type=int, default=20)
    ap.add_argument("--duration-s", type=float, default=0.0,
                    help="Max duration in seconds (0 = unlimited)")
    ap.add_argument("--output", type=Path, default=Path("shadow-events.jsonl"))
    ap.add_argument("--swap-stream-path", type=Path, default=None,
                    help="decoded swap JSONL written by the Solana/TS observer")
    ap.add_argument("--base-mint", default="")
    ap.add_argument("--quote-mint", default="")
    ap.add_argument("--fake", action="store_true",
                    help="Use FakeExecBridge (offline testing, no Gateway needed)")
    return ap.parse_args()


async def main():
    args = build_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    grid = VenueGrid(
        ref_price=args.ref_price,
        bin_step_bps=args.bin_step_bps,
        base_decimals=args.base_decimals,
        quote_decimals=args.quote_decimals,
    )

    dlmm_cfg = DLMMConfig(
        gamma=args.gamma, kappa=args.kappa,
        bin_step_bps=args.bin_step_bps, ref_price=args.ref_price,
        levels=args.levels, inner_offset=args.inner_offset,
        capital=args.capital, level_weight=args.level_weight,
    )

    if args.fake:
        bridge = FakeExecBridge()
        bridge.set_state(args.pool, active_bin=grid.bin_from_price(args.ref_price),
                         balances={"base": 0.0, "quote": args.capital}, tvl_usd=50000.0)
    else:
        gw_cfg = GatewayConfig(
            gateway_url=args.gateway_url, wallet=args.wallet,
            chain=args.chain, network=args.network,
        )
        bridge = GatewayExecBridge(gw_cfg)
        bridge.start()

    keeper_cfg = KeeperConfig(
        dlmm=dlmm_cfg, grid=grid, pool_address=args.pool,
        drift_threshold_bins=args.drift_threshold_bins,
        refresh_interval=args.refresh_interval,
        pair_type=PairType(args.pair_type),
        dry_run=True,
        base_mint=args.base_mint,
        quote_mint=args.quote_mint,
        executor_version=type(bridge).__name__,
    )

    event_log = EventLog(
        str(args.output), config_hash=hash_keeper_config(keeper_cfg)
    )
    observer = SwapObserver(event_log, grid, args.pool)
    keeper = Keeper(
        cfg=keeper_cfg, exec_bridge=bridge,
        event_log=event_log, swap_observer=observer,
    )
    swap_runner = (
        SwapStreamRunner(observer, JsonlSwapEventSource(str(args.swap_stream_path)))
        if args.swap_stream_path else None
    )
    if swap_runner is not None:
        swap_runner.start()
    else:
        keeper._ensure_run_started()
        keeper.emit(
            "swap_stream_unavailable",
            reason="--swap-stream-path is not configured",
        )
    logger.info("Starting shadow run: %d cycles, dry_run=%s", args.cycles, True)

    try:
        if args.duration_s > 0:
            await asyncio.wait_for(keeper.run(max_cycles=args.cycles), timeout=args.duration_s)
        else:
            await keeper.run(max_cycles=args.cycles)
    except asyncio.TimeoutError:
        logger.info("Shadow run reached duration limit; stopping keeper")
    finally:
        if swap_runner is not None:
            swap_runner.stop()
        keeper.stop()
        event_log.close()
        bridge.stop()

    logger.info(
        "Wrote %d keeper cycles to event log %s",
        len(keeper.decision_log), args.output,
    )


if __name__ == "__main__":
    asyncio.run(main())

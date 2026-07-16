"""
D5.1 dry-run (Step A.1): N read-only get_state cycles against a live Gateway.

Exercises the *real* GatewayExecBridge.get_state path (no signing, no writes)
so the numbers can be reconciled against the Meteora UI:
  active_bin, price, derived TVL, and — if POSITION set — position balances.

Usage:
  POOL=CgqwPLSFfht89pF5RSKGUUMFj5zRxoUt4861w2SkXaqY \
  .venv-legacy/bin/python -m opms.research.dryrun_dlmm

Optional env: GATEWAY_URL, NETWORK, CYCLES, INTERVAL, POSITION (NFT address).
"""
from __future__ import annotations

import os
import time

from opms.gateway.exec_bridge import GatewayConfig, GatewayExecBridge


def main() -> int:
    pool = os.environ["POOL"]
    cycles = int(os.environ.get("CYCLES", "20"))
    interval = float(os.environ.get("INTERVAL", "3"))
    cfg = GatewayConfig(
        gateway_url=os.environ.get("GATEWAY_URL", "http://localhost:15888"),
        network=os.environ.get("NETWORK", "mainnet-beta"),
    )
    bridge = GatewayExecBridge(cfg)
    bridge.start()
    pos = os.environ.get("POSITION")
    if pos:
        bridge._positions[pool] = pos

    ok = 0
    try:
        for i in range(1, cycles + 1):
            r = bridge.get_state(pool)
            if not r.ok:
                print(f"[{i:02d}/{cycles}] ERROR: {r.error}")
            else:
                d = r.data
                b = d["balances"]
                print(f"[{i:02d}/{cycles}] active_bin={d['active_bin']:>6} "
                      f"price={d['price']:.4f} tvl_usd={d['tvl_usd']:,.0f} "
                      f"pos_base={b['base']:.6f} pos_quote={b['quote']:.6f}")
                ok += 1
            if i < cycles:
                time.sleep(interval)
    finally:
        bridge.stop()

    print(f"\n{ok}/{cycles} cycles OK against {cfg.gateway_url} (pool {pool})")
    return 0 if ok == cycles else 1


if __name__ == "__main__":
    raise SystemExit(main())

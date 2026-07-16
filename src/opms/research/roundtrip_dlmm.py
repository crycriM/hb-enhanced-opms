"""
D5.2 round trip: close existing DLMM position, reopen with a predefined shape.

Drives Gateway directly (Gateway holds the encrypted signing key — the HB
signer). This is a *predefined-shape* manual round trip, not keeper-driven, so
it bypasses the keeper's bin-array contract (that adapter is the PWL work).

SAFETY: dry by default. Prints exactly what it would sign. Only CONFIRM=yes
sends real transactions on mainnet.

Usage (dry):
  POOL=Cgqw... POSITION=4mF3... WALLET=1odAb... \
  PYTHONPATH=opms/src .venv-legacy/bin/python -m opms.research.roundtrip_dlmm

Add CONFIRM=yes to actually sign. Shape env: LOWER, UPPER, BASE_AMT, STRATEGY
(0=Spot flat, 1=Curve, 2=BidAsk), SLIPPAGE_PCT.
"""
from __future__ import annotations

import os
import time

import httpx

GW = os.environ.get("GATEWAY_URL", "http://localhost:15888")
NET = os.environ.get("NETWORK", "mainnet-beta")
CLMM = "/connectors/meteora/clmm"


def _post(c: httpx.Client, path: str, body: dict) -> dict:
    r = c.post(path, json={"network": NET, **body})
    r.raise_for_status()
    return r.json()


def _get(c: httpx.Client, path: str, params: dict) -> dict:
    r = c.get(path, params={"network": NET, **params})
    r.raise_for_status()
    return r.json()


def main() -> int:
    pool = os.environ["POOL"]
    position = os.environ["POSITION"]
    wallet = os.environ["WALLET"]
    lower = float(os.environ.get("LOWER", "69.85"))
    upper = float(os.environ.get("UPPER", "85.25"))
    base_amt = float(os.environ.get("BASE_AMT", "0.014"))
    strategy = int(os.environ.get("STRATEGY", "0"))
    slippage = float(os.environ.get("SLIPPAGE_PCT", "1"))
    confirm = os.environ.get("CONFIRM") == "yes"

    c = httpx.Client(base_url=GW, timeout=90.0)

    pos = _get(c, f"{CLMM}/position-info", {"positionAddress": position})
    print(f"Current position {position}:")
    print(f"  base={pos.get('baseTokenAmount')} quote={pos.get('quoteTokenAmount')} "
          f"range={pos.get('lowerPrice'):.2f}-{pos.get('upperPrice'):.2f} "
          f"fees(base/quote)={pos.get('baseFeeAmount')}/{pos.get('quoteFeeAmount')}")
    print("\nPLAN:")
    print(f"  1. close-position {position}  -> returns base+quote+fees to {wallet}")
    print(f"  2. open-position pool={pool}")
    print(f"       range={lower}-{upper}  baseTokenAmount={base_amt} quoteTokenAmount=0")
    print(f"       strategyType={strategy} ({['Spot','Curve','BidAsk'][strategy]})  slippagePct={slippage}")

    if not confirm:
        print("\nDRY RUN — nothing signed. Set CONFIRM=yes to execute.")
        return 0

    print("\n>>> CONFIRM=yes — signing on mainnet.")
    close = _post(c, f"{CLMM}/close-position", {"walletAddress": wallet, "positionAddress": position})
    print(f"  close tx: {close.get('signature')}")
    time.sleep(8)  # let the chain settle before reopening
    opened = _post(c, f"{CLMM}/open-position", {
        "walletAddress": wallet, "poolAddress": pool,
        "lowerPrice": lower, "upperPrice": upper,
        "baseTokenAmount": base_amt, "quoteTokenAmount": 0,
        "strategyType": strategy, "slippagePct": slippage,
    })
    print(f"  open tx: {opened.get('signature')}  new position: {opened.get('positionAddress')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

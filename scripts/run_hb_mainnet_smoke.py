"""Live read-only smoke: real Hummingbot HL connector + PerpMMController.

Functional-plan step 2 (see `perp-bot/status.md`): bring up one real
`PerpMMController` on a live Hyperliquid mainnet connector, call the real
`on_start()` (FillObserver registration) and `update_processed_data()`, and
check mid / funding / equity / analytics — **without placing any orders**.

Standalone (not a pytest test) because HB connectors spawn background tasks
that outlive pytest-asyncio's per-test event loop and error on loop close.

Usage:
  OPMS_HB_MAINNET=confirm python scripts/run_hb_mainnet_smoke.py --account-id e2_mm1

`--use-vault` is auto-detected: a subaccount address (not the master) routes
with `vaultAddress`, exactly as the live deployment will.
"""

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")

PAIR = "ETH-USD"
CONNECTOR_NAME = "hyperliquid_perpetual"


def _master_address(account_id: str) -> str:
    """Master of an account family: ``e3_sub1`` -> ``HYPERLIQUID_E3_MAIN_ACCOUNT_ADDRESS``."""
    family = account_id.split("_")[0].upper()
    return (os.environ.get(f"HYPERLIQUID_{family}_MAIN_ACCOUNT_ADDRESS")
            or os.environ.get("HYPERLIQUID_MASTER_ACCOUNT_ADDRESS") or "").lower()


def _resolve_account(account_id: str, use_vault: str | None) -> tuple[str, str, bool]:
    prefix = f"HYPERLIQUID_{account_id.upper()}"
    address = os.environ.get(f"{prefix}_ACCOUNT_ADDRESS")
    private_key = os.environ.get(f"{prefix}_PRIVATE_KEY")
    if not address or not private_key:
        raise SystemExit(f"Missing {prefix}_ACCOUNT_ADDRESS / _PRIVATE_KEY")
    master = _master_address(account_id)
    if use_vault is not None:
        vault = use_vault == "yes"
    elif master:
        vault = address.lower() != master
    else:
        raise SystemExit("Set HYPERLIQUID_MASTER_ACCOUNT_ADDRESS or pass --use-vault yes|no")
    return address.lower(), private_key, vault


def _build_connector(address: str, private_key: str, use_vault: bool):
    from hummingbot.connector.derivative.hyperliquid_perpetual.hyperliquid_perpetual_derivative import (
        HyperliquidPerpetualDerivative,
    )

    return HyperliquidPerpetualDerivative(
        hyperliquid_perpetual_secret_key=private_key,
        hyperliquid_perpetual_address=address,
        use_vault=use_vault,
        hyperliquid_perpetual_mode="api_wallet",
        trading_pairs=[PAIR],
        trading_required=True,
    )


async def _wait_ready(connector, timeout_s: float) -> float:
    from hummingbot.core.data_type.common import PriceType

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            mid = connector.get_price_by_type(PAIR, PriceType.MidPrice)
            if mid and float(mid) > 0:
                await connector._update_balances()
                return float(mid)
        except Exception:
            pass
        await asyncio.sleep(1.0)
    raise RuntimeError(f"order book for {PAIR} not ready within {timeout_s}s")


def _nonce_patch_ok() -> bool:
    """Two signs under a frozen clock must get distinct nonces (local HB patch)."""
    import json

    import eth_account
    from hummingbot.connector.derivative.hyperliquid_perpetual import hyperliquid_perpetual_constants as CONSTANTS
    from hummingbot.connector.derivative.hyperliquid_perpetual.hyperliquid_perpetual_auth import (
        HyperliquidPerpetualAuth,
    )

    auth = HyperliquidPerpetualAuth("0x" + "11" * 20, eth_account.Account.create().key.hex(), True)
    now = time.time()
    auth._get_timestamp = lambda: now
    cancel = json.dumps({"type": "cancel", "cancels": {"asset": 1, "cloid": "0x" + "00" * 16}})
    a, b = (json.loads(auth.add_auth_to_params_post(cancel, CONSTANTS.PERPETUAL_BASE_URL))["nonce"]
            for _ in range(2))
    return a != b


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--account-id", default="e2_mm1")
    ap.add_argument("--use-vault", choices=["yes", "no"], default=None)
    ap.add_argument("--timeout", type=float, default=90.0)
    ap.add_argument("--funding-samples", type=int, default=5)
    args = ap.parse_args()

    if os.environ.get("OPMS_HB_MAINNET") != "confirm":
        print("refusing: set OPMS_HB_MAINNET=confirm", file=sys.stderr)
        return 2

    from hummingbot.data_feed.market_data_provider import MarketDataProvider

    from opms.controllers.generic.perp_mm_controller import PerpMMController, PerpMMControllerConfig

    address, private_key, use_vault = _resolve_account(args.account_id, args.use_vault)
    print(f"account={args.account_id} use_vault={use_vault} address={address[:8]}..{address[-4:]}")

    connector = _build_connector(address, private_key, use_vault)
    failures: list[str] = []
    if not _nonce_patch_ok():
        failures.append("HL auth reuses a nonce for same-ms signs (local nonce patch missing after HB update?)")
    try:
        await connector._initialize_trading_pair_symbol_map()
        await connector.start_network()
        mid = await _wait_ready(connector, args.timeout)
        print(f"mid={mid}")
        print(f"balances={connector.get_all_balances()}")

        fundings: list[float] = []
        for _ in range(args.funding_samples):
            info = connector.get_funding_info(PAIR)
            if info is not None and info.rate is not None:
                fundings.append(float(info.rate))
            await asyncio.sleep(1.0)
        print(f"funding_samples={fundings}")
        if not fundings:
            failures.append("no funding info received")
        elif any(abs(r) >= 0.01 for r in fundings):
            failures.append(f"implausible funding rate(s) {fundings} (openInterest-as-funding regression?)")

        provider = MarketDataProvider(connectors={CONNECTOR_NAME: connector})
        config = PerpMMControllerConfig(
            id="hb-mainnet-smoke",
            controller_name="perp_mm",
            connector_name=CONNECTOR_NAME,
            trading_pair=PAIR,
            venue="hyperliquid",
            account_id=args.account_id,
            leverage=1,
        )
        controller = PerpMMController(config, provider, asyncio.Queue(), update_interval=5.0)
        await controller.on_start()
        if controller._fill_observer._connector is not connector:
            failures.append("FillObserver not registered in on_start()")
        await controller.update_processed_data()
        equity = controller._current_equity()
        print(f"equity={equity}")
        if equity <= 0:
            failures.append(f"non-positive equity ({equity})")
        info = controller.get_custom_info()
        print(f"custom_info_keys={sorted(info)}")
        if set(info) != {"fill_pnl", "markout", "slippage"}:
            failures.append(f"unexpected custom_info shape: {sorted(info)}")
        controller.on_stop()
    finally:
        await connector.stop_network()

    if failures:
        print("\nSMOKE FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nSMOKE PASSED (read-only; no orders placed)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

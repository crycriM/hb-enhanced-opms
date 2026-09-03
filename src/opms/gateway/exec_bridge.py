"""
GatewayExecBridge — implements dlmm_bot.exec_bridge.ExecBridge's interface
against the Hummingbot Gateway HTTP API.

Drop-in replacement for the TS subprocess bridge: same verbs, same ExecResult
return type, same start/stop lifecycle.  The dlmm_bot.keeper.Keeper never
changes a line.

HB-free: no hummingbot imports.  All tests run against a mock HTTP server
(httpx.MockTransport or responses library).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from dlmm_bot.exec_bridge import ExecResult

logger = logging.getLogger(__name__)


@dataclass
class GatewayConfig:
    gateway_url: str = "http://localhost:15888"
    wallet: str = ""
    connector: str = "meteora"
    chain: str = "solana"
    network: str = "mainnet-beta"
    timeout: float = 30.0
    base_decimals: int = 9
    quote_decimals: int = 6


class GatewayExecBridge:
    """HTTP client to Hummingbot Gateway Meteora + Jupiter endpoints."""

    def __init__(self, cfg: GatewayConfig):
        self.cfg = cfg
        self._client: httpx.Client | None = None
        self._positions: dict[str, str] = {}

    def start(self) -> None:
        self._client = httpx.Client(base_url=self.cfg.gateway_url, timeout=self.cfg.timeout)

    def stop(self) -> None:
        if self._client:
            self._client.close()
            self._client = None

    # Gateway 2.x namespaces every Meteora route under this prefix.
    _CLMM = "/connectors/meteora/clmm"

    def _request(self, method: str, path: str, payload: dict) -> ExecResult:
        assert self._client is not None, "call start() before using the bridge"
        try:
            if method == "GET":
                resp = self._client.get(path, params=payload)
            else:
                resp = self._client.post(path, json=payload)
            resp.raise_for_status()
            raw = resp.json()
        except httpx.HTTPStatusError as e:
            return ExecResult(ok=False, error=f"HTTP {e.response.status_code}: {e.response.text[:200]}")
        except Exception as e:
            return ExecResult(ok=False, error=str(e))

        if "error" in raw and raw["error"]:
            return ExecResult(ok=False, error=str(raw["error"]))
        data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
        envelope = dict(raw)
        envelope["ok"] = True
        envelope["data"] = dict(data)
        return ExecResult.from_payload(envelope)

    def _get(self, path: str, params: dict) -> ExecResult:
        return self._request("GET", path, {"network": self.cfg.network, **params})

    def _post(self, path: str, body: dict) -> ExecResult:
        return self._request("POST", path, {"network": self.cfg.network, "walletAddress": self.cfg.wallet, **body})

    _STRATEGY_INT = {"Spot": 0, "Curve": 1, "BidAsk": 2}

    def _bin_price_fn(self, pool: str) -> ExecResult:
        """One pool-info read anchoring (price, activeBinId, binStep) so bin
        ids can be converted to prices — Meteora bin ids aren't a fixed
        ref=1.0 grid, see keeper_dryrun.py. Returns a bin_id -> price callable
        in `.data["fn"]` on success."""
        anchor = self._get(f"{self._CLMM}/pool-info", {"poolAddress": pool})
        if not anchor.ok:
            return anchor
        d = anchor.data
        price, active_bin, bin_step_bps = float(d["price"]), int(d["activeBinId"]), int(d["binStep"])
        step = 1.0 + bin_step_bps / 1e4
        return ExecResult(ok=True, data={"fn": lambda bin_id: price * step ** (bin_id - active_bin)})

    @staticmethod
    def _extract_sigs(raw: dict) -> list[str]:
        for key in ("signature", "txSignature", "tx_signatures", "signatures"):
            val = raw.get(key)
            if val:
                return [val] if isinstance(val, str) else list(val)
        return []

    @staticmethod
    def _first(data: dict, *keys, default=None):
        for key in keys:
            if data.get(key) is not None:
                return data[key]
        return default

    def get_position(self, position_id: str) -> ExecResult:
        result = self._get(
            f"{self._CLMM}/position-info", {"positionAddress": position_id}
        )
        if not result.ok or not isinstance(result.data, dict):
            return result
        raw = result.data
        fee_x_raw = self._first(
            raw, "claimable_fee_x_raw", "claimableFeeXRaw", "feeXRaw"
        )
        fee_y_raw = self._first(
            raw, "claimable_fee_y_raw", "claimableFeeYRaw", "feeYRaw"
        )
        fee_x = self._first(raw, "claimable_fee_x", "claimableFeeX")
        fee_y = self._first(raw, "claimable_fee_y", "claimableFeeY")
        if fee_x is None and fee_x_raw is not None:
            fee_x = float(fee_x_raw) / 10 ** self.cfg.base_decimals
        if fee_y is None and fee_y_raw is not None:
            fee_y = float(fee_y_raw) / 10 ** self.cfg.quote_decimals

        bins = []
        for source in raw.get("bins", raw.get("positions", [])) or []:
            if not isinstance(source, dict):
                continue
            row = dict(source)
            row["bin_id"] = self._first(source, "bin_id", "binId", "activeBin")
            row["amount_x_raw"] = self._first(
                source, "amount_x_raw", "amountXRaw"
            )
            row["amount_y_raw"] = self._first(
                source, "amount_y_raw", "amountYRaw"
            )
            bins.append(row)

        result.data = {
            "raw": raw,
            "position_id": position_id,
            "active_bin": self._first(raw, "active_bin", "activeBinId", "activeBin"),
            "bins": bins,
            "claimable_fee_x_raw": fee_x_raw,
            "claimable_fee_y_raw": fee_y_raw,
            "claimable_fee_x": float(fee_x or 0.0),
            "claimable_fee_y": float(fee_y or 0.0),
            "base_amount": float(self._first(
                raw, "baseTokenAmount", "baseAmount", default=0.0
            ) or 0.0),
            "quote_amount": float(self._first(
                raw, "quoteTokenAmount", "quoteAmount", default=0.0
            ) or 0.0),
        }
        result.position_id = position_id
        return result

    def get_state(self, pool: str) -> ExecResult:
        pool_r = self._get(f"{self._CLMM}/pool-info", {"poolAddress": pool})
        if not pool_r.ok:
            return pool_r
        d = pool_r.data

        # Quote is USDC in the SOL/USDC pool; TVL in quote terms. pool-info
        # exposes no tvl field, so derive it from reserves × price.
        price = float(d.get("price", 0) or 0)
        base_amt = float(d.get("baseTokenAmount", 0) or 0)
        quote_amt = float(d.get("quoteTokenAmount", 0) or 0)
        state: dict = {
            "active_bin": d.get("activeBinId", d.get("activeBin", 0)),
            "price": price,
            "tvl_usd": base_amt * price + quote_amt,
            "balances": {"base": 0.0, "quote": 0.0},
        }

        pos_id = self._positions.get(pool)
        if pos_id:
            pos_r = self.get_position(pos_id)
            if pos_r.ok and pos_r.data:
                state["balances"] = {
                    "base": float(pos_r.data.get("base_amount", 0.0)),
                    "quote": float(pos_r.data.get("quote_amount", 0.0)),
                }

        return ExecResult(ok=True, data=state)

    def deposit_single_sided(
        self,
        pool: str,
        side: str,
        bin_ids: list[int],
        amounts: list[float],
        strategy_type: str = "Spot",
    ) -> ExecResult:
        """Collapses the keeper's per-bin ladder levels for one side into a
        single canned-strategy sub-position spanning [min(bin_ids),
        max(bin_ids)] — the PWL-tiling insight (STATUS.md §4): Gateway has no
        per-bin control, but one flat Spot tile per side is a valid (if
        coarse) first cut. Multi-segment AS-skew tiling is future work."""
        anchor = self._bin_price_fn(pool)
        if not anchor.ok:
            return anchor
        price_of = anchor.data["fn"]
        lower_price, upper_price = price_of(min(bin_ids)), price_of(max(bin_ids))
        total = sum(amounts)

        # ponytail: add-liquidity body shape unverified live (only
        # open-position/close-position proven, D5.2/D5.3) — also, tracking
        # `_positions` by pool alone (not pool+side) means a second
        # single-sided deposit on the other side overwrites this one's
        # position_id. Fine for a single-leg call; fix before depositing both
        # sides of a ladder in the same cycle.
        existing_pos = self._positions.get(pool)
        body = {
            "poolAddress": pool,
            "lowerPrice": lower_price,
            "upperPrice": upper_price,
            "baseTokenAmount": total if side == "ask" else 0,
            "quoteTokenAmount": total if side == "bid" else 0,
            "strategyType": self._STRATEGY_INT.get(strategy_type, 0),
            "slippagePct": 1.0,
        }
        if existing_pos:
            body["positionAddress"] = existing_pos
            result = self._post(f"{self._CLMM}/add-liquidity", body)
        else:
            result = self._post(f"{self._CLMM}/open-position", body)

        if result.ok and result.data:
            pos_addr = result.data.get("positionAddress", result.data.get("position_id"))
            if pos_addr:
                self._positions[pool] = pos_addr
                result.data["position_id"] = pos_addr

        return result

    def withdraw(self, position_id: str, bps: int = 100) -> ExecResult:
        if bps >= 100:
            # Verified shape (D5.2/D5.3): close-position takes only
            # positionAddress, no liquidityToRemoveBps.
            result = self._post(f"{self._CLMM}/close-position", {"positionAddress": position_id})
            self._positions = {k: v for k, v in self._positions.items() if v != position_id}
        else:
            # ponytail: remove-liquidity body shape unverified live.
            result = self._post(f"{self._CLMM}/remove-liquidity",
                                 {"positionAddress": position_id, "liquidityToRemoveBps": bps})
        return result

    def swap(
        self,
        in_mint: str,
        out_mint: str,
        amount: float,
        max_slippage_bps: int = 50,
        pool: Optional[str] = None,
    ) -> ExecResult:
        return self._post(f"{self._CLMM}/execute-swap", {
            "tokenAddress": in_mint,
            "tokenAddress2": out_mint,
            "amount": amount,
            "allowedSlippage": str(max_slippage_bps / 100),
        })

    def refresh_bundle(
        self,
        withdraw_position_id: str,
        swap_spec: dict | None,
        deposit_spec: dict,
    ) -> ExecResult:
        withdraw_r = self.withdraw(withdraw_position_id, bps=100)
        if not withdraw_r.ok:
            return ExecResult(ok=False, error=f"refresh_bundle: withdraw failed: {withdraw_r.error}")

        if swap_spec:
            swap_r = self.swap(
                in_mint=swap_spec["in_mint"],
                out_mint=swap_spec["out_mint"],
                amount=swap_spec["amount"],
                max_slippage_bps=swap_spec.get("max_slippage_bps", 50),
            )
            if not swap_r.ok:
                logger.error("refresh_bundle: swap failed (will re-deposit without rebalance): %s", swap_r.error)

        pool = deposit_spec["pool"]
        bid_bins = deposit_spec.get("bid_bins", [])
        ask_bins = deposit_spec.get("ask_bins", [])
        bid_amounts = deposit_spec.get("bid_amounts", [])
        ask_amounts = deposit_spec.get("ask_amounts", [])

        step_results = [withdraw_r]
        if swap_spec and 'swap_r' in locals():
            step_results.append(swap_r)
        if bid_bins:
            r = self.deposit_single_sided(pool, "bid", bid_bins, bid_amounts)
            step_results.append(r)
            if not r.ok:
                return ExecResult(ok=False, error=f"refresh_bundle: bid deposit failed: {r.error}")
        if ask_bins:
            r = self.deposit_single_sided(pool, "ask", ask_bins, ask_amounts)
            step_results.append(r)
            if not r.ok:
                return ExecResult(ok=False, error=f"refresh_bundle: ask deposit failed: {r.error}")

        sigs = [sig for step in step_results for sig in step.tx_signatures]
        receipts = [receipt for step in step_results for receipt in step.tx_receipts]
        position_id = self._positions.get(pool)
        return ExecResult(
            ok=True,
            data={
                "steps": [step.data for step in step_results],
                "position_id": position_id,
            },
            tx_signatures=sigs,
            tx_receipts=receipts,
            position_id=position_id,
            slot=receipts[-1].get("slot") if receipts else None,
            block_time=receipts[-1].get("block_time") if receipts else None,
            fee_lamports=sum(
                int(receipt["fee_lamports"])
                for receipt in receipts if receipt.get("fee_lamports") is not None
            ) if any(r.get("fee_lamports") is not None for r in receipts) else None,
        )


__all__ = ["GatewayConfig", "GatewayExecBridge"]

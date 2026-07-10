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

    def _post(self, path: str, body: dict) -> ExecResult:
        assert self._client is not None, "call start() before using the bridge"
        try:
            resp = self._client.post(path, json={**self._base_fields(), **body})
            resp.raise_for_status()
            raw = resp.json()
        except httpx.HTTPStatusError as e:
            return ExecResult(ok=False, error=f"HTTP {e.response.status_code}: {e.response.text[:200]}")
        except Exception as e:
            return ExecResult(ok=False, error=str(e))

        if "error" in raw and raw["error"]:
            return ExecResult(ok=False, error=str(raw["error"]))
        return ExecResult(
            ok=True,
            data=raw,
            tx_signatures=self._extract_sigs(raw),
        )

    def _base_fields(self) -> dict:
        return {
            "connector": self.cfg.connector,
            "chain": self.cfg.chain,
            "network": self.cfg.network,
            "wallet": self.cfg.wallet,
        }

    @staticmethod
    def _extract_sigs(raw: dict) -> list[str]:
        for key in ("signature", "txSignature", "tx_signatures", "signatures"):
            val = raw.get(key)
            if val:
                return [val] if isinstance(val, str) else list(val)
        return []

    def get_state(self, pool: str) -> ExecResult:
        pool_r = self._post("/meteora/pool-info", {"poolAddress": pool})
        if not pool_r.ok:
            return pool_r

        state: dict = {
            "active_bin": pool_r.data.get("activeBin", pool_r.data.get("active_bin", 0)),
            "tvl_usd": pool_r.data.get("tvl", pool_r.data.get("tvlUsd")),
            "balances": {"base": 0.0, "quote": 0.0},
        }

        pos_id = self._positions.get(pool)
        if pos_id:
            pos_r = self._post("/meteora/position-info", {"positionAddress": pos_id, "poolAddress": pool})
            if pos_r.ok and pos_r.data:
                state["balances"] = {
                    "base": float(pos_r.data.get("baseAmount", pos_r.data.get("balanceX", 0))),
                    "quote": float(pos_r.data.get("quoteAmount", pos_r.data.get("balanceY", 0))),
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
        existing_pos = self._positions.get(pool)
        if existing_pos:
            body = {
                "poolAddress": pool,
                "positionAddress": existing_pos,
                "binIds": bin_ids,
                "amounts": amounts,
                "side": side,
                "strategy": strategy_type,
            }
            result = self._post("/meteora/add-liquidity", body)
        else:
            body = {
                "poolAddress": pool,
                "binIds": bin_ids,
                "amounts": amounts,
                "side": side,
                "strategy": strategy_type,
            }
            result = self._post("/meteora/open-position", body)

        if result.ok and result.data:
            pos_addr = result.data.get("positionAddress", result.data.get("position_id"))
            if pos_addr:
                self._positions[pool] = pos_addr
                result.data["position_id"] = pos_addr

        return result

    def withdraw(self, position_id: str, bps: int = 100) -> ExecResult:
        endpoint = "/meteora/close-position" if bps >= 100 else "/meteora/remove-liquidity"
        result = self._post(endpoint, {"positionAddress": position_id, "liquidityToRemoveBps": bps})
        if result.ok and bps >= 100:
            self._positions = {k: v for k, v in self._positions.items() if v != position_id}
        return result

    def swap(
        self,
        in_mint: str,
        out_mint: str,
        amount: float,
        max_slippage_bps: int = 50,
        pool: Optional[str] = None,
    ) -> ExecResult:
        return self._post("/jupiter/execute-swap", {
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

        sigs = []
        if bid_bins:
            r = self.deposit_single_sided(pool, "bid", bid_bins, bid_amounts)
            if not r.ok:
                return ExecResult(ok=False, error=f"refresh_bundle: bid deposit failed: {r.error}")
            sigs.extend(r.tx_signatures)
        if ask_bins:
            r = self.deposit_single_sided(pool, "ask", ask_bins, ask_amounts)
            if not r.ok:
                return ExecResult(ok=False, error=f"refresh_bundle: ask deposit failed: {r.error}")
            sigs.extend(r.tx_signatures)

        return ExecResult(ok=True, data={"steps": "withdraw+deposit"}, tx_signatures=sigs)


__all__ = ["GatewayConfig", "GatewayExecBridge"]

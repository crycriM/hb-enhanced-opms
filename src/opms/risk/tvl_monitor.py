"""
Standalone TVL / rug-detection monitor.

Polls Gateway pool-info at a configurable interval and fires a kill
sentinel when TVL drops beyond threshold or falls below minimum.  Runs as
a separate process — a hung keeper cannot block this monitor.

Kill signal: writes a sentinel file to a shared path that the keeper checks
at the start of each cycle (a non-blocking, crash-safe IPC mechanism).
"""

import asyncio
import logging
import os
import time

import httpx

from dlmm_bot.risk_dlmm import DLMMRiskPolicy, DLMMRiskConfig, PairType

logger = logging.getLogger(__name__)


class TVLMonitor:
    """Poll pool-info and write a kill sentinel if TVL drops."""

    def __init__(
        self,
        gateway_url: str,
        pool_address: str,
        risk_cfg: DLMMRiskConfig,
        sentinel_path: str,
        poll_interval: float = 30.0,
        wallet: str = "",
        chain: str = "solana",
        network: str = "mainnet-beta",
    ):
        self.gateway_url = gateway_url
        self.pool_address = pool_address
        self.risk = DLMMRiskPolicy(risk_cfg)
        self.sentinel_path = sentinel_path
        self.poll_interval = poll_interval
        self._params = {"connector": "meteora", "chain": chain, "network": network, "wallet": wallet}
        self._running = False

    async def run(self) -> None:
        self._running = True
        async with httpx.AsyncClient(base_url=self.gateway_url) as client:
            while self._running:
                try:
                    await self._poll(client)
                except Exception as e:
                    logger.error("TVLMonitor poll error: %s", e)
                await asyncio.sleep(self.poll_interval)

    async def _poll(self, client: httpx.AsyncClient) -> None:
        resp = await client.post(
            "/meteora/pool-info",
            json={**self._params, "poolAddress": self.pool_address},
        )
        resp.raise_for_status()
        data = resp.json()
        tvl = data.get("tvl", data.get("tvlUsd"))
        ts = time.time()
        kill, reason = self.risk.evaluate_tvl(ts, float(tvl) if tvl is not None else None)
        if kill:
            logger.critical("TVLMonitor: KILL sentinel written: %s", reason)
            with open(self.sentinel_path, "w") as f:
                f.write(reason)

    def stop(self) -> None:
        self._running = False

"""
DLMMController — HB V2 controller wrapping the DLMM keeper.

All strategy/risk/execution logic lives in dlmm_bot.keeper.Keeper (backed
by GatewayExecBridge).  This controller:
  - Schedules keeper cycles at each update_processed_data() call.
  - Manages keeper lifecycle (start/stop with the controller).
  - Exposes decision log + PnL via get_custom_info() for the Dashboard.
  - Returns empty executor actions — DLMM execution is direct via Gateway.
"""

import asyncio
import logging
import os
import time
from typing import List

from pydantic import Field

from hummingbot.strategy_v2.controllers.controller_base import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.models.executor_actions import ExecutorAction

from dlmm_bot.event_log import EventLog
from dlmm_bot.exec_bridge import FakeExecBridge
from dlmm_bot.grid import VenueGrid
from dlmm_bot.keeper import Keeper, KeeperConfig, hash_keeper_config
from dlmm_bot.risk_dlmm import PairType
from dlmm_bot.swap_observer import (
    JsonlSwapEventSource, SwapObserver, SwapStreamRunner,
)
from opms.gateway.exec_bridge import GatewayConfig, GatewayExecBridge

logger = logging.getLogger(__name__)


class DLMMControllerConfig(ControllerConfigBase):
    controller_type: str = "generic"
    # Gateway config
    gateway_url: str = "http://localhost:15888"
    wallet: str = Field(json_schema_extra={"prompt": "Gateway wallet address: ", "prompt_on_new": True})
    chain: str = "solana"
    network: str = "mainnet-beta"
    # Pool config
    pool_address: str = Field(json_schema_extra={"prompt": "Pool address: ", "prompt_on_new": True})
    # Grid config
    ref_price: float = 1.0
    bin_step_bps: int = 2
    base_decimals: int = 9
    quote_decimals: int = 6
    # AS params
    gamma: float = 1.0
    kappa: float = 0.5
    # Keeper params
    levels: int = 5
    inner_offset: int = 1
    capital: float = 1000.0
    level_weight: float = 0.2
    drift_threshold_bins: int = 3
    refresh_interval: float = 5.0
    pair_type: str = "bluechip"
    dry_run: bool = True
    # Hedge config (optional)
    hedge_enabled: bool = False
    hedge_venue: str = "hyperliquid_perpetual"
    hedge_coin: str = ""
    decision_log_path: str | None = None
    event_log_dir: str = "logs/dlmm"
    swap_stream_path: str = ""
    base_mint: str = ""
    quote_mint: str = ""
    gas_token_price_quote: float | None = None
    shared_book_key: str = ""

    @property
    def coin(self) -> str:
        return self.pool_address.split("-")[0] if "-" in self.pool_address else self.pool_address

    def update_markets(self, markets):
        return markets


class DLMMController(ControllerBase):

    def __init__(self, config: DLMMControllerConfig, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config

        gw_cfg = GatewayConfig(
            gateway_url=config.gateway_url,
            wallet=config.wallet,
            chain=config.chain,
            network=config.network,
            base_decimals=config.base_decimals,
            quote_decimals=config.quote_decimals,
        )
        self._bridge = GatewayExecBridge(gw_cfg)

        grid = VenueGrid(
            ref_price=config.ref_price,
            bin_step_bps=config.bin_step_bps,
            base_decimals=config.base_decimals,
            quote_decimals=config.quote_decimals,
        )
        from dlmm_bot.config import DLMMConfig
        dlmm_cfg = DLMMConfig(
            gamma=config.gamma,
            kappa=config.kappa,
            bin_step_bps=config.bin_step_bps,
            ref_price=config.ref_price,
            inner_offset=config.inner_offset,
            levels=config.levels,
            capital=config.capital,
            level_weight=config.level_weight,
        )
        keeper_cfg = KeeperConfig(
            dlmm=dlmm_cfg,
            grid=grid,
            pool_address=config.pool_address,
            drift_threshold_bins=config.drift_threshold_bins,
            refresh_interval=config.refresh_interval,
            pair_type=PairType(config.pair_type),
            dry_run=config.dry_run,
            base_mint=config.base_mint,
            quote_mint=config.quote_mint,
            executor_version="hummingbot-gateway",
            gas_token_price_quote=config.gas_token_price_quote,
        )
        run_id = (
            f"run_{config.pool_address.replace('/', '_')}_{time.time_ns()}"
        )
        log_path = config.decision_log_path or os.path.join(
            config.event_log_dir, f"{run_id}.jsonl"
        )
        self._event_log = EventLog(
            log_path, run_id=run_id, config_hash=hash_keeper_config(keeper_cfg)
        )
        observer = SwapObserver(self._event_log, grid, config.pool_address)
        self.keeper = Keeper(
            cfg=keeper_cfg, exec_bridge=self._bridge,
            event_log=self._event_log, swap_observer=observer,
        )
        source = (
            JsonlSwapEventSource(config.swap_stream_path)
            if config.swap_stream_path else None
        )
        self._swap_stream = (
            SwapStreamRunner(observer, source) if source is not None else None
        )
        self._cycle_task: asyncio.Task | None = None

        from .shared_risk_book import get_shared_book
        self._shared_book = get_shared_book(config.shared_book_key) if config.shared_book_key else None

    async def on_start(self):
        self._bridge.start()
        self.keeper._ensure_run_started()
        if self._swap_stream is not None:
            self._swap_stream.start()
        else:
            self.keeper.emit(
                "swap_stream_unavailable",
                reason="swap_stream_path is not configured",
            )

    def on_stop(self):
        if self._swap_stream is not None:
            self._swap_stream.stop()
        self.keeper.stop()
        self._event_log.close()
        self._bridge.stop()

    async def update_processed_data(self):
        try:
            await self.keeper._cycle()
            self.keeper._cycle_count += 1
        except Exception as e:
            logger.exception("DLMMController: keeper cycle error: %s", e)
            return

        if self._shared_book is not None and self.keeper.decision_log:
            last = self.keeper.decision_log[-1]
            self._shared_book.dlmm_net_delta = last.net_delta
            self._shared_book.dlmm_inventory_value_usd = last.mid * (
                last.inventory_base + last.inventory_quote / last.mid if last.mid > 0 else 0.0
            )
            self._shared_book.dlmm_sigma = last.sigma
            self._shared_book.last_dlmm_ts = last.ts

    def determine_executor_actions(self) -> List[ExecutorAction]:
        return []

    def get_custom_info(self) -> dict:
        ts = self.market_data_provider.time() if self.market_data_provider else 0.0
        mid = self.keeper._price_history[-1] if self.keeper._price_history else 0.0
        pnl = self.keeper.pnl.explain(ts, mid) if mid > 0 else None
        last_record = self.keeper.decision_log[-1] if self.keeper.decision_log else None
        return {
            "last_decision": last_record.decision if last_record else "none",
            "last_action": last_record.action if last_record else "none",
            "active_bin": self.keeper._active_bin,
            "center_bin": self.keeper._center_bin,
            "inventory_base": self.keeper._inventory_base,
            "inventory_quote": self.keeper._inventory_quote,
            "pnl_total": pnl.total_pnl if pnl else 0.0,
            "cycle_count": self.keeper._cycle_count,
            "dry_run": self.config.dry_run,
        }

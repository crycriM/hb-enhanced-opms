"""HedgeController — HB V2 controller for DLMM exotic pair hedging.

Reads DLMM inventory state from SharedRiskBook and emits executor
actions on the perp connector to maintain hedge position.

Uses dlmm_bot.hedge.HedgeController (aliased as DlmmHedgeController)
as the evaluate()-only engine.
"""

import logging
from decimal import Decimal
from typing import List

from pydantic import Field

from hummingbot.core.data_type.common import TradeType
from hummingbot.strategy_v2.controllers.controller_base import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction

from dlmm_bot.hedge import HedgeController as DlmmHedgeController, HedgeConfig
from opms.executors.passive_aggressive_executor import PassiveAggressiveExecutorConfig
from opms.controllers.generic.shared_risk_book import get_shared_book, SharedRiskBook

logger = logging.getLogger(__name__)


class HedgeControllerConfig(ControllerConfigBase):
    controller_type: str = "generic"
    perp_connector: str = Field(json_schema_extra={"prompt": "Perp connector name: ", "prompt_on_new": True})
    perp_trading_pair: str = Field(json_schema_extra={"prompt": "Trading pair: ", "prompt_on_new": True})
    hedge_coin: str = ""
    shared_book_key: str = Field(json_schema_extra={"prompt": "Shared book key: ", "prompt_on_new": True})
    tau_h: float = 3600.0
    tau_min: float = 900.0
    tau_max: float = 7200.0
    sigma_ref: float = 0.5
    deadband_base_bps: float = 10.0
    per_trade_cost_bps: float = 2.0
    delta_cap_bps: float = 200.0
    cube_root_constant: float = 1.0
    deadband_base: float = 0.0
    refresh_interval: float = 5.0

    def update_markets(self, markets):
        return markets.add_or_update(self.perp_connector, self.perp_trading_pair)


class HedgeController(ControllerBase):
    def __init__(self, config: HedgeControllerConfig, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config
        self._shared_book = get_shared_book(config.shared_book_key) if config.shared_book_key else None
        self._hedge = DlmmHedgeController(cfg=HedgeConfig(
            tau_h=config.tau_h,
            tau_min=config.tau_min,
            tau_max=config.tau_max,
            sigma_ref=config.sigma_ref,
            deadband_base_bps=config.deadband_base_bps,
            per_trade_cost_bps=config.per_trade_cost_bps,
            delta_cap_bps=config.delta_cap_bps,
            cube_root_constant=config.cube_root_constant,
            deadband_base=config.deadband_base,
            venue=config.perp_connector,
            coin=config.hedge_coin or config.perp_trading_pair.split("-")[0],
            enabled=True,
        ))

    def _current_perp_position(self) -> float:
        total = 0.0
        for position in self.positions_held:
            if position.connector_name != self.config.perp_connector or position.trading_pair != self.config.perp_trading_pair:
                continue
            total += position.amount if position.side == TradeType.BUY else -position.amount
        return total

    async def update_processed_data(self):
        if not self._shared_book:
            return
        perp_position = float(self._current_perp_position())
        inv_base = self._shared_book.dlmm_net_delta
        inv_value = self._shared_book.dlmm_inventory_value_usd
        sigma = self._shared_book.dlmm_sigma

        action, target, _ = self._hedge.evaluate(
            inventory_base=inv_base,
            current_short=perp_position,
            inventory_value_usd=inv_value,
            sigma_now=sigma,
            dt=self.config.refresh_interval,
        )
        self._shared_book.hedge_action = action
        self._shared_book.hedge_target_short = target
        self._shared_book.hedge_urgency = "passive" if action == "rehedge" else "normal"
        self._shared_book.gamma_relaxation = 0.5 if action == "force_hedge" else 1.0
        self._shared_book.last_hedge_ts = self.market_data_provider.time()

    def determine_executor_actions(self) -> List[ExecutorAction]:
        if self._shared_book is None or self._shared_book.hedge_action == "no_trade":
            return []
        current = float(self._current_perp_position())
        target_short = self._shared_book.hedge_target_short
        delta = -target_short - current
        if abs(delta) < 1e-6:
            return []
        side = TradeType.SELL if delta < 0 else TradeType.BUY
        ts = self.market_data_provider.time()
        return [CreateExecutorAction(
            controller_id=self.config.id,
            executor_config=PassiveAggressiveExecutorConfig(
                timestamp=ts,
                connector_name=self.config.perp_connector,
                trading_pair=self.config.perp_trading_pair,
                side=side,
                total_amount_base=Decimal(str(abs(delta))),
                child_order_quantity=Decimal(str(abs(delta) / 3)),
                child_order_time_limit=60.0,
                child_order_refresh_time=20.0,
                leverage=1,
            ),
        )]

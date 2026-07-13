"""
TwapRoundTripController — one-shot BUY-then-SELL TWAP round trip.

Ad-hoc controller for the legacy-OPMS-PA-V2 vs HB-native-TWAP execution-
profile comparison (see clmm-animation/docs/mm-remaining-tasks-BCDE.md,
Stream C parity note, 2026-07-10). Not part of the HB migration's
permanent controller set — a throwaway harness to drive one TWAPExecutor
buy of `total_amount_quote`, then one TWAPExecutor sell of the same size,
then stop. Position-over-time is observed externally (direct HL API
polling), not by this controller.
"""

import logging
from decimal import Decimal
from typing import List, Optional

from pydantic import Field

from hummingbot.core.data_type.common import TradeType
from hummingbot.strategy_v2.controllers.controller_base import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.twap_executor.data_types import TWAPExecutorConfig
from hummingbot.strategy_v2.models.base import RunnableStatus
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction

logger = logging.getLogger(__name__)


class TwapRoundTripControllerConfig(ControllerConfigBase):
    controller_type: str = "generic"
    connector_name: str = Field(json_schema_extra={"prompt": "Connector name: ", "prompt_on_new": True})
    trading_pair: str = Field(json_schema_extra={"prompt": "Trading pair: ", "prompt_on_new": True})
    # Named distinctly from ControllerConfigBase's own `total_amount_quote`
    # (a generic "capital allocated to this controller" field used for
    # portfolio/dashboard bookkeeping elsewhere) -- this is per-leg TWAP
    # notional, a different concept, and redefining the inherited field
    # would silently repurpose it.
    leg_notional_quote: Decimal = Field(json_schema_extra={"prompt": "Notional per leg (quote): ", "prompt_on_new": True})
    total_duration: int = 60
    order_interval: int = 20
    leverage: int = 1

    def update_markets(self, markets):
        return markets.add_or_update(self.connector_name, self.trading_pair)


class TwapRoundTripController(ControllerBase):
    """buy leg -> wait for close -> sell leg -> wait for close -> idle."""

    def __init__(self, config: TwapRoundTripControllerConfig, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config
        self.phase = "buy"          # "buy" | "sell" | "done"
        self.buy_executor_id: Optional[str] = None
        self.sell_executor_id: Optional[str] = None

    async def update_processed_data(self):
        pass  # all decisions are made in determine_executor_actions

    def _make_twap_config(self, side: TradeType) -> TWAPExecutorConfig:
        return TWAPExecutorConfig(
            timestamp=self.market_data_provider.time(),
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            side=side,
            leverage=self.config.leverage,
            total_amount_quote=self.config.leg_notional_quote,
            total_duration=self.config.total_duration,
            order_interval=self.config.order_interval,
        )

    def _is_done(self, executor_id: str) -> bool:
        return any(e.id == executor_id and e.status == RunnableStatus.TERMINATED
                   for e in self.get_executors())

    def determine_executor_actions(self) -> List[ExecutorAction]:
        if self.phase == "buy" and self.buy_executor_id is None:
            twap_cfg = self._make_twap_config(TradeType.BUY)
            self.buy_executor_id = twap_cfg.id
            logger.info(f"TwapRoundTrip: starting BUY leg, executor {self.buy_executor_id}")
            return [CreateExecutorAction(controller_id=self.config.id, executor_config=twap_cfg)]

        if self.phase == "buy" and self._is_done(self.buy_executor_id):
            logger.info("TwapRoundTrip: BUY leg done, starting SELL leg")
            self.phase = "sell"
            return []

        if self.phase == "sell" and self.sell_executor_id is None:
            twap_cfg = self._make_twap_config(TradeType.SELL)
            self.sell_executor_id = twap_cfg.id
            logger.info(f"TwapRoundTrip: starting SELL leg, executor {self.sell_executor_id}")
            return [CreateExecutorAction(controller_id=self.config.id, executor_config=twap_cfg)]

        if self.phase == "sell" and self._is_done(self.sell_executor_id):
            logger.info("TwapRoundTrip: SELL leg done, round trip complete")
            self.phase = "done"
            return []

        return []

    def get_custom_info(self) -> dict:
        return {
            "phase": self.phase,
            "buy_executor_id": self.buy_executor_id,
            "sell_executor_id": self.sell_executor_id,
        }

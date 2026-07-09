"""The one module that speaks Hummingbot for the perp keeper.

PerpMMController drives an unmodified perp_bot.keeper.Keeper each control
cycle via the InProcessClient bridge (perp_mm_bridge.py) and translates its
ExecIntent into HB executor actions. All quoting/risk logic lives in
mm_core + Keeper — nothing is re-implemented here.
"""

from decimal import Decimal
from typing import List, Optional

from pydantic import Field

from hummingbot.core.data_type.common import PositionAction, PriceType, TradeType
from hummingbot.strategy_v2.controllers.controller_base import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.order_executor.data_types import ExecutionStrategy, OrderExecutorConfig
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction, StopExecutorAction

from mm_core.inventory import Caps

from perp_bot.config import PerpPairConfig
from perp_bot.keeper import Keeper
from perp_bot.opms_client import Position

from opms.controllers.perp_mm_bridge import InProcessClient, intent_to_order_specs


class PerpMMControllerConfig(ControllerConfigBase):
    controller_type: str = "generic"
    connector_name: str = Field(json_schema_extra={"prompt": "Perpetual connector name: ", "prompt_on_new": True})
    trading_pair: str = Field(json_schema_extra={"prompt": "Trading pair (e.g. BTC-USD): ", "prompt_on_new": True})
    account_id: str = "default"
    gamma: float = 0.5
    kappa: float = 0.3
    widen_factor: float = 2.0
    max_position: float = 10.0
    critical_position: float = 20.0
    leverage: int = 1
    decision_log_path: Optional[str] = None

    @property
    def coin(self) -> str:
        return self.trading_pair.split("-")[0]


class PerpMMController(ControllerBase):
    def __init__(self, config: PerpMMControllerConfig, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config
        pair_config = PerpPairConfig(
            coin=config.coin,
            gamma=config.gamma,
            kappa=config.kappa,
            widen_factor=config.widen_factor,
            exchange=config.connector_name,
            account_id=config.account_id,
            caps=Caps(max_position=config.max_position, critical_position=config.critical_position),
        )
        self._client = InProcessClient()
        self.keeper = Keeper(self._client, pair_config, decision_log_path=config.decision_log_path)
        self._client.on_snapshot(self.keeper._on_snapshot)
        self._client.on_fill(self.keeper._on_fill)
        self._client.on_error(self.keeper._on_error)

    async def update_processed_data(self):
        mid = self.get_current_price(self.config.connector_name, self.config.trading_pair, PriceType.MidPrice)
        funding_info = self.market_data_provider.get_funding_info(self.config.connector_name, self.config.trading_pair)
        funding_rate = float(funding_info.rate) if funding_info is not None else None

        self._client.set_positions({
            self.config.coin: Position(
                coin=self.config.coin,
                position=float(self._current_base_position()),
                equity=float(self._current_equity()),
            )
        })

        await self.keeper._on_snapshot({
            "ts": self.market_data_provider.time(),
            "mid": float(mid),
            "funding_rate": funding_rate,
        })
        await self.keeper._tick()

    def _current_base_position(self) -> Decimal:
        total = Decimal("0")
        for position in self.positions_held:
            if position.connector_name != self.config.connector_name or position.trading_pair != self.config.trading_pair:
                continue
            total += position.amount if position.side == TradeType.BUY else -position.amount
        return total

    def _current_equity(self) -> Decimal:
        # ponytail: static stand-in for real account equity — RiskPolicy's
        # drawdown breaker (mm_core.risk_policy) is inert until this reads
        # live equity from the connector (balance/margin API), not config.
        # Wire before any non-shadow deployment.
        return self.config.total_amount_quote

    def determine_executor_actions(self) -> List[ExecutorAction]:
        actions: List[ExecutorAction] = [
            StopExecutorAction(controller_id=self.config.id, executor_id=executor.id)
            for executor in self.get_active_executors(connector_names=[self.config.connector_name],
                                                        trading_pairs=[self.config.trading_pair])
        ]

        specs = intent_to_order_specs(self._client.last_intent)
        timestamp = self.market_data_provider.time()
        for spec in specs:
            if spec.side is None:
                continue
            actions.append(CreateExecutorAction(
                controller_id=self.config.id,
                executor_config=OrderExecutorConfig(
                    timestamp=timestamp,
                    trading_pair=self.config.trading_pair,
                    connector_name=self.config.connector_name,
                    side=TradeType.BUY if spec.side == "buy" else TradeType.SELL,
                    amount=Decimal(str(spec.amount)),
                    price=Decimal(str(spec.price)) if spec.price is not None else None,
                    execution_strategy=ExecutionStrategy.MARKET if spec.price is None else ExecutionStrategy.LIMIT_MAKER,
                    position_action=PositionAction.CLOSE if spec.reduce_only else PositionAction.OPEN,
                    leverage=self.config.leverage,
                ),
            ))
        return actions

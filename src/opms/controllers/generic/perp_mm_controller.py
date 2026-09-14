"""The one module that speaks Hummingbot for the perp keeper.

PerpMMController drives an unmodified perp_bot.keeper.Keeper each control
cycle via the InProcessClient bridge (perp_mm_bridge.py) and translates its
ExecIntent into HB executor actions. All quoting/risk logic lives in
mm_core + Keeper — nothing is re-implemented here.
"""

from decimal import Decimal
from typing import List

from pydantic import Field

from hummingbot.core.data_type.common import PositionAction, PriceType, TradeType
from hummingbot.strategy_v2.controllers.controller_base import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.executor_orchestrator import ExecutorOrchestrator
from hummingbot.strategy_v2.executors.order_executor.data_types import ExecutionStrategy, OrderExecutorConfig
from hummingbot.strategy_v2.executors.twap_executor.data_types import TWAPExecutorConfig, TWAPMode
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction, StopExecutorAction

from mm_core.inventory import Caps

from perp_bot.config import PerpPairConfig
from perp_bot.keeper import Keeper
from perp_bot.opms_client import Position

from opms.analytics.fill_observer import FillObserver
from opms.executors.passive_aggressive_executor import PassiveAggressiveExecutor, PassiveAggressiveExecutorConfig

from .perp_mm_bridge import (
    ExecutionRequest,
    InProcessClient,
    intent_is_quoting,
    intent_to_execution_request,
    intent_to_order_specs,
)

# HB's ExecutorOrchestrator only knows its built-in executors. Register the
# passive-aggressive executor so a CreateExecutorAction carrying our config
# type maps to a real executor instead of raising "Unsupported executor config
# type" when the controller routes a de-risk/emergency intent.
ExecutorOrchestrator._executor_mapping.setdefault(
    "passive_aggressive_executor", PassiveAggressiveExecutor
)


class PerpMMControllerConfig(ControllerConfigBase):
    controller_type: str = "generic"
    connector_name: str = Field(json_schema_extra={"prompt": "Perpetual connector name: ", "prompt_on_new": True})
    trading_pair: str = Field(json_schema_extra={"prompt": "Trading pair (e.g. BTC-USD): ", "prompt_on_new": True})
    # perp_bot/OPMS venue key (perp_bot.venue_capabilities), a different
    # namespace from HB's connector_name — e.g. connector_name may be
    # "hyperliquid_perpetual_testnet" while venue is "hyperliquid".
    venue: str = Field(json_schema_extra={"prompt": "perp_bot venue key (e.g. hyperliquid): ", "prompt_on_new": True})
    account_id: str = "default"
    gamma: float = 0.5
    kappa: float = 0.3
    widen_factor: float = 2.0
    max_position: float = 10.0
    critical_position: float = 20.0
    leverage: int = 1
    # The quote/collateral asset label the connector reports balances under.
    # Hummingbot's Hyperliquid connector uses "USD" (CONSTANTS.CURRENCY), not
    # "USDC" — _current_equity also falls back across common labels.
    collateral_asset: str = "USD"
    decision_log_path: str | None = None

    @property
    def coin(self) -> str:
        return self.trading_pair.split("-")[0]

    def update_markets(self, markets):
        return markets.add_or_update(self.connector_name, self.trading_pair)


class PerpMMController(ControllerBase):
    def __init__(self, config: PerpMMControllerConfig, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config
        pair_config = PerpPairConfig(
            coin=config.coin,
            gamma=config.gamma,
            kappa=config.kappa,
            widen_factor=config.widen_factor,
            exchange=config.venue,
            account_id=config.account_id,
            caps=Caps(max_position=config.max_position, critical_position=config.critical_position),
        )
        self._client = InProcessClient()
        self.keeper = Keeper(self._client, pair_config, decision_log_path=config.decision_log_path)
        self._client.on_snapshot(self.keeper._on_snapshot)
        self._client.on_fill(self.keeper._on_fill)
        self._client.on_error(self.keeper._on_error)
        self._fill_observer = FillObserver(
            venue=config.connector_name,
            symbol=config.trading_pair,
        )

    async def on_start(self):
        connector = self.market_data_provider.get_connector(self.config.connector_name)
        self._fill_observer.register(connector)

    def on_stop(self):
        connector = self.market_data_provider.get_connector(self.config.connector_name)
        self._fill_observer.unregister(connector)

    async def update_processed_data(self):
        mid = self.get_current_price(self.config.connector_name, self.config.trading_pair, PriceType.MidPrice)
        funding_info = self.market_data_provider.get_funding_info(self.config.connector_name, self.config.trading_pair)
        funding_rate = float(funding_info.rate) if funding_info is not None else None

        self._fill_observer.update_mid(float(mid))

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
        # Cross-margined account value (collateral + unrealized PnL), resolved
        # against the connector's actual balances. HB's Hyperliquid connector
        # labels the balance "USD" (CONSTANTS.CURRENCY), not "USDC" — trusting
        # the label alone yields a silent 0 equity.
        balances = self.market_data_provider.get_connector(
            self.config.connector_name
        ).get_all_balances()
        if balances.get(self.config.collateral_asset):
            return Decimal(str(balances[self.config.collateral_asset]))
        for asset in ("USD", "USDC", "USDT"):
            if balances.get(asset):
                return Decimal(str(balances[asset]))
        if len(balances) == 1:
            return Decimal(str(next(iter(balances.values()))))
        return Decimal("0")

    def _execution_actions(self, req: ExecutionRequest) -> list[ExecutorAction]:
        ts = self.market_data_provider.time()
        side = TradeType.BUY if req.side == "buy" else TradeType.SELL

        if req.urgency in ("passive", "normal"):
            config = PassiveAggressiveExecutorConfig(
                timestamp=ts,
                connector_name=self.config.connector_name,
                trading_pair=self.config.trading_pair,
                side=side,
                total_amount_base=Decimal(str(req.amount)),
                child_order_quantity=Decimal(str(req.amount / 5)),
                child_order_time_limit=60.0,
                child_order_refresh_time=20.0,
                leverage=self.config.leverage,
            )
            return [CreateExecutorAction(controller_id=self.config.id, executor_config=config)]

        if req.urgency == "immediate":
            mid = self.get_current_price(
                self.config.connector_name, self.config.trading_pair, PriceType.MidPrice
            )
            config = TWAPExecutorConfig(
                timestamp=ts,
                connector_name=self.config.connector_name,
                trading_pair=self.config.trading_pair,
                side=side,
                total_amount_quote=Decimal(str(req.amount)) * Decimal(str(mid)),
                total_duration=120,
                order_interval=30,
                mode=TWAPMode.TAKER,
                leverage=self.config.leverage,
            )
            return [CreateExecutorAction(controller_id=self.config.id, executor_config=config)]

        # "emergency"
        min_size = self.market_data_provider.get_trading_rules(
            self.config.connector_name, self.config.trading_pair
        ).min_order_size
        if Decimal(str(req.amount)) < min_size:
            config = OrderExecutorConfig(
                timestamp=ts,
                trading_pair=self.config.trading_pair,
                connector_name=self.config.connector_name,
                side=side,
                amount=Decimal(str(req.amount)),
                price=None,
                execution_strategy=ExecutionStrategy.MARKET,
                position_action=PositionAction.CLOSE if req.reduce_only else PositionAction.OPEN,
                leverage=self.config.leverage,
            )
            return [CreateExecutorAction(controller_id=self.config.id, executor_config=config)]

        config = PassiveAggressiveExecutorConfig(
            timestamp=ts,
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            side=side,
            total_amount_base=Decimal(str(req.amount)),
            child_order_quantity=Decimal(str(req.amount)),
            child_order_time_limit=10.0,
            child_order_refresh_time=5.0,
            leverage=self.config.leverage,
        )
        return [CreateExecutorAction(controller_id=self.config.id, executor_config=config)]

    def determine_executor_actions(self) -> List[ExecutorAction]:
        stop_all = [
            StopExecutorAction(controller_id=self.config.id, executor_id=executor.id)
            for executor in self.get_active_executors(
                connector_names=[self.config.connector_name],
                trading_pairs=[self.config.trading_pair],
            )
        ]

        if intent_is_quoting(self._client.last_intent):
            specs = intent_to_order_specs(self._client.last_intent)
            timestamp = self.market_data_provider.time()
            new_actions = [
                CreateExecutorAction(
                    controller_id=self.config.id,
                    executor_config=OrderExecutorConfig(
                        timestamp=timestamp,
                        trading_pair=self.config.trading_pair,
                        connector_name=self.config.connector_name,
                        side=TradeType.BUY if s.side == "buy" else TradeType.SELL,
                        amount=Decimal(str(s.amount)),
                        price=Decimal(str(s.price)) if s.price is not None else None,
                        execution_strategy=ExecutionStrategy.LIMIT_MAKER if s.price else ExecutionStrategy.MARKET,
                        position_action=PositionAction.CLOSE if s.reduce_only else PositionAction.OPEN,
                        leverage=self.config.leverage,
                    ),
                )
                for s in specs if s.side is not None
            ]
        else:
            req = intent_to_execution_request(self._client.last_intent)
            new_actions = self._execution_actions(req) if req else []

        return stop_all + new_actions

    def get_custom_info(self) -> dict:
        mid = float(
            self.get_current_price(
                self.config.connector_name, self.config.trading_pair, PriceType.MidPrice
            )
        )
        return {
            "fill_pnl": self._fill_observer.explain(mid=mid),
            "markout": self._fill_observer.markout_stats(),
            "slippage": self._fill_observer.slippage_stats(),
        }

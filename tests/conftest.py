"""
Path fix and shared HB stubs for Phase 2 tests.

All HB mocks are injected ONCE here (conftest.py is loaded before any
test module) so there's no first-writer-wins race between test files.
Shared enum / stub classes are exported so test modules can import them
rather than re-defining their own.
"""

import sys
from pathlib import Path
from enum import Enum, auto
from decimal import Decimal
from unittest.mock import MagicMock
from dataclasses import dataclass
from typing import Optional

# ---------------------------------------------------------------------------
# Path: ensure NEW hb-enhanced-opms/src takes precedence over dex_executor editable install
# ---------------------------------------------------------------------------

_src = str(Path(__file__).parent.parent / "src")
if _src not in sys.path:
    sys.path.insert(0, _src)

# ---------------------------------------------------------------------------
# Shared HB enum / data-type stubs (canonical, shared across all test files)
# ---------------------------------------------------------------------------

class RunnableStatus(Enum):
    NOT_STARTED = auto()
    RUNNING = auto()
    SHUTTING_DOWN = auto()
    TERMINATED = auto()


class CloseType(Enum):
    COMPLETED = auto()
    EARLY_STOP = auto()
    FAILED = auto()
    INSUFFICIENT_BALANCE = auto()
    POSITION_HOLD = auto()


class TradeType(Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class PriceType(Enum):
    MidPrice = "mid"
    BestBid = "bid"
    BestAsk = "ask"


class PositionAction(Enum):
    OPEN = "open"
    NIL = "nil"
    CLOSE = "close"


class MarketEvent:
    OrderFilled = "OrderFilled"
    BuyOrderCreated = "BuyOrderCreated"
    SellOrderCreated = "SellOrderCreated"
    BuyOrderCompleted = "BuyOrderCompleted"
    SellOrderCompleted = "SellOrderCompleted"
    OrderCancelled = "OrderCancelled"
    OrderFailure = "OrderFailure"


@dataclass
class OrderCandidate:
    trading_pair: str
    is_maker: bool
    order_type: OrderType
    order_side: TradeType
    amount: Decimal
    price: Decimal


@dataclass
class PerpetualOrderCandidate(OrderCandidate):
    leverage: Decimal = Decimal("1")


class TrackedOrder:
    def __init__(self, order_id: Optional[str] = None):
        self.order_id = order_id
        self.order = None
        self.is_done = False

    @property
    def executed_amount_base(self) -> Decimal:
        return Decimal("0")


class ExecutorBase:
    """Minimal ExecutorBase stub used by all executor tests."""

    def __init__(self, strategy, connectors, config, update_interval=0.5, max_retries=10):
        self.config = config
        self._strategy = strategy
        self._status = RunnableStatus.RUNNING
        self._max_retries = max_retries
        self._current_retries = 0
        self.close_type = None
        self.close_timestamp = None
        self.connectors = {
            name: strategy.connectors.get(name, MagicMock())
            for name in connectors
        }

    def start(self): pass
    def stop(self): pass

    @property
    def status(self): return self._status

    def place_order(self, connector_name, trading_pair, order_type, side, amount,
                    position_action=None, price=Decimal("NaN")):
        if side == TradeType.BUY:
            return self._strategy.buy(connector_name, trading_pair, amount, order_type, price, position_action)
        return self._strategy.sell(connector_name, trading_pair, amount, order_type, price, position_action)

    def get_price(self, connector_name, trading_pair, price_type=None):
        return self.connectors[connector_name].get_price_by_type(trading_pair, price_type)

    def get_trading_rules(self, connector_name, trading_pair):
        return self.connectors[connector_name].trading_rules[trading_pair]

    def adjust_order_candidates(self, connector_name, candidates):
        return candidates

    @staticmethod
    def is_perpetual_connector(name):
        return "perp" in name.lower() or "perpetual" in name.lower()


# ---------------------------------------------------------------------------
# Inject all HB stubs (once, before any test module imports)
# ---------------------------------------------------------------------------

def _mk(**attrs):
    m = MagicMock()
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


class ExecutionStrategy(Enum):
    MARKET = "MARKET"
    LIMIT_MAKER = "LIMIT_MAKER"
    LIMIT = "LIMIT"
    LIMIT_CHASER = "LIMIT_CHASER"


class TWAPMode(Enum):
    MAKER = "MAKER"
    TAKER = "TAKER"


class TWAPExecutorConfig:
    """Stub mirroring the real HB class name and fields (total_amount_quote,
    total_duration, order_interval, mode) so a regression to the non-existent
    ``TwapExecutorConfig``/``duration_seconds`` shape is caught."""
    type = "twap_executor"
    def __init__(self, timestamp, connector_name, trading_pair, side, total_amount_quote,
                 total_duration=120, order_interval=30, mode=TWAPMode.TAKER, leverage=1):
        self.timestamp = timestamp
        self.connector_name = connector_name
        self.trading_pair = trading_pair
        self.side = side
        self.total_amount_quote = total_amount_quote
        self.total_duration = total_duration
        self.order_interval = order_interval
        self.mode = mode
        self.leverage = leverage


class OrderExecutorConfig:
    """Minimal stub for OrderExecutorConfig — enough to check .type and .execution_strategy."""
    type = "order_executor"
    def __init__(self, timestamp, trading_pair, connector_name, side, amount, price,
                 execution_strategy, position_action, leverage=1):
        self.timestamp = timestamp
        self.trading_pair = trading_pair
        self.connector_name = connector_name
        self.side = side
        self.amount = amount
        self.price = price
        self.execution_strategy = execution_strategy
        self.position_action = position_action
        self.leverage = leverage


class ExecutorOrchestrator:
    """Stub exposing the mapping the controller registers its PA executor into."""
    _executor_mapping = {
        "position_executor": "PositionExecutor",
        "order_executor": OrderExecutorConfig,
        "twap_executor": TWAPExecutorConfig,
    }


_HB_STUBS = {
    "hummingbot": MagicMock(),
    "hummingbot.connector": MagicMock(),
    "hummingbot.connector.connector_base": _mk(ConnectorBase=object),
    "hummingbot.connector.trading_rule": _mk(TradingRule=object),
    "hummingbot.core": MagicMock(),
    "hummingbot.core.data_type": MagicMock(),
    "hummingbot.core.data_type.common": _mk(
        ExecutionStrategy=ExecutionStrategy,
        OrderType=OrderType,
        PositionAction=PositionAction,
        PriceType=PriceType,
        TradeType=TradeType,
    ),
    "hummingbot.core.data_type.order_candidate": _mk(
        OrderCandidate=OrderCandidate,
        PerpetualOrderCandidate=PerpetualOrderCandidate,
    ),
    "hummingbot.core.event": MagicMock(),
    "hummingbot.core.event.event_forwarder": _mk(SourceInfoEventForwarder=MagicMock),
    "hummingbot.core.event.events": _mk(
        BuyOrderCompletedEvent=object,
        BuyOrderCreatedEvent=object,
        MarketEvent=MarketEvent,
        MarketOrderFailureEvent=object,
        OrderCancelledEvent=object,
        OrderFilledEvent=object,
        SellOrderCompletedEvent=object,
        SellOrderCreatedEvent=object,
    ),
    "hummingbot.logger": _mk(HummingbotLogger=object),
    "hummingbot.strategy": MagicMock(),
    "hummingbot.strategy.strategy_v2_base": _mk(StrategyV2Base=object),
    "hummingbot.strategy_v2": MagicMock(),
    "hummingbot.strategy_v2.controllers": MagicMock(),
    "hummingbot.strategy_v2.controllers.controller_base": _mk(
        ControllerBase=object,
        ControllerConfigBase=MagicMock,
    ),
    "hummingbot.strategy_v2.executors": MagicMock(),
    "hummingbot.strategy_v2.executors.data_types": _mk(ExecutorConfigBase=MagicMock),
    "hummingbot.strategy_v2.executors.executor_base": _mk(ExecutorBase=ExecutorBase),
    "hummingbot.strategy_v2.executors.executor_orchestrator": _mk(ExecutorOrchestrator=ExecutorOrchestrator),
    "hummingbot.strategy_v2.executors.twap_executor": MagicMock(),
    "hummingbot.strategy_v2.executors.twap_executor.data_types": _mk(
        TWAPExecutorConfig=TWAPExecutorConfig, TWAPMode=TWAPMode
    ),
    "hummingbot.strategy_v2.executors.order_executor": MagicMock(),
    "hummingbot.strategy_v2.executors.order_executor.data_types": _mk(
        OrderExecutorConfig=OrderExecutorConfig, ExecutionStrategy=ExecutionStrategy
    ),
    "hummingbot.strategy_v2.models": MagicMock(),
    "hummingbot.strategy_v2.models.base": _mk(RunnableStatus=RunnableStatus),
    "hummingbot.strategy_v2.models.executors": _mk(CloseType=CloseType, TrackedOrder=TrackedOrder),
    "hummingbot.strategy_v2.models.executor_actions": MagicMock(),
    "hummingbot.client": MagicMock(),
    "hummingbot.client.settings": MagicMock(),
    "base58": MagicMock(),
    "pydantic": MagicMock(),
}

for _name, _mod in _HB_STUBS.items():
    sys.modules.setdefault(_name, _mod)

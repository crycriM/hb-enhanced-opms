"""PerpMMController against the *real* Hummingbot runtime (no stubs).

These tests exist because the stub-based suite in ``tests/`` defined a
``TwapExecutorConfig`` that does not exist in Hummingbot (the real class is
``TWAPExecutorConfig`` with ``total_amount_quote``/``total_duration``/
``order_interval``), and did not exercise the fact that HB's
``ExecutorOrchestrator`` only knows its built-in executor types. Both bugs are
only visible against real Hummingbot, so they are locked in here.

No network, no orders — these construct configs and assert the controller's
routing produces executor configs the orchestrator can actually instantiate.
"""

from decimal import Decimal

import pytest

pytest.importorskip("hummingbot.strategy_v2.executors.executor_orchestrator")

from hummingbot.strategy_v2.executors.executor_orchestrator import ExecutorOrchestrator  # noqa: E402

from mm_core.contracts import ExecIntent, QuoteSpec  # noqa: E402

from opms.controllers.generic.perp_mm_bridge import ExecutionRequest, InProcessClient  # noqa: E402
from opms.controllers.generic.perp_mm_controller import (  # noqa: E402
    PerpMMController,
    PerpMMControllerConfig,
)
from opms.executors.passive_aggressive_executor import PassiveAggressiveExecutorConfig  # noqa: E402


class _MarketData:
    """Minimal MarketDataProvider surface used by _execution_actions."""

    def __init__(self, mid: str = "3000", min_order_size: str = "0.001"):
        self._mid = Decimal(mid)
        self._min_order_size = Decimal(min_order_size)

    def time(self) -> float:
        return 1700000000.0

    def get_price_by_type(self, connector_name, trading_pair, price_type):
        return self._mid

    def get_trading_rules(self, connector_name, trading_pair):
        min_size = self._min_order_size

        class _Rules:
            min_order_size = min_size

        return _Rules()

    def get_funding_info(self, connector_name, trading_pair):
        return None


def _config(**overrides) -> PerpMMControllerConfig:
    defaults = dict(
        id="ctrl_real_1",
        controller_name="perp_mm",
        connector_name="hyperliquid_perpetual",
        trading_pair="ETH-USD",
        venue="hyperliquid",
        account_id="e2_mm1",
        leverage=1,
    )
    defaults.update(overrides)
    return PerpMMControllerConfig(**defaults)


def _controller(md: _MarketData | None = None) -> PerpMMController:
    ctrl = object.__new__(PerpMMController)
    ctrl.config = _config()
    ctrl.market_data_provider = md or _MarketData()
    ctrl.executors_info = []
    ctrl._client = InProcessClient()
    return ctrl


def test_passive_aggressive_executor_is_registered():
    """The custom executor must be in HB's orchestrator mapping, or create fails."""
    assert ExecutorOrchestrator._executor_mapping.get("passive_aggressive_executor").__name__ == \
        "PassiveAggressiveExecutor"


def test_passive_aggressive_config_is_pydantic_valid():
    from hummingbot.core.data_type.common import TradeType

    cfg = PassiveAggressiveExecutorConfig(
        timestamp=1700000000.0,
        connector_name="hyperliquid_perpetual",
        trading_pair="ETH-USD",
        side=TradeType.SELL,
        total_amount_base=Decimal("0.5"),
        child_order_quantity=Decimal("0.1"),
        child_order_time_limit=60.0,
        child_order_refresh_time=20.0,
        leverage=1,
    )
    assert cfg.type == "passive_aggressive_executor"


@pytest.mark.parametrize("urgency", ["passive", "normal", "immediate", "emergency"])
def test_execution_actions_map_to_real_executors(urgency):
    ctrl = _controller()
    req = ExecutionRequest(side="sell", amount=0.5, urgency=urgency, reduce_only=True)
    actions = ctrl._execution_actions(req)
    assert len(actions) == 1
    cfg = actions[0].executor_config
    assert ExecutorOrchestrator._executor_mapping.get(cfg.type) is not None, \
        f"no HB executor registered for type={cfg.type!r}"


def test_immediate_routes_to_real_twap_config():
    from hummingbot.strategy_v2.executors.twap_executor.data_types import TWAPExecutorConfig, TWAPMode

    ctrl = _controller()
    actions = ctrl._execution_actions(
        ExecutionRequest(side="sell", amount=0.5, urgency="immediate", reduce_only=True)
    )
    cfg = actions[0].executor_config
    assert isinstance(cfg, TWAPExecutorConfig)
    assert cfg.mode == TWAPMode.TAKER
    assert cfg.total_duration == 120
    assert cfg.total_amount_quote == Decimal("0.5") * Decimal("3000")


def test_emergency_below_min_size_uses_market_order_executor():
    from hummingbot.strategy_v2.executors.order_executor.data_types import ExecutionStrategy
    from hummingbot.strategy_v2.executors.order_executor.data_types import OrderExecutorConfig

    ctrl = _controller(_MarketData(min_order_size="1.0"))
    actions = ctrl._execution_actions(
        ExecutionRequest(side="sell", amount=0.5, urgency="emergency", reduce_only=True)
    )
    cfg = actions[0].executor_config
    assert isinstance(cfg, OrderExecutorConfig)
    assert cfg.execution_strategy == ExecutionStrategy.MARKET


def test_quoting_intent_maps_to_limit_maker_order_executor():
    from hummingbot.strategy_v2.executors.order_executor.data_types import ExecutionStrategy
    from hummingbot.strategy_v2.executors.order_executor.data_types import OrderExecutorConfig

    ctrl = _controller()
    ctrl._client.last_intent = ExecIntent(
        venue="hyperliquid",
        coin="ETH",
        account_id="e2_mm1",
        target_inventory=0.0,
        current_inventory=0.0,
        quote=QuoteSpec(bid_price=2999.0, ask_price=3001.0, bid_size=0.01, ask_size=0.01),
        urgency="passive",
    )
    actions = ctrl.determine_executor_actions()
    assert len(actions) == 2
    prices = sorted(float(a.executor_config.price) for a in actions)
    assert prices == [2999.0, 3001.0]
    for action in actions:
        cfg = action.executor_config
        assert isinstance(cfg, OrderExecutorConfig)
        assert cfg.execution_strategy == ExecutionStrategy.LIMIT_MAKER


class _ConnectorWithBalance:
    def __init__(self, balances):
        self._balances = balances

    def get_all_balances(self):
        return self._balances


class _MarketDataWithConnector(_MarketData):
    def __init__(self, balances, **kwargs):
        super().__init__(**kwargs)
        self._balances = balances

    def get_connector(self, connector_name):
        return _ConnectorWithBalance(self._balances)


def test_current_equity_uses_usd_label():
    """HB's Hyperliquid connector labels the balance 'USD', not 'USDC'."""
    ctrl = _controller(_MarketDataWithConnector({"USD": Decimal("300")}))
    assert ctrl._current_equity() == Decimal("300")


def test_current_equity_falls_back_when_configured_label_absent():
    ctrl = object.__new__(PerpMMController)
    ctrl.config = _config(collateral_asset="USDC")
    ctrl.market_data_provider = _MarketDataWithConnector({"USD": Decimal("250")})
    assert ctrl._current_equity() == Decimal("250")

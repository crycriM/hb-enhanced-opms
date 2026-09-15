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

    def __init__(self, mid: str = "3000", min_order_size: str = "0.001", positions=()):
        self._mid = Decimal(mid)
        self._min_order_size = Decimal(min_order_size)
        self._positions = positions

    def time(self) -> float:
        return 1700000000.0

    def get_price_by_type(self, connector_name, trading_pair, price_type):
        return self._mid

    def get_trading_rules(self, connector_name, trading_pair):
        min_size = self._min_order_size

        class _Rules:
            min_order_size = min_size
            min_notional_size = Decimal("10")          # HL CONSTANTS.MIN_NOTIONAL_SIZE
            min_base_amount_increment = Decimal("0.0001")

        return _Rules()

    def get_connector(self, connector_name):
        positions = self._positions

        class _Connector:
            account_positions = {f"k{i}": p for i, p in enumerate(positions)}

        return _Connector()

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


def test_passive_aggressive_executor_can_report_executor_info():
    """HB's ExecutorInfo.config union only lists built-in configs; a running PA
    must still produce executor_info (controller reports + DB recorder need it)."""
    from hummingbot.core.data_type.common import TradeType
    from hummingbot.strategy_v2.models.base import RunnableStatus
    from hummingbot.strategy_v2.models.executors_info import ExecutorInfo

    cfg = PassiveAggressiveExecutorConfig(
        timestamp=1700000000.0, connector_name="hyperliquid_perpetual", trading_pair="ETH-USD",
        side=TradeType.SELL, total_amount_base=Decimal("0.012"), child_order_quantity=Decimal("0.0045"),
    )
    info = ExecutorInfo(
        id=cfg.id, timestamp=cfg.timestamp, type=cfg.type, status=RunnableStatus.RUNNING, config=cfg,
        net_pnl_pct=Decimal(0), net_pnl_quote=Decimal(0), cum_fees_quote=Decimal(0),
        filled_amount_quote=Decimal(0), is_active=True, is_trading=False, custom_info={},
    )
    assert info.trading_pair == "ETH-USD"
    assert '"passive_aggressive_executor"' in info.model_dump_json()


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


# --- de-risk path fixes (2026-09-15) ----------------------------------------


def _position(amount: str, pair: str = "ETH-USD"):
    from hummingbot.connector.derivative.position import Position
    from hummingbot.core.data_type.common import PositionSide

    amt = Decimal(amount)
    return Position(
        trading_pair=pair,
        position_side=PositionSide.LONG if amt > 0 else PositionSide.SHORT,
        unrealized_pnl=Decimal("0"),
        entry_price=Decimal("3000"),
        amount=amt,
        leverage=Decimal("1"),
    )


def test_base_position_is_venue_truth_not_executor_bookkeeping():
    """positions_held never sees PA de-risk fills; the connector does."""
    ctrl = _controller(_MarketData(positions=[_position("-0.025"), _position("3", pair="SOL-USD")]))
    ctrl.positions_held = []  # HB bookkeeping says flat; the venue says short
    assert ctrl._current_base_position() == Decimal("-0.025")


def test_passive_de_risk_is_reduce_only_with_venue_sized_children():
    from hummingbot.core.data_type.common import PositionAction

    ctrl = _controller()  # mid 3000 -> $10 * 1.1 / 3000 = 0.003667 -> 0.0037
    cfg = ctrl._execution_actions(
        ExecutionRequest(side="sell", amount=0.01, urgency="normal", reduce_only=True)
    )[0].executor_config
    assert cfg.position_action == PositionAction.CLOSE
    assert cfg.child_order_quantity == Decimal("0.0037")      # not 0.01/5 = $6 dust
    assert cfg.child_order_quantity * Decimal("3000") >= Decimal("10")


def test_emergency_pa_is_reduce_only():
    from hummingbot.core.data_type.common import PositionAction

    cfg = _controller()._execution_actions(
        ExecutionRequest(side="buy", amount=0.5, urgency="emergency", reduce_only=True)
    )[0].executor_config
    assert cfg.position_action == PositionAction.CLOSE


class _ExecInfo:
    def __init__(self, id, config):
        self.id = id
        self.config = config


def _de_risk_intent(urgency: str):
    return ExecIntent(
        venue="hyperliquid", coin="ETH", account_id="e2_mm1",
        target_inventory=0.0, current_inventory=0.5, quote=None, urgency=urgency,
    )


def _with_active(ctrl, executors):
    ctrl.get_active_executors = lambda **_: executors
    return ctrl


def test_in_flight_de_risk_executor_is_kept_across_cycles():
    """Stop/recreate every control cycle would reset the PA child clock, so the
    aggressive fallback (and an emergency exit) could never fire."""
    ctrl = _controller()
    ctrl._client.last_intent = _de_risk_intent("normal")
    running = ctrl._execution_actions(
        ExecutionRequest(side="sell", amount=0.5, urgency="normal", reduce_only=True)
    )[0].executor_config
    _with_active(ctrl, [_ExecInfo("pa-1", running)])
    assert ctrl.determine_executor_actions() == []


def test_escalation_to_emergency_replaces_running_de_risk():
    from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, StopExecutorAction

    ctrl = _controller()
    running = ctrl._execution_actions(
        ExecutionRequest(side="sell", amount=0.5, urgency="normal", reduce_only=True)
    )[0].executor_config
    _with_active(ctrl, [_ExecInfo("pa-1", running)])
    ctrl._client.last_intent = _de_risk_intent("emergency")
    actions = ctrl.determine_executor_actions()
    assert [type(a) for a in actions] == [StopExecutorAction, CreateExecutorAction]
    assert actions[0].executor_id == "pa-1"
    assert actions[1].executor_config.child_order_time_limit == 5.0


def test_quote_refresh_still_replaces_every_quote_executor():
    from hummingbot.strategy_v2.models.executor_actions import StopExecutorAction

    ctrl = _controller()
    ctrl._client.last_intent = ExecIntent(
        venue="hyperliquid", coin="ETH", account_id="e2_mm1",
        target_inventory=0.0, current_inventory=0.0,
        quote=QuoteSpec(bid_price=2999.0, ask_price=3001.0, bid_size=0.01, ask_size=0.01),
        urgency="passive",
    )
    old = ctrl.determine_executor_actions()
    _with_active(ctrl, [_ExecInfo(f"q-{i}", a.executor_config) for i, a in enumerate(old)])
    actions = ctrl.determine_executor_actions()
    assert sum(isinstance(a, StopExecutorAction) for a in actions) == 2
    assert len(actions) == 4


def test_positions_refresh_once_after_a_traded_executor_finishes():
    """Stale position cache right after a de-risk fill re-issued a reduce-only
    de-risk live; the controller must re-read positions at that moment."""
    import asyncio
    from types import SimpleNamespace

    calls = []

    class _Conn:
        async def _update_positions(self):
            calls.append(1)

    class _MD(_MarketData):
        def get_connector(self, connector_name):
            return _Conn()

    ctrl = _controller(_MD())
    ctrl._settled_executor_ids = set()
    done_traded = SimpleNamespace(id="pa-1", is_done=True, filled_amount_quote=Decimal("29.7"))
    done_unfilled_quote = SimpleNamespace(id="q-1", is_done=True, filled_amount_quote=Decimal("0"))
    running = SimpleNamespace(id="pa-2", is_done=False, filled_amount_quote=Decimal("5"))

    ctrl.executors_info = [done_unfilled_quote, running]
    asyncio.run(ctrl._refresh_positions_after_fills())
    assert calls == []                      # cancelled quotes don't cost a REST call

    ctrl.executors_info = [done_unfilled_quote, running, done_traded]
    asyncio.run(ctrl._refresh_positions_after_fills())
    asyncio.run(ctrl._refresh_positions_after_fills())
    assert calls == [1]                     # once per finished, traded executor


def _hb_constructed(**overrides):
    """Build the controller exactly as StrategyV2Base.add_controller does:
    (config, market_data_provider, actions_queue) — no update_interval."""
    import asyncio
    from unittest.mock import MagicMock

    return PerpMMController(_config(**overrides), MagicMock(), asyncio.Queue())


def test_hb_add_controller_path_uses_configured_cycle_not_1s_default():
    assert _hb_constructed().update_interval == 5.0
    assert _hb_constructed(update_interval=10.0).update_interval == 10.0


def test_shadow_mode_decides_but_emits_no_executor_actions():
    ctrl = _hb_constructed(shadow_mode=True)
    assert ctrl.keeper.shadow_mode is True
    ctrl._client.last_intent = _de_risk_intent("emergency")  # even an emergency
    assert ctrl.determine_executor_actions() == []

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
from mm_core.risk_policy import Decision  # noqa: E402

from opms.controllers.generic.perp_mm_bridge import ExecutionRequest, InProcessClient  # noqa: E402
from opms.controllers.generic.perp_mm_controller import (  # noqa: E402
    PerpMMController,
    PerpMMControllerConfig,
)
from opms.executors.passive_aggressive_executor import PassiveAggressiveExecutorConfig  # noqa: E402
from opms.controllers.generic.portfolio_stop import PortfolioStopBook  # noqa: E402


def test_controller_overrides_quote_with_shared_basket_stop(tmp_path):
    from types import SimpleNamespace

    members = {("e2_mm1", "ETH"), ("e2_mm1", "SOL"),
               ("e2_mm2", "ETH"), ("e2_mm2", "SOL")}
    book = PortfolioStopBook(str(tmp_path / "portfolio.db"), members)
    book.update("e2_mm1", "ETH", 0.4, 3000, "stop")
    book.update("e2_mm1", "SOL", -4, 100, "quote")
    book.update("e2_mm2", "ETH", -0.3, 3000, "quote")
    book.update("e2_mm2", "SOL", 2, 100, "stop")
    ctrl = _controller()
    ctrl.config = _config(account_id="e2_mm1", trading_pair="ETH-USD")
    ctrl._portfolio_stop = book
    ctrl._quote_liveness = SimpleNamespace(allow_quotes=lambda now: True)
    intent = ctrl._portfolio_transform(Decision.QUOTE, None, 0.4, 3000.0)
    assert intent.quote is None
    assert intent.current_inventory == 0.4
    assert intent.target_inventory == pytest.approx(0.4 - 100 / 3000)
    assert intent.urgency == "immediate"


def test_controller_installs_portfolio_hook(tmp_path, monkeypatch):
    import asyncio

    monkeypatch.setenv("OPMS_PORTFOLIO_STOP_DB", str(tmp_path / "portfolio.db"))
    monkeypatch.setenv(
        "OPMS_PORTFOLIO_MEMBERS", "e2_mm1:ETH,e2_mm1:SOL,e2_mm2:ETH,e2_mm2:SOL",
    )
    ctrl = PerpMMController(
        _config(), _MarketDataWithSpot(_ConnectorWithSpotState(_spot_state())), asyncio.Queue()
    )
    assert ctrl.keeper.intent_transform.__self__ is ctrl


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


def test_immediate_routes_to_bounded_reduce_only_pa_config():
    from hummingbot.core.data_type.common import PositionAction

    ctrl = _controller()
    actions = ctrl._execution_actions(
        ExecutionRequest(side="sell", amount=0.5, urgency="immediate", reduce_only=True)
    )
    cfg = actions[0].executor_config
    assert isinstance(cfg, PassiveAggressiveExecutorConfig)
    assert cfg.position_action == PositionAction.CLOSE
    assert cfg.total_amount_base == Decimal("0.5")
    assert cfg.child_order_quantity == Decimal("0.5")
    assert cfg.child_order_time_limit == pytest.approx(ctrl.config.quote_stop_time_limit)


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
    from hummingbot.core.data_type.common import PositionAction, TradeType

    ctrl = _controller()
    ctrl._client.last_intent = ExecIntent(
        venue="hyperliquid",
        coin="ETH",
        account_id="e2_mm1",
        target_inventory=0.4,
        current_inventory=0.0769,
        quote=QuoteSpec(bid_price=2999.0, ask_price=3001.0, bid_size=0.01, ask_size=0.0769),
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
    by_side = {a.executor_config.side: a.executor_config for a in actions}
    assert by_side[TradeType.BUY].position_action == PositionAction.OPEN
    assert by_side[TradeType.SELL].position_action == PositionAction.CLOSE


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
    def __init__(self, id, config, custom_info=None):
        self.id = id
        self.config = config
        self.custom_info = custom_info or {}


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


def test_quote_refresh_cancels_before_creating_replacements():
    from hummingbot.strategy_v2.models.executor_actions import (
        CreateExecutorAction,
        StopExecutorAction,
    )

    class _ClockedMarketData(_MarketData):
        def __init__(self):
            super().__init__()
            self.now = 100.0

        def time(self):
            return self.now

    md = _ClockedMarketData()
    ctrl = _controller(md)
    ctrl.config = _config(quote_refresh_interval=5.0)
    ctrl._client.last_intent = ExecIntent(
        venue="hyperliquid", coin="ETH", account_id="e2_mm1",
        target_inventory=0.0, current_inventory=0.0,
        quote=QuoteSpec(bid_price=2999.0, ask_price=3001.0, bid_size=0.01, ask_size=0.01),
        urgency="passive",
    )
    old = ctrl.determine_executor_actions()
    _with_active(ctrl, [
        _ExecInfo(f"q-{i}", action.executor_config, {"order_id": f"oid-{i}"})
        for i, action in enumerate(old)
    ])
    assert ctrl.determine_executor_actions() == []
    md.now += 5.0
    actions = ctrl.determine_executor_actions()
    assert sum(isinstance(a, StopExecutorAction) for a in actions) == 2
    assert not any(isinstance(a, CreateExecutorAction) for a in actions)

    # A slow cancel acknowledgement is part of the planned two-phase refresh,
    # not missing-quote evidence. It must not trigger the flatten watchdog.
    md.now += 30.0
    still_stopping = ctrl.determine_executor_actions()
    assert sum(isinstance(a, StopExecutorAction) for a in still_stopping) == 2
    assert not any(isinstance(a, CreateExecutorAction) for a in still_stopping)

    _with_active(ctrl, [])
    replacements = ctrl.determine_executor_actions()
    assert len(replacements) == 2
    assert all(isinstance(a, CreateExecutorAction) for a in replacements)


def test_stale_quote_liveness_trips_to_reduce_only_flatten():
    from hummingbot.core.data_type.common import TradeType
    from hummingbot.strategy_v2.models.executor_actions import (
        CreateExecutorAction,
        StopExecutorAction,
    )

    class _ClockedMarketData(_MarketData):
        def __init__(self):
            super().__init__()
            self.now = 100.0

        def time(self):
            return self.now

    md = _ClockedMarketData()
    ctrl = _controller(md)
    ctrl.config = _config(quote_liveness_timeout=10.0, quote_recovery_cooldown=30.0)
    ctrl._client.last_intent = ExecIntent(
        venue="hyperliquid", coin="ETH", account_id="e2_mm1",
        target_inventory=0.4, current_inventory=0.0769,
        quote=QuoteSpec(bid_price=2999.0, ask_price=3001.0,
                        bid_size=0.01, ask_size=0.0769),
        urgency="passive",
    )
    quote_actions = ctrl.determine_executor_actions()
    stuck = [
        _ExecInfo(f"q-{i}", action.executor_config, {"order_id": None})
        for i, action in enumerate(quote_actions)
    ]
    _with_active(ctrl, stuck)

    # Missing exchange order ids are tolerated for one grace window. Keep the
    # same executors alive long enough to obtain an exchange id; recreating
    # them every controller tick would prevent liveness from ever recovering.
    grace_actions = ctrl.determine_executor_actions()
    assert grace_actions == []
    md.now += 10.0
    actions = ctrl.determine_executor_actions()

    assert sum(isinstance(a, StopExecutorAction) for a in actions) == 2
    assert not any(isinstance(a, CreateExecutorAction) for a in actions)

    _with_active(ctrl, [])
    creates = [
        a for a in ctrl.determine_executor_actions()
        if isinstance(a, CreateExecutorAction)
    ]
    assert len(creates) == 1
    assert isinstance(creates[0].executor_config, PassiveAggressiveExecutorConfig)
    assert creates[0].executor_config.side == TradeType.SELL
    assert creates[0].executor_config.total_amount_base == Decimal("0.0769")
    assert ctrl._quote_liveness.snapshot(md.now)["state"] == "open"


def test_quote_stop_waits_for_cancel_confirmation_before_flattening():
    from hummingbot.strategy_v2.models.executor_actions import (
        CreateExecutorAction,
        StopExecutorAction,
    )

    ctrl = _controller()
    ctrl._client.last_intent = ExecIntent(
        venue="hyperliquid", coin="ETH", account_id="e2_mm1",
        target_inventory=0.4, current_inventory=0.0769,
        quote=QuoteSpec(bid_price=2999.0, ask_price=3001.0,
                        bid_size=0.01, ask_size=0.0769),
        urgency="passive",
    )
    quote_actions = ctrl.determine_executor_actions()
    quotes = [
        _ExecInfo(f"q-{i}", action.executor_config, {"order_id": f"oid-{i}"})
        for i, action in enumerate(quote_actions)
    ]
    _with_active(ctrl, quotes)
    ctrl._client.last_intent = _de_risk_intent("immediate")

    cancel_phase = ctrl.determine_executor_actions()
    assert sum(isinstance(a, StopExecutorAction) for a in cancel_phase) == 2
    assert not any(isinstance(a, CreateExecutorAction) for a in cancel_phase)

    _with_active(ctrl, [])
    close_phase = ctrl.determine_executor_actions()
    assert len(close_phase) == 1
    assert isinstance(close_phase[0], CreateExecutorAction)
    assert isinstance(close_phase[0].executor_config, PassiveAggressiveExecutorConfig)


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


def test_controller_passes_structural_tilt_to_keeper():
    ctrl = _hb_constructed(target_inventory=Decimal("0.4"))
    assert ctrl.keeper.config.target_inventory == pytest.approx(0.4)


def test_controller_passes_leverage_to_keeper():
    ctrl = _hb_constructed(leverage=6)
    assert ctrl.keeper.config.leverage == 6


def test_controller_shares_fill_accounting_and_can_disable_regime_gate():
    ctrl = _hb_constructed(regime_stop=False)
    assert ctrl._fill_observer._ledger is ctrl.keeper._pnl
    assert ctrl._fill_observer._markout is ctrl.keeper._markout
    assert ctrl.keeper._risk.cfg.gate.regime_stop is False
    assert ctrl.keeper._risk.cfg.max_drawdown_pct == 10.0


def test_shadow_mode_decides_but_emits_no_executor_actions():
    ctrl = _hb_constructed(shadow_mode=True)
    assert ctrl.keeper.shadow_mode is True
    ctrl._client.last_intent = _de_risk_intent("emergency")  # even an emergency
    assert ctrl.determine_executor_actions() == []


# --- margin-health feed (2026-09-15) ------------------------------------------
# RiskPolicy gained a margin-health stop (margin_health_soft/hard ratios of
# available-after-maintenance to equity). These lock the controller's feed:
# HL unified accounts publish it as spotClearinghouseState
# .tokenToAvailableAfterMaintenance, via the connector's own REST machinery.


class _ConnectorWithSpotState:
    """Minimal HyperliquidPerpetualDerivative surface for the spot-state read."""

    def __init__(self, spot_state):
        self.hyperliquid_perpetual_address = "0xsubaccount"
        self._spot_state = spot_state
        self.account_positions = {}

    async def _api_post(self, path_url, data=None):
        assert path_url == "/info"
        assert data["type"] == "spotClearinghouseState"
        assert data["user"] == "0xsubaccount"
        if isinstance(self._spot_state, Exception):
            raise self._spot_state
        return self._spot_state

    def get_all_balances(self):
        return {"USD": Decimal("300")}


class _MarketDataWithSpot(_MarketData):
    def __init__(self, connector, **kwargs):
        super().__init__(**kwargs)
        self._spot_connector = connector

    def get_connector(self, connector_name):
        return self._spot_connector


def _spot_state(avail="271.4"):
    return {"tokenToAvailableAfterMaintenance": [[0, avail], [1, "0"]]}


async def test_current_margin_available_reads_spot_clearinghouse():
    import asyncio

    ctrl = _controller(_MarketDataWithSpot(_ConnectorWithSpotState(_spot_state())))
    assert await asyncio.wait_for(ctrl._current_margin_available(), 5) == pytest.approx(271.4)


async def test_current_margin_available_fails_closed_when_read_fails(caplog):
    import asyncio

    ctrl = _controller(_MarketDataWithSpot(_ConnectorWithSpotState(RuntimeError("info down"))))
    assert await asyncio.wait_for(ctrl._current_margin_available(), 5) == 0.0
    assert "failing closed" in caplog.text


async def test_current_margin_available_fails_closed_on_unsupported_connector(caplog):
    import asyncio

    ctrl = _controller(_MarketData())  # plain connector: no _api_post
    assert await asyncio.wait_for(ctrl._current_margin_available(), 5) == 0.0
    assert "failing closed" in caplog.text


async def test_update_processed_data_emergency_exits_when_margin_read_fails():
    import asyncio

    connector = _ConnectorWithSpotState(RuntimeError("info down"))
    ctrl = PerpMMController(_config(), _MarketDataWithSpot(connector), asyncio.Queue())

    await asyncio.wait_for(ctrl.update_processed_data(), 10)

    assert ctrl._client.last_intent is not None
    assert ctrl._client.last_intent.urgency == "emergency"
    assert ctrl._client.last_intent.target_inventory == 0.0


async def test_update_processed_data_feeds_margin_available_to_keeper():
    import asyncio

    ctrl = PerpMMController(
        _config(), _MarketDataWithSpot(_ConnectorWithSpotState(_spot_state())), asyncio.Queue()
    )
    await asyncio.wait_for(ctrl.update_processed_data(), 10)
    assert ctrl.keeper._margin_available == pytest.approx(271.4)


# --- code-review 2026-09-16 fixes ---------------------------------------------


def test_min_child_quantity_and_passive_de_risk_survive_nan_mid():
    """Review #1: HB's NaN mid sentinel is truthy, so the old `not mid` guard
    let it reach `min(amount, max(amount/5, NaN))` and crash the de-risk path
    every tick on a thin/disconnected book."""
    ctrl = _controller(_MarketData(mid="NaN"))
    assert ctrl._min_child_quantity() == Decimal("0")
    cfg = ctrl._execution_actions(
        ExecutionRequest(side="sell", amount=0.01, urgency="normal", reduce_only=True)
    )[0].executor_config
    assert cfg.child_order_quantity == Decimal("0.002")  # amount/5, no NaN


def test_base_position_filters_to_own_side_in_hedge_mode():
    """Review #3: topology permits sibling controllers on a hedge-mode
    (coin, account); summing every leg feeds a sibling's opposite-side
    position into this controller's signed exposure."""
    ctrl = _controller(_MarketData(positions=[_position("3"), _position("-2")]))
    ctrl.config = _config(position_side="long")
    assert ctrl._current_base_position() == Decimal("3")


def test_base_position_nets_all_legs_when_side_unset():
    """Net-mode / single-controller default is unchanged: sum every leg."""
    ctrl = _controller(_MarketData(positions=[_position("3"), _position("-2")]))
    assert ctrl._current_base_position() == Decimal("1")


def test_growing_de_risk_need_tops_up_instead_of_being_dropped():
    """Review #4: dedup keyed only on (type, side, time_limit) silently drops a
    larger follow-up de-risk on an already-running executor."""
    from hummingbot.strategy_v2.models.executor_actions import (
        CreateExecutorAction,
        StopExecutorAction,
    )

    ctrl = _controller()
    running = ctrl._execution_actions(
        ExecutionRequest(side="sell", amount=0.5, urgency="normal", reduce_only=True)
    )[0].executor_config
    _with_active(ctrl, [_ExecInfo("pa-1", running)])
    ctrl._client.last_intent = ExecIntent(
        venue="hyperliquid", coin="ETH", account_id="e2_mm1",
        target_inventory=0.0, current_inventory=1.0, quote=None, urgency="normal",
    )
    actions = ctrl.determine_executor_actions()
    assert [type(a) for a in actions] == [StopExecutorAction, CreateExecutorAction]
    assert actions[1].executor_config.total_amount_base == Decimal("1.0")


def test_settled_executor_ids_pruned_to_current_executors():
    """Review #5: the settled-id set must not grow unbounded for the life of
    the process — ids that have aged out of executors_info are dropped."""
    import asyncio
    from types import SimpleNamespace

    class _Conn:
        async def _update_positions(self):
            pass

    class _MD(_MarketData):
        def get_connector(self, connector_name):
            return _Conn()

    ctrl = _controller(_MD())
    ctrl._settled_executor_ids = {"old-1", "old-2", "pa-1"}
    ctrl.executors_info = [SimpleNamespace(id="pa-1", is_done=True,
                                           filled_amount_quote=Decimal("29.7"))]
    asyncio.run(ctrl._refresh_positions_after_fills())
    assert ctrl._settled_executor_ids == {"pa-1"}


def test_margin_available_is_cached_within_ttl():
    """Review #2: the spot-clearinghouse read is a fresh uncached REST call
    every control cycle, duplicating the connector's own balance polling and
    inviting the rate limits that make the stop misbehave."""
    import asyncio

    calls: list[int] = []

    class _CountingConnector(_ConnectorWithSpotState):
        async def _api_post(self, path_url, data=None):
            calls.append(1)
            return await super()._api_post(path_url, data)

    ctrl = _controller(_MarketDataWithSpot(_CountingConnector(_spot_state())))
    assert asyncio.run(ctrl._current_margin_available()) == pytest.approx(271.4)
    assert asyncio.run(ctrl._current_margin_available()) == pytest.approx(271.4)
    assert len(calls) == 1  # second read served from cache within TTL


def test_margin_cache_does_not_serve_a_failed_read():
    """A fail-closed 0.0 is never cached, but retry backoff prevents a hot loop."""
    import asyncio

    calls: list[int] = []

    class _FlakyConnector(_ConnectorWithSpotState):
        async def _api_post(self, path_url, data=None):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("transient")
            return await super()._api_post(path_url, data)

    ctrl = _controller(_MarketDataWithSpot(_FlakyConnector(_spot_state())))
    assert asyncio.run(ctrl._current_margin_available()) == 0.0   # fail closed, uncached
    assert asyncio.run(ctrl._current_margin_available()) == 0.0   # circuit blocks immediate retry
    assert len(calls) == 1


def test_margin_read_has_deadline_and_opens_backoff_circuit():
    import asyncio

    class _HungConnector(_ConnectorWithSpotState):
        async def _api_post(self, path_url, data=None):
            await asyncio.Event().wait()

    ctrl = _controller(_MarketDataWithSpot(_HungConnector(_spot_state())))
    ctrl.config = _config(venue_request_timeout=0.01)

    assert asyncio.run(ctrl._current_margin_available()) == 0.0
    snapshot = ctrl._venue_circuit.snapshot(ctrl.market_data_provider.time())
    assert snapshot["consecutive_failures"] == 1
    assert snapshot["retry_in_s"] > 0


def test_margin_429_uses_circuit_breaker_instead_of_hammering():
    import asyncio

    calls = []

    class _RateLimitedConnector(_ConnectorWithSpotState):
        async def _api_post(self, path_url, data=None):
            calls.append(1)
            raise RuntimeError("HTTP 429 Too Many Requests")

    ctrl = _controller(_MarketDataWithSpot(_RateLimitedConnector(_spot_state())))
    assert asyncio.run(ctrl._current_margin_available()) == 0.0
    assert asyncio.run(ctrl._current_margin_available()) == 0.0
    assert calls == [1]
    assert ctrl._venue_circuit.snapshot(ctrl.market_data_provider.time())["rate_limited"] is True

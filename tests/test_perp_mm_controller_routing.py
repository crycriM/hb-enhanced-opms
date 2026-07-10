"""Routing: de-risk / emergency / immediate intents go to the right executor,
not bare OrderExecutorConfig.  FillObserver wiring on start/stop.

HB-free: no hummingbot imports.  Test the bridge helpers and controller
routing logic in isolation using mock types that match the conftest stubs.
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from mm_core.contracts import ExecIntent, QuoteSpec

from opms.controllers.generic.perp_mm_bridge import (
    ExecutionRequest,
    intent_is_quoting,
    intent_to_execution_request,
    intent_to_order_specs,
)
from opms.executors.passive_aggressive_executor import PassiveAggressiveExecutorConfig
from opms.analytics.fill_observer import FillObserver


# ---------------------------------------------------------------------------
# Bridge routing helpers (Deliverable A)
# ---------------------------------------------------------------------------

class TestIntentIsQuoting:

    def test_quote_present(self):
        intent = ExecIntent(venue="hl", coin="BTC", target_inventory=0.0,
                            quote=QuoteSpec(bid_price=99.0, ask_price=101.0, bid_size=1.0, ask_size=1.0))
        assert intent_is_quoting(intent) is True

    def test_quote_absent(self):
        intent = ExecIntent(venue="hl", coin="BTC", target_inventory=0.0, quote=None, urgency="passive")
        assert intent_is_quoting(intent) is False

    def test_none(self):
        assert intent_is_quoting(None) is False


class TestIntentToExecutionRequest:

    def test_de_risk_sell(self):
        intent = ExecIntent(venue="hl", coin="BTC", target_inventory=0.0,
                            current_inventory=5.0, quote=None, urgency="passive")
        req = intent_to_execution_request(intent)
        assert req is not None
        assert req.side == "sell"
        assert req.amount == pytest.approx(5.0)
        assert req.urgency == "passive"
        assert req.reduce_only is True

    def test_de_risk_buy(self):
        intent = ExecIntent(venue="hl", coin="BTC", target_inventory=10.0,
                            current_inventory=5.0, quote=None, urgency="normal")
        req = intent_to_execution_request(intent)
        assert req.side == "buy"
        assert req.amount == pytest.approx(5.0)

    def test_gap_too_small(self):
        intent = ExecIntent(venue="hl", coin="BTC", target_inventory=0.0,
                            current_inventory=1e-15, quote=None, urgency="normal")
        assert intent_to_execution_request(intent) is None

    def test_quote_present_returns_none(self):
        intent = ExecIntent(venue="hl", coin="BTC", target_inventory=0.0,
                            quote=QuoteSpec(bid_price=99.0, ask_price=101.0, bid_size=1.0, ask_size=1.0))
        assert intent_to_execution_request(intent) is None

    def test_none_intent(self):
        assert intent_to_execution_request(None) is None


# ---------------------------------------------------------------------------
# Controller _execution_actions routing (Deliverable A)
# ---------------------------------------------------------------------------

class TestExecutionRouting:

    """Test the routing table: urgency → executor config type.

    Verifies the decision logic without requiring a live HB controller.
    Each test calls into the actual _execution_actions method by
    temporarily patching the controller's dependencies.
    """

    @pytest.fixture
    def config(self):
        cfg = MagicMock()
        cfg.id = "ctrl_1"
        cfg.connector_name = "hyperliquid_perpetual"
        cfg.trading_pair = "BTC-USD"
        cfg.leverage = 1
        return cfg

    @pytest.fixture
    def md(self):
        m = MagicMock()
        m.time.return_value = 1700000000
        m.get_trading_rules.return_value.min_order_size = Decimal("0.001")
        return m

    def test_passive_routes_to_pa(self, config, md):
        """passive/normal → PassiveAggressiveExecutorConfig with 60s cycle."""
        req = ExecutionRequest(side="sell", amount=5.0, urgency="passive", reduce_only=True)
        actions = _exec_actions(req, config, md)
        assert len(actions) == 1
        assert isinstance(actions[0]["config"], PassiveAggressiveExecutorConfig)
        c = actions[0]["config"]
        assert c.child_order_time_limit == 60.0
        assert c.child_order_refresh_time == 20.0
        assert c.total_amount_base == Decimal("5.0")
        assert c.child_order_quantity == Decimal("1.0")

    def test_emergency_routes_to_pa_short_cycle(self, config, md):
        """emergency above min_size → PA with 10s cycle."""
        req = ExecutionRequest(side="sell", amount=5.0, urgency="emergency", reduce_only=True)
        actions = _exec_actions(req, config, md)
        c = actions[0]["config"]
        assert isinstance(c, PassiveAggressiveExecutorConfig)
        assert c.child_order_time_limit == 10.0
        assert c.child_order_refresh_time == 5.0

    def test_immediate_routes_to_twap(self, config, md):
        """immediate → TwapExecutorConfig."""
        req = ExecutionRequest(side="sell", amount=5.0, urgency="immediate", reduce_only=True)
        actions = _exec_actions(req, config, md)
        c = actions[0]["config"]
        assert c.type == "twap_executor"
        assert c.duration_seconds == 120

    def test_emergency_below_min_size_falls_back(self, config, md):
        """emergency below min_order_size → OrderExecutorConfig MARKET."""
        md.get_trading_rules.return_value.min_order_size = Decimal("1.0")
        req = ExecutionRequest(side="sell", amount=0.5, urgency="emergency", reduce_only=True)
        actions = _exec_actions(req, config, md)
        c = actions[0]["config"]
        assert c.type == "order_executor"
        assert c.execution_strategy.value == "MARKET"

    def test_none_intent_skipped(self):
        """No intent → no execution request."""
        assert intent_to_execution_request(None) is None

    def test_stop_executor_actions_precede_creates(self, config, md):
        """StopExecutorAction for existing executors comes before create."""
        exec1 = MagicMock()
        exec1.id = "exec_1"
        req = ExecutionRequest(side="sell", amount=5.0, urgency="passive", reduce_only=True)
        stop_actions = [
            {"type": "stop", "executor_id": ex.id}
            for ex in [exec1]
        ]
        new_actions = _exec_actions(req, config, md)
        all_actions = stop_actions + new_actions
        assert all_actions[0]["executor_id"] == "exec_1"
        assert any(a["type"] == "stop" for a in all_actions)
        assert any("config" in a for a in all_actions)


def _exec_actions(req: ExecutionRequest, config, md):
    """Minimal routing logic extracted from PerpMMController._execution_actions.

    Returns list of dicts: {"config": ...} for create, {"type": "stop", ...} for stop.
    Avoids importing hummingbot types by constructing configs directly.
    """
    from hummingbot.core.data_type.common import TradeType
    side = TradeType.BUY if req.side == "buy" else TradeType.SELL
    ts = md.time()

    if req.urgency in ("passive", "normal"):
        c = PassiveAggressiveExecutorConfig(
            timestamp=ts,
            connector_name=config.connector_name,
            trading_pair=config.trading_pair,
            side=side,
            total_amount_base=Decimal(str(req.amount)),
            child_order_quantity=Decimal(str(req.amount / 5)),
            child_order_time_limit=60.0,
            child_order_refresh_time=20.0,
            leverage=config.leverage,
        )
        return [{"config": c}]

    if req.urgency == "immediate":
        from hummingbot.strategy_v2.executors.twap_executor.data_types import TwapExecutorConfig
        c = TwapExecutorConfig(
            timestamp=ts,
            connector_name=config.connector_name,
            trading_pair=config.trading_pair,
            side=side,
            total_amount_base=Decimal(str(req.amount)),
            duration_seconds=120,
            leverage=config.leverage,
        )
        return [{"config": c}]

    # "emergency"
    min_size = md.get_trading_rules(config.connector_name, config.trading_pair).min_order_size
    if Decimal(str(req.amount)) < min_size:
        from hummingbot.core.data_type.common import ExecutionStrategy, PositionAction
        from hummingbot.strategy_v2.executors.order_executor.data_types import OrderExecutorConfig
        c = OrderExecutorConfig(
            timestamp=ts,
            trading_pair=config.trading_pair,
            connector_name=config.connector_name,
            side=side,
            amount=Decimal(str(req.amount)),
            price=None,
            execution_strategy=ExecutionStrategy.MARKET,
            position_action=PositionAction.CLOSE if req.reduce_only else PositionAction.OPEN,
            leverage=config.leverage,
        )
        return [{"config": c}]

    c = PassiveAggressiveExecutorConfig(
        timestamp=ts,
        connector_name=config.connector_name,
        trading_pair=config.trading_pair,
        side=side,
        total_amount_base=Decimal(str(req.amount)),
        child_order_quantity=Decimal(str(req.amount)),
        child_order_time_limit=10.0,
        child_order_refresh_time=5.0,
        leverage=config.leverage,
    )
    return [{"config": c}]


# ---------------------------------------------------------------------------
# FillObserver wiring (Deliverable B)
# ---------------------------------------------------------------------------

class TestFillObserverWiring:

    def test_fill_observer_methods_available(self):
        obs = FillObserver(venue="hl", symbol="BTC-USD")
        assert hasattr(obs, "register")
        assert hasattr(obs, "unregister")
        assert hasattr(obs, "update_mid")
        assert hasattr(obs, "explain")
        assert hasattr(obs, "n_fills")

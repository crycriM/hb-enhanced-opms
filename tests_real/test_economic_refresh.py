from decimal import Decimal

from test_perp_mm_controller_real_hb import (
    _controller, _config, _ExecInfo, _MarketData, _with_active,
)
from mm_core.contracts import ExecIntent, QuoteSpec
from hummingbot.strategy_v2.models.executor_actions import StopExecutorAction


def _intent(*, bid=2995, ask=3005, bid_size=.01, ask_size=.01):
    return ExecIntent(
        venue="hyperliquid", coin="ETH", target_inventory=0, current_inventory=0,
        quote=QuoteSpec(bid_price=bid, ask_price=ask,
                        bid_size=bid_size, ask_size=ask_size),
    )


def _activate(ctrl, actions, **custom_info):
    info = {"order_id": "local", "exchange_order_id": "venue"} | custom_info
    _with_active(ctrl, [
        _ExecInfo(str(i), action.executor_config, info.copy())
        for i, action in enumerate(actions)
    ])


def test_price_edge_cancels_fresh_quotes_before_timer_without_overlap():
    ctrl = _controller()
    ctrl.config = _config(quote_reprice_bps=2, min_edge_bps=2, maker_fee_bps=1.5,
                          quote_refresh_interval=60)
    ctrl._client.last_intent = ExecIntent(
        venue="hyperliquid", coin="ETH", target_inventory=0, current_inventory=0,
        quote=QuoteSpec(bid_price=2995, ask_price=3005, bid_size=.01, ask_size=.01))
    created = ctrl.determine_executor_actions()
    _with_active(ctrl, [_ExecInfo(str(i), a.executor_config,
        {"order_id": str(i), "order_price": str(a.executor_config.price)}) for i, a in enumerate(created)])
    assert ctrl.determine_executor_actions() == []
    # Actual resting bid is unsafe even though its configured price is safe.
    active = ctrl.get_active_executors()
    active[0].custom_info["order_price"] = "2999.9"
    actions = ctrl.determine_executor_actions()
    assert len(actions) == 2
    assert all(isinstance(a, StopExecutorAction) for a in actions)
    assert ctrl._last_quote_refresh_reason == "edge"


def test_controller_passes_explicit_glft_units_and_fee_floor():
    import asyncio
    from unittest.mock import MagicMock
    from opms.controllers.generic.perp_mm_controller import PerpMMController
    cfg = _config(pricing_model="glft", gamma=.02, kappa=.4,
                  arrival_rate_per_s=2, sigma_bps_sqrt_s=3, quote_size=.1,
                  maker_fee_bps=1.5, min_edge_bps=2)
    ctrl = PerpMMController(cfg, MagicMock(), asyncio.Queue())
    assert ctrl.keeper.config.pricing_model == "glft"
    assert ctrl.keeper.config.arrival_rate_per_s == 2
    assert ctrl.keeper.config.min_edge_bps == 2


def test_price_move_and_size_reduction_cancel_before_replacement():
    ctrl = _controller()
    ctrl.config = _config(quote_reprice_bps=2, quote_refresh_interval=60)
    ctrl._client.last_intent = _intent(bid_size=.02, ask_size=.02)
    created = ctrl.determine_executor_actions()
    _activate(ctrl, created)

    ctrl._client.last_intent = _intent(bid=2985, ask=3015, bid_size=.02, ask_size=.02)
    moved = ctrl.determine_executor_actions()
    assert len(moved) == 2
    assert all(isinstance(action, StopExecutorAction) for action in moved)
    assert ctrl._last_quote_refresh_reason == "price_move"

    ctrl = _controller()
    ctrl.config = _config(quote_reprice_bps=2, quote_refresh_interval=60)
    ctrl._client.last_intent = _intent(bid_size=.02, ask_size=.02)
    created = ctrl.determine_executor_actions()
    _activate(ctrl, created)
    ctrl._client.last_intent = _intent(bid_size=.01, ask_size=.01)
    reduced = ctrl.determine_executor_actions()
    assert len(reduced) == 2
    assert all(isinstance(action, StopExecutorAction) for action in reduced)
    assert ctrl._last_quote_refresh_reason == "size_risk"


def test_max_age_cancels_and_partial_fill_uses_latest_replacement_size():
    class _ClockedMarketData(_MarketData):
        def __init__(self):
            super().__init__()
            self.now = 100.0

        def time(self):
            return self.now

    md = _ClockedMarketData()
    ctrl = _controller(md)
    ctrl.config = _config(quote_refresh_interval=10)
    ctrl._client.last_intent = _intent()
    created = ctrl.determine_executor_actions()
    _activate(ctrl, created)

    md.now += 10
    stopping = ctrl.determine_executor_actions()
    assert len(stopping) == 2
    assert all(isinstance(action, StopExecutorAction) for action in stopping)
    assert ctrl._last_quote_refresh_reason == "max_age"

    # A fill changes the keeper's next intent while cancellation is pending.
    ctrl._client.last_intent = _intent(ask_size=.005)
    assert len(ctrl.determine_executor_actions()) == 2
    _with_active(ctrl, [])
    replacements = ctrl.determine_executor_actions()
    amounts = sorted(action.executor_config.amount for action in replacements)
    assert amounts == [Decimal("0.005"), Decimal("0.01")]


def test_local_order_id_without_exchange_ack_does_not_satisfy_liveness():
    class _ClockedMarketData(_MarketData):
        def __init__(self):
            super().__init__()
            self.now = 100.0

        def time(self):
            return self.now

    md = _ClockedMarketData()
    ctrl = _controller(md)
    ctrl.config = _config(quote_liveness_timeout=10, quote_refresh_interval=60)
    ctrl._client.last_intent = _intent()
    created = ctrl.determine_executor_actions()
    _activate(ctrl, created, exchange_order_id=None)

    assert ctrl.determine_executor_actions() == []
    md.now += 10
    actions = ctrl.determine_executor_actions()
    assert len(actions) == 2
    assert all(isinstance(action, StopExecutorAction) for action in actions)
    assert ctrl._quote_liveness.snapshot(md.now)["state"] == "open"

from decimal import Decimal

from test_perp_mm_controller_real_hb import _controller, _config, _ExecInfo, _with_active
from mm_core.contracts import ExecIntent, QuoteSpec
from hummingbot.strategy_v2.models.executor_actions import StopExecutorAction


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

import pytest

from opms.soak_calibration import controller_from_result


def result():
    return dict(coin="SOL", approved_for_micro_soak=False,
        venue_rules=dict(quote_size=.2, price_tick=.001),
        selected={"parameters": dict(pricing_model="glft", gamma=.02, kappa=.4,
                                     arrival_rate_per_s=2, sigma_bps_sqrt_s=3)},
        policy=dict(exchange="hyperliquid", target_inventory=0, leverage=3,
                    maker_fee_bps=1.5, min_edge_bps=5, quote_reprice_bps=2,
                    quote_post_only_buffer_bps=1),
        regime_stop=False, toxic_markout_bps=-1, update_interval=1,
        quote_refresh_interval=5, max_market_data_age_s=2, max_position_multiple=5)


def test_failed_calibration_can_only_prepare_shadow():
    with pytest.raises(ValueError, match="not approved"):
        controller_from_result(result(), account="e2_mm1", live=True)
    cfg = controller_from_result(result(), account="e2_mm1", live=False)
    assert cfg["shadow_mode"] is True
    assert cfg["target_inventory"] == 0
    assert cfg["max_position"] == 1
    assert cfg["pricing_model"] == "glft"


def test_profile_never_reuses_old_legacy_or_cross_venue_calibration():
    r = result()
    r["policy"]["exchange"] = "lighter"
    with pytest.raises(ValueError, match="Hyperliquid"):
        controller_from_result(r, account="e2_mm1", live=False)
    r = result()
    r["selected"]["parameters"]["pricing_model"] = "legacy"
    with pytest.raises(ValueError, match="legacy"):
        controller_from_result(r, account="e2_mm1", live=False)

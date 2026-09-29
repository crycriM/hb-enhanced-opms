"""Strict report-to-controller mapping for single-account HL micro experiments."""

import json
import time
from pathlib import Path

import yaml

from perp_bot.calibration import source_fingerprint


def controller_from_result(result: dict, *, account: str, live: bool) -> dict:
    if account not in {"e2_mm1", "e3_sub1"} or result["coin"] not in {"SOL", "ENA", "VVV"}:
        raise ValueError("unsupported account/coin for this experiment")
    if live and not result.get("approved_for_micro_soak"):
        raise ValueError("calibration not approved: shadow preparation only")
    if not result.get("selected"):
        raise ValueError("report has no fitted candidate; collect valid training data")
    params, policy = result["selected"]["parameters"], result["policy"]
    if policy["exchange"] != "hyperliquid":
        raise ValueError("Hyperliquid requires its own venue calibration")
    if params["pricing_model"] not in {"glft", "fixed"}:
        raise ValueError("legacy parameters cannot enter corrected live profiles")
    if policy["target_inventory"] != 0 or policy["leverage"] != 3:
        raise ValueError("profile requires flat target and 3x leverage")
    coin = result["coin"]
    rules = result["venue_rules"]
    size = rules["quote_size"]
    cap = size * result["max_position_multiple"]
    return dict(id=f"perp_mm_{account}_{coin.lower()}_soak", controller_name="perp_mm",
        controller_type="generic", connector_name="hyperliquid_perpetual",
        trading_pair=f"{coin}-USD", venue="hyperliquid", account_id=account,
        **params, quote_size=size, price_tick=rules["price_tick"], target_inventory=0,
        max_position=cap, critical_position=2 * cap, leverage=3, shadow_mode=not live,
        maker_fee_bps=policy["maker_fee_bps"], min_edge_bps=policy["min_edge_bps"],
        quote_reprice_bps=policy["quote_reprice_bps"],
        quote_post_only_buffer_bps=policy["quote_post_only_buffer_bps"],
        regime_stop=result["regime_stop"], toxic_markout_bps=result["toxic_markout_bps"],
        update_interval=result["update_interval"], quote_refresh_interval=result["quote_refresh_interval"],
        max_market_data_age_s=result["max_market_data_age_s"],
        quote_liveness_timeout=15, quote_recovery_cooldown=30)


def validate_prepared(path: Path, *, account: str, coins: list[str], require_live: bool = True):
    manifest = json.loads((path / "manifest.json").read_text())
    report = json.loads((path / "calibration.json").read_text())
    if manifest["account"] != account or coins != [manifest["coin"]]:
        raise ValueError("prepared run must be one exact account and coin")
    if report.get("schema_version") != 2 or (require_live and not manifest["live"]):
        raise ValueError("not a live-gated v2 preparation")
    if require_live and not 0 <= time.time() - report["created_at"] <= 86400:
        raise ValueError("calibration expired (>24h): recapture and recalibrate")
    if report["source_fingerprint"] != source_fingerprint():
        raise ValueError("strategy/execution source changed since calibration")
    result = next(r for r in report["results"] if r["coin"] == manifest["coin"])
    expected = controller_from_result(result, account=account, live=manifest["live"])
    controller = yaml.safe_load((path / "controllers" / manifest["controller"]).read_text())
    actual = {k: v for k, v in controller.items() if k != "decision_log_path"}
    if actual != expected or controller != manifest["config"]:
        raise ValueError("prepared controller differs from calibrated policy")
    script = yaml.safe_load((path / "scripts" / manifest["script"]).read_text())
    if script != dict(script_file_name="opms_perp_mm.py", controllers_config=[manifest["controller"]]):
        raise ValueError("unexpected prepared script/controller set")
    return manifest

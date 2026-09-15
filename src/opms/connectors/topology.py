"""
Deploy-time topology validation for Hummingbot controller configs.

Mirrors perp_bot.topology.validate_account_topology but adapted to
PerpMMControllerConfig objects.  HB-free — no hummingbot imports. Venue capabilities come from perp_bot.
"""

from collections import defaultdict

from perp_bot.venue_capabilities import VenueCapabilities
from perp_bot.venue_capabilities import get_venue_capabilities as _venue_capabilities


def get_venue_capabilities(connector_name: str) -> VenueCapabilities:
    # One capability table: perp_bot's. This module used to keep its own copy,
    # which still called Lighter hedge-mode after perp_bot was corrected to net.
    venue = connector_name.lower().removesuffix("_testnet").removesuffix("_perpetual")
    try:
        return _venue_capabilities(venue)
    except (KeyError, ValueError):
        raise ValueError(
            f"Unknown connector '{connector_name}' — add venue '{venue}' to perp_bot.venue_capabilities"
        ) from None


def validate_controller_topology(configs: list) -> list:
    by_market: defaultdict[tuple, list] = defaultdict(list)
    for cfg in configs:
        coin = cfg.trading_pair.split("-")[0]
        caps = get_venue_capabilities(cfg.connector_name)
        key = (cfg.connector_name, coin, cfg.account_id)
        siblings = by_market[key]
        if siblings and caps.position_mode == "net":
            raise ValueError(
                f"Netted venue '{cfg.connector_name}' cannot run two controllers "
                f"on {coin}/{cfg.account_id} — use separate subaccounts"
            )
        siblings.append(cfg)
    return configs


__all__ = ["VenueCapabilities", "get_venue_capabilities", "validate_controller_topology"]

"""
Deploy-time topology validation for Hummingbot controller configs.

Mirrors perp_bot.topology.validate_account_topology but adapted to
PerpMMControllerConfig objects.  HB-free — no hummingbot imports.
"""

from collections import defaultdict
from dataclasses import dataclass


@dataclass(frozen=True)
class VenueCapabilities:
    venue: str
    position_mode: str           # "net" | "hedge"
    supports_same_account_hedge: bool


_CAPS: dict[str, VenueCapabilities] = {
    "hyperliquid_perpetual": VenueCapabilities("hyperliquid_perpetual", "net", False),
    "hyperliquid": VenueCapabilities("hyperliquid", "net", False),
    "aster_perpetual": VenueCapabilities("aster_perpetual", "hedge", True),
    "aster": VenueCapabilities("aster", "hedge", True),
    "lighter_perpetual": VenueCapabilities("lighter_perpetual", "hedge", True),
    "lighter": VenueCapabilities("lighter", "hedge", True),
    "mock": VenueCapabilities("mock", "hedge", True),
}


def get_venue_capabilities(connector_name: str) -> VenueCapabilities:
    key = connector_name.lower()
    if key not in _CAPS:
        raise ValueError(f"Unknown connector '{connector_name}' — update topology._CAPS")
    return _CAPS[key]


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

from dataclasses import dataclass

@dataclass
class SharedRiskBook:
    dlmm_net_delta: float = 0.0
    dlmm_inventory_value_usd: float = 0.0
    dlmm_sigma: float = 0.0
    hedge_action: str = "no_trade"
    hedge_target_short: float = 0.0
    hedge_urgency: str = "normal"
    gamma_relaxation: float = 1.0
    last_dlmm_ts: float = 0.0
    last_hedge_ts: float = 0.0

_REGISTRY: dict[str, SharedRiskBook] = {}

def get_shared_book(key: str) -> SharedRiskBook:
    return _REGISTRY.setdefault(key, SharedRiskBook())

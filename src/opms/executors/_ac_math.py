"""
Pure Almgren-Chriss schedule math — no I/O, no asyncio, no service deps.

Extracted from dex_executor/algorithms/almgren_chriss.py so that
ACScheduleExecutor can import just the math without dragging in the OPMS
service layer.  All logic here is HB-free and therefore independently testable.
"""

import math
from decimal import Decimal
from typing import Optional


def _sinh_ratio(u: float, a: float) -> float:
    """
    Numerically stable ``sinh(a*u) / sinh(a)`` for ``u in [0, 1]``, ``a >= 0``.

    As ``a -> 0`` this tends to ``u`` (risk-neutral / linear limit).
    """
    if a <= 0:
        return u
    num = math.exp(a * (u - 1.0)) - math.exp(-a * (u + 1.0))
    den = 1.0 - math.exp(-2.0 * a)
    return num / den


def build_schedule(
    total_quantity: Decimal,
    duration_seconds: float,
    num_intervals: int,
    risk_aversion: float,
    volatility: float,
    eta: float,
    gamma: float = 0.0,
    min_order_size: Optional[Decimal] = None,
    cumulative_volume_fractions: Optional[list[float]] = None,
) -> list[Decimal]:
    """
    Compute the Almgren-Chriss trading schedule as a list of slice sizes.

    Args:
        total_quantity: Total quantity Q to execute.
        duration_seconds: Total execution horizon T.
        num_intervals: Number of slices N.
        risk_aversion: Risk-aversion lambda (lambda -> 0 → TWAP).
        volatility: Per-unit-time price volatility sigma.
        eta: Temporary market-impact coefficient.
        gamma: Permanent market-impact coefficient (default 0).
        min_order_size: If set, slices below this are merged into adjacent ones.
        cumulative_volume_fractions: Optional volume-time fractions V_0..V_N.
            When None, clock-time (uniform) fractions are used.

    Returns:
        List of slice sizes (Decimal) summing exactly to total_quantity.

    Raises:
        ValueError: On invalid parameter combinations.
    """
    if duration_seconds <= 0:
        raise ValueError("duration_seconds must be positive")
    if num_intervals <= 0:
        raise ValueError("num_intervals must be positive")
    if risk_aversion < 0:
        raise ValueError("risk_aversion must be non-negative")
    if volatility < 0:
        raise ValueError("volatility must be non-negative")

    tau = duration_seconds / num_intervals
    eta_tilde = eta - 0.5 * gamma * tau
    if eta_tilde <= 0:
        raise ValueError(
            f"eta - 0.5*gamma*tau must be positive (got {eta_tilde:.6g}); "
            "reduce gamma or increase num_intervals/eta"
        )

    kappa = math.sqrt(risk_aversion * volatility**2 / eta_tilde)
    a = kappa * duration_seconds

    if cumulative_volume_fractions is None:
        fractions = [j / num_intervals for j in range(num_intervals + 1)]
    else:
        fractions = cumulative_volume_fractions

    # Holdings: x_j = Q * sinh_ratio(1 - V_j, a)
    holdings = [_sinh_ratio(1.0 - v, a) for v in fractions]

    sizes: list[Decimal] = []
    cumulative = Decimal("0")
    n = len(fractions) - 1
    for j in range(1, n + 1):
        if j == n:
            size = total_quantity - cumulative
        else:
            frac = holdings[j - 1] - holdings[j]
            size = Decimal(str(frac)) * total_quantity
            cumulative += size
        sizes.append(size)

    return _merge_min_size(sizes, min_order_size)


def _merge_min_size(
    sizes: list[Decimal], min_order_size: Optional[Decimal]
) -> list[Decimal]:
    """Merge slices below min_order_size forward; preserves total quantity."""
    if not min_order_size or min_order_size <= 0:
        return sizes

    merged: list[Decimal] = []
    acc = Decimal("0")
    for s in sizes:
        acc += s
        if acc >= min_order_size:
            merged.append(acc)
            acc = Decimal("0")
    if acc > 0:
        if merged:
            merged[-1] += acc
        else:
            merged.append(acc)
    return merged


__all__ = ["build_schedule"]

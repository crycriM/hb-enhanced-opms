"""
Tests for the pure Almgren-Chriss schedule math (_ac_math.py).

No HB dependencies — runs in the legacy venv or any Python env with mm_core.
"""

import math
from decimal import Decimal

import pytest

from opms.executors._ac_math import build_schedule, _sinh_ratio


class TestSinhRatio:
    def test_zero_a_gives_u(self):
        for u in [0.0, 0.3, 0.7, 1.0]:
            assert abs(_sinh_ratio(u, 0.0) - u) < 1e-12

    def test_large_a_front_loads(self):
        # sinh(a*u)/sinh(a) with large a is heavily concentrated near u=1.
        # At u=0.5 it should be much less than 0.5 (front-loaded means more
        # remains at the start: holdings drop fast early).
        val = _sinh_ratio(0.5, 10.0)
        assert val < 0.5

    def test_boundary_u1(self):
        # sinh_ratio(1, a) should be 1 by definition.
        for a in [0.0, 1.0, 5.0]:
            assert abs(_sinh_ratio(1.0, a) - 1.0) < 1e-9

    def test_boundary_u0(self):
        # sinh_ratio(0, a) should be 0 (no holdings at the end).
        for a in [0.1, 1.0, 5.0]:
            assert abs(_sinh_ratio(0.0, a)) < 1e-9


class TestBuildSchedule:
    def _base(self, **overrides):
        params = dict(
            total_quantity=Decimal("10"),
            duration_seconds=3600.0,
            num_intervals=10,
            risk_aversion=1e-5,
            volatility=0.03,
            eta=0.01,
            gamma=0.0,
        )
        params.update(overrides)
        return params

    def test_sum_equals_total(self):
        sizes = build_schedule(**self._base())
        assert sum(sizes) == Decimal("10")

    def test_num_slices_equals_num_intervals(self):
        sizes = build_schedule(**self._base(num_intervals=8))
        assert len(sizes) == 8

    def test_front_loaded_for_positive_risk_aversion(self):
        sizes = build_schedule(**self._base(risk_aversion=1e-4))
        # With positive lambda, first slice > last slice (front-loaded).
        assert sizes[0] > sizes[-1]

    def test_risk_neutral_approaches_uniform(self):
        # lambda -> 0 recovers TWAP (all slices equal).
        sizes = build_schedule(**self._base(risk_aversion=1e-12))
        expected = Decimal("10") / 10
        for s in sizes:
            assert abs(s - expected) < Decimal("0.001")

    def test_invalid_duration(self):
        with pytest.raises(ValueError, match="duration_seconds"):
            build_schedule(**self._base(duration_seconds=-1.0))

    def test_invalid_num_intervals(self):
        with pytest.raises(ValueError):
            build_schedule(**self._base(num_intervals=0))

    def test_negative_risk_aversion(self):
        with pytest.raises(ValueError):
            build_schedule(**self._base(risk_aversion=-1.0))

    def test_negative_volatility(self):
        with pytest.raises(ValueError):
            build_schedule(**self._base(volatility=-0.1))

    def test_eta_tilde_positive_check(self):
        with pytest.raises(ValueError, match="eta"):
            build_schedule(**self._base(eta=0.001, gamma=1.0, num_intervals=2))

    def test_min_order_size_merge(self):
        # With min_order_size larger than individual slices, they get merged.
        sizes = build_schedule(
            **self._base(num_intervals=10, total_quantity=Decimal("1")),
            min_order_size=Decimal("0.3"),
        )
        assert all(s >= Decimal("0.3") for s in sizes[:-1])
        assert sum(sizes) == Decimal("1")

    def test_volume_aware_schedule(self):
        # With custom volume fractions (uniform), result should match the
        # clock-time schedule.
        n = 10
        uniform_fractions = [j / n for j in range(n + 1)]
        sizes_vol = build_schedule(**self._base(num_intervals=n),
                                   cumulative_volume_fractions=uniform_fractions)
        sizes_clock = build_schedule(**self._base(num_intervals=n))
        for a, b in zip(sizes_vol, sizes_clock):
            assert abs(a - b) < Decimal("1e-10")

    def test_front_weighted_volume_fractions(self):
        # Fractions concentrating volume in early buckets should produce
        # smaller early slices (less need to rush — liquidity is there).
        # This is counter-intuitive: MORE volume early => LARGER early AC slice
        # because we get more of Q done in the high-liquidity window.
        n = 4
        # Heavy-front: 70% of volume in first two buckets.
        fractions = [0.0, 0.35, 0.70, 0.85, 1.0]
        sizes_vol = build_schedule(**self._base(num_intervals=n, risk_aversion=1e-4),
                                   cumulative_volume_fractions=fractions)
        sizes_clock = build_schedule(**self._base(num_intervals=n, risk_aversion=1e-4))
        # Volume-aware: more traded early (steeper front-loading)
        assert sizes_vol[0] >= sizes_clock[0]
        assert sum(sizes_vol) == Decimal("10")

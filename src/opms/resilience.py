"""Small, HB-free safety primitives for the live controller path."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Iterable


_RATE_LIMIT_TEXT = re.compile(r"(?:\b429\b|too many requests|rate[ -]?limit)", re.IGNORECASE)


def _response(error: BaseException):
    return getattr(error, "response", None)


def _status_code(error: BaseException) -> int | None:
    candidates = (
        getattr(error, "status", None),
        getattr(error, "status_code", None),
        getattr(_response(error), "status", None),
        getattr(_response(error), "status_code", None),
    )
    for value in candidates:
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            continue
    return None


def is_rate_limit_error(error: BaseException) -> bool:
    """Recognize both structured HTTP errors and connector-wrapped 429s."""
    return _status_code(error) == 429 or bool(_RATE_LIMIT_TEXT.search(str(error)))


def _retry_after(error: BaseException) -> float | None:
    headers = getattr(_response(error), "headers", None) or getattr(error, "headers", None)
    if not headers:
        return None
    value = headers.get("Retry-After") or headers.get("retry-after")
    try:
        delay = float(value)
    except (TypeError, ValueError):
        return None
    return max(delay, 0.0)


@dataclass
class VenueCircuitBreaker:
    """Exponential request backoff with immediate 429 circuit opening.

    A request becomes eligible again after ``retry_at`` (half-open behavior).
    One successful probe closes and resets the circuit.  The class deliberately
    owns no clock so deterministic controller and replay tests can supply one.
    """

    failure_threshold: int = 3
    base_backoff_s: float = 2.0
    max_backoff_s: float = 60.0
    consecutive_failures: int = 0
    retry_at: float = 0.0
    rate_limited: bool = False
    tripped: bool = False
    last_error: str | None = None
    last_failure_kind: str | None = None

    def allow_request(self, now: float) -> bool:
        return now >= self.retry_at

    def is_open(self, now: float) -> bool:
        return self.tripped and not self.allow_request(now)

    def record_failure(self, now: float, error: BaseException) -> float:
        self.consecutive_failures += 1
        self.rate_limited = is_rate_limit_error(error)
        self.tripped = self.rate_limited or self.consecutive_failures >= self.failure_threshold
        self.last_error = f"{type(error).__name__}: {error}"
        self.last_failure_kind = (
            "rate_limit" if self.rate_limited
            else "timeout" if isinstance(error, (asyncio.TimeoutError, TimeoutError))
            else "transport"
        )
        exponential = min(
            self.base_backoff_s * (2 ** (self.consecutive_failures - 1)),
            self.max_backoff_s,
        )
        header_delay = _retry_after(error) if self.rate_limited else None
        delay = min(
            max(exponential, header_delay or 0.0),
            self.max_backoff_s,
        )
        self.retry_at = now + delay
        return delay

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.retry_at = 0.0
        self.rate_limited = False
        self.tripped = False
        self.last_error = None
        self.last_failure_kind = None

    def snapshot(self, now: float) -> dict:
        blocked = not self.allow_request(now)
        if self.tripped and blocked:
            state = "open"
        elif self.tripped:
            state = "half_open"
        elif blocked:
            state = "backoff"
        else:
            state = "closed"
        return {
            "state": state,
            "consecutive_failures": self.consecutive_failures,
            "retry_in_s": max(self.retry_at - now, 0.0),
            "rate_limited": self.rate_limited,
            "last_failure_kind": self.last_failure_kind,
            "last_error": self.last_error,
        }


@dataclass
class QuoteLivenessWatchdog:
    """Track whether every intentionally quoted side has a live venue order."""

    timeout_s: float = 15.0
    recovery_cooldown_s: float = 30.0
    missing_since: float | None = None
    last_live_at: float | None = None
    open_until: float = 0.0
    trips: int = 0
    expected_sides: tuple[str, ...] = ()
    live_sides: tuple[str, ...] = ()

    def allow_quotes(self, now: float) -> bool:
        return now >= self.open_until

    def observe(
        self,
        now: float,
        expected_sides: Iterable[str],
        live_sides: Iterable[str],
    ) -> bool:
        expected = set(expected_sides)
        live = set(live_sides)
        self.expected_sides = tuple(sorted(expected))
        self.live_sides = tuple(sorted(live))

        if not expected:
            self.missing_since = None
            return False

        if not self.allow_quotes(now):
            return True
        if self.open_until:
            # Cooldown just elapsed: give the recovery attempt a fresh grace
            # window instead of immediately reusing the pre-trip timestamp.
            self.open_until = 0.0
            self.missing_since = None

        if expected.issubset(live):
            self.last_live_at = now
            self.missing_since = None
            return False

        if self.missing_since is None:
            self.missing_since = now
            return False
        if now - self.missing_since < self.timeout_s:
            return False

        self.trips += 1
        self.open_until = now + self.recovery_cooldown_s
        return True

    def suspend(self) -> None:
        """Pause missing-quote timing while quoting is not intended."""
        self.missing_since = None
        self.expected_sides = ()
        self.live_sides = ()

    def snapshot(self, now: float) -> dict:
        if not self.allow_quotes(now):
            state = "open"
        elif self.missing_since is not None:
            state = "missing"
        elif self.last_live_at is not None:
            state = "live"
        else:
            state = "idle"
        return {
            "state": state,
            "expected_sides": list(self.expected_sides),
            "live_sides": list(self.live_sides),
            "missing_for_s": (
                max(now - self.missing_since, 0.0)
                if self.missing_since is not None else 0.0
            ),
            "last_live_at": self.last_live_at,
            "retry_in_s": max(self.open_until - now, 0.0),
            "trips": self.trips,
        }


__all__ = [
    "QuoteLivenessWatchdog",
    "VenueCircuitBreaker",
    "is_rate_limit_error",
]

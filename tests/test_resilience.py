import asyncio

import pytest

from opms.resilience import QuoteLivenessWatchdog, VenueCircuitBreaker, is_rate_limit_error


class _Response:
    status_code = 429
    headers = {"Retry-After": "7"}


class _RateLimited(RuntimeError):
    response = _Response()


def test_rate_limit_detection_uses_status_and_message():
    assert is_rate_limit_error(_RateLimited("busy"))
    assert is_rate_limit_error(RuntimeError("HTTP 429 Too Many Requests"))
    assert not is_rate_limit_error(RuntimeError("connection reset"))


def test_429_opens_circuit_for_retry_after_window():
    breaker = VenueCircuitBreaker(
        failure_threshold=3, base_backoff_s=1.0, max_backoff_s=60.0
    )

    delay = breaker.record_failure(100.0, _RateLimited("busy"))

    assert delay == pytest.approx(7.0)
    assert breaker.is_open(106.9)
    assert not breaker.allow_request(106.9)
    assert breaker.allow_request(107.0)
    assert breaker.snapshot(106.9)["rate_limited"] is True


def test_timeout_backoff_is_exponential_and_success_resets_it():
    breaker = VenueCircuitBreaker(
        failure_threshold=2, base_backoff_s=2.0, max_backoff_s=10.0
    )

    assert breaker.record_failure(10.0, asyncio.TimeoutError()) == pytest.approx(2.0)
    assert breaker.allow_request(11.9) is False
    assert breaker.record_failure(12.0, asyncio.TimeoutError()) == pytest.approx(4.0)
    assert breaker.snapshot(12.0)["state"] == "open"
    breaker.record_success()
    assert breaker.allow_request(12.0)
    assert breaker.snapshot(12.0)["consecutive_failures"] == 0


def test_quote_liveness_trips_only_after_continuous_missing_window():
    watchdog = QuoteLivenessWatchdog(timeout_s=15.0, recovery_cooldown_s=30.0)

    assert not watchdog.observe(100.0, {"buy", "sell"}, set())
    assert not watchdog.observe(114.9, {"buy", "sell"}, {"buy"})
    assert watchdog.observe(115.0, {"buy", "sell"}, {"buy"})
    assert not watchdog.allow_quotes(144.9)
    assert watchdog.allow_quotes(145.0)

    # A fully live pair clears the missing timer after recovery.
    assert not watchdog.observe(145.0, {"buy", "sell"}, {"buy", "sell"})
    assert watchdog.missing_since is None
    assert watchdog.last_live_at == pytest.approx(145.0)

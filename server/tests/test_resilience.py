"""
Tests for RONIN Resilience — Rate limiter + circuit breakers.
Run: pytest tests/test_resilience.py -v
"""
import asyncio
import pytest

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from resilience import (
    TokenBucket, CircuitBreaker, CircuitState, CircuitOpenError,
    CircuitRegistry, RateLimiterState, set_test_mode,
)


# ─── Token Bucket ─────────────────────────────────────────────────────────

def test_token_bucket_allows_within_capacity():
    bucket = TokenBucket(capacity=5, refill_rate=1.0, tokens=5.0)
    assert bucket.consume() is True
    assert bucket.consume() is True


def test_token_bucket_blocks_when_empty():
    bucket = TokenBucket(capacity=2, refill_rate=0.01, tokens=0.0)
    assert bucket.consume() is False


def test_token_bucket_refills_over_time():
    import time
    bucket = TokenBucket(capacity=10, refill_rate=10.0, tokens=0.0)
    time.sleep(0.15)  # Wait for ~1.5 tokens to refill
    assert bucket.consume() is True  # Should have at least 1 token


# ─── Rate Limiter State ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rate_limiter_allows_normal_traffic():
    limiter = RateLimiterState()
    allowed, _ = await limiter.is_allowed("127.0.0.1", "/api/tools/some_tool")
    assert allowed is True


@pytest.mark.asyncio
async def test_rate_limiter_blocks_after_limit():
    """Override: use a low-capacity bucket via direct state manipulation."""
    from resilience import TokenBucket, set_test_mode
    set_test_mode(False)  # Enable rate limiting for this test
    try:
        limiter = RateLimiterState()
        ip, path = "10.0.0.99", "/api/test_limit"
        # Pre-populate with depleted buckets
        limiter._buckets[ip][path] = (
            TokenBucket(capacity=3, refill_rate=0.01, tokens=0.0),
            TokenBucket(capacity=100, refill_rate=1.0, tokens=100.0),
        )
        allowed, retry = await limiter.is_allowed(ip, path)
        assert allowed is False
        assert retry > 0
    finally:
        set_test_mode(True)  # Restore test mode


@pytest.mark.asyncio
async def test_rate_limiter_test_mode_bypasses():
    set_test_mode(True)
    limiter = RateLimiterState()
    for _ in range(200):
        allowed, _ = await limiter.is_allowed("1.2.3.4", "/api/tools/x")
        assert allowed is True
    set_test_mode(False)


# ─── Circuit Breaker ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_circuit_starts_closed():
    cb = CircuitBreaker(name="test_cb")
    assert cb.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_circuit_executes_success():
    cb = CircuitBreaker(name="test_success")
    async def good_fn():
        return "ok"
    result = await cb.call(good_fn)
    assert result == "ok"
    assert cb.success_count == 1


@pytest.mark.asyncio
async def test_circuit_trips_after_threshold():
    cb = CircuitBreaker(name="test_trip", failure_threshold=3, window_seconds=60)
    async def bad_fn():
        raise RuntimeError("fail")

    for _ in range(3):
        try:
            await cb.call(bad_fn)
        except RuntimeError:
            pass

    assert cb.state == CircuitState.OPEN


@pytest.mark.asyncio
async def test_circuit_open_rejects_fast():
    cb = CircuitBreaker(name="test_fast_fail", failure_threshold=2, window_seconds=60)
    async def bad_fn():
        raise RuntimeError("fail")

    # Trip the circuit
    for _ in range(2):
        try:
            await cb.call(bad_fn)
        except RuntimeError:
            pass

    assert cb.state == CircuitState.OPEN

    # Next call should raise CircuitOpenError, not RuntimeError
    with pytest.raises(CircuitOpenError):
        await cb.call(bad_fn)


@pytest.mark.asyncio
async def test_circuit_half_open_after_recovery():
    import time
    cb = CircuitBreaker(name="test_recovery", failure_threshold=2, window_seconds=60, recovery_seconds=0.1)
    async def bad_fn():
        raise RuntimeError("fail")
    async def good_fn():
        return "recovered"

    # Trip the circuit
    for _ in range(2):
        try:
            await cb.call(bad_fn)
        except RuntimeError:
            pass

    assert cb.state == CircuitState.OPEN

    # Wait for recovery window
    await asyncio.sleep(0.15)

    # Next call transitions to half_open, then closed on success
    result = await cb.call(good_fn)
    assert result == "recovered"
    assert cb.state == CircuitState.CLOSED


def test_circuit_status():
    cb = CircuitBreaker(name="status_test")
    status = cb.status()
    assert status["name"] == "status_test"
    assert "state" in status
    assert "failure_count" in status


# ─── Circuit Registry ─────────────────────────────────────────────────────

def test_registry_creates_new_circuits():
    registry = CircuitRegistry()
    cb = registry.get("my_service")
    assert cb.name == "my_service"


def test_registry_returns_same_instance():
    registry = CircuitRegistry()
    cb1 = registry.get("svc")
    cb2 = registry.get("svc")
    assert cb1 is cb2


def test_registry_all_status():
    registry = CircuitRegistry()
    registry.get("a")
    registry.get("b")
    statuses = registry.all_status()
    names = [s["name"] for s in statuses]
    assert "a" in names
    assert "b" in names


# ─── API Metrics ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_metrics_endpoint():
    set_test_mode(True)
    from httpx import ASGITransport, AsyncClient
    from api import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/metrics")
        assert r.status_code == 200
        data = r.json()
        assert "metrics" in data
        assert "circuits" in data

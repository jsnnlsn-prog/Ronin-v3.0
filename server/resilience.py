"""
JARVIS Resilience — Rate Limiting + Circuit Breakers
======================================================
Token bucket rate limiter as FastAPI middleware.
Circuit breaker for external API calls (Claude, Venice, webhooks).
Graceful degradation when services are unavailable.
"""

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Optional

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

# ─── RATE LIMITER ────────────────────────────────────────────────────────────

# Per-route overrides: path prefix → (requests_per_min, requests_per_hour)
ROUTE_LIMITS: Dict[str, tuple] = {
    "/api/webhooks/": (120, 1200),
    "/api/tools/": (30, 300),
    "/api/auth/login": (10, 60),
    "/api/auth/register": (5, 20),
}

DEFAULT_LIMIT = (60, 600)  # per minute, per hour

# Test mode: bypass rate limiting when running under pytest
_TEST_MODE = False


def set_test_mode(enabled: bool) -> None:
    global _TEST_MODE
    _TEST_MODE = enabled


@dataclass
class TokenBucket:
    """Token bucket for rate limiting."""
    capacity: int
    refill_rate: float  # tokens per second
    tokens: float = field(default=0.0)
    last_refill: float = field(default_factory=time.monotonic)

    def consume(self, amount: int = 1) -> bool:
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now
        if self.tokens >= amount:
            self.tokens -= amount
            return True
        return False


class RateLimiterState:
    """In-memory rate limiter state."""

    def __init__(self):
        # ip -> path_prefix -> (minute_bucket, hour_bucket)
        self._buckets: Dict[str, Dict[str, tuple]] = defaultdict(dict)
        self._lock = asyncio.Lock()

    def _get_route_limits(self, path: str) -> tuple:
        for prefix, limits in ROUTE_LIMITS.items():
            if path.startswith(prefix):
                return limits
        return DEFAULT_LIMIT

    async def is_allowed(self, ip: str, path: str) -> tuple:
        """Returns (allowed: bool, retry_after: int)."""
        if _TEST_MODE:
            return True, 0

        limits = self._get_route_limits(path)
        per_min, per_hour = limits

        async with self._lock:
            if path not in self._buckets[ip]:
                self._buckets[ip][path] = (
                    TokenBucket(capacity=per_min, refill_rate=per_min / 60.0, tokens=float(per_min)),
                    TokenBucket(capacity=per_hour, refill_rate=per_hour / 3600.0, tokens=float(per_hour)),
                )
            min_bucket, hour_bucket = self._buckets[ip][path]

            if not min_bucket.consume():
                return False, 60
            if not hour_bucket.consume():
                return False, 3600
            return True, 0


_rate_limiter = RateLimiterState()

# Routes that bypass rate limiting entirely
_EXEMPT_PATHS = {"/api/health", "/docs", "/openapi.json", "/redoc"}


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if request.url.path in _EXEMPT_PATHS:
            return await call_next(request)

        ip = request.client.host if request.client else "unknown"
        allowed, retry_after = await _rate_limiter.is_allowed(ip, request.url.path)

        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests"},
                headers={"Retry-After": str(retry_after)},
            )
        return await call_next(request)


# ─── CIRCUIT BREAKER ─────────────────────────────────────────────────────────

class CircuitState(str, Enum):
    CLOSED = "closed"       # Normal operation
    OPEN = "open"           # Failing, reject fast
    HALF_OPEN = "half_open" # Testing recovery


@dataclass
class CircuitBreaker:
    """
    Circuit breaker for external API calls.
    Trips after `failure_threshold` failures within `window_seconds`.
    """
    name: str
    failure_threshold: int = 5
    window_seconds: float = 60.0
    recovery_seconds: float = 30.0

    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    last_failure: float = 0.0
    last_attempt: float = 0.0
    success_count: int = 0
    _failures_in_window: list = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def _clean_window(self) -> None:
        cutoff = time.monotonic() - self.window_seconds
        self._failures_in_window = [t for t in self._failures_in_window if t > cutoff]

    async def call(self, fn: Callable, *args, **kwargs) -> Any:
        """Execute fn through the circuit breaker."""
        async with self._lock:
            now = time.monotonic()

            if self.state == CircuitState.OPEN:
                if now - self.last_failure < self.recovery_seconds:
                    raise CircuitOpenError(f"Circuit '{self.name}' is open")
                # Try half-open
                self.state = CircuitState.HALF_OPEN
                self.last_attempt = now

        try:
            result = await fn(*args, **kwargs)
            async with self._lock:
                self.success_count += 1
                if self.state == CircuitState.HALF_OPEN:
                    self.state = CircuitState.CLOSED
                    self.failure_count = 0
                    self._failures_in_window.clear()
            return result

        except Exception as exc:
            async with self._lock:
                now = time.monotonic()
                self.last_failure = now
                self.failure_count += 1
                self._failures_in_window.append(now)
                self._clean_window()

                if len(self._failures_in_window) >= self.failure_threshold:
                    self.state = CircuitState.OPEN

            raise exc

    def status(self) -> dict:
        self._clean_window()
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self.failure_count,
            "failures_in_window": len(self._failures_in_window),
            "success_count": self.success_count,
            "last_failure": self.last_failure,
        }


class CircuitOpenError(Exception):
    """Raised when a circuit breaker is open."""
    pass


# ─── CIRCUIT REGISTRY ────────────────────────────────────────────────────────

class CircuitRegistry:
    """Manages named circuit breakers."""

    def __init__(self):
        self._circuits: Dict[str, CircuitBreaker] = {}

    def get(self, name: str) -> CircuitBreaker:
        if name not in self._circuits:
            self._circuits[name] = CircuitBreaker(name=name)
        return self._circuits[name]

    def all_status(self) -> list:
        return [cb.status() for cb in self._circuits.values()]


# Global circuit registry
circuits = CircuitRegistry()

# Pre-create the standard circuits
circuits.get("circuit:claude")
circuits.get("circuit:venice")
circuits.get("circuit:webhook_out")

"""
RONIN Logging — JSON Structured Logging + Request Middleware
==============================================================
Replaces ad-hoc print() calls with structured JSON logging.
Adds request_id correlation across all downstream calls.
"""

import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

# ─── CONFIGURATION ───────────────────────────────────────────────────────────

RONIN_HOME = Path(os.environ.get("RONIN_HOME", Path.home() / ".ronin"))
LOG_LEVEL = os.environ.get("RONIN_LOG_LEVEL", "INFO").upper()
LOG_FILE = RONIN_HOME / "ronin.log"
MAX_LOG_BYTES = 10 * 1024 * 1024  # 10MB
LOG_BACKUP_COUNT = 5

# Request-scoped storage (thread-local style via context var)
try:
    from contextvars import ContextVar
    _request_id_var: ContextVar[str] = ContextVar("request_id", default="")
except ImportError:
    _request_id_var = None  # type: ignore


def get_request_id() -> str:
    if _request_id_var is None:
        return ""
    return _request_id_var.get("")


def set_request_id(rid: str) -> None:
    if _request_id_var is not None:
        _request_id_var.set(rid)


# ─── JSON FORMATTER ──────────────────────────────────────────────────────────

class JSONFormatter(logging.Formatter):
    """Formats log records as JSON lines."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Inject request_id if available
        rid = get_request_id()
        if rid:
            log_entry["request_id"] = rid

        # Include extra fields passed via extra={}
        for key, val in record.__dict__.items():
            if key not in (
                "name", "msg", "args", "levelname", "levelno", "pathname",
                "filename", "module", "exc_info", "exc_text", "stack_info",
                "lineno", "funcName", "created", "msecs", "relativeCreated",
                "thread", "threadName", "processName", "process", "message",
                "taskName",
            ):
                log_entry["extra"] = log_entry.get("extra", {})
                log_entry["extra"][key] = val

        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_entry)


# ─── SETUP ───────────────────────────────────────────────────────────────────

def setup_logging() -> None:
    """Configure root logger with JSON output to stdout + rotating file."""
    level = getattr(logging, LOG_LEVEL, logging.INFO)

    handlers = []

    # Stdout handler
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(JSONFormatter())
    handlers.append(stdout_handler)

    # File handler
    try:
        RONIN_HOME.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            LOG_FILE,
            maxBytes=MAX_LOG_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(JSONFormatter())
        handlers.append(file_handler)
    except Exception:
        pass  # File logging optional — don't crash if filesystem unavailable

    logging.basicConfig(level=level, handlers=handlers, force=True)

    # Quiet down noisy libraries
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("watchfiles").setLevel(logging.WARNING)


# ─── METRICS ─────────────────────────────────────────────────────────────────

class RequestMetrics:
    """In-memory metrics for API requests."""

    def __init__(self):
        self._requests = 0
        self._errors = 0
        self._total_ms = 0.0
        self._route_counts: dict = {}
        self._tool_counts: dict = {}
        self._provider_counts: dict = {}
        self._start_time = time.time()
        self._recent: list = []  # Last 100 request durations for rate calc

    def record(self, path: str, status: int, duration_ms: float) -> None:
        self._requests += 1
        self._total_ms += duration_ms
        if status >= 400:
            self._errors += 1
        self._route_counts[path] = self._route_counts.get(path, 0) + 1
        self._recent.append((time.time(), duration_ms))
        # Keep last 200 for rate calculation
        if len(self._recent) > 200:
            self._recent = self._recent[-200:]

    def record_tool(self, tool_name: str) -> None:
        self._tool_counts[tool_name] = self._tool_counts.get(tool_name, 0) + 1

    def record_provider(self, provider: str) -> None:
        self._provider_counts[provider] = self._provider_counts.get(provider, 0) + 1

    def snapshot(self) -> dict:
        now = time.time()
        uptime = now - self._start_time

        # Requests/min over last 60s
        recent_60 = [r for r in self._recent if now - r[0] <= 60]
        req_per_min = len(recent_60)

        avg_ms = self._total_ms / max(self._requests, 1)
        error_rate = self._errors / max(self._requests, 1)

        # p95 latency from recent
        if self._recent:
            durations = sorted(r[1] for r in self._recent[-100:])
            p95 = durations[int(len(durations) * 0.95)] if durations else 0.0
        else:
            p95 = 0.0

        return {
            "total_requests": self._requests,
            "total_errors": self._errors,
            "error_rate": round(error_rate, 4),
            "avg_response_ms": round(avg_ms, 2),
            "p95_response_ms": round(p95, 2),
            "requests_per_min": req_per_min,
            "uptime_seconds": round(uptime, 0),
            "top_routes": sorted(self._route_counts.items(), key=lambda x: x[1], reverse=True)[:10],
            "tool_call_counts": self._tool_counts,
            "provider_call_counts": self._provider_counts,
        }


metrics = RequestMetrics()


# ─── REQUEST LOGGING MIDDLEWARE ───────────────────────────────────────────────

logger = logging.getLogger("ronin.api")


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Assign request_id
        rid = str(uuid.uuid4())[:8]
        set_request_id(rid)

        start = time.monotonic()
        user_id = None

        try:
            response = await call_next(request)
        except Exception as exc:
            duration_ms = (time.monotonic() - start) * 1000
            logger.error(
                f"{request.method} {request.url.path} 500 [{duration_ms:.1f}ms]",
                extra={"error": str(exc)},
            )
            metrics.record(request.url.path, 500, duration_ms)
            raise

        duration_ms = (time.monotonic() - start) * 1000
        metrics.record(request.url.path, response.status_code, duration_ms)

        # Log request (skip health checks to reduce noise)
        if request.url.path != "/api/health":
            logger.info(
                f"{request.method} {request.url.path} {response.status_code} [{duration_ms:.1f}ms]",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": round(duration_ms, 2),
                },
            )

        # Inject request_id into response
        response.headers["X-Request-ID"] = rid
        return response

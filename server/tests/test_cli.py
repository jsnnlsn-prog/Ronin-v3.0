"""
Tests for Phase 6 CLI endpoint: /api/cli/run and cli.py formatters
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import api
from api import app
from auth import require_auth, User
from resilience import set_test_mode

set_test_mode(True)


# ─── Auth bypass ──────────────────────────────────────────────────────────

def _dummy_user():
    u = MagicMock(spec=User)
    u.username = "testuser"
    u.is_admin = False
    return u


@pytest.fixture
def auth_client_setup():
    app.dependency_overrides[require_auth] = lambda: _dummy_user()
    yield
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def auth_client():
    app.dependency_overrides[require_auth] = lambda: _dummy_user()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


# ─── CLI Endpoint Tests ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cli_run_unauthenticated(client):
    """Returns 401 when no auth provided."""
    resp = await client.post("/api/cli/run", json={"command": "hello"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_cli_run_authenticated(auth_client):
    """Returns 200 with result field when authenticated."""
    mock_result = {
        "content": [{"type": "text", "text": "Hello from RONIN"}],
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "routing": {"tier": "simple"},
    }
    with patch("model_router.ModelRouter.call", new_callable=AsyncMock, return_value=mock_result):
        resp = await auth_client.post("/api/cli/run", json={"command": "hello"})

    assert resp.status_code == 200
    data = resp.json()
    assert "result" in data
    assert data["result"] == "Hello from RONIN"


@pytest.mark.asyncio
async def test_cli_run_returns_tier(auth_client):
    """Response includes tier field."""
    mock_result = {
        "content": [{"type": "text", "text": "Done."}],
        "usage": {"input_tokens": 5, "output_tokens": 3},
        "routing": {"tier": "simple"},
    }
    with patch("model_router.ModelRouter.call", new_callable=AsyncMock, return_value=mock_result):
        resp = await auth_client.post("/api/cli/run", json={"command": "ping"})

    assert resp.status_code == 200
    data = resp.json()
    assert "tier" in data
    assert isinstance(data["tier"], str)
    assert len(data["tier"]) > 0


@pytest.mark.asyncio
async def test_cli_run_empty_command(auth_client):
    """Returns 422 for empty command."""
    resp = await auth_client.post("/api/cli/run", json={"command": ""})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_cli_run_model_error_graceful(auth_client):
    """Returns 200 with error message when model call fails (graceful degradation)."""
    with patch("model_router.ModelRouter.call", new_callable=AsyncMock, side_effect=Exception("API down")):
        resp = await auth_client.post("/api/cli/run", json={"command": "hello"})

    assert resp.status_code == 200
    data = resp.json()
    assert "error" in data["result"].lower() or "RONIN" in data["result"]


# ─── CLI Formatter Unit Tests (no HTTP) ───────────────────────────────────

def test_cli_status_formatter():
    """format_status returns a non-empty string for a health dict."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from cli import format_status

    health = {
        "status": "operational",
        "uptime_seconds": 3661,
        "database": {"semantic_memories": 5, "episodic_memories": 3, "events": 12},
        "system": {"cpu_percent": 10.5, "memory_percent": 45.2, "disk_percent": 60.0},
    }
    result = format_status(health)
    assert "operational" in result
    assert "1h" in result  # 3661 seconds = 1h 1m 1s
    assert "semantic" in result


def test_cli_format_event():
    """format_event returns a string with source and type."""
    from cli import format_event

    event = {
        "event_id": "abc123",
        "source": "filesystem",
        "event_type": "file_created",
        "created_at": "2025-12-01T10:00:00Z",
        "processed": True,
        "priority": "normal",
    }
    result = format_event(event)
    assert "filesystem" in result
    assert "file_created" in result


def test_cli_format_memories():
    """format_memories returns a list of fact strings."""
    from cli import format_memories

    memories = [
        {"fact": "User prefers Python", "confidence": 0.9},
        {"fact": "User lives in LA", "confidence": 0.75},
    ]
    result = format_memories(memories)
    assert "User prefers Python" in result
    assert "90%" in result


def test_cli_format_schedules():
    """format_schedules returns schedule info."""
    from cli import format_schedules

    schedules = [
        {
            "name": "daily_scrape",
            "cron_expression": "0 9 * * *",
            "enabled": True,
            "run_count": 5,
        }
    ]
    result = format_schedules(schedules)
    assert "daily_scrape" in result
    assert "0 9 * * *" in result

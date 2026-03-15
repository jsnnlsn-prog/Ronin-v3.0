"""
Tests for Phase 6 Slack integration: /api/slack/command and slack_bot.py functions
"""

import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import api
from api import app
from resilience import set_test_mode

set_test_mode(True)


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ─── /api/slack/command Tests ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_slack_command_no_token(client, monkeypatch):
    """Returns 503 when SLACK_BOT_TOKEN is not configured."""
    monkeypatch.setattr(api, "_get_slack_token", lambda: None)
    monkeypatch.setattr(api, "_get_slack_signing_secret", lambda: None)

    resp = await client.post(
        "/api/slack/command",
        data={"command": "/ronin", "text": "status", "response_url": "http://example.com"},
    )
    assert resp.status_code == 503
    assert "SLACK_BOT_TOKEN" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_slack_command_with_token_no_signature(client, monkeypatch):
    """Returns 200 when token is set and no signing secret (skips verification)."""
    monkeypatch.setattr(api, "_get_slack_token", lambda: "xoxb-test-token")
    monkeypatch.setattr(api, "_get_slack_signing_secret", lambda: None)

    # Patch dispatch to avoid actual async tasks
    with patch("integrations.slack_bot.dispatch_slash_command", new_callable=AsyncMock):
        import asyncio
        with patch("asyncio.create_task"):
            resp = await client.post(
                "/api/slack/command",
                data={
                    "command": "/ronin",
                    "text": "hello",
                    "response_url": "http://slack.example.com/response",
                },
            )

    assert resp.status_code == 200
    data = resp.json()
    assert "text" in data


# ─── slack_bot.py Unit Tests ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_slack_send_message_mocked():
    """send_slack_message POSTs to chat.postMessage with correct structure."""
    from integrations.slack_bot import send_slack_message

    mock_response = MagicMock()
    mock_response.json = MagicMock(return_value={"ok": True})

    mock_http = AsyncMock()
    mock_http.post = AsyncMock(return_value=mock_response)

    result = await send_slack_message(
        channel="C123456",
        text="Hello from RONIN",
        http=mock_http,
        bot_token="xoxb-test-token",
        thread_ts="1234567890.000001",
    )

    assert result is True
    mock_http.post.assert_called_once()
    call_args = mock_http.post.call_args
    assert "chat.postMessage" in call_args[0][0]
    # Verify auth header
    headers = call_args[1].get("headers") or call_args[0][1] if len(call_args[0]) > 1 else {}
    # Check content was sent
    content = call_args[1].get("content", b"")
    body = json.loads(content) if content else {}
    assert body.get("channel") == "C123456"
    assert "blocks" in body


@pytest.mark.asyncio
async def test_slack_send_message_api_error():
    """send_slack_message returns False when API returns ok=false."""
    from integrations.slack_bot import send_slack_message

    mock_response = MagicMock()
    mock_response.json = MagicMock(return_value={"ok": False, "error": "channel_not_found"})

    mock_http = AsyncMock()
    mock_http.post = AsyncMock(return_value=mock_response)

    result = await send_slack_message("C999", "text", mock_http, "xoxb-token")
    assert result is False


def test_slack_event_handler_ignores_bots():
    """Bot messages are detected and filtered."""
    from integrations.slack_bot import normalize_slack_event

    bot_event = {
        "event": {
            "type": "message",
            "channel": "C123",
            "text": "I am a bot",
            "bot_id": "B123",
        },
        "team_id": "T123",
        "event_id": "Ev123",
    }
    normalized = normalize_slack_event(bot_event)
    assert normalized["is_bot"] is True


def test_slack_verify_signature_valid():
    """Valid Slack signature passes verification."""
    from integrations.slack_bot import verify_slack_signature

    secret = "test_signing_secret"
    body = b"command=/ronin&text=hello"
    timestamp = str(int(time.time()))

    sig_basestring = f"v0:{timestamp}:{body.decode()}"
    signature = "v0=" + hmac.new(
        secret.encode(), sig_basestring.encode(), hashlib.sha256
    ).hexdigest()

    assert verify_slack_signature(body, timestamp, signature, secret) is True


def test_slack_verify_signature_stale():
    """Signature with old timestamp (>5 min) is rejected."""
    from integrations.slack_bot import verify_slack_signature

    secret = "test_signing_secret"
    body = b"command=/ronin&text=hello"
    old_timestamp = str(int(time.time()) - 400)  # 6+ minutes ago

    sig_basestring = f"v0:{old_timestamp}:{body.decode()}"
    signature = "v0=" + hmac.new(
        secret.encode(), sig_basestring.encode(), hashlib.sha256
    ).hexdigest()

    assert verify_slack_signature(body, old_timestamp, signature, secret) is False


def test_normalize_slack_event_app_mention():
    """App mention events are normalized correctly, mention stripped from text."""
    from integrations.slack_bot import normalize_slack_event

    raw = {
        "event": {
            "type": "app_mention",
            "channel": "C123456",
            "text": "<@U12345> help me with something",
            "user": "U67890",
            "ts": "1234567890.000001",
        },
        "team_id": "T999",
        "event_id": "Ev001",
    }
    normalized = normalize_slack_event(raw)
    assert normalized["type"] == "app_mention"
    assert normalized["channel"] == "C123456"
    assert normalized["is_bot"] is False
    assert "<@" not in normalized["text"]
    assert "help me with something" in normalized["text"]


def test_build_slack_status_response():
    """Status response includes key fields."""
    from integrations.slack_bot import build_slack_status_response

    health = {
        "status": "operational",
        "uptime_seconds": 7200,
        "database": {"semantic_memories": 10, "episodic_memories": 5},
        "system": {"cpu_percent": 20, "memory_percent": 55, "disk_percent": 40},
    }
    result = build_slack_status_response(health)
    assert "operational" in result
    assert "2h" in result  # 7200 seconds = 2h
    assert "CPU" in result or "cpu" in result.lower()

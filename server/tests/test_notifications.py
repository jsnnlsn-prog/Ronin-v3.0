"""
Tests for RONIN Notification System.

Run: pytest tests/test_notifications.py -v
"""
import json
import sqlite3
import pytest
from httpx import ASGITransport, AsyncClient

from api import app
from event_queue import Event, EventPriority, EventSource
from notifications import (
    Notification, NotificationChannel, NotificationConfig, ChannelConfig,
    NotificationRouter, create_notification_handler,
)


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS key_value_store (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            agent TEXT,
            input_summary TEXT,
            output_summary TEXT,
            success INTEGER DEFAULT 1,
            execution_ms REAL
        );
    """)
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def router(db):
    return NotificationRouter(db)


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ─── NotificationConfig ──────────────────────────────────────────────────

def test_default_config():
    config = NotificationConfig()
    assert config.log.enabled is True
    assert config.webhook_out.enabled is False
    assert config.slack.enabled is False


def test_config_save_load(db, router):
    config = NotificationConfig(
        webhook_out=ChannelConfig(enabled=True, recipient="https://example.com/hook"),
    )
    router.save_config(config)

    # Clear cache
    router._config = None
    loaded = router.get_config()
    assert loaded.webhook_out.enabled is True
    assert loaded.webhook_out.recipient == "https://example.com/hook"


# ─── NotificationRouter ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_log_notification(db, router):
    notif = Notification(
        channel=NotificationChannel.log,
        title="Test Alert",
        body="Something happened",
        priority=EventPriority.normal,
    )
    # Ensure log channel is enabled
    config = router.get_config()
    config.log.enabled = True
    router.save_config(config)

    result = await router.send(notif)
    assert "log" in result
    assert result["log"]["sent"] is True


@pytest.mark.asyncio
async def test_send_webhook_no_url(db, router):
    config = NotificationConfig(
        webhook_out=ChannelConfig(enabled=True, recipient=""),
    )
    router.save_config(config)

    notif = Notification(
        channel=NotificationChannel.webhook_out,
        title="Test",
        body="No URL",
    )
    result = await router.send(notif)
    assert result.get("webhook_out", {}).get("sent") is False


@pytest.mark.asyncio
async def test_send_to_all_filters_priority(db, router):
    config = NotificationConfig(
        log=ChannelConfig(enabled=True, min_priority=EventPriority.high),
    )
    router.save_config(config)

    # Normal priority should NOT pass high threshold
    result = await router.send_to_all("Low Priority", "This is low", EventPriority.low)
    assert "log" not in result

    # High priority should pass
    result = await router.send_to_all("High Priority", "This is high", EventPriority.high)
    assert "log" in result


@pytest.mark.asyncio
async def test_send_to_all_disabled_channel(db, router):
    config = NotificationConfig(
        log=ChannelConfig(enabled=False),
    )
    router.save_config(config)

    result = await router.send_to_all("Test", "Body")
    assert "log" not in result  # Disabled channel skipped


def test_priority_comparison(router):
    assert router._passes_priority(EventPriority.critical, EventPriority.normal) is True
    assert router._passes_priority(EventPriority.high, EventPriority.normal) is True
    assert router._passes_priority(EventPriority.normal, EventPriority.normal) is True
    assert router._passes_priority(EventPriority.low, EventPriority.normal) is False


# ─── Event Handler Integration ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_notification_handler_high_priority(db, router):
    config = NotificationConfig(
        log=ChannelConfig(enabled=True, min_priority=EventPriority.low),
    )
    router.save_config(config)

    handler = create_notification_handler(router)
    event = Event(
        source=EventSource.system,
        event_type="system_disk_critical",
        payload={"disk_percent": 97},
        priority=EventPriority.critical,
    )
    result = await handler(event)
    assert result is not None  # Returns JSON string of results


@pytest.mark.asyncio
async def test_notification_handler_low_priority_skipped(db, router):
    handler = create_notification_handler(router)
    event = Event(
        source=EventSource.filesystem,
        event_type="file_modified",
        payload={},
        priority=EventPriority.low,
    )
    result = await handler(event)
    assert result is None  # Low priority events don't trigger notifications


# ─── API Endpoints ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_api_get_notification_config(client):
    r = await client.get("/api/notifications/config")
    assert r.status_code == 200
    data = r.json()
    assert "log" in data
    assert "webhook_out" in data


@pytest.mark.asyncio
async def test_api_update_notification_config(client):
    r = await client.put("/api/notifications/config", json={
        "log": {"enabled": True, "min_priority": "high"},
        "webhook_out": {"enabled": True, "recipient": "https://example.com/hook"},
    })
    assert r.status_code == 200
    assert r.json()["updated"] is True


@pytest.mark.asyncio
async def test_api_test_notification(client):
    r = await client.post("/api/notifications/test")
    assert r.status_code == 200
    assert "results" in r.json()

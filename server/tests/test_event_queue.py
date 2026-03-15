"""
Tests for RONIN Event Queue, EventBus, and dispatcher.

Run: pytest tests/test_event_queue.py -v
"""
import asyncio
import json
import pytest
from httpx import ASGITransport, AsyncClient

from api import app
from event_queue import Event, EventBus, EventPriority, EventQueue, EventSource
from ronin_mcp_server import MEMORY_DB, init_database


# ─── Fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture
def db():
    """Fresh in-memory database for each test."""
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload TEXT DEFAULT '{}',
            priority TEXT DEFAULT 'normal',
            created_at TEXT NOT NULL,
            processed INTEGER DEFAULT 0,
            processed_at TEXT,
            error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_events_processed ON events(processed);

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

        CREATE TABLE IF NOT EXISTS key_value_store (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS scheduled_tasks (
            task_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            cron_expression TEXT NOT NULL,
            handler TEXT NOT NULL,
            payload TEXT DEFAULT '{}',
            enabled INTEGER DEFAULT 1,
            last_run TEXT,
            next_run TEXT,
            run_count INTEGER DEFAULT 0,
            last_result TEXT,
            created_at TEXT NOT NULL
        );
    """)
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ─── Event Model ──────────────────────────────────────────────────────────

def test_event_creation():
    e = Event(source=EventSource.filesystem, event_type="file_created", payload={"path": "/test"})
    assert e.event_id  # UUID generated
    assert e.source == EventSource.filesystem
    assert e.processed is False
    assert e.priority == EventPriority.normal


def test_event_priority_rank():
    assert EventPriority.critical.rank == 0
    assert EventPriority.high.rank == 1
    assert EventPriority.normal.rank == 2
    assert EventPriority.low.rank == 3


# ─── EventQueue ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_queue_push_pop(db):
    q = EventQueue(db)
    e = Event(source=EventSource.manual, event_type="test_event")
    await q.push(e)
    assert q._queue.qsize() == 1

    popped = await q.pop()
    assert popped.event_id == e.event_id


@pytest.mark.asyncio
async def test_queue_priority_ordering(db):
    q = EventQueue(db)
    low = Event(source=EventSource.manual, event_type="low", priority=EventPriority.low)
    critical = Event(source=EventSource.manual, event_type="critical", priority=EventPriority.critical)
    normal = Event(source=EventSource.manual, event_type="normal", priority=EventPriority.normal)

    await q.push(low)
    await q.push(normal)
    await q.push(critical)

    first = await q.pop()
    assert first.priority == EventPriority.critical

    second = await q.pop()
    assert second.priority == EventPriority.normal


@pytest.mark.asyncio
async def test_queue_persistence(db):
    q = EventQueue(db)
    e = Event(source=EventSource.webhook, event_type="test")
    await q.push(e)

    # Verify persisted to SQLite
    row = db.execute("SELECT * FROM events WHERE event_id = ?", (e.event_id,)).fetchone()
    assert row is not None
    assert row["source"] == "webhook"
    assert row["processed"] == 0


@pytest.mark.asyncio
async def test_queue_mark_processed(db):
    q = EventQueue(db)
    e = Event(source=EventSource.manual, event_type="test")
    await q.push(e)
    await q.mark_processed(e.event_id)

    row = db.execute("SELECT * FROM events WHERE event_id = ?", (e.event_id,)).fetchone()
    assert row["processed"] == 1
    assert row["processed_at"] is not None


@pytest.mark.asyncio
async def test_queue_mark_processed_with_error(db):
    q = EventQueue(db)
    e = Event(source=EventSource.manual, event_type="test")
    await q.push(e)
    await q.mark_processed(e.event_id, error="handler failed")

    row = db.execute("SELECT * FROM events WHERE event_id = ?", (e.event_id,)).fetchone()
    assert row["error"] == "handler failed"


@pytest.mark.asyncio
async def test_queue_replay_unprocessed(db):
    q = EventQueue(db)
    e1 = Event(source=EventSource.manual, event_type="test1")
    e2 = Event(source=EventSource.manual, event_type="test2")
    await q.push(e1)
    await q.push(e2)
    await q.mark_processed(e1.event_id)

    # Simulate restart with new queue pointing to same db
    q2 = EventQueue(db)
    replayed = await q2.replay_unprocessed()
    assert replayed == 1  # Only e2 unprocessed


@pytest.mark.asyncio
async def test_queue_peek(db):
    q = EventQueue(db)
    e1 = Event(source=EventSource.manual, event_type="t1")
    e2 = Event(source=EventSource.manual, event_type="t2")
    await q.push(e1)
    await q.push(e2)

    peeked = await q.peek(5)
    assert len(peeked) == 2
    # Peek doesn't consume
    assert q._queue.qsize() == 2


@pytest.mark.asyncio
async def test_queue_stats(db):
    q = EventQueue(db)
    e = Event(source=EventSource.filesystem, event_type="test")
    await q.push(e)
    await q.mark_processed(e.event_id)

    stats = q.get_stats()
    assert stats["total_pushed"] == 1
    assert stats["processed"] == 1
    assert "filesystem" in stats["by_source"]


# ─── EventBus ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bus_emit(db):
    bus = EventBus(db)
    eid = await bus.emit(EventSource.manual, "test_event", {"key": "val"})
    assert eid  # Returns event_id string


@pytest.mark.asyncio
async def test_bus_handler_registration(db):
    bus = EventBus(db)
    calls = []

    async def handler(event):
        calls.append(event.event_type)

    bus.register_handler("test_*", handler)
    assert len(bus._handlers) == 1


@pytest.mark.asyncio
async def test_bus_dispatch(db):
    bus = EventBus(db)
    results = []

    async def handler(event):
        results.append(event.event_type)

    bus.register_handler("*", handler)
    await bus.start()

    await bus.emit(EventSource.manual, "hello")
    await asyncio.sleep(0.3)  # Let dispatcher process

    await bus.stop()
    assert "hello" in results


@pytest.mark.asyncio
async def test_bus_glob_matching(db):
    bus = EventBus(db)
    results = []

    async def fs_handler(event):
        results.append(("fs", event.event_type))

    async def all_handler(event):
        results.append(("all", event.event_type))

    bus.register_handler("file_*", fs_handler)
    bus.register_handler("*", all_handler)
    await bus.start()

    await bus.emit(EventSource.filesystem, "file_created")
    await bus.emit(EventSource.manual, "other_event")
    await asyncio.sleep(0.5)

    await bus.stop()
    # file_created should match both handlers
    assert ("fs", "file_created") in results
    assert ("all", "file_created") in results
    # other_event matches only all_handler
    assert ("all", "other_event") in results


@pytest.mark.asyncio
async def test_bus_handler_error(db):
    """Handler errors should be caught and logged, not crash the dispatcher."""
    bus = EventBus(db)

    async def bad_handler(event):
        raise ValueError("intentional test error")

    bus.register_handler("*", bad_handler)
    await bus.start()

    await bus.emit(EventSource.manual, "test")
    await asyncio.sleep(0.3)

    await bus.stop()

    # Event should be marked processed with error
    row = db.execute("SELECT * FROM events WHERE processed = 1").fetchone()
    assert row is not None
    assert "intentional test error" in (row["error"] or "")


@pytest.mark.asyncio
async def test_bus_stats(db):
    bus = EventBus(db)
    stats = bus.get_stats()
    assert "queue_depth" in stats
    assert "handlers_registered" in stats
    assert stats["running"] is False


# ─── API Endpoints ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_webhook_endpoint(client):
    r = await client.post("/api/webhooks/custom", json={"data": "test"})
    assert r.status_code == 200
    data = r.json()
    assert data["received"] is True
    assert "event_id" in data
    assert data["event_type"] == "webhook_custom"


@pytest.mark.asyncio
async def test_webhook_github_event_type(client):
    r = await client.post(
        "/api/webhooks/github",
        json={"ref": "refs/heads/main"},
        headers={"X-GitHub-Event": "push"},
    )
    assert r.status_code == 200
    assert r.json()["event_type"] == "webhook_github_push"


@pytest.mark.asyncio
async def test_events_list(client):
    r = await client.get("/api/events")
    assert r.status_code == 200
    data = r.json()
    assert "events" in data
    assert "count" in data


@pytest.mark.asyncio
async def test_events_stats(client):
    r = await client.get("/api/events/stats")
    assert r.status_code == 200
    data = r.json()
    assert "total_events" in data


@pytest.mark.asyncio
async def test_context_endpoint(client):
    r = await client.get("/api/context")
    assert r.status_code == 200
    data = r.json()
    assert "context" in data
    assert "has_content" in data

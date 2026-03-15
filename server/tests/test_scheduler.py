"""
Tests for RONIN Scheduler — cron-based task runner.

Run: pytest tests/test_scheduler.py -v
"""
import asyncio
import json
import sqlite3
import pytest
from httpx import ASGITransport, AsyncClient

from api import app
from event_queue import EventBus, EventSource
from scheduler import Scheduler, CreateScheduleRequest, UpdateScheduleRequest


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
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
    """)
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def bus(db):
    return EventBus(db)


@pytest.fixture
def scheduler(db, bus):
    return Scheduler(db, bus)


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ─── Scheduler CRUD ───────────────────────────────────────────────────────

def test_create_schedule(scheduler):
    req = CreateScheduleRequest(
        name="Test Job",
        cron_expression="*/5 * * * *",
        handler="test_handler",
        payload={"key": "val"},
    )
    task = scheduler.create(req)
    assert task.task_id
    assert task.name == "Test Job"
    assert task.next_run  # Should be computed
    assert task.enabled is True


def test_create_invalid_cron(scheduler):
    req = CreateScheduleRequest(
        name="Bad Cron",
        cron_expression="not a cron",
        handler="test",
    )
    with pytest.raises(ValueError, match="Invalid cron"):
        scheduler.create(req)


def test_get_schedule(scheduler):
    req = CreateScheduleRequest(name="Get Test", cron_expression="0 * * * *", handler="h")
    created = scheduler.create(req)
    fetched = scheduler.get(created.task_id)
    assert fetched is not None
    assert fetched.name == "Get Test"


def test_get_nonexistent(scheduler):
    assert scheduler.get("nonexistent") is None


def test_list_schedules(scheduler):
    scheduler.create(CreateScheduleRequest(name="A", cron_expression="0 * * * *", handler="h"))
    scheduler.create(CreateScheduleRequest(name="B", cron_expression="0 * * * *", handler="h", enabled=False))

    all_tasks = scheduler.list_all()
    assert len(all_tasks) == 2

    enabled = scheduler.list_all(enabled_only=True)
    assert len(enabled) == 1
    assert enabled[0].name == "A"


def test_update_schedule(scheduler):
    task = scheduler.create(CreateScheduleRequest(name="Old", cron_expression="0 * * * *", handler="h"))
    updated = scheduler.update(task.task_id, UpdateScheduleRequest(name="New", enabled=False))
    assert updated.name == "New"
    assert updated.enabled is False


def test_update_invalid_cron(scheduler):
    task = scheduler.create(CreateScheduleRequest(name="X", cron_expression="0 * * * *", handler="h"))
    with pytest.raises(ValueError):
        scheduler.update(task.task_id, UpdateScheduleRequest(cron_expression="bad"))


def test_delete_schedule(scheduler):
    task = scheduler.create(CreateScheduleRequest(name="Del", cron_expression="0 * * * *", handler="h"))
    assert scheduler.delete(task.task_id) is True
    assert scheduler.get(task.task_id) is None
    assert scheduler.delete("nonexistent") is False


@pytest.mark.asyncio
async def test_run_now(scheduler, bus):
    task = scheduler.create(CreateScheduleRequest(name="Immediate", cron_expression="0 0 1 1 *", handler="test"))
    old_count = task.run_count

    result = await scheduler.run_now(task.task_id)
    assert result is not None
    assert result.run_count == old_count + 1
    assert result.last_run is not None


@pytest.mark.asyncio
async def test_run_now_nonexistent(scheduler):
    result = await scheduler.run_now("fake-id")
    assert result is None


# ─── API Endpoints ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_api_create_schedule(client):
    r = await client.post("/api/schedules", json={
        "name": "API Test",
        "cron_expression": "*/10 * * * *",
        "handler": "api_test",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["created"] is True
    assert data["schedule"]["name"] == "API Test"


@pytest.mark.asyncio
async def test_api_list_schedules(client):
    r = await client.get("/api/schedules")
    assert r.status_code == 200
    data = r.json()
    assert "schedules" in data
    assert "count" in data


@pytest.mark.asyncio
async def test_api_create_invalid_cron(client):
    r = await client.post("/api/schedules", json={
        "name": "Bad",
        "cron_expression": "invalid",
        "handler": "h",
    })
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_api_delete_schedule(client):
    # Create then delete
    cr = await client.post("/api/schedules", json={
        "name": "ToDelete",
        "cron_expression": "0 * * * *",
        "handler": "h",
    })
    task_id = cr.json()["schedule"]["task_id"]

    dr = await client.delete(f"/api/schedules/{task_id}")
    assert dr.status_code == 200
    assert dr.json()["deleted"] is True


@pytest.mark.asyncio
async def test_api_run_now(client):
    cr = await client.post("/api/schedules", json={
        "name": "RunNow",
        "cron_expression": "0 0 1 1 *",
        "handler": "h",
    })
    task_id = cr.json()["schedule"]["task_id"]

    rr = await client.post(f"/api/schedules/{task_id}/run")
    assert rr.status_code == 200
    assert rr.json()["triggered"] is True

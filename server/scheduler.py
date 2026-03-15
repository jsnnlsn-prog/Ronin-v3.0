"""
RONIN Scheduler — Cron-based Task Runner
==========================================
P1 deliverable for Phase 4.

Manages scheduled tasks with cron expressions. Each tick checks
if any task's next_run has passed, emits a schedule event to the
EventBus, and updates the schedule.

Uses croniter for cron expression parsing.
"""

import asyncio
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from croniter import croniter
from pydantic import BaseModel, Field

from event_queue import EventBus, EventPriority, EventSource

logger = logging.getLogger("RoninScheduler")


# ═══════════════════════════════════════════════════════════════════════════
# MODELS
# ═══════════════════════════════════════════════════════════════════════════

class ScheduledTask(BaseModel):
    """A recurring task with cron schedule."""
    task_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    cron_expression: str
    handler: str  # identifier for registered handler
    payload: Dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    last_run: Optional[str] = None
    next_run: Optional[str] = None
    run_count: int = 0
    last_result: Optional[str] = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def compute_next_run(self, base_time: Optional[datetime] = None) -> str:
        """Compute next run time from cron expression."""
        base = base_time or datetime.now(timezone.utc)
        cron = croniter(self.cron_expression, base)
        next_dt = cron.get_next(datetime)
        return next_dt.isoformat()


class CreateScheduleRequest(BaseModel):
    name: str
    cron_expression: str
    handler: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class UpdateScheduleRequest(BaseModel):
    name: Optional[str] = None
    cron_expression: Optional[str] = None
    handler: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None
    enabled: Optional[bool] = None


# ═══════════════════════════════════════════════════════════════════════════
# SCHEDULER
# ═══════════════════════════════════════════════════════════════════════════

class Scheduler:
    """
    Background task that checks scheduled tasks every 30 seconds
    and emits events to the EventBus when it's time to run.
    """

    def __init__(self, db: sqlite3.Connection, event_bus: EventBus, check_interval: float = 30.0):
        self.db = db
        self.event_bus = event_bus
        self.check_interval = check_interval
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._tick_loop())
        logger.info("Scheduler started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Scheduler stopped")

    # ─── CRUD ─────────────────────────────────────────────────────────────

    def create(self, req: CreateScheduleRequest) -> ScheduledTask:
        """Create a new scheduled task."""
        # Validate cron expression
        if not croniter.is_valid(req.cron_expression):
            raise ValueError(f"Invalid cron expression: {req.cron_expression}")

        task = ScheduledTask(
            name=req.name,
            cron_expression=req.cron_expression,
            handler=req.handler,
            payload=req.payload,
            enabled=req.enabled,
        )
        task.next_run = task.compute_next_run()

        self.db.execute(
            """INSERT INTO scheduled_tasks
               (task_id, name, cron_expression, handler, payload, enabled,
                last_run, next_run, run_count, last_result, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task.task_id, task.name, task.cron_expression, task.handler,
                json.dumps(task.payload), int(task.enabled),
                task.last_run, task.next_run, task.run_count,
                task.last_result, task.created_at,
            ),
        )
        self.db.commit()
        return task

    def get(self, task_id: str) -> Optional[ScheduledTask]:
        row = self.db.execute(
            "SELECT * FROM scheduled_tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if not row:
            return None
        return self._row_to_task(row)

    def list_all(self, enabled_only: bool = False) -> List[ScheduledTask]:
        query = "SELECT * FROM scheduled_tasks"
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY next_run ASC"
        rows = self.db.execute(query).fetchall()
        return [self._row_to_task(r) for r in rows]

    def update(self, task_id: str, req: UpdateScheduleRequest) -> Optional[ScheduledTask]:
        task = self.get(task_id)
        if not task:
            return None

        if req.name is not None:
            task.name = req.name
        if req.cron_expression is not None:
            if not croniter.is_valid(req.cron_expression):
                raise ValueError(f"Invalid cron expression: {req.cron_expression}")
            task.cron_expression = req.cron_expression
            task.next_run = task.compute_next_run()
        if req.handler is not None:
            task.handler = req.handler
        if req.payload is not None:
            task.payload = req.payload
        if req.enabled is not None:
            task.enabled = req.enabled

        self.db.execute(
            """UPDATE scheduled_tasks SET
               name=?, cron_expression=?, handler=?, payload=?, enabled=?, next_run=?
               WHERE task_id=?""",
            (
                task.name, task.cron_expression, task.handler,
                json.dumps(task.payload), int(task.enabled), task.next_run,
                task_id,
            ),
        )
        self.db.commit()
        return task

    def delete(self, task_id: str) -> bool:
        result = self.db.execute(
            "DELETE FROM scheduled_tasks WHERE task_id = ?", (task_id,)
        )
        self.db.commit()
        return result.rowcount > 0

    async def run_now(self, task_id: str) -> Optional[ScheduledTask]:
        """Trigger a task immediately, bypassing cron schedule."""
        task = self.get(task_id)
        if not task:
            return None
        await self._fire_task(task)
        return self.get(task_id)  # Return refreshed

    # ─── Tick Loop ────────────────────────────────────────────────────────

    async def _tick_loop(self) -> None:
        """Check scheduled tasks every check_interval seconds."""
        while self._running:
            try:
                await self._check_schedules()
                await asyncio.sleep(self.check_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Scheduler tick error: {e}")
                await asyncio.sleep(self.check_interval)

    async def _check_schedules(self) -> None:
        """Check all enabled tasks and fire any that are due."""
        now = datetime.now(timezone.utc)
        tasks = self.list_all(enabled_only=True)

        for task in tasks:
            if not task.next_run:
                continue
            try:
                next_run_dt = datetime.fromisoformat(task.next_run)
                # Make naive datetimes UTC-aware for comparison
                if next_run_dt.tzinfo is None:
                    next_run_dt = next_run_dt.replace(tzinfo=timezone.utc)
                if now >= next_run_dt:
                    await self._fire_task(task)
            except (ValueError, TypeError) as e:
                logger.error(f"Bad next_run for task {task.task_id}: {e}")

    async def _fire_task(self, task: ScheduledTask) -> None:
        """Emit a schedule event and update task metadata."""
        now_iso = datetime.now(timezone.utc).isoformat()

        # Emit event
        await self.event_bus.emit(
            source=EventSource.schedule,
            event_type=f"cron_{task.handler}",
            payload={
                "task_id": task.task_id,
                "name": task.name,
                "handler": task.handler,
                "payload": task.payload,
                "run_count": task.run_count + 1,
            },
            priority=EventPriority.normal,
        )

        # Update task
        new_next = task.compute_next_run()
        self.db.execute(
            """UPDATE scheduled_tasks SET
               last_run=?, next_run=?, run_count=run_count+1, last_result=?
               WHERE task_id=?""",
            (now_iso, new_next, "fired", task.task_id),
        )
        self.db.commit()
        logger.info(f"Scheduled task fired: {task.name} (next: {new_next})")

    # ─── Helpers ──────────────────────────────────────────────────────────

    def _row_to_task(self, row: sqlite3.Row) -> ScheduledTask:
        return ScheduledTask(
            task_id=row["task_id"],
            name=row["name"],
            cron_expression=row["cron_expression"],
            handler=row["handler"],
            payload=json.loads(row["payload"]),
            enabled=bool(row["enabled"]),
            last_run=row["last_run"],
            next_run=row["next_run"],
            run_count=row["run_count"],
            last_result=row["last_result"],
            created_at=row["created_at"],
        )


# ═══════════════════════════════════════════════════════════════════════════
# DATABASE TABLE
# ═══════════════════════════════════════════════════════════════════════════

SCHEDULED_TASKS_TABLE_SQL = """
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

CREATE INDEX IF NOT EXISTS idx_sched_enabled ON scheduled_tasks(enabled);
CREATE INDEX IF NOT EXISTS idx_sched_next_run ON scheduled_tasks(next_run);
"""

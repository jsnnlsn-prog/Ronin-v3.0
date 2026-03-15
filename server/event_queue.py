"""
RONIN Event Queue — Async Event-Driven Architecture
======================================================
P0 deliverable for Phase 4: Proactive Intelligence.

Components:
  - Event (Pydantic model): standardized event envelope
  - EventQueue: asyncio.Queue + SQLite persistence
  - EventBus: facade tying queue + dispatcher + handler registry
  - Dispatcher loop: background task routing events to handlers

All events are persisted to SQLite for crash recovery. On startup,
unprocessed events are replayed into the in-memory queue.
"""

import asyncio
import fnmatch
import json
import logging
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger("RoninEventQueue")


# ═══════════════════════════════════════════════════════════════════════════
# MODELS
# ═══════════════════════════════════════════════════════════════════════════

class EventSource(str, Enum):
    filesystem = "filesystem"
    webhook = "webhook"
    schedule = "schedule"
    system = "system"
    manual = "manual"


class EventPriority(str, Enum):
    low = "low"
    normal = "normal"
    high = "high"
    critical = "critical"

    @property
    def rank(self) -> int:
        return {"critical": 0, "high": 1, "normal": 2, "low": 3}[self.value]


class Event(BaseModel):
    """Standardized event envelope for all perception sources."""
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source: EventSource
    event_type: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    priority: EventPriority = EventPriority.normal
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    processed: bool = False
    processed_at: Optional[str] = None
    error: Optional[str] = None


# ═══════════════════════════════════════════════════════════════════════════
# EVENT QUEUE — asyncio + SQLite persistence
# ═══════════════════════════════════════════════════════════════════════════

class EventQueue:
    """
    Priority-ordered event queue backed by SQLite for persistence.
    Uses asyncio.PriorityQueue for in-memory dispatch.
    """

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._total_pushed = 0
        self._total_processed = 0

    async def push(self, event: Event) -> None:
        """Enqueue event and persist to SQLite."""
        # Persist first (crash safety)
        self.db.execute(
            """INSERT OR REPLACE INTO events
               (event_id, source, event_type, payload, priority, created_at, processed, processed_at, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event.event_id, event.source.value, event.event_type,
                json.dumps(event.payload), event.priority.value,
                event.created_at, int(event.processed),
                event.processed_at, event.error,
            ),
        )
        self.db.commit()

        # Enqueue in-memory (priority rank, creation time for FIFO within priority, event)
        await self._queue.put((event.priority.rank, event.created_at, event))
        self._total_pushed += 1

    async def pop(self) -> Event:
        """Dequeue next event (priority-ordered, blocks if empty)."""
        _, _, event = await self._queue.get()
        return event

    def pop_nowait(self) -> Optional[Event]:
        """Non-blocking pop, returns None if empty."""
        try:
            _, _, event = self._queue.get_nowait()
            return event
        except asyncio.QueueEmpty:
            return None

    async def peek(self, n: int = 5) -> List[Event]:
        """View upcoming events without consuming. Returns up to n events."""
        rows = self.db.execute(
            """SELECT * FROM events WHERE processed = 0
               ORDER BY
                 CASE priority
                   WHEN 'critical' THEN 0
                   WHEN 'high' THEN 1
                   WHEN 'normal' THEN 2
                   WHEN 'low' THEN 3
                 END,
                 created_at ASC
               LIMIT ?""",
            (n,),
        ).fetchall()
        return [self._row_to_event(r) for r in rows]

    async def mark_processed(self, event_id: str, error: Optional[str] = None) -> None:
        """Mark event as processed in SQLite."""
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            "UPDATE events SET processed = 1, processed_at = ?, error = ? WHERE event_id = ?",
            (now, error, event_id),
        )
        self.db.commit()
        self._total_processed += 1

    async def replay_unprocessed(self) -> int:
        """On startup, re-enqueue any persisted but unprocessed events."""
        rows = self.db.execute(
            """SELECT * FROM events WHERE processed = 0
               ORDER BY
                 CASE priority
                   WHEN 'critical' THEN 0
                   WHEN 'high' THEN 1
                   WHEN 'normal' THEN 2
                   WHEN 'low' THEN 3
                 END,
                 created_at ASC""",
        ).fetchall()
        count = 0
        for row in rows:
            event = self._row_to_event(row)
            await self._queue.put((event.priority.rank, event.created_at, event))
            count += 1
        if count:
            logger.info(f"Replayed {count} unprocessed events from crash recovery")
        return count

    def get_stats(self) -> Dict[str, Any]:
        """Queue statistics: counts by source, processed/pending, throughput."""
        rows = self.db.execute(
            """SELECT source, processed, COUNT(*) as cnt
               FROM events GROUP BY source, processed"""
        ).fetchall()

        by_source: Dict[str, Dict[str, int]] = {}
        total_pending = 0
        total_processed = 0
        for r in rows:
            src = r["source"]
            if src not in by_source:
                by_source[src] = {"pending": 0, "processed": 0}
            if r["processed"]:
                by_source[src]["processed"] += r["cnt"]
                total_processed += r["cnt"]
            else:
                by_source[src]["pending"] += r["cnt"]
                total_pending += r["cnt"]

        return {
            "queue_depth": self._queue.qsize(),
            "total_pushed": self._total_pushed,
            "total_processed_session": self._total_processed,
            "pending": total_pending,
            "processed": total_processed,
            "by_source": by_source,
        }

    def _row_to_event(self, row: sqlite3.Row) -> Event:
        return Event(
            event_id=row["event_id"],
            source=EventSource(row["source"]),
            event_type=row["event_type"],
            payload=json.loads(row["payload"]),
            priority=EventPriority(row["priority"]),
            created_at=row["created_at"],
            processed=bool(row["processed"]),
            processed_at=row["processed_at"],
            error=row["error"],
        )


# ═══════════════════════════════════════════════════════════════════════════
# EVENT BUS — facade: queue + dispatcher + handler registry
# ═══════════════════════════════════════════════════════════════════════════

# Handler type: async callable receiving Event, returning optional result string
EventHandler = Callable[[Event], Any]


class EventBus:
    """
    Central event bus: register handlers by event_type pattern (glob),
    emit events, start/stop the background dispatcher.
    """

    def __init__(self, db: sqlite3.Connection, audit_fn: Optional[Callable] = None, max_concurrency: int = 3):
        self.queue = EventQueue(db)
        self.db = db
        self._handlers: List[tuple] = []  # (pattern, handler)
        self._dispatcher_task: Optional[asyncio.Task] = None
        self._running = False
        self._audit_fn = audit_fn
        self._semaphore = asyncio.Semaphore(max_concurrency)

    def register_handler(self, event_type_pattern: str, handler: EventHandler) -> None:
        """
        Register a handler for events matching a glob pattern.
        Examples: 'file_*', 'webhook_github_*', '*' (catch-all).
        """
        self._handlers.append((event_type_pattern, handler))
        logger.info(f"Registered handler for pattern '{event_type_pattern}': {handler.__name__}")

    async def emit(
        self,
        source: EventSource,
        event_type: str,
        payload: Optional[Dict[str, Any]] = None,
        priority: EventPriority = EventPriority.normal,
    ) -> str:
        """Convenience: create Event and push to queue. Returns event_id."""
        event = Event(
            source=source,
            event_type=event_type,
            payload=payload or {},
            priority=priority,
        )
        await self.queue.push(event)
        return event.event_id

    async def start(self) -> None:
        """Start the background dispatcher loop."""
        if self._running:
            return
        self._running = True
        # Replay any unprocessed events from previous crash
        replayed = await self.queue.replay_unprocessed()
        if replayed:
            logger.info(f"Crash recovery: {replayed} events replayed")
        self._dispatcher_task = asyncio.create_task(self._dispatch_loop())
        logger.info("EventBus dispatcher started")

    async def stop(self) -> None:
        """Stop the dispatcher loop gracefully."""
        self._running = False
        if self._dispatcher_task:
            self._dispatcher_task.cancel()
            try:
                await self._dispatcher_task
            except asyncio.CancelledError:
                pass
            self._dispatcher_task = None
        logger.info("EventBus dispatcher stopped")

    def get_stats(self) -> Dict[str, Any]:
        """Delegate to queue stats + add handler count."""
        stats = self.queue.get_stats()
        stats["handlers_registered"] = len(self._handlers)
        stats["running"] = self._running
        return stats

    # ─── Dispatcher Loop ──────────────────────────────────────────────────

    async def _dispatch_loop(self) -> None:
        """Continuously pop events and route to matching handlers."""
        while self._running:
            try:
                # Use wait_for with timeout so we can check _running periodically
                try:
                    event = await asyncio.wait_for(self.queue.pop(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                # Find matching handlers
                matching = [
                    handler
                    for pattern, handler in self._handlers
                    if fnmatch.fnmatch(event.event_type, pattern)
                ]

                if not matching:
                    logger.debug(f"No handler for event type '{event.event_type}', marking processed")
                    await self.queue.mark_processed(event.event_id)
                    continue

                # Execute handlers with concurrency limit
                error = None
                for handler in matching:
                    try:
                        async with self._semaphore:
                            result = handler(event)
                            # Support both sync and async handlers
                            if asyncio.iscoroutine(result):
                                result = await result
                    except Exception as e:
                        error = f"{handler.__name__}: {str(e)}"
                        logger.error(f"Handler error for event {event.event_id}: {error}")

                # Mark processed
                await self.queue.mark_processed(event.event_id, error=error)

                # Audit trail
                if self._audit_fn:
                    try:
                        self._audit_fn(
                            self.db,
                            "event_bus",
                            "dispatcher",
                            f"{event.source.value}:{event.event_type}",
                            error or "ok",
                            error is None,
                            0.0,
                        )
                    except Exception:
                        pass

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Dispatcher loop error: {e}")
                await asyncio.sleep(0.5)


# ═══════════════════════════════════════════════════════════════════════════
# DATABASE TABLE INIT
# ═══════════════════════════════════════════════════════════════════════════

EVENTS_TABLE_SQL = """
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
CREATE INDEX IF NOT EXISTS idx_events_source ON events(source);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_priority ON events(priority);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at DESC);
"""

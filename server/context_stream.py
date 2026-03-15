"""
RONIN Context Stream — Unified Perception Summary
====================================================
P3 deliverable for Phase 4.

Subscribes to EventBus, maintains a sliding window of recent events,
and produces a compressed summary (<500 tokens) suitable for injection
into the system prompt as a [CONTEXT] block.
"""

import json
import logging
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from event_queue import Event, EventBus, EventPriority, EventSource

logger = logging.getLogger("RoninContextStream")


class ContextStream:
    """
    Aggregates events from all sources into a compressed context summary.
    The summary is injected into the system prompt so the agent has
    awareness of what's happening without being asked.
    """

    def __init__(self, db: sqlite3.Connection, max_window: int = 100):
        self.db = db
        self.max_window = max_window
        self._recent_events: List[Event] = []

    def add_event(self, event: Event) -> None:
        """Add an event to the sliding window."""
        self._recent_events.append(event)
        if len(self._recent_events) > self.max_window:
            self._recent_events = self._recent_events[-self.max_window:]

    def get_context(self, max_events: int = 20) -> str:
        """
        Generate a compressed context summary string.
        Groups events by source/type for conciseness. Target: <500 tokens.
        """
        if not self._recent_events:
            return ""

        # Use the most recent events
        events = self._recent_events[-max_events:]

        sections = []

        # Group by source
        by_source: Dict[str, List[Event]] = defaultdict(list)
        for e in events:
            by_source[e.source.value].append(e)

        # Filesystem events — compress to counts
        if "filesystem" in by_source:
            fs_events = by_source["filesystem"]
            type_counts = Counter(e.event_type for e in fs_events)
            paths = set()
            for e in fs_events[-5:]:
                rel = e.payload.get("relative_path", e.payload.get("path", ""))
                if rel:
                    paths.add(rel)
            parts = [f"{v} {k.replace('file_', '')}" for k, v in type_counts.items()]
            line = f"Files: {', '.join(parts)}"
            if paths:
                line += f" (recent: {', '.join(list(paths)[:3])})"
            sections.append(line)

        # Webhook events
        if "webhook" in by_source:
            wh_events = by_source["webhook"]
            type_counts = Counter(e.event_type for e in wh_events)
            parts = [f"{v}x {k.replace('webhook_', '')}" for k, v in type_counts.items()]
            sections.append(f"Webhooks: {', '.join(parts)}")

        # Schedule events
        if "schedule" in by_source:
            sch_events = by_source["schedule"]
            names = set(e.payload.get("name", e.event_type) for e in sch_events)
            sections.append(f"Scheduled runs: {', '.join(list(names)[:5])}")

        # System events — only show warnings/critical
        if "system" in by_source:
            sys_events = [e for e in by_source["system"] if e.priority in (EventPriority.high, EventPriority.critical)]
            if sys_events:
                alerts = [e.event_type.replace("system_", "") for e in sys_events[-3:]]
                sections.append(f"System alerts: {', '.join(alerts)}")

        # Pending scheduled tasks from DB
        try:
            pending = self.db.execute(
                """SELECT name, next_run FROM scheduled_tasks
                   WHERE enabled = 1 ORDER BY next_run ASC LIMIT 3"""
            ).fetchall()
            if pending:
                task_lines = [f"{r['name']} (next: {r['next_run'][:16]})" for r in pending]
                sections.append(f"Upcoming tasks: {', '.join(task_lines)}")
        except Exception:
            pass

        if not sections:
            return ""

        return "\n".join(sections)

    def get_recent_events(
        self,
        source: Optional[str] = None,
        event_type: Optional[str] = None,
        processed: Optional[bool] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Query recent events from SQLite with optional filters."""
        query = "SELECT * FROM events WHERE 1=1"
        params = []

        if source:
            query += " AND source = ?"
            params.append(source)
        if event_type:
            query += " AND event_type LIKE ?"
            params.append(f"%{event_type}%")
        if processed is not None:
            query += " AND processed = ?"
            params.append(int(processed))

        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        rows = self.db.execute(query, params).fetchall()
        return [
            {
                "event_id": r["event_id"],
                "source": r["source"],
                "event_type": r["event_type"],
                "payload": json.loads(r["payload"]),
                "priority": r["priority"],
                "created_at": r["created_at"],
                "processed": bool(r["processed"]),
                "processed_at": r["processed_at"],
                "error": r["error"],
            }
            for r in rows
        ]

    def get_event_stats(self) -> Dict[str, Any]:
        """Event processing statistics."""
        rows = self.db.execute(
            """SELECT source, event_type, processed, COUNT(*) as cnt
               FROM events GROUP BY source, event_type, processed"""
        ).fetchall()

        by_source: Dict[str, int] = Counter()
        by_type: Dict[str, int] = Counter()
        total = 0
        processed = 0
        errors = 0

        for r in rows:
            by_source[r["source"]] += r["cnt"]
            by_type[r["event_type"]] += r["cnt"]
            total += r["cnt"]
            if r["processed"]:
                processed += r["cnt"]

        # Count errors
        err_row = self.db.execute(
            "SELECT COUNT(*) as cnt FROM events WHERE error IS NOT NULL"
        ).fetchone()
        if err_row:
            errors = err_row["cnt"]

        return {
            "total_events": total,
            "processed": processed,
            "pending": total - processed,
            "errors": errors,
            "by_source": dict(by_source),
            "by_type": dict(by_type.most_common(10)),
            "window_size": len(self._recent_events),
        }


def create_context_handler(stream: ContextStream):
    """Create an EventBus handler that feeds events into the context stream."""
    def _handler(event: Event) -> None:
        stream.add_event(event)
    return _handler

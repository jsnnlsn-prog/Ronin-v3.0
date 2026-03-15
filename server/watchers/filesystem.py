"""
RONIN Filesystem Watcher — Detects workspace file changes
============================================================
Uses watchfiles (Rust-powered, async, cross-platform) to monitor
the RONIN workspace directory and emit events via the EventBus.

Opt-in by default: enabled via KV store config:fs_watch_enabled.
"""

import asyncio
import fnmatch
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from event_queue import EventBus, EventPriority, EventSource

logger = logging.getLogger("RoninFilesystemWatcher")

# Default exclusions
DEFAULT_EXCLUDES = {"__pycache__", "*.pyc", ".git", ".git/**", "*.swp", "*.tmp", ".DS_Store"}


class FilesystemWatcher:
    """
    Watches a directory for file changes and emits events to the EventBus.
    Debounces rapid changes (1 second window).
    """

    def __init__(
        self,
        event_bus: EventBus,
        watch_path: Path,
        exclude_patterns: Optional[Set[str]] = None,
        debounce_ms: int = 1000,
    ):
        self.event_bus = event_bus
        self.watch_path = watch_path
        self.exclude_patterns = exclude_patterns or DEFAULT_EXCLUDES
        self.debounce_ms = debounce_ms
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self) -> None:
        """Start watching in background task."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._watch_loop())
        logger.info(f"Filesystem watcher started: {self.watch_path}")

    async def stop(self) -> None:
        """Stop watching."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Filesystem watcher stopped")

    def _should_exclude(self, path: str) -> bool:
        """Check if path matches any exclude pattern."""
        rel = os.path.relpath(path, self.watch_path)
        name = os.path.basename(path)
        for pat in self.exclude_patterns:
            if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(rel, pat):
                return True
        return False

    async def _watch_loop(self) -> None:
        """Main watch loop using watchfiles."""
        try:
            from watchfiles import awatch, Change
        except ImportError:
            logger.error("watchfiles not installed — filesystem watcher disabled")
            return

        change_type_map = {
            Change.added: "file_created",
            Change.modified: "file_modified",
            Change.deleted: "file_deleted",
        }

        try:
            async for changes in awatch(
                self.watch_path,
                debounce=self.debounce_ms,
                recursive=True,
                step=200,
                stop_event=None,
            ):
                if not self._running:
                    break

                for change_type, path_str in changes:
                    if self._should_exclude(path_str):
                        continue

                    event_type = change_type_map.get(change_type, "file_unknown")
                    p = Path(path_str)

                    payload: Dict[str, Any] = {
                        "path": str(p),
                        "relative_path": str(p.relative_to(self.watch_path)) if p.is_relative_to(self.watch_path) else str(p),
                        "change_type": event_type,
                        "extension": p.suffix,
                        "name": p.name,
                    }

                    # Add size for non-deleted files
                    if change_type != Change.deleted and p.exists():
                        try:
                            payload["size"] = p.stat().st_size
                        except OSError:
                            pass

                    await self.event_bus.emit(
                        source=EventSource.filesystem,
                        event_type=event_type,
                        payload=payload,
                        priority=EventPriority.low,
                    )
                    logger.debug(f"FS event: {event_type} {payload.get('relative_path', path_str)}")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Filesystem watcher error: {e}")


def load_watch_rules(db) -> Dict[str, Any]:
    """Load filesystem watch configuration from KV store."""
    try:
        row = db.execute(
            "SELECT value FROM key_value_store WHERE key = ?",
            ("config:fs_watch_rules",),
        ).fetchone()
        if row:
            return json.loads(row["value"])
    except Exception:
        pass
    return {"excludes": list(DEFAULT_EXCLUDES)}


def is_watch_enabled(db) -> bool:
    """Check if filesystem watching is enabled in config."""
    try:
        row = db.execute(
            "SELECT value FROM key_value_store WHERE key = ?",
            ("config:fs_watch_enabled",),
        ).fetchone()
        if row:
            return json.loads(row["value"]) is True
    except Exception:
        pass
    return False  # Disabled by default

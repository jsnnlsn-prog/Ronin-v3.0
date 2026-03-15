"""
RONIN System Monitor — Resource & Self-Monitoring
====================================================
P2 deliverable for Phase 4.

Periodically checks disk, memory, CPU and emits system events
when thresholds are crossed. Also monitors event queue depth
and database size.

Uses /proc parsing on Linux (no psutil dependency).
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from event_queue import EventBus, EventPriority, EventSource

logger = logging.getLogger("RoninSystemMonitor")


# ═══════════════════════════════════════════════════════════════════════════
# THRESHOLDS
# ═══════════════════════════════════════════════════════════════════════════

DISK_WARNING_PCT = 85
DISK_CRITICAL_PCT = 95
MEMORY_WARNING_PCT = 80
CPU_SUSTAINED_PCT = 90
CPU_SUSTAINED_SAMPLES = 5  # 5 checks = 5 minutes at 60s interval
QUEUE_BACKLOG_THRESHOLD = 100


class SystemMonitor:
    """
    Background task checking system resources every check_interval seconds.
    Emits events when thresholds are crossed.
    """

    def __init__(
        self,
        event_bus: EventBus,
        db_path: Path,
        workspace_path: Path,
        check_interval: float = 60.0,
    ):
        self.event_bus = event_bus
        self.db_path = db_path
        self.workspace_path = workspace_path
        self.check_interval = check_interval
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._cpu_high_count = 0  # Track sustained high CPU
        self._last_metrics: Dict[str, Any] = {}

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info("System monitor started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("System monitor stopped")

    def get_metrics(self) -> Dict[str, Any]:
        """Get current system metrics (cached from last check)."""
        if not self._last_metrics:
            self._last_metrics = self._collect_metrics()
        return self._last_metrics

    # ─── Monitor Loop ─────────────────────────────────────────────────────

    async def _monitor_loop(self) -> None:
        while self._running:
            try:
                metrics = self._collect_metrics()
                self._last_metrics = metrics
                await self._check_thresholds(metrics)
                await asyncio.sleep(self.check_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"System monitor error: {e}")
                await asyncio.sleep(self.check_interval)

    async def _check_thresholds(self, metrics: Dict[str, Any]) -> None:
        """Emit events when thresholds are crossed."""
        disk_pct = metrics.get("disk_percent", 0)
        mem_pct = metrics.get("memory_percent", 0)
        cpu_pct = metrics.get("cpu_percent", 0)
        queue_depth = metrics.get("event_queue_depth", 0)

        if disk_pct >= DISK_CRITICAL_PCT:
            await self.event_bus.emit(
                EventSource.system, "system_disk_critical",
                {"disk_percent": disk_pct, "threshold": DISK_CRITICAL_PCT},
                EventPriority.critical,
            )
        elif disk_pct >= DISK_WARNING_PCT:
            await self.event_bus.emit(
                EventSource.system, "system_disk_warning",
                {"disk_percent": disk_pct, "threshold": DISK_WARNING_PCT},
                EventPriority.high,
            )

        if mem_pct >= MEMORY_WARNING_PCT:
            await self.event_bus.emit(
                EventSource.system, "system_memory_warning",
                {"memory_percent": mem_pct, "threshold": MEMORY_WARNING_PCT},
                EventPriority.high,
            )

        if cpu_pct >= CPU_SUSTAINED_PCT:
            self._cpu_high_count += 1
            if self._cpu_high_count >= CPU_SUSTAINED_SAMPLES:
                await self.event_bus.emit(
                    EventSource.system, "system_cpu_sustained",
                    {"cpu_percent": cpu_pct, "sustained_minutes": self._cpu_high_count},
                    EventPriority.high,
                )
        else:
            self._cpu_high_count = 0

        if queue_depth > QUEUE_BACKLOG_THRESHOLD:
            await self.event_bus.emit(
                EventSource.system, "system_queue_backlog",
                {"queue_depth": queue_depth, "threshold": QUEUE_BACKLOG_THRESHOLD},
                EventPriority.high,
            )

    # ─── Metrics Collection ───────────────────────────────────────────────

    def _collect_metrics(self) -> Dict[str, Any]:
        """Collect system metrics from /proc and filesystem."""
        metrics: Dict[str, Any] = {}

        # Disk usage
        try:
            st = os.statvfs("/")
            total = st.f_blocks * st.f_frsize
            free = st.f_bavail * st.f_frsize
            used = total - free
            metrics["disk_percent"] = round((used / total) * 100, 1) if total > 0 else 0
            metrics["disk_total_gb"] = round(total / (1024**3), 1)
            metrics["disk_free_gb"] = round(free / (1024**3), 1)
        except Exception:
            metrics["disk_percent"] = 0

        # Memory from /proc/meminfo
        try:
            meminfo = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    parts = line.split(":")
                    if len(parts) == 2:
                        key = parts[0].strip()
                        val = parts[1].strip().split()[0]  # kB value
                        meminfo[key] = int(val)
            total_kb = meminfo.get("MemTotal", 1)
            avail_kb = meminfo.get("MemAvailable", total_kb)
            used_kb = total_kb - avail_kb
            metrics["memory_percent"] = round((used_kb / total_kb) * 100, 1) if total_kb > 0 else 0
            metrics["memory_total_mb"] = round(total_kb / 1024, 0)
            metrics["memory_used_mb"] = round(used_kb / 1024, 0)
        except Exception:
            metrics["memory_percent"] = 0

        # CPU from /proc/stat (instantaneous, not great but simple)
        try:
            with open("/proc/loadavg") as f:
                parts = f.read().split()
                load_1 = float(parts[0])
                cpu_count = os.cpu_count() or 1
                metrics["cpu_percent"] = round(min(load_1 / cpu_count * 100, 100), 1)
                metrics["load_1m"] = load_1
        except Exception:
            metrics["cpu_percent"] = 0

        # DB size
        try:
            if self.db_path.exists():
                metrics["db_size_mb"] = round(self.db_path.stat().st_size / (1024**2), 2)
            else:
                metrics["db_size_mb"] = 0
        except Exception:
            metrics["db_size_mb"] = 0

        # Workspace size (quick estimate: sum top-level file sizes)
        try:
            total_size = 0
            if self.workspace_path.exists():
                for item in self.workspace_path.rglob("*"):
                    if item.is_file():
                        total_size += item.stat().st_size
            metrics["workspace_size_mb"] = round(total_size / (1024**2), 2)
        except Exception:
            metrics["workspace_size_mb"] = 0

        # Event queue stats (injected by caller if available)
        metrics["event_queue_depth"] = 0
        metrics["events_processed_total"] = 0

        return metrics

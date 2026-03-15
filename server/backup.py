"""
RONIN Backup — SQLite Backup/Restore + Export/Import
=======================================================
Hot backup using sqlite3.backup() — no downtime required.
Restore swaps in a validated backup file.
Export/import JSON for migration.
"""

import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

# ─── CONFIGURATION ───────────────────────────────────────────────────────────

RONIN_HOME = Path(os.environ.get("RONIN_HOME", Path.home() / ".ronin"))
BACKUP_DIR = RONIN_HOME / "backups"
DEFAULT_RETENTION = 7  # keep last N backups


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


# ─── BACKUP ──────────────────────────────────────────────────────────────────

def backup_database(
    db_path: Path,
    backup_dir: Optional[Path] = None,
    retention: int = DEFAULT_RETENTION,
) -> Path:
    """
    Create a hot backup of the SQLite database.
    Returns path to the new backup file.
    """
    backup_dir = backup_dir or BACKUP_DIR
    backup_dir.mkdir(parents=True, exist_ok=True)

    stamp = _now_stamp()
    backup_path = backup_dir / f"memory_{stamp}.db"

    # sqlite3.backup() is safe to run while DB is in use
    source = sqlite3.connect(str(db_path))
    dest = sqlite3.connect(str(backup_path))
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()

    # Prune old backups
    _prune_backups(backup_dir, retention)

    return backup_path


def _prune_backups(backup_dir: Path, keep: int) -> None:
    """Remove old backup files beyond the retention limit."""
    backups = sorted(
        backup_dir.glob("memory_*.db"),
        key=lambda p: p.stat().st_mtime,
    )
    to_delete = backups[:-keep] if len(backups) > keep else []
    for old in to_delete:
        try:
            old.unlink()
        except Exception:
            pass


def list_backups(backup_dir: Optional[Path] = None) -> List[dict]:
    """List available backup files with metadata."""
    backup_dir = backup_dir or BACKUP_DIR
    if not backup_dir.exists():
        return []

    result = []
    for p in sorted(backup_dir.glob("memory_*.db"), reverse=True):
        try:
            stat = p.stat()
            result.append({
                "filename": p.name,
                "size_bytes": stat.st_size,
                "size_mb": round(stat.st_size / (1024 * 1024), 3),
                "created_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                "path": str(p),
            })
        except Exception:
            pass
    return result


# ─── RESTORE ─────────────────────────────────────────────────────────────────

def validate_backup(backup_path: Path) -> bool:
    """Check that a backup file is a valid SQLite database."""
    try:
        conn = sqlite3.connect(str(backup_path))
        # Quick integrity check
        result = conn.execute("PRAGMA integrity_check").fetchone()
        conn.close()
        return result and result[0] == "ok"
    except Exception:
        return False


def restore_database(backup_path: Path, target_path: Path) -> None:
    """
    Restore from a backup. Creates a safety copy of current DB first.
    Raises ValueError if backup is invalid.
    """
    backup_path = Path(backup_path)
    if not backup_path.exists():
        raise FileNotFoundError(f"Backup not found: {backup_path}")

    if not validate_backup(backup_path):
        raise ValueError(f"Backup failed integrity check: {backup_path}")

    # Save current DB as emergency backup
    if target_path.exists():
        safety = target_path.parent / f"memory_prerestore_{_now_stamp()}.db"
        shutil.copy2(str(target_path), str(safety))

    # Restore
    shutil.copy2(str(backup_path), str(target_path))


# ─── EXPORT / IMPORT ─────────────────────────────────────────────────────────

_EXPORT_TABLES = [
    ("semantic_memory", "SELECT id, fact, confidence, source, tags, created_at, last_accessed, access_count FROM semantic_memory"),
    ("episodic_memory", "SELECT id, interaction, reflection, importance_score, agent, tags, created_at FROM episodic_memory"),
    ("key_value_store", "SELECT key, value, updated_at FROM key_value_store"),
    ("scheduled_tasks", "SELECT task_id, name, cron_expression, handler, payload, enabled, last_run, next_run, run_count, last_result, created_at FROM scheduled_tasks"),
    ("audit_log", "SELECT id, timestamp, tool_name, agent, input_summary, output_summary, success, execution_ms FROM audit_log ORDER BY timestamp DESC LIMIT 1000"),
]


def export_data(conn: sqlite3.Connection) -> dict:
    """
    Export user data as JSON dict.
    Vault keys are exported by name only (values omitted for security).
    """
    conn.row_factory = sqlite3.Row
    export = {
        "exported_at": _now_iso(),
        "version": "3.0",
        "tables": {},
    }

    for table_name, query in _EXPORT_TABLES:
        try:
            rows = conn.execute(query).fetchall()
            export["tables"][table_name] = [dict(r) for r in rows]
        except Exception as e:
            export["tables"][table_name] = {"error": str(e)}

    # Vault: names only
    try:
        vault_rows = conn.execute("SELECT key_name, created_at, updated_at FROM vault").fetchall()
        export["vault_key_names"] = [r[0] for r in vault_rows]
    except Exception:
        export["vault_key_names"] = []

    return export


def import_data(conn: sqlite3.Connection, data: dict) -> dict:
    """
    Import data from an export dict.
    Skips tables not present. Returns summary of imported counts.
    """
    summary = {}
    tables = data.get("tables", {})

    _INSERT_QUERIES = {
        "semantic_memory": """
            INSERT OR IGNORE INTO semantic_memory
            (id, fact, confidence, source, tags, created_at, last_accessed, access_count)
            VALUES (:id, :fact, :confidence, :source, :tags, :created_at, :last_accessed, :access_count)
        """,
        "episodic_memory": """
            INSERT OR IGNORE INTO episodic_memory
            (id, interaction, reflection, importance_score, agent, tags, created_at)
            VALUES (:id, :interaction, :reflection, :importance_score, :agent, :tags, :created_at)
        """,
        "key_value_store": """
            INSERT OR IGNORE INTO key_value_store (key, value, updated_at)
            VALUES (:key, :value, :updated_at)
        """,
        "scheduled_tasks": """
            INSERT OR IGNORE INTO scheduled_tasks
            (task_id, name, cron_expression, handler, payload, enabled, last_run, next_run, run_count, last_result, created_at)
            VALUES (:task_id, :name, :cron_expression, :handler, :payload, :enabled, :last_run, :next_run, :run_count, :last_result, :created_at)
        """,
    }

    for table_name, query in _INSERT_QUERIES.items():
        rows = tables.get(table_name, [])
        if not isinstance(rows, list):
            continue
        count = 0
        for row in rows:
            try:
                conn.execute(query, row)
                count += 1
            except Exception:
                pass
        summary[table_name] = count

    conn.commit()
    return summary

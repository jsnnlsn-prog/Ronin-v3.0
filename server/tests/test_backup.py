"""
Tests for RONIN Backup/Restore + Export/Import.
Run: pytest tests/test_backup.py -v
"""
import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backup import (
    backup_database, list_backups, restore_database,
    validate_backup, export_data, import_data,
)
from ronin_mcp_server import init_database


@pytest.fixture
def tmp_db(tmp_path):
    """Create a temporary SQLite DB with some test data."""
    db_path = tmp_path / "test_memory.db"
    conn = init_database(db_path)
    # Insert some test data
    conn.execute(
        "INSERT INTO semantic_memory (id, fact, confidence, source, tags, created_at, last_accessed) VALUES (?,?,?,?,?,?,?)",
        ("m1", "Test fact about RONIN", 0.9, "test", "[]", "2025-01-01T00:00:00Z", "2025-01-01T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO key_value_store (key, value, updated_at) VALUES (?,?,?)",
        ("test:key", "test_value", "2025-01-01T00:00:00Z"),
    )
    conn.commit()
    conn.close()
    return db_path


# ─── Backup ───────────────────────────────────────────────────────────────

def test_backup_creates_file(tmp_db, tmp_path):
    backup_path = backup_database(tmp_db, backup_dir=tmp_path / "backups")
    assert backup_path.exists()
    assert backup_path.suffix == ".db"
    assert "memory_" in backup_path.name


def test_backup_is_valid_sqlite(tmp_db, tmp_path):
    backup_path = backup_database(tmp_db, backup_dir=tmp_path / "backups")
    conn = sqlite3.connect(str(backup_path))
    result = conn.execute("PRAGMA integrity_check").fetchone()
    conn.close()
    assert result[0] == "ok"


def test_backup_contains_data(tmp_db, tmp_path):
    backup_path = backup_database(tmp_db, backup_dir=tmp_path / "backups")
    conn = sqlite3.connect(str(backup_path))
    row = conn.execute("SELECT fact FROM semantic_memory WHERE id='m1'").fetchone()
    conn.close()
    assert row is not None
    assert "RONIN" in row[0]


def test_list_backups(tmp_db, tmp_path):
    backup_dir = tmp_path / "backups"
    backup_database(tmp_db, backup_dir=backup_dir)
    import time; time.sleep(1.1)  # Ensure different timestamps
    backup_database(tmp_db, backup_dir=backup_dir)
    backups = list_backups(backup_dir)
    assert len(backups) >= 2
    for b in backups:
        assert "filename" in b
        assert "size_mb" in b
        assert "created_at" in b


def test_backup_retention(tmp_db, tmp_path):
    backup_dir = tmp_path / "backups"
    for _ in range(5):
        backup_database(tmp_db, backup_dir=backup_dir, retention=3)
    backups = list_backups(backup_dir)
    assert len(backups) <= 3


def test_list_backups_empty_dir(tmp_path):
    backups = list_backups(tmp_path / "nonexistent")
    assert backups == []


# ─── Validate Backup ─────────────────────────────────────────────────────

def test_validate_backup_valid(tmp_db, tmp_path):
    backup_path = backup_database(tmp_db, backup_dir=tmp_path / "backups")
    assert validate_backup(backup_path) is True


def test_validate_backup_corrupt(tmp_path):
    bad_file = tmp_path / "corrupt.db"
    bad_file.write_bytes(b"not a sqlite file at all")
    assert validate_backup(bad_file) is False


# ─── Restore ─────────────────────────────────────────────────────────────

def test_restore_replaces_db(tmp_db, tmp_path):
    backup_dir = tmp_path / "backups"
    backup_path = backup_database(tmp_db, backup_dir=backup_dir)

    # Wipe the original
    conn = sqlite3.connect(str(tmp_db))
    conn.execute("DELETE FROM semantic_memory")
    conn.commit()
    conn.close()

    # Restore
    restore_database(backup_path, tmp_db)

    conn = sqlite3.connect(str(tmp_db))
    row = conn.execute("SELECT fact FROM semantic_memory WHERE id='m1'").fetchone()
    conn.close()
    assert row is not None


def test_restore_missing_file_raises(tmp_path, tmp_db):
    with pytest.raises(FileNotFoundError):
        restore_database(tmp_path / "nonexistent.db", tmp_db)


def test_restore_corrupt_file_raises(tmp_path, tmp_db):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"garbage")
    with pytest.raises(ValueError):
        restore_database(bad, tmp_db)


# ─── Export / Import ─────────────────────────────────────────────────────

def test_export_data(tmp_db):
    conn = sqlite3.connect(str(tmp_db))
    conn.row_factory = sqlite3.Row
    data = export_data(conn)
    conn.close()

    assert "exported_at" in data
    assert "tables" in data
    assert "semantic_memory" in data["tables"]
    assert len(data["tables"]["semantic_memory"]) >= 1
    assert data["tables"]["semantic_memory"][0]["id"] == "m1"


def test_export_vault_keys_only(tmp_db):
    conn = sqlite3.connect(str(tmp_db))
    conn.row_factory = sqlite3.Row
    # Add a vault entry
    conn.execute(
        "INSERT INTO vault (key_name, encrypted_value, created_at, updated_at) VALUES (?,?,?,?)",
        ("SECRET_KEY", "encryptedvalue", "2025-01-01Z", "2025-01-01Z"),
    )
    conn.commit()
    data = export_data(conn)
    conn.close()

    assert "vault_key_names" in data
    assert "SECRET_KEY" in data["vault_key_names"]
    # Encrypted values not in export
    for table in data.get("tables", {}).values():
        if isinstance(table, list):
            for row in table:
                assert "encrypted_value" not in row


def test_import_data(tmp_path):
    target_db = tmp_path / "import_target.db"
    conn = init_database(target_db)

    export = {
        "version": "3.0",
        "exported_at": "2025-01-01Z",
        "tables": {
            "semantic_memory": [
                {
                    "id": "imported_1",
                    "fact": "Imported fact",
                    "confidence": 0.8,
                    "source": "import",
                    "tags": "[]",
                    "created_at": "2025-01-01Z",
                    "last_accessed": "2025-01-01Z",
                    "access_count": 0,
                }
            ],
            "key_value_store": [
                {"key": "imported:key", "value": "imported_val", "updated_at": "2025-01-01Z"}
            ],
        },
    }

    summary = import_data(conn, export)
    conn.commit()

    assert summary["semantic_memory"] >= 1
    row = conn.execute("SELECT fact FROM semantic_memory WHERE id='imported_1'").fetchone()
    conn.close()
    assert row is not None


def test_import_skips_duplicates(tmp_db):
    conn = sqlite3.connect(str(tmp_db))
    conn.row_factory = sqlite3.Row

    # Try to import the same record twice
    export = {
        "tables": {
            "semantic_memory": [
                {
                    "id": "m1",  # Already exists
                    "fact": "Should not overwrite",
                    "confidence": 0.1,
                    "source": "dup",
                    "tags": "[]",
                    "created_at": "2020-01-01Z",
                    "last_accessed": "2020-01-01Z",
                    "access_count": 0,
                }
            ]
        }
    }
    import_data(conn, export)
    row = conn.execute("SELECT fact FROM semantic_memory WHERE id='m1'").fetchone()
    conn.close()
    # Original fact should be preserved (INSERT OR IGNORE)
    assert "RONIN" in row[0]


# ─── Backup API Endpoints ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_backup_api_list():
    from resilience import set_test_mode
    set_test_mode(True)
    from httpx import ASGITransport, AsyncClient
    from api import app
    import uuid

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        u = f"ba_{uuid.uuid4().hex[:8]}"
        await client.post("/api/auth/register", json={"username": u, "password": "bp", "is_admin": True})
        r = await client.post("/api/auth/login", json={"username": u, "password": "bp"})
        assert r.status_code == 200
        token = r.json()["access_token"]
        r = await client.get("/api/backups", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert "backups" in r.json()


@pytest.mark.asyncio
async def test_backup_api_create():
    from resilience import set_test_mode
    set_test_mode(True)
    from httpx import ASGITransport, AsyncClient
    from api import app
    import uuid

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        u = f"ba2_{uuid.uuid4().hex[:8]}"
        await client.post("/api/auth/register", json={"username": u, "password": "bp2", "is_admin": True})
        r = await client.post("/api/auth/login", json={"username": u, "password": "bp2"})
        assert r.status_code == 200
        token = r.json()["access_token"]
        r = await client.post("/api/backups", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        data = r.json()
        assert "filename" in data
        assert "size_mb" in data

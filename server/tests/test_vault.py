"""
Tests for RONIN Vault — encrypted API key storage.
Run: pytest tests/test_vault.py -v
"""
import sqlite3
import pytest

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vault import VaultStore, init_vault_table, import_env_to_vault
from cryptography.fernet import Fernet


@pytest.fixture
def db():
    """In-memory SQLite DB for each test."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_vault_table(conn)
    return conn


@pytest.fixture
def vault(db):
    key = Fernet.generate_key()
    return VaultStore(db, master_key=key)


# ─── Basic Operations ─────────────────────────────────────────────────────

def test_set_and_get(vault):
    vault.set("MY_KEY", "my_secret_value")
    assert vault.get("MY_KEY") == "my_secret_value"


def test_get_missing_returns_none(vault):
    assert vault.get("NONEXISTENT") is None


def test_overwrite_updates_value(vault):
    vault.set("KEY", "old")
    vault.set("KEY", "new")
    assert vault.get("KEY") == "new"


def test_delete_existing(vault):
    vault.set("DEL_ME", "value")
    assert vault.delete("DEL_ME") is True
    assert vault.get("DEL_ME") is None


def test_delete_missing_returns_false(vault):
    assert vault.delete("NOPE") is False


def test_list_keys(vault):
    vault.set("KEY_A", "val_a")
    vault.set("KEY_B", "val_b")
    keys = vault.list_keys()
    names = [k["name"] for k in keys]
    assert "KEY_A" in names
    assert "KEY_B" in names
    # Values not included
    for k in keys:
        assert "value" not in k


def test_values_are_encrypted_in_db(vault, db):
    vault.set("SECRET", "plaintext_value")
    row = db.execute("SELECT encrypted_value FROM vault WHERE key_name='SECRET'").fetchone()
    assert row is not None
    # Stored value is NOT the plaintext
    assert row[0] != "plaintext_value"
    # Cannot be read without the key
    assert b"plaintext" not in row[0].encode()


def test_get_or_env_prefers_vault(vault, monkeypatch):
    monkeypatch.setenv("MY_API_KEY", "env_value")
    vault.set("MY_API_KEY", "vault_value")
    assert vault.get_or_env("MY_API_KEY") == "vault_value"


def test_get_or_env_falls_back(vault, monkeypatch):
    monkeypatch.setenv("ONLY_IN_ENV", "env_value")
    assert vault.get_or_env("ONLY_IN_ENV") == "env_value"


def test_get_or_env_returns_none_if_missing(vault, monkeypatch):
    monkeypatch.delenv("TOTALLY_MISSING", raising=False)
    assert vault.get_or_env("TOTALLY_MISSING") is None


# ─── Import from Env ──────────────────────────────────────────────────────

def test_import_env_to_vault(db, monkeypatch):
    key = Fernet.generate_key()
    v = VaultStore(db, master_key=key)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-123")
    imported = import_env_to_vault(v)
    assert "ANTHROPIC_API_KEY" in imported
    assert v.get("ANTHROPIC_API_KEY") == "sk-ant-test-123"


def test_import_skips_existing(db, monkeypatch):
    key = Fernet.generate_key()
    v = VaultStore(db, master_key=key)
    v.set("ANTHROPIC_API_KEY", "existing_value")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "new_value")
    imported = import_env_to_vault(v)
    # Should not reimport an already-present key
    assert "ANTHROPIC_API_KEY" not in imported
    assert v.get("ANTHROPIC_API_KEY") == "existing_value"


# ─── Vault API Endpoints ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_vault_api_list_keys():
    from resilience import set_test_mode
    set_test_mode(True)
    from httpx import ASGITransport, AsyncClient
    from api import app
    import uuid

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _get_admin_token_for_api(client)
        r = await client.get("/api/vault/keys", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert "keys" in r.json()


@pytest.mark.asyncio
async def test_vault_api_set_and_get():
    from resilience import set_test_mode
    set_test_mode(True)
    from httpx import ASGITransport, AsyncClient
    from api import app
    import uuid

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _get_admin_token_for_api(client)
        headers = {"Authorization": f"Bearer {token}"}

        key_name = f"TEST_{uuid.uuid4().hex[:6].upper()}"
        r = await client.put(f"/api/vault/{key_name}", json={"value": "testval"}, headers=headers)
        assert r.status_code == 200

        r = await client.get(f"/api/vault/{key_name}", headers=headers)
        assert r.status_code == 200
        assert r.json()["value"] == "testval"

        r = await client.delete(f"/api/vault/{key_name}", headers=headers)
        assert r.status_code == 200
        assert r.json()["deleted"] is True


# ─── Shared admin fixture helper for vault API tests ──────────────────────

async def _get_admin_token_for_api(client):
    """Create a fresh admin via API and return token."""
    import uuid
    u = f"va_{uuid.uuid4().hex[:8]}"
    await client.post("/api/auth/register", json={"username": u, "password": "vp", "is_admin": True})
    r = await client.post("/api/auth/login", json={"username": u, "password": "vp"})
    return r.json()["access_token"]

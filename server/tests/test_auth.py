"""
Tests for RONIN Phase 5 Authentication system.
Run: pytest tests/test_auth.py -v
"""
import uuid
import pytest
from httpx import ASGITransport, AsyncClient

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from resilience import set_test_mode
set_test_mode(True)  # Disable rate limiting for tests

import api as _api
import ronin_mcp_server


@pytest.fixture
async def client(tmp_path, monkeypatch):
    """Each test gets its own isolated DB to avoid cross-test lock contention."""
    db_path = tmp_path / "memory.db"
    # Patch MEMORY_DB in BOTH modules so lifespan, endpoints, and auth all use same temp file
    monkeypatch.setattr(_api, "MEMORY_DB", db_path)
    monkeypatch.setattr(ronin_mcp_server, "MEMORY_DB", db_path)

    from api import app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def admin_creds(client):
    """Create a fresh admin user for this test."""
    username = f"admin_{uuid.uuid4().hex[:8]}"
    password = "testadminpass"
    r = await client.post("/api/auth/register", json={
        "username": username, "password": password, "is_admin": True
    })
    assert r.status_code == 200, r.text
    return {"username": username, "password": password}


@pytest.fixture
async def admin_token(client, admin_creds):
    r = await client.post("/api/auth/login", json=admin_creds)
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


# ─── Registration ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_register_user(client):
    username = f"testuser_{uuid.uuid4().hex[:6]}"
    r = await client.post("/api/auth/register", json={
        "username": username, "password": "testpass123", "is_admin": False,
    })
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["username"] == username
    assert "id" in data
    assert data["is_admin"] is False


@pytest.mark.asyncio
async def test_register_duplicate_fails(client):
    username = f"dupuser_{uuid.uuid4().hex[:6]}"
    await client.post("/api/auth/register", json={
        "username": username, "password": "pass1", "is_admin": False,
    })
    r = await client.post("/api/auth/register", json={
        "username": username, "password": "pass2", "is_admin": False,
    })
    assert r.status_code == 400
    assert "already exists" in r.json()["detail"]


# ─── Login ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_login_success(client, admin_creds):
    r = await client.post("/api/auth/login", json=admin_creds)
    assert r.status_code == 200, r.text
    data = r.json()
    assert "access_token" in data
    assert "refresh_token" in data
    assert data["token_type"] == "bearer"


@pytest.mark.asyncio
async def test_login_wrong_password(client, admin_creds):
    r = await client.post("/api/auth/login", json={
        "username": admin_creds["username"], "password": "wrongpass",
    })
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_login_unknown_user(client):
    r = await client.post("/api/auth/login", json={
        "username": "nobody", "password": "pass",
    })
    assert r.status_code == 401


# ─── Token Refresh ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_refresh_token(client, admin_creds):
    login_r = await client.post("/api/auth/login", json=admin_creds)
    refresh_token = login_r.json()["refresh_token"]
    r = await client.post("/api/auth/refresh", json={"refresh_token": refresh_token})
    assert r.status_code == 200
    assert "access_token" in r.json()


@pytest.mark.asyncio
async def test_refresh_with_access_token_fails(client, admin_token):
    r = await client.post("/api/auth/refresh", json={"refresh_token": admin_token})
    assert r.status_code == 401


# ─── Protected Endpoints ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_me_authenticated(client, admin_token, admin_creds):
    r = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["username"] == admin_creds["username"]
    assert data["is_admin"] is True


@pytest.mark.asyncio
async def test_me_unauthenticated(client):
    r = await client.get("/api/auth/me")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_me_invalid_token(client):
    r = await client.get("/api/auth/me", headers={"Authorization": "Bearer invalid.token.here"})
    assert r.status_code == 401


# ─── Admin Endpoints ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_users_admin(client, admin_token):
    r = await client.get("/api/auth/users", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    assert "users" in r.json()


@pytest.mark.asyncio
async def test_list_users_non_admin(client):
    username = f"plain_{uuid.uuid4().hex[:6]}"
    await client.post("/api/auth/register", json={
        "username": username, "password": "pass123", "is_admin": False,
    })
    login_r = await client.post("/api/auth/login", json={
        "username": username, "password": "pass123",
    })
    token = login_r.json()["access_token"]
    r = await client.get("/api/auth/users", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


# ─── Edge Cases ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_multiple_users_isolated(client):
    """Verify multiple users can register in the same test session."""
    for i in range(3):
        username = f"user{i}_{uuid.uuid4().hex[:4]}"
        r = await client.post("/api/auth/register", json={
            "username": username, "password": f"pass{i}123", "is_admin": False,
        })
        assert r.status_code == 200, f"User {i} registration failed: {r.text}"

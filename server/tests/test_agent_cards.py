"""
Tests for RONIN Agent Cards & Registry.

Run: pytest tests/test_agent_cards.py -v
"""
import json
import pytest
from httpx import ASGITransport, AsyncClient

from api import app


@pytest.fixture
async def client():
    """Async test client — no real server needed."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ─── Well-Known Discovery ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_well_known_agent_json(client):
    r = await client.get("/.well-known/agent.json")
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "ronin"
    assert "3.0.0" in data["version"]
    assert len(data["skills"]) > 0
    assert "agents" in data["metadata"]
    # Should list all 6 internal agents
    assert len(data["metadata"]["agents"]) >= 6


# ─── List Agents ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_agents(client):
    r = await client.get("/api/agents")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] >= 6  # 6 internal agents
    names = [a["name"] for a in data["agents"]]
    assert "cortex" in names
    assert "scout" in names
    assert "forge" in names
    assert "prism" in names
    assert "echo" in names
    assert "aegis" in names


# ─── Get Specific Agent ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_agent_cortex(client):
    r = await client.get("/api/agents/cortex")
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "cortex"
    assert data["url"] == "internal://cortex"
    assert len(data["skills"]) == 3
    skill_ids = [s["id"] for s in data["skills"]]
    assert "task_decomposition" in skill_ids
    assert "agent_delegation" in skill_ids


@pytest.mark.asyncio
async def test_get_agent_forge(client):
    r = await client.get("/api/agents/forge")
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "forge"
    skill_ids = [s["id"] for s in data["skills"]]
    assert "code_exec" in skill_ids
    assert "shell_exec" in skill_ids
    assert "file_write" in skill_ids


@pytest.mark.asyncio
async def test_get_agent_not_found(client):
    r = await client.get("/api/agents/nonexistent")
    assert r.status_code == 404


# ─── Register External Agent ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_register_external_agent(client):
    r = await client.post("/api/agents", json={
        "name": "test_external",
        "description": "A test external agent",
        "url": "http://external-agent:9000",
        "version": "1.0.0",
        "skills": [
            {"id": "translate", "name": "Translation", "description": "Translate text between languages"},
        ],
        "metadata": {"provider": "test"},
    })
    assert r.status_code == 200
    data = r.json()
    assert data["registered"] is True
    assert data["agent"]["name"] == "test_external"
    assert data["agent"]["url"] == "http://external-agent:9000"

    # Verify it appears in list
    r = await client.get("/api/agents")
    names = [a["name"] for a in r.json()["agents"]]
    assert "test_external" in names

    # Clean up
    await client.delete("/api/agents/test_external")


# ─── Unregister Agent ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unregister_external_agent(client):
    # Register first
    await client.post("/api/agents", json={
        "name": "temp_agent",
        "url": "http://temp:8000",
    })

    # Unregister
    r = await client.delete("/api/agents/temp_agent")
    assert r.status_code == 200
    assert r.json()["unregistered"] is True

    # Verify removed
    r = await client.get("/api/agents/temp_agent")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_cannot_unregister_internal_agent(client):
    r = await client.delete("/api/agents/cortex")
    assert r.status_code == 403


# ─── Agent Health ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_agent_health(client):
    r = await client.get("/api/agents/cortex/health")
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "cortex"
    assert data["status"] == "online"
    assert data["is_internal"] is True


@pytest.mark.asyncio
async def test_health_check_endpoint(client):
    r = await client.post("/api/agents/health-check")
    assert r.status_code == 200
    data = r.json()
    assert "cortex" in data["results"]
    assert data["results"]["cortex"] == "online"


# ─── Capability Matching ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_match_code_task(client):
    r = await client.post("/api/agents/match", json={
        "task_description": "Execute a Python script to process data",
        "required_skills": ["code_exec"],
        "exclude_agents": ["cortex"],
    })
    assert r.status_code == 200
    matches = r.json()["matches"]
    assert len(matches) > 0
    # Forge should be the top match for code execution
    assert matches[0]["agent"]["name"] == "forge"


@pytest.mark.asyncio
async def test_match_safety_task(client):
    r = await client.post("/api/agents/match", json={
        "task_description": "Check if this action is safe to execute",
        "required_skills": ["safety_check"],
    })
    assert r.status_code == 200
    matches = r.json()["matches"]
    agent_names = [m["agent"]["name"] for m in matches]
    assert "aegis" in agent_names


@pytest.mark.asyncio
async def test_match_with_keyword_only(client):
    r = await client.post("/api/agents/match", json={
        "task_description": "fetch web content and extract information from a URL",
    })
    assert r.status_code == 200
    matches = r.json()["matches"]
    assert len(matches) > 0
    # Scout should rank high for web fetch tasks
    top_names = [m["agent"]["name"] for m in matches[:3]]
    assert "scout" in top_names


# ─── Health endpoint includes agents ─────────────────────────────────────

@pytest.mark.asyncio
async def test_health_includes_agents(client):
    r = await client.get("/api/health")
    assert r.status_code == 200
    data = r.json()
    assert "agents" in data
    assert data["agents"] >= 6


# ─── Internal agent card properties ──────────────────────────────────────

@pytest.mark.asyncio
async def test_internal_agents_have_metadata(client):
    """All internal agents should have icon, color, and role in metadata."""
    r = await client.get("/api/agents")
    for agent in r.json()["agents"]:
        if agent["url"].startswith("internal://"):
            assert "icon" in agent["metadata"], f"{agent['name']} missing icon"
            assert "color" in agent["metadata"], f"{agent['name']} missing color"
            assert "role" in agent["metadata"], f"{agent['name']} missing role"

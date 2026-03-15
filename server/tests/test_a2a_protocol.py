"""
Tests for RONIN A2A Protocol — message routing & task lifecycle.

Run: pytest tests/test_a2a_protocol.py -v
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


# ─── Task Creation & Lifecycle ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_task_to_internal_agent(client):
    """Sending a task to an internal agent should succeed."""
    r = await client.post("/a2a/tasks/send", json={
        "from_agent": "cortex",
        "to_agent": "forge",
        "content": "List all files in the workspace",
    })
    assert r.status_code == 200
    data = r.json()
    assert "task_id" in data
    assert data["from_agent"] == "cortex"
    assert data["to_agent"] == "forge"
    # Task should complete (internal routing is synchronous)
    assert data["status"] in ("completed", "failed")
    assert len(data["messages"]) >= 1


@pytest.mark.asyncio
async def test_create_task_to_reasoning_agent(client):
    """Tasks to reasoning-only agents (echo, prism) return delegation ack."""
    r = await client.post("/a2a/tasks/send", json={
        "from_agent": "cortex",
        "to_agent": "echo",
        "content": "Summarize the latest project status",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "completed"
    # Should have an artifact with delegation info
    assert len(data["artifacts"]) > 0
    artifact = data["artifacts"][0]["data"]
    assert artifact["agent"] == "echo"
    assert artifact["requires_llm"] is True


@pytest.mark.asyncio
async def test_create_task_with_tool_params(client):
    """Tasks with tool_params metadata should execute the mapped tool."""
    r = await client.post("/a2a/tasks/send", json={
        "from_agent": "cortex",
        "to_agent": "aegis",
        "content": "Check if this action is safe",
        "metadata": {
            "tool_params": {
                "action_description": "Read a configuration file",
                "risk_level": "low",
            }
        },
    })
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "completed"
    # Aegis should have executed safety_check tool
    assert len(data["artifacts"]) > 0
    result = data["artifacts"][0]["data"]
    assert result.get("decision") == "APPROVED"


@pytest.mark.asyncio
async def test_get_task(client):
    """Can retrieve a task by ID after creation."""
    # Create task
    r = await client.post("/a2a/tasks/send", json={
        "from_agent": "cortex",
        "to_agent": "echo",
        "content": "Generate a report",
    })
    task_id = r.json()["task_id"]

    # Retrieve
    r = await client.get(f"/a2a/tasks/{task_id}")
    assert r.status_code == 200
    data = r.json()
    assert data["task_id"] == task_id
    assert data["from_agent"] == "cortex"
    assert data["to_agent"] == "echo"


@pytest.mark.asyncio
async def test_get_task_not_found(client):
    r = await client.get("/a2a/tasks/nonexistent-id")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_cancel_task(client):
    """Can cancel a task (though internal tasks complete immediately, test the path)."""
    # Create task
    r = await client.post("/a2a/tasks/send", json={
        "from_agent": "cortex",
        "to_agent": "echo",
        "content": "Long running analysis",
    })
    task_id = r.json()["task_id"]

    # Cancel (may already be completed for internal agents)
    r = await client.post(f"/a2a/tasks/{task_id}/cancel")
    assert r.status_code == 200
    data = r.json()
    # Status should be either cancelled (if was pending) or completed (if already done)
    assert data["status"] in ("cancelled", "completed", "failed")


@pytest.mark.asyncio
async def test_cancel_task_not_found(client):
    r = await client.post("/a2a/tasks/nonexistent-id/cancel")
    assert r.status_code == 404


# ─── Task Listing ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_tasks(client):
    """List all tasks."""
    # Create a few tasks
    await client.post("/a2a/tasks/send", json={
        "from_agent": "cortex", "to_agent": "echo", "content": "Task 1",
    })
    await client.post("/a2a/tasks/send", json={
        "from_agent": "cortex", "to_agent": "prism", "content": "Task 2",
    })

    r = await client.get("/a2a/tasks")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] >= 2
    assert len(data["tasks"]) >= 2


@pytest.mark.asyncio
async def test_list_tasks_filter_by_status(client):
    """Filter tasks by status."""
    # Create a task (will be completed for internal agents)
    await client.post("/a2a/tasks/send", json={
        "from_agent": "cortex", "to_agent": "echo", "content": "Filtered task",
    })

    r = await client.get("/a2a/tasks?status=completed")
    assert r.status_code == 200
    for task in r.json()["tasks"]:
        assert task["status"] == "completed"


# ─── External Agent Routing ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_task_to_nonexistent_agent(client):
    """Task to unknown agent should fail gracefully."""
    r = await client.post("/a2a/tasks/send", json={
        "from_agent": "cortex",
        "to_agent": "ghost_agent",
        "content": "This should fail",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "failed"


@pytest.mark.asyncio
async def test_task_to_external_agent_no_server(client):
    """Task to external agent that's unreachable should fail but not crash."""
    # Register an external agent pointing to nowhere
    await client.post("/api/agents", json={
        "name": "offline_ext",
        "url": "http://127.0.0.1:19999",
        "skills": [{"id": "noop", "name": "No-Op"}],
    })

    r = await client.post("/a2a/tasks/send", json={
        "from_agent": "cortex",
        "to_agent": "offline_ext",
        "content": "Ping",
    })
    assert r.status_code == 200
    data = r.json()
    # Should complete with error artifact, not crash
    assert data["status"] in ("completed", "failed")

    # Clean up
    await client.delete("/api/agents/offline_ext")


# ─── Message Structure ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_task_has_proper_message_structure(client):
    """Task messages should have required A2A fields."""
    r = await client.post("/a2a/tasks/send", json={
        "from_agent": "cortex",
        "to_agent": "forge",
        "content": "Check workspace",
    })
    data = r.json()
    msg = data["messages"][0]
    assert "message_id" in msg
    assert "task_id" in msg
    assert msg["from_agent"] == "cortex"
    assert msg["to_agent"] == "forge"
    assert msg["type"] == "task_request"
    assert len(msg["content"]) > 0
    assert msg["content"][0]["type"] == "text"
    assert "created_at" in msg


@pytest.mark.asyncio
async def test_completed_task_has_response_message(client):
    """Completed tasks should have a response message from the target agent."""
    r = await client.post("/a2a/tasks/send", json={
        "from_agent": "cortex",
        "to_agent": "echo",
        "content": "Acknowledge this",
    })
    data = r.json()
    assert data["status"] == "completed"
    assert len(data["messages"]) >= 2
    response_msg = data["messages"][1]
    assert response_msg["from_agent"] == "echo"
    assert response_msg["to_agent"] == "cortex"
    assert response_msg["type"] == "task_response"

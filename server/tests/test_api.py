"""
Tests for RONIN REST API (FastAPI wrapper around MCP tools).

Run: pytest tests/test_api.py -v
"""
import json
import pytest
from httpx import ASGITransport, AsyncClient

# Import the FastAPI app
from api import app


@pytest.fixture
async def client():
    """Async test client — no real server needed."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ─── Health & Discovery ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health(client):
    r = await client.get("/api/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "operational"
    assert data["tools"] > 0
    assert "memory" in data


@pytest.mark.asyncio
async def test_list_tools(client):
    r = await client.get("/api/tools")
    assert r.status_code == 200
    data = r.json()
    assert "tools" in data
    names = [t["name"] for t in data["tools"]]
    assert "ronin_shell_exec" in names
    assert "ronin_file_write" in names
    assert "ronin_memory_store" in names
    assert "ronin_safety_check" in names
    # Each tool should have input_schema
    for t in data["tools"]:
        assert "input_schema" in t
        assert t["input_schema"]["type"] == "object"


# ─── Tool Execution ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_shell_exec(client):
    r = await client.post("/api/tools/ronin_shell_exec", json={
        "input": {"command": "echo hello world"}
    })
    assert r.status_code == 200
    data = r.json()
    assert data["success"] is True
    assert "hello world" in data["result"]["stdout"]
    assert data["execution_ms"] >= 0


@pytest.mark.asyncio
async def test_shell_exec_blocked(client):
    r = await client.post("/api/tools/ronin_shell_exec", json={
        "input": {"command": "rm -rf /"}
    })
    assert r.status_code == 200
    data = r.json()
    assert "BLOCKED" in str(data["result"])


@pytest.mark.asyncio
async def test_file_write_read(client):
    # Write
    r = await client.post("/api/tools/ronin_file_write", json={
        "input": {"path": "test_api_file.txt", "content": "hello from test"}
    })
    assert r.status_code == 200
    assert r.json()["success"] is True

    # Read back
    r = await client.post("/api/tools/ronin_file_read", json={
        "input": {"path": "test_api_file.txt"}
    })
    assert r.status_code == 200
    assert "hello from test" in r.json()["result"]["content"]


@pytest.mark.asyncio
async def test_file_list(client):
    # Write a file first
    await client.post("/api/tools/ronin_file_write", json={
        "input": {"path": "list_test.txt", "content": "x"}
    })
    r = await client.post("/api/tools/ronin_file_list", json={
        "input": {"directory": "."}
    })
    assert r.status_code == 200
    data = r.json()
    assert data["result"]["count"] >= 1


@pytest.mark.asyncio
async def test_code_exec_python(client):
    r = await client.post("/api/tools/ronin_code_exec", json={
        "input": {"language": "python", "code": "print(2 + 2)"}
    })
    assert r.status_code == 200
    data = r.json()
    assert "4" in data["result"]["stdout"]


@pytest.mark.asyncio
async def test_memory_store_and_query(client):
    # Store
    r = await client.post("/api/tools/ronin_memory_store", json={
        "input": {
            "fact": "The API test suite verifies all 13 tools",
            "confidence": 0.95,
            "tags": ["testing", "api"]
        }
    })
    assert r.status_code == 200
    assert r.json()["result"]["success"] is True

    # Query
    r = await client.post("/api/tools/ronin_memory_query", json={
        "input": {"query": "API test suite tools"}
    })
    assert r.status_code == 200
    results = r.json()["result"]["results"]
    assert len(results) > 0
    assert any("API test suite" in m["fact"] for m in results)


@pytest.mark.asyncio
async def test_kv_store_and_get(client):
    r = await client.post("/api/tools/ronin_kv_set", json={
        "input": {"key": "test_key", "value": json.dumps({"answer": 42})}
    })
    assert r.status_code == 200
    assert r.json()["result"]["success"] is True

    r = await client.post("/api/tools/ronin_kv_get", json={
        "input": {"key": "test_key"}
    })
    assert r.status_code == 200
    val = json.loads(r.json()["result"]["value"])
    assert val["answer"] == 42


@pytest.mark.asyncio
async def test_safety_check(client):
    # Low risk — should approve
    r = await client.post("/api/tools/ronin_safety_check", json={
        "input": {"action_description": "Read a log file", "risk_level": "low"}
    })
    assert r.status_code == 200
    assert r.json()["result"]["decision"] == "APPROVED"

    # Critical — should deny
    r = await client.post("/api/tools/ronin_safety_check", json={
        "input": {"action_description": "Delete all production data", "risk_level": "critical"}
    })
    assert r.status_code == 200
    assert r.json()["result"]["decision"] == "DENIED"


@pytest.mark.asyncio
async def test_system_info(client):
    r = await client.post("/api/tools/ronin_system_info", json={
        "input": {"component": "overview"}
    })
    assert r.status_code == 200
    data = r.json()["result"]
    assert data["status"] == "operational"


@pytest.mark.asyncio
async def test_episodic_store(client):
    r = await client.post("/api/tools/ronin_episodic_store", json={
        "input": {
            "interaction": "User asked about API architecture",
            "reflection": "FastAPI wrapper provides clean REST interface",
            "importance": 0.7
        }
    })
    assert r.status_code == 200
    assert r.json()["result"]["success"] is True


# ─── Error Handling ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unknown_tool(client):
    r = await client.post("/api/tools/ronin_nonexistent", json={
        "input": {"foo": "bar"}
    })
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_validation_error(client):
    r = await client.post("/api/tools/ronin_shell_exec", json={
        "input": {"wrong_field": "bad"}
    })
    assert r.status_code == 422


# ─── Batch Execution ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_batch_execute(client):
    r = await client.post("/api/batch", json={
        "calls": [
            {"name": "ronin_system_info", "input": {"component": "overview"}},
            {"name": "ronin_safety_check", "input": {"action_description": "test", "risk_level": "low"}},
        ]
    })
    assert r.status_code == 200
    data = r.json()
    assert len(data["results"]) == 2
    assert data["results"][0]["success"] is True
    assert data["results"][1]["success"] is True
    assert data["total_ms"] >= 0


# ─── Memory Endpoints ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_semantic_memory_list(client):
    # Store first
    await client.post("/api/tools/ronin_memory_store", json={
        "input": {"fact": "Test memory for listing", "confidence": 0.8}
    })
    r = await client.get("/api/memory/semantic")
    assert r.status_code == 200
    assert r.json()["count"] >= 1


@pytest.mark.asyncio
async def test_episodic_memory_list(client):
    r = await client.get("/api/memory/episodic")
    assert r.status_code == 200
    assert "episodes" in r.json()


# ─── Conversations ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_conversation_crud(client):
    # Create
    r = await client.post("/api/conversations", json={
        "messages": [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"}
        ],
        "metadata": {"title": "Test conversation"}
    })
    assert r.status_code == 200
    conv_id = r.json()["id"]

    # List
    r = await client.get("/api/conversations")
    assert r.status_code == 200
    assert r.json()["count"] >= 1

    # Get
    r = await client.get(f"/api/conversations/{conv_id}")
    assert r.status_code == 200
    assert len(r.json()["messages"]) == 2

    # Delete
    r = await client.delete(f"/api/conversations/{conv_id}")
    assert r.status_code == 200
    assert r.json()["deleted"] is True

    # Verify deleted
    r = await client.get(f"/api/conversations/{conv_id}")
    assert r.status_code == 404


# ─── Audit ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_audit_log(client):
    # Execute a tool to generate audit entry
    await client.post("/api/tools/ronin_shell_exec", json={
        "input": {"command": "echo audit test"}
    })
    r = await client.get("/api/audit")
    assert r.status_code == 200
    assert r.json()["count"] >= 1

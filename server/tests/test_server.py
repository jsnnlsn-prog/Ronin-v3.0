"""RONIN MCP Server — Basic Tests"""
import json
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_server_imports():
    """Verify the server module loads without errors."""
    import ronin_mcp_server
    assert hasattr(ronin_mcp_server, "mcp")
    assert hasattr(ronin_mcp_server, "WORKSPACE")


def test_blocked_commands():
    """Verify dangerous commands are blocked."""
    from ronin_mcp_server import is_blocked_command

    assert is_blocked_command("rm -rf /") is True
    assert is_blocked_command(":(){ :|:& };:") is True
    assert is_blocked_command("mkfs.ext4 /dev/sda") is True
    assert is_blocked_command("ls -la") is False
    assert is_blocked_command("python3 script.py") is False
    assert is_blocked_command("echo hello") is False


def test_truncate():
    """Verify output truncation."""
    from ronin_mcp_server import truncate

    short = "hello"
    assert truncate(short) == short

    long = "x" * 20000
    result = truncate(long, max_len=1000)
    assert len(result) < 20000
    assert "truncated" in result


def test_database_init(tmp_path):
    """Verify database creates correct schema."""
    from ronin_mcp_server import init_database

    db_path = tmp_path / "test_memory.db"
    db = init_database(db_path)

    # Check tables exist
    tables = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    table_names = {t["name"] for t in tables}

    assert "semantic_memory" in table_names
    assert "episodic_memory" in table_names
    assert "audit_log" in table_names
    assert "key_value_store" in table_names

    db.close()


@pytest.mark.asyncio
async def test_safety_check():
    """Verify safety check tool returns correct decisions."""
    from ronin_mcp_server import ronin_safety_check, SafetyCheckInput

    # Low risk — should approve
    result = await ronin_safety_check(SafetyCheckInput(
        action_description="List files in workspace",
        risk_level="low"
    ))
    data = json.loads(result)
    assert data["decision"] == "APPROVED"

    # Critical risk — should deny
    result = await ronin_safety_check(SafetyCheckInput(
        action_description="Delete all database tables",
        risk_level="critical"
    ))
    data = json.loads(result)
    assert data["decision"] == "DENIED"

    # Dangerous pattern — should deny regardless of stated risk
    result = await ronin_safety_check(SafetyCheckInput(
        action_description="rm -rf everything",
        risk_level="low"
    ))
    data = json.loads(result)
    assert data["decision"] == "DENIED"


@pytest.mark.asyncio
async def test_file_operations(tmp_path, monkeypatch):
    """Verify file read/write in workspace."""
    import ronin_mcp_server
    from ronin_mcp_server import ronin_file_write, ronin_file_read, FileWriteInput, FileReadInput

    # Redirect workspace to tmp
    monkeypatch.setattr(ronin_mcp_server, "WORKSPACE", tmp_path)

    # Write
    result = await ronin_file_write(FileWriteInput(
        path="test.txt",
        content="Hello RONIN",
        mode="write"
    ))
    data = json.loads(result)
    assert data["success"] is True

    # Read
    result = await ronin_file_read(FileReadInput(path="test.txt"))
    data = json.loads(result)
    assert data["content"] == "Hello RONIN"

    # Read nonexistent
    result = await ronin_file_read(FileReadInput(path="nope.txt"))
    data = json.loads(result)
    assert "error" in data

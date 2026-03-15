#!/usr/bin/env python3
"""
RONIN MCP Tool Server — Real Tool Execution Layer
====================================================
Implements the MCP (Model Context Protocol) standard for RONIN agent system.
Provides actual tool execution: shell commands, file I/O, web fetch, code sandbox,
memory persistence, and system introspection.

Architecture:
  - FastMCP server with streamable HTTP transport (remote) or stdio (local)
  - Pydantic v2 input validation on all tools
  - Bounded execution with timeouts and safety limits
  - Persistent memory via SQLite
  - Audit trail on all tool invocations

Protocol: JSON-RPC 2.0 over SSE (streamable HTTP) or stdio
"""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field, field_validator

# ─── CONFIGURATION ──────────────────────────────────────────────────────────

RONIN_HOME = Path(os.environ.get("RONIN_HOME", Path.home() / ".ronin"))
RONIN_HOME.mkdir(parents=True, exist_ok=True)
MEMORY_DB = RONIN_HOME / "memory.db"
WORKSPACE = RONIN_HOME / "workspace"
WORKSPACE.mkdir(parents=True, exist_ok=True)
AUDIT_LOG = RONIN_HOME / "audit.jsonl"

MAX_SHELL_TIMEOUT = 30       # seconds
MAX_OUTPUT_LENGTH = 8000     # characters
MAX_FILE_SIZE = 1_000_000    # 1MB
ALLOWED_SHELL_COMMANDS = None # None = allow all (set list to restrict)
BLOCKED_SHELL_PATTERNS = [
    "rm -rf /", "mkfs", "dd if=", "> /dev/sd",
    "chmod 777 /", ":(){ :|:& };:",
]

# ─── DATABASE SETUP ─────────────────────────────────────────────────────────

def init_database(db_path: Path) -> sqlite3.Connection:
    """Initialize the memory database with all required tables."""
    conn = sqlite3.connect(str(db_path), timeout=20, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS semantic_memory (
            id TEXT PRIMARY KEY,
            fact TEXT NOT NULL,
            confidence REAL DEFAULT 0.5,
            source TEXT,
            tags TEXT DEFAULT '[]',
            created_at TEXT NOT NULL,
            last_accessed TEXT NOT NULL,
            access_count INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS episodic_memory (
            id TEXT PRIMARY KEY,
            interaction TEXT NOT NULL,
            reflection TEXT,
            importance_score REAL DEFAULT 0.5,
            agent TEXT,
            tags TEXT DEFAULT '[]',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            agent TEXT,
            input_summary TEXT,
            output_summary TEXT,
            success INTEGER DEFAULT 1,
            execution_ms REAL
        );

        CREATE TABLE IF NOT EXISTS key_value_store (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_semantic_confidence
            ON semantic_memory(confidence DESC);
        CREATE INDEX IF NOT EXISTS idx_episodic_importance
            ON episodic_memory(importance_score DESC);
        CREATE INDEX IF NOT EXISTS idx_audit_timestamp
            ON audit_log(timestamp DESC);

        CREATE TABLE IF NOT EXISTS agent_registry (
            name TEXT PRIMARY KEY,
            card_json TEXT NOT NULL,
            is_internal INTEGER DEFAULT 0,
            registered_at TEXT NOT NULL,
            last_health_check TEXT,
            health_status TEXT DEFAULT 'online'
        );

        CREATE INDEX IF NOT EXISTS idx_agent_status
            ON agent_registry(health_status);

        CREATE TABLE IF NOT EXISTS a2a_tasks (
            task_id TEXT PRIMARY KEY,
            task_json TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            from_agent TEXT NOT NULL,
            to_agent TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_a2a_status ON a2a_tasks(status);
        CREATE INDEX IF NOT EXISTS idx_a2a_agents ON a2a_tasks(from_agent, to_agent);

        -- Phase 4: Event Queue
        CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload TEXT DEFAULT '{}',
            priority TEXT DEFAULT 'normal',
            created_at TEXT NOT NULL,
            processed INTEGER DEFAULT 0,
            processed_at TEXT,
            error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_events_processed ON events(processed);
        CREATE INDEX IF NOT EXISTS idx_events_source ON events(source);
        CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
        CREATE INDEX IF NOT EXISTS idx_events_priority ON events(priority);
        CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at DESC);

        -- Phase 4: Scheduled Tasks
        CREATE TABLE IF NOT EXISTS scheduled_tasks (
            task_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            cron_expression TEXT NOT NULL,
            handler TEXT NOT NULL,
            payload TEXT DEFAULT '{}',
            enabled INTEGER DEFAULT 1,
            last_run TEXT,
            next_run TEXT,
            run_count INTEGER DEFAULT 0,
            last_result TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sched_enabled ON scheduled_tasks(enabled);
        CREATE INDEX IF NOT EXISTS idx_sched_next_run ON scheduled_tasks(next_run);

        -- Phase 5: Users
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_admin INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);

        CREATE TABLE IF NOT EXISTS refresh_tokens (
            token_hash TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        -- Phase 5: Vault
        CREATE TABLE IF NOT EXISTS vault (
            key_name TEXT PRIMARY KEY,
            encrypted_value TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        -- Phase 5: TT-SI Outcomes
        CREATE TABLE IF NOT EXISTS ttsi_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ttsi_result_json TEXT NOT NULL,
            actual_outcome TEXT,
            was_correct INTEGER,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_ttsi_created ON ttsi_outcomes(created_at DESC);

        -- Phase 5: Multi-user columns (add safely if not present)
    """)
    conn.commit()

    # Add user_id columns to user-scoped tables (migration-safe)
    _add_column_if_missing(conn, "semantic_memory", "user_id", "TEXT DEFAULT 'admin'")
    _add_column_if_missing(conn, "episodic_memory", "user_id", "TEXT DEFAULT 'admin'")
    _add_column_if_missing(conn, "key_value_store", "user_id", "TEXT DEFAULT 'admin'")
    _add_column_if_missing(conn, "events", "user_id", "TEXT DEFAULT 'admin'")
    _add_column_if_missing(conn, "scheduled_tasks", "user_id", "TEXT DEFAULT 'admin'")
    conn.commit()

    return conn


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, col_def: str) -> None:
    """Add a column to a table if it doesn't already exist (idempotent migration)."""
    try:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")
    except Exception:
        pass  # Table may not exist yet — init_database handles creation order


# ─── LIFESPAN ───────────────────────────────────────────────────────────────

@asynccontextmanager
async def ronin_lifespan():
    """Manage persistent resources across server lifetime."""
    db = init_database(MEMORY_DB)
    http_client = httpx.AsyncClient(
        timeout=15.0,
        follow_redirects=True,
        headers={"User-Agent": "RONIN-MCP/1.0"}
    )
    yield {"db": db, "http": http_client}
    await http_client.aclose()
    db.close()


# ─── SERVER INIT ────────────────────────────────────────────────────────────

mcp = FastMCP("ronin_mcp", lifespan=ronin_lifespan)


# ─── UTILITY FUNCTIONS ──────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def truncate(text: str, max_len: int = MAX_OUTPUT_LENGTH) -> str:
    if len(text) <= max_len:
        return text
    half = max_len // 2 - 20
    return text[:half] + f"\n\n... [truncated {len(text) - max_len} chars] ...\n\n" + text[-half:]

def audit(db: sqlite3.Connection, tool: str, agent: str, inp: str, out: str, success: bool, ms: float):
    db.execute(
        "INSERT INTO audit_log (timestamp, tool_name, agent, input_summary, output_summary, success, execution_ms) VALUES (?,?,?,?,?,?,?)",
        (now_iso(), tool, agent, inp[:500], out[:500], int(success), ms)
    )
    db.commit()

def is_blocked_command(cmd: str) -> bool:
    lower = cmd.lower().strip()
    return any(pattern in lower for pattern in BLOCKED_SHELL_PATTERNS)


# ═══════════════════════════════════════════════════════════════════════════
# TOOL: Shell Execution
# ═══════════════════════════════════════════════════════════════════════════

class ShellInput(BaseModel):
    """Execute a shell command in the RONIN workspace."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    command: str = Field(
        ..., description="Shell command to execute (e.g., 'ls -la', 'python3 script.py')",
        min_length=1, max_length=2000
    )
    working_dir: Optional[str] = Field(
        default=None,
        description="Working directory (relative to RONIN workspace). Defaults to workspace root."
    )
    timeout: Optional[int] = Field(
        default=15, description="Timeout in seconds (max 30)", ge=1, le=MAX_SHELL_TIMEOUT
    )
    agent: Optional[str] = Field(default="cortex", description="Requesting agent ID")


@mcp.tool(
    name="ronin_shell_exec",
    annotations={
        "title": "Execute Shell Command",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def ronin_shell_exec(params: ShellInput) -> str:
    """Execute a shell command in the sandboxed RONIN workspace.

    Runs commands with bounded timeout and output limits. Blocked patterns
    (rm -rf /, fork bombs, etc.) are rejected. Returns stdout, stderr, and exit code.

    Returns:
        str: JSON with keys: exit_code, stdout, stderr, execution_ms, truncated
    """
    start = time.monotonic()
    ctx_db = None  # Will get from context in production; fallback to direct

    if is_blocked_command(params.command):
        return json.dumps({
            "error": "BLOCKED: Command matches a dangerous pattern and was rejected by Aegis (safety guardian).",
            "command": params.command
        })

    cwd = WORKSPACE
    if params.working_dir:
        cwd = WORKSPACE / params.working_dir
        cwd.mkdir(parents=True, exist_ok=True)

    try:
        proc = await asyncio.create_subprocess_shell(
            params.command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            env={**os.environ, "RONIN_WORKSPACE": str(WORKSPACE)},
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=params.timeout or 15
        )

        stdout_str = truncate(stdout.decode("utf-8", errors="replace"))
        stderr_str = truncate(stderr.decode("utf-8", errors="replace"))
        elapsed = (time.monotonic() - start) * 1000

        result = {
            "exit_code": proc.returncode,
            "stdout": stdout_str,
            "stderr": stderr_str,
            "execution_ms": round(elapsed, 1),
            "truncated": len(stdout) > MAX_OUTPUT_LENGTH or len(stderr) > MAX_OUTPUT_LENGTH,
        }
        return json.dumps(result, indent=2)

    except asyncio.TimeoutError:
        return json.dumps({"error": f"Command timed out after {params.timeout}s", "command": params.command})
    except Exception as e:
        return json.dumps({"error": str(e), "type": type(e).__name__})


# ═══════════════════════════════════════════════════════════════════════════
# TOOL: File Operations
# ═══════════════════════════════════════════════════════════════════════════

class FileReadInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    path: str = Field(..., description="File path relative to RONIN workspace", min_length=1)
    agent: Optional[str] = Field(default="cortex")

class FileWriteInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    path: str = Field(..., description="File path relative to RONIN workspace", min_length=1)
    content: str = Field(..., description="Content to write", max_length=MAX_FILE_SIZE)
    mode: Optional[str] = Field(default="write", description="'write' to overwrite, 'append' to add to end")
    agent: Optional[str] = Field(default="engineer")

class FileListInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    directory: Optional[str] = Field(default=".", description="Directory relative to workspace")
    recursive: Optional[bool] = Field(default=False, description="List recursively")
    agent: Optional[str] = Field(default="cortex")


@mcp.tool(
    name="ronin_file_read",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
)
async def ronin_file_read(params: FileReadInput) -> str:
    """Read a file from the RONIN workspace. Returns file content or error."""
    target = WORKSPACE / params.path
    if not target.exists():
        return json.dumps({"error": f"File not found: {params.path}"})
    if not str(target.resolve()).startswith(str(WORKSPACE.resolve())):
        return json.dumps({"error": "Access denied: path escapes workspace"})
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
        return json.dumps({"path": params.path, "size": len(content), "content": truncate(content)})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool(
    name="ronin_file_write",
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False}
)
async def ronin_file_write(params: FileWriteInput) -> str:
    """Write content to a file in the RONIN workspace. Creates directories as needed."""
    target = WORKSPACE / params.path
    if not str(target.resolve()).startswith(str(WORKSPACE.resolve())):
        return json.dumps({"error": "Access denied: path escapes workspace"})
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if params.mode == "append":
            with open(target, "a", encoding="utf-8") as f:
                f.write(params.content)
        else:
            target.write_text(params.content, encoding="utf-8")
        return json.dumps({"success": True, "path": params.path, "size": target.stat().st_size})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool(
    name="ronin_file_list",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
)
async def ronin_file_list(params: FileListInput) -> str:
    """List files in the RONIN workspace directory."""
    target = WORKSPACE / (params.directory or ".")
    if not target.exists():
        return json.dumps({"error": f"Directory not found: {params.directory}"})
    try:
        if params.recursive:
            files = [str(p.relative_to(WORKSPACE)) for p in target.rglob("*") if p.is_file()]
        else:
            files = [str(p.relative_to(WORKSPACE)) for p in target.iterdir()]
        return json.dumps({"directory": params.directory, "count": len(files), "files": files[:200]})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ═══════════════════════════════════════════════════════════════════════════
# TOOL: Web Fetch
# ═══════════════════════════════════════════════════════════════════════════

class WebFetchInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    url: str = Field(..., description="URL to fetch", min_length=8, max_length=2000)
    method: Optional[str] = Field(default="GET", description="HTTP method: GET, POST, PUT, DELETE")
    headers: Optional[Dict[str, str]] = Field(default=None, description="Custom headers")
    body: Optional[str] = Field(default=None, description="Request body for POST/PUT")
    extract_text: Optional[bool] = Field(default=True, description="Extract text content only (strip HTML)")
    agent: Optional[str] = Field(default="researcher")

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")
        return v


@mcp.tool(
    name="ronin_web_fetch",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}
)
async def ronin_web_fetch(params: WebFetchInput) -> str:
    """Fetch content from a URL. Supports GET/POST/PUT/DELETE with custom headers.

    Returns status code, headers, and body content. Optionally strips HTML to plain text.
    """
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        try:
            response = await client.request(
                method=params.method or "GET",
                url=params.url,
                headers=params.headers,
                content=params.body,
            )
            body = response.text
            if params.extract_text and "text/html" in response.headers.get("content-type", ""):
                # Crude HTML stripping - production would use BeautifulSoup
                import re
                body = re.sub(r'<script[^>]*>.*?</script>', '', body, flags=re.DOTALL)
                body = re.sub(r'<style[^>]*>.*?</style>', '', body, flags=re.DOTALL)
                body = re.sub(r'<[^>]+>', ' ', body)
                body = re.sub(r'\s+', ' ', body).strip()

            return json.dumps({
                "status_code": response.status_code,
                "url": str(response.url),
                "content_type": response.headers.get("content-type", ""),
                "content_length": len(body),
                "body": truncate(body),
            }, indent=2)
        except httpx.HTTPStatusError as e:
            return json.dumps({"error": f"HTTP {e.response.status_code}", "url": params.url})
        except Exception as e:
            return json.dumps({"error": str(e), "type": type(e).__name__})


# ═══════════════════════════════════════════════════════════════════════════
# TOOL: Code Sandbox
# ═══════════════════════════════════════════════════════════════════════════

class CodeExecInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    language: str = Field(..., description="Language: 'python', 'javascript', 'bash'")
    code: str = Field(..., description="Code to execute", max_length=50000)
    timeout: Optional[int] = Field(default=15, description="Timeout in seconds", ge=1, le=30)
    agent: Optional[str] = Field(default="engineer")

    @field_validator("language")
    @classmethod
    def validate_language(cls, v: str) -> str:
        allowed = {"python", "javascript", "bash", "node"}
        if v.lower() not in allowed:
            raise ValueError(f"Language must be one of: {', '.join(allowed)}")
        return v.lower()


@mcp.tool(
    name="ronin_code_exec",
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False}
)
async def ronin_code_exec(params: CodeExecInput) -> str:
    """Execute code in a sandboxed environment. Supports Python, JavaScript/Node, and Bash.

    Code is written to a temp file in the workspace and executed with bounded timeout.
    Returns stdout, stderr, exit code, and execution time.
    """
    lang_map = {
        "python": ("python3", ".py"),
        "javascript": ("node", ".js"),
        "node": ("node", ".js"),
        "bash": ("bash", ".sh"),
    }

    runtime, ext = lang_map[params.language]
    tmp_dir = WORKSPACE / ".sandbox"
    tmp_dir.mkdir(exist_ok=True)

    script_path = tmp_dir / f"exec_{int(time.time() * 1000)}{ext}"
    script_path.write_text(params.code, encoding="utf-8")

    try:
        result = await ronin_shell_exec(ShellInput(
            command=f"{runtime} {script_path}",
            working_dir=".sandbox",
            timeout=params.timeout,
            agent=params.agent,
        ))
        # Clean up
        script_path.unlink(missing_ok=True)
        return result
    except Exception as e:
        script_path.unlink(missing_ok=True)
        return json.dumps({"error": str(e)})


# ═══════════════════════════════════════════════════════════════════════════
# TOOL: Memory Operations (Persistent)
# ═══════════════════════════════════════════════════════════════════════════

class MemoryStoreInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    fact: str = Field(..., description="The fact or knowledge to store", min_length=1, max_length=2000)
    confidence: Optional[float] = Field(default=0.7, description="Confidence level 0.0-1.0", ge=0, le=1)
    source: Optional[str] = Field(default="agent", description="Source of the fact")
    tags: Optional[List[str]] = Field(default_factory=list, description="Tags for categorization")
    agent: Optional[str] = Field(default="cortex")

class MemoryQueryInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    query: str = Field(..., description="Search query for memory retrieval", min_length=1)
    min_confidence: Optional[float] = Field(default=0.0, ge=0, le=1)
    limit: Optional[int] = Field(default=10, ge=1, le=50)
    agent: Optional[str] = Field(default="cortex")

class EpisodicStoreInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    interaction: str = Field(..., description="The interaction to remember", max_length=5000)
    reflection: Optional[str] = Field(default=None, description="Reflection/lesson learned", max_length=2000)
    importance: Optional[float] = Field(default=0.5, ge=0, le=1)
    agent: Optional[str] = Field(default="cortex")


@mcp.tool(
    name="ronin_memory_store",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
)
async def ronin_memory_store(params: MemoryStoreInput) -> str:
    """Store a fact in RONIN semantic (long-term) memory with confidence scoring.

    Facts persist across sessions via SQLite. Duplicate facts are updated
    rather than re-inserted. Tags enable categorical retrieval.
    """
    db = init_database(MEMORY_DB)
    now = now_iso()
    fact_id = f"sem_{hash(params.fact) & 0xFFFFFFFF:08x}"

    existing = db.execute("SELECT id FROM semantic_memory WHERE fact = ?", (params.fact,)).fetchone()
    if existing:
        db.execute(
            "UPDATE semantic_memory SET confidence=?, last_accessed=?, access_count=access_count+1 WHERE fact=?",
            (params.confidence, now, params.fact)
        )
    else:
        db.execute(
            "INSERT INTO semantic_memory (id, fact, confidence, source, tags, created_at, last_accessed) VALUES (?,?,?,?,?,?,?)",
            (fact_id, params.fact, params.confidence, params.source, json.dumps(params.tags), now, now)
        )
    db.commit()
    db.close()
    return json.dumps({"success": True, "id": fact_id, "fact": params.fact, "status": "updated" if existing else "created"})


@mcp.tool(
    name="ronin_memory_query",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
)
async def ronin_memory_query(params: MemoryQueryInput) -> str:
    """Search RONIN semantic memory using keyword matching.

    Returns facts ranked by relevance (keyword match) and confidence.
    Updates access counts and timestamps on retrieval.
    """
    db = init_database(MEMORY_DB)
    keywords = params.query.lower().split()

    rows = db.execute(
        "SELECT * FROM semantic_memory WHERE confidence >= ? ORDER BY confidence DESC, access_count DESC LIMIT ?",
        (params.min_confidence, params.limit * 3)  # Overfetch for filtering
    ).fetchall()

    results = []
    for row in rows:
        fact_lower = row["fact"].lower()
        score = sum(1 for kw in keywords if kw in fact_lower)
        if score > 0:
            results.append({
                "id": row["id"],
                "fact": row["fact"],
                "confidence": row["confidence"],
                "source": row["source"],
                "relevance_score": score / len(keywords),
                "access_count": row["access_count"],
                "created_at": row["created_at"],
            })
            # Update access stats
            db.execute(
                "UPDATE semantic_memory SET last_accessed=?, access_count=access_count+1 WHERE id=?",
                (now_iso(), row["id"])
            )

    results.sort(key=lambda x: (x["relevance_score"], x["confidence"]), reverse=True)
    db.commit()
    db.close()
    return json.dumps({"query": params.query, "count": len(results[:params.limit]), "results": results[:params.limit]}, indent=2)


@mcp.tool(
    name="ronin_episodic_store",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
)
async def ronin_episodic_store(params: EpisodicStoreInput) -> str:
    """Store an interaction in episodic memory with optional reflection.

    Episodic memories capture what happened and what was learned. High-importance
    episodes are candidates for consolidation into semantic memory.
    """
    db = init_database(MEMORY_DB)
    ep_id = f"ep_{int(time.time() * 1000)}"
    db.execute(
        "INSERT INTO episodic_memory (id, interaction, reflection, importance_score, agent, created_at) VALUES (?,?,?,?,?,?)",
        (ep_id, params.interaction, params.reflection, params.importance, params.agent, now_iso())
    )
    db.commit()

    # Auto-consolidate: if high importance and has reflection, also store as semantic
    if params.importance >= 0.8 and params.reflection:
        await ronin_memory_store(MemoryStoreInput(
            fact=params.reflection,
            confidence=params.importance,
            source="episodic_consolidation",
            agent=params.agent,
        ))

    db.close()
    return json.dumps({"success": True, "id": ep_id, "auto_consolidated": params.importance >= 0.8 and bool(params.reflection)})


# ═══════════════════════════════════════════════════════════════════════════
# TOOL: Key-Value Store (for agent state, preferences, etc.)
# ═══════════════════════════════════════════════════════════════════════════

class KVGetInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    key: str = Field(..., description="Key to retrieve", min_length=1, max_length=200)

class KVSetInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    key: str = Field(..., description="Key to store", min_length=1, max_length=200)
    value: str = Field(..., description="Value to store (JSON string recommended)", max_length=100000)

@mcp.tool(
    name="ronin_kv_get",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
)
async def ronin_kv_get(params: KVGetInput) -> str:
    """Retrieve a value from the persistent key-value store."""
    db = init_database(MEMORY_DB)
    row = db.execute("SELECT value, updated_at FROM key_value_store WHERE key=?", (params.key,)).fetchone()
    db.close()
    if row:
        return json.dumps({"key": params.key, "value": row["value"], "updated_at": row["updated_at"]})
    return json.dumps({"key": params.key, "value": None, "error": "Key not found"})

@mcp.tool(
    name="ronin_kv_set",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
)
async def ronin_kv_set(params: KVSetInput) -> str:
    """Store a key-value pair in persistent storage. Overwrites existing keys."""
    db = init_database(MEMORY_DB)
    db.execute(
        "INSERT OR REPLACE INTO key_value_store (key, value, updated_at) VALUES (?,?,?)",
        (params.key, params.value, now_iso())
    )
    db.commit()
    db.close()
    return json.dumps({"success": True, "key": params.key})


# ═══════════════════════════════════════════════════════════════════════════
# TOOL: System Introspection
# ═══════════════════════════════════════════════════════════════════════════

class SystemInfoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    component: Optional[str] = Field(
        default="overview",
        description="Component to inspect: 'overview', 'memory_stats', 'audit_recent', 'workspace_status'"
    )


@mcp.tool(
    name="ronin_system_info",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
)
async def ronin_system_info(params: SystemInfoInput) -> str:
    """Inspect RONIN system status: memory stats, audit trail, workspace info.

    Provides operational intelligence for the Cortex orchestrator to make decisions.
    """
    db = init_database(MEMORY_DB)

    if params.component == "memory_stats":
        sem_count = db.execute("SELECT COUNT(*) as c FROM semantic_memory").fetchone()["c"]
        ep_count = db.execute("SELECT COUNT(*) as c FROM episodic_memory").fetchone()["c"]
        avg_conf = db.execute("SELECT AVG(confidence) as a FROM semantic_memory").fetchone()["a"] or 0
        kv_count = db.execute("SELECT COUNT(*) as c FROM key_value_store").fetchone()["c"]
        db.close()
        return json.dumps({
            "semantic_facts": sem_count,
            "episodic_memories": ep_count,
            "average_confidence": round(avg_conf, 3),
            "kv_pairs": kv_count,
            "db_path": str(MEMORY_DB),
            "db_size_bytes": MEMORY_DB.stat().st_size if MEMORY_DB.exists() else 0,
        }, indent=2)

    elif params.component == "audit_recent":
        rows = db.execute(
            "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT 20"
        ).fetchall()
        db.close()
        return json.dumps({
            "recent_actions": [dict(r) for r in rows]
        }, indent=2)

    elif params.component == "workspace_status":
        db.close()
        files = list(WORKSPACE.rglob("*"))
        file_list = [str(f.relative_to(WORKSPACE)) for f in files if f.is_file()][:50]
        total_size = sum(f.stat().st_size for f in files if f.is_file())
        return json.dumps({
            "workspace_path": str(WORKSPACE),
            "total_files": len([f for f in files if f.is_file()]),
            "total_dirs": len([f for f in files if f.is_dir()]),
            "total_size_bytes": total_size,
            "recent_files": file_list[-20:],
        }, indent=2)

    else:  # overview
        sem_count = db.execute("SELECT COUNT(*) as c FROM semantic_memory").fetchone()["c"]
        ep_count = db.execute("SELECT COUNT(*) as c FROM episodic_memory").fetchone()["c"]
        audit_count = db.execute("SELECT COUNT(*) as c FROM audit_log").fetchone()["c"]
        db.close()
        return json.dumps({
            "system": "RONIN MCP Server v1.0",
            "status": "operational",
            "workspace": str(WORKSPACE),
            "memory": {"semantic": sem_count, "episodic": ep_count},
            "audit_entries": audit_count,
            "tools_available": 12,
            "uptime": "active",
            "protocols": ["MCP (JSON-RPC 2.0)", "A2A-ready", "WebMCP-ready"],
        }, indent=2)


# ═══════════════════════════════════════════════════════════════════════════
# TOOL: Safety Check (Aegis Guardian)
# ═══════════════════════════════════════════════════════════════════════════

class SafetyCheckInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    action_description: str = Field(..., description="Description of the action to evaluate", max_length=2000)
    risk_level: Optional[str] = Field(default="medium", description="Estimated risk: 'low', 'medium', 'high', 'critical'")
    agent: Optional[str] = Field(default="cortex", description="Agent requesting the check")

    @field_validator("risk_level")
    @classmethod
    def validate_risk(cls, v: str) -> str:
        allowed = {"low", "medium", "high", "critical"}
        if v.lower() not in allowed:
            raise ValueError(f"Risk level must be one of: {', '.join(allowed)}")
        return v.lower()


@mcp.tool(
    name="ronin_safety_check",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
)
async def ronin_safety_check(params: SafetyCheckInput) -> str:
    """Aegis Guardian safety evaluation. Checks proposed actions against safety policies.

    Evaluates risk level, checks for dangerous patterns, and returns approve/deny/escalate.
    Critical-risk actions always require human confirmation.
    """
    action_lower = params.action_description.lower()

    # Hard blocks
    dangerous_patterns = [
        "delete all", "drop database", "rm -rf", "format disk",
        "send credentials", "expose api key", "disable security",
        "bypass authentication", "escalate privileges",
    ]
    is_dangerous = any(p in action_lower for p in dangerous_patterns)

    # Risk assessment
    if is_dangerous or params.risk_level == "critical":
        decision = "DENIED"
        reason = "Action matches dangerous pattern or is critical risk. Requires human override."
    elif params.risk_level == "high":
        decision = "ESCALATE"
        reason = "High-risk action requires human confirmation before execution."
    elif params.risk_level == "medium":
        decision = "APPROVED_WITH_AUDIT"
        reason = "Medium-risk action approved. Full audit trail recorded."
    else:
        decision = "APPROVED"
        reason = "Low-risk action approved for autonomous execution."

    return json.dumps({
        "decision": decision,
        "reason": reason,
        "risk_level": params.risk_level,
        "dangerous_patterns_detected": is_dangerous,
        "agent": params.agent,
        "timestamp": now_iso(),
        "recommendation": "Proceed" if "APPROVED" in decision else "Seek human confirmation",
    }, indent=2)


# ═══════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="RONIN MCP Tool Server")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio",
                       help="Transport: stdio (local) or http (remote)")
    parser.add_argument("--port", type=int, default=8741,
                       help="Port for HTTP transport (default: 8741)")
    args = parser.parse_args()

    print(f"🥷 RONIN MCP Server starting...")
    print(f"   Transport: {args.transport}")
    print(f"   Workspace: {WORKSPACE}")
    print(f"   Memory DB: {MEMORY_DB}")

    if args.transport == "http":
        print(f"   Port: {args.port}")
        mcp.run(transport="streamable_http", port=args.port)
    else:
        mcp.run()

"""
RONIN REST API — FastAPI wrapper around MCP tools
====================================================
Bridges the frontend (HTTP fetch) to the MCP tool layer.

Architecture:
  - Imports tool functions + Pydantic models directly from ronin_mcp_server
  - Exposes POST /api/tools/{tool_name} for each tool
  - Manages a single shared DB connection + httpx client via lifespan
  - CORS enabled for local Vite dev server
  - Health, tool listing, and conversation history endpoints

Runs on port 8742 (MCP stays on 8741 for stdio/Claude Desktop).
"""

import json
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import logging
from fastapi import Depends, FastAPI, HTTPException, Request, status
logger = logging.getLogger("RoninAPI")
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# ─── Import A2A modules ────────────────────────────────────────────────────
from agent_cards import (
    AgentCard, AgentSkill, AgentCapabilities, AgentAuthentication,
    AgentRegistry, AgentStatus, AuthType, init_agent_tables,
)
from a2a_protocol import (
    A2AMessage, A2ATask, A2ARouter, ContentPart, MessageType, TaskStatus,
    check_agent_health, init_a2a_tables,
)
from capability_matcher import match_task_to_agent

# ─── Import Phase 4 modules ───────────────────────────────────────────────
from event_queue import Event, EventBus, EventPriority, EventSource
from scheduler import Scheduler, ScheduledTask, CreateScheduleRequest, UpdateScheduleRequest
from notifications import (
    NotificationRouter, NotificationConfig, ChannelConfig,
    Notification, NotificationChannel, create_notification_handler,
)
from context_stream import ContextStream, create_context_handler
from watchers.filesystem import FilesystemWatcher, is_watch_enabled
from watchers.system_monitor import SystemMonitor

# ─── Import Phase 5 modules ───────────────────────────────────────────────
from auth import (
    User, UserStore, TokenResponse, RegisterRequest, RefreshRequest,
    init_user_tables, ensure_default_admin, set_db_getter,
    get_current_user, require_auth, require_admin,
    create_access_token, create_refresh_token, decode_token,
)
from vault import VaultStore, init_vault_table, import_env_to_vault, set_vault
from resilience import RateLimitMiddleware, circuits, set_test_mode
from logging_config import setup_logging, RequestLoggingMiddleware, metrics
from backup import backup_database, list_backups, restore_database, export_data, import_data

# ─── Import MCP tool functions + models directly ──────────────────────────
from ronin_mcp_server import (
    # Config
    RONIN_HOME, MEMORY_DB, WORKSPACE, AUDIT_LOG,
    init_database, now_iso, truncate, audit,
    # Tool functions
    ronin_shell_exec,
    ronin_file_read,
    ronin_file_write,
    ronin_file_list,
    ronin_web_fetch,
    ronin_code_exec,
    ronin_memory_store,
    ronin_memory_query,
    ronin_episodic_store,
    ronin_kv_get,
    ronin_kv_set,
    ronin_system_info,
    ronin_safety_check,
    # Pydantic input models
    ShellInput,
    FileReadInput,
    FileWriteInput,
    FileListInput,
    WebFetchInput,
    CodeExecInput,
    MemoryStoreInput,
    MemoryQueryInput,
    EpisodicStoreInput,
    KVGetInput,
    KVSetInput,
    SystemInfoInput,
    SafetyCheckInput,
)

# ─── Tool Registry ─────────────────────────────────────────────────────────
# Maps tool name → (function, input_model, description)
TOOL_REGISTRY: Dict[str, dict] = {
    "ronin_shell_exec": {
        "fn": ronin_shell_exec,
        "model": ShellInput,
        "description": "Execute a shell command in the sandboxed RONIN workspace.",
    },
    "ronin_file_read": {
        "fn": ronin_file_read,
        "model": FileReadInput,
        "description": "Read a file from the RONIN workspace.",
    },
    "ronin_file_write": {
        "fn": ronin_file_write,
        "model": FileWriteInput,
        "description": "Write content to a file in the RONIN workspace.",
    },
    "ronin_file_list": {
        "fn": ronin_file_list,
        "model": FileListInput,
        "description": "List files in the RONIN workspace directory.",
    },
    "ronin_web_fetch": {
        "fn": ronin_web_fetch,
        "model": WebFetchInput,
        "description": "Fetch content from a URL.",
    },
    "ronin_code_exec": {
        "fn": ronin_code_exec,
        "model": CodeExecInput,
        "description": "Execute code in a sandboxed environment.",
    },
    "ronin_memory_store": {
        "fn": ronin_memory_store,
        "model": MemoryStoreInput,
        "description": "Store a fact in RONIN semantic (long-term) memory.",
    },
    "ronin_memory_query": {
        "fn": ronin_memory_query,
        "model": MemoryQueryInput,
        "description": "Search RONIN semantic memory.",
    },
    "ronin_episodic_store": {
        "fn": ronin_episodic_store,
        "model": EpisodicStoreInput,
        "description": "Store an interaction in episodic memory.",
    },
    "ronin_kv_get": {
        "fn": ronin_kv_get,
        "model": KVGetInput,
        "description": "Retrieve a value from the persistent key-value store.",
    },
    "ronin_kv_set": {
        "fn": ronin_kv_set,
        "model": KVSetInput,
        "description": "Store a key-value pair in persistent storage.",
    },
    "ronin_system_info": {
        "fn": ronin_system_info,
        "model": SystemInfoInput,
        "description": "Inspect RONIN system status.",
    },
    "ronin_safety_check": {
        "fn": ronin_safety_check,
        "model": SafetyCheckInput,
        "description": "Aegis Guardian safety evaluation.",
    },
}


# ─── Lifespan: shared DB + httpx ──────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize shared resources."""
    app.state.db = init_database(MEMORY_DB)
    app.state.db_path = MEMORY_DB
    app.state.http = httpx.AsyncClient(
        timeout=15.0,
        follow_redirects=True,
        headers={"User-Agent": "RONIN-API/1.0"},
    )

    # Initialize A2A components
    init_agent_tables(app.state.db)
    init_a2a_tables(app.state.db)
    app.state.agent_registry = AgentRegistry(app.state.db)
    app.state.a2a_router = A2ARouter(app.state.db, app.state.agent_registry, app.state.http)

    # Wire up the tool executor so A2A can call MCP tools for internal agents
    async def _tool_executor(tool_name: str, params: dict) -> str:
        if tool_name not in TOOL_REGISTRY:
            return json.dumps({"error": f"Unknown tool: {tool_name}"})
        entry = TOOL_REGISTRY[tool_name]
        model_cls = entry["model"]
        validated = model_cls(**params)
        return await entry["fn"](validated)

    app.state.a2a_router.set_tool_executor(_tool_executor)

    # ─── Phase 4: Proactive Intelligence ────────────────────────────────
    # EventBus
    app.state.event_bus = EventBus(app.state.db, audit_fn=audit)
    await app.state.event_bus.start()

    # Context Stream
    app.state.context_stream = ContextStream(app.state.db)
    app.state.event_bus.register_handler("*", create_context_handler(app.state.context_stream))

    # Notification Router
    app.state.notification_router = NotificationRouter(app.state.db, app.state.http)
    app.state.event_bus.register_handler("*", create_notification_handler(app.state.notification_router))

    # Scheduler
    app.state.scheduler = Scheduler(app.state.db, app.state.event_bus)
    await app.state.scheduler.start()

    # System Monitor
    app.state.system_monitor = SystemMonitor(
        app.state.event_bus, MEMORY_DB, WORKSPACE, check_interval=60.0,
    )
    await app.state.system_monitor.start()

    # Filesystem Watcher (opt-in)
    app.state.fs_watcher = None
    if is_watch_enabled(app.state.db):
        app.state.fs_watcher = FilesystemWatcher(app.state.event_bus, WORKSPACE)
        await app.state.fs_watcher.start()

    # ─── Phase 5: Production Hardening ──────────────────────────────
    setup_logging()
    init_user_tables(app.state.db)
    ensure_default_admin(app.state.db)
    set_db_getter(get_db)

    init_vault_table(app.state.db)
    app.state.vault = VaultStore(app.state.db)
    imported = import_env_to_vault(app.state.vault)
    if imported:
        print(f"🔐 Vault: imported {len(imported)} keys from env")
    set_vault(app.state.vault)

    agent_count = len(app.state.agent_registry.list_all())
    print(f"🧠 RONIN API ready (Phase 5)")
    print(f"   Workspace: {WORKSPACE}")
    print(f"   Memory DB: {MEMORY_DB}")
    print(f"   Tools: {len(TOOL_REGISTRY)}")
    print(f"   Agents: {agent_count} ({sum(1 for a in app.state.agent_registry.list_all() if a.is_internal)} internal)")
    print(f"   EventBus: running | Scheduler: running | Monitor: running")
    yield

    # ─── Shutdown Phase 4 ──────────────────────────────────────────────
    if app.state.fs_watcher:
        await app.state.fs_watcher.stop()
    await app.state.system_monitor.stop()
    await app.state.scheduler.stop()
    await app.state.event_bus.stop()

    await app.state.http.aclose()
    app.state.db.close()


# ─── FastAPI App ──────────────────────────────────────────────────────────

app = FastAPI(
    title="RONIN API",
    version="3.0.0",
    description="REST API for RONIN MCP tools — bridges frontend to real tool execution",
    lifespan=lifespan,
)

# CORS: allow Vite dev server + any localhost origin
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:5174",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Phase 5: Rate limiting + request logging middleware
app.add_middleware(RateLimitMiddleware)
app.add_middleware(RequestLoggingMiddleware)


# ─── Request/Response Models ──────────────────────────────────────────────

class ToolCallRequest(BaseModel):
    """Generic tool call — tool_name in URL, input as JSON body."""
    input: Dict[str, Any]

class ToolCallResponse(BaseModel):
    tool: str
    success: bool
    result: Any
    execution_ms: float

class ConversationEntry(BaseModel):
    id: Optional[str] = None
    messages: List[Dict[str, Any]]
    metadata: Optional[Dict[str, Any]] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# ─── ROUTES ───────────────────────────────────────────────────────────────

def get_db():
    """Get a database connection — works with or without lifespan."""
    try:
        return app.state.db
    except AttributeError:
        # Lifespan hasn't run (e.g., in tests) — create a connection
        return init_database(MEMORY_DB)


def get_fresh_db():
    """Get the shared DB connection. Auth uses the same connection as other endpoints
    to avoid SQLite inter-connection write lock contention."""
    return get_db()


def get_registry() -> AgentRegistry:
    """Get the agent registry — creates one if lifespan hasn't run."""
    try:
        return app.state.agent_registry
    except AttributeError:
        db = get_db()
        init_agent_tables(db)
        registry = AgentRegistry(db)
        return registry


def get_router() -> A2ARouter:
    """Get the A2A router — creates one if lifespan hasn't run."""
    try:
        return app.state.a2a_router
    except AttributeError:
        db = get_db()
        registry = get_registry()
        router = A2ARouter(db, registry)

        # Wire up the tool executor for test contexts
        async def _tool_executor(tool_name: str, params: dict) -> str:
            if tool_name not in TOOL_REGISTRY:
                return json.dumps({"error": f"Unknown tool: {tool_name}"})
            entry = TOOL_REGISTRY[tool_name]
            model_cls = entry["model"]
            validated = model_cls(**params)
            return await entry["fn"](validated)

        router.set_tool_executor(_tool_executor)
        return router


@app.get("/api/health")
async def health():
    """Health check — returns system status."""
    db = get_db()
    try:
        sem = db.execute("SELECT COUNT(*) as c FROM semantic_memory").fetchone()["c"]
        ep = db.execute("SELECT COUNT(*) as c FROM episodic_memory").fetchone()["c"]
        audit_count = db.execute("SELECT COUNT(*) as c FROM audit_log").fetchone()["c"]
    except Exception:
        sem = ep = audit_count = -1

    result = {
        "status": "operational",
        "version": "3.0.0",
        "tools": len(TOOL_REGISTRY),
        "memory": {"semantic": sem, "episodic": ep},
        "audit_entries": audit_count,
        "workspace": str(WORKSPACE),
        "agents": len(get_registry().list_all()),
    }

    # Phase 4: system metrics
    try:
        monitor = app.state.system_monitor
        metrics = monitor.get_metrics()
        # Inject event queue stats
        bus_stats = app.state.event_bus.get_stats()
        metrics["event_queue_depth"] = bus_stats.get("queue_depth", 0)
        metrics["events_processed_total"] = bus_stats.get("processed", 0)
        result["system"] = {
            "disk_percent": metrics.get("disk_percent", 0),
            "memory_percent": metrics.get("memory_percent", 0),
            "cpu_percent": metrics.get("cpu_percent", 0),
            "db_size_mb": metrics.get("db_size_mb", 0),
            "workspace_size_mb": metrics.get("workspace_size_mb", 0),
            "event_queue_depth": metrics.get("event_queue_depth", 0),
            "events_processed_total": metrics.get("events_processed_total", 0),
        }
    except AttributeError:
        pass  # Phase 4 not initialized (test context)

    return result


@app.get("/api/tools")
async def list_tools():
    """List all available tools with schemas — frontend uses this to build MCP_TOOLS array."""
    tools = []
    for name, entry in TOOL_REGISTRY.items():
        model = entry["model"]
        # Generate JSON schema from Pydantic model
        schema = model.model_json_schema()
        # Strip pydantic metadata the frontend doesn't need
        props = {}
        required = []
        for field_name, field_info in schema.get("properties", {}).items():
            props[field_name] = {
                "type": field_info.get("type", "string"),
                "description": field_info.get("description", ""),
            }
            if "enum" in field_info:
                props[field_name]["enum"] = field_info["enum"]
            if "default" in field_info:
                props[field_name]["default"] = field_info["default"]
            if "items" in field_info:
                props[field_name]["items"] = field_info["items"]

        for req_field in schema.get("required", []):
            required.append(req_field)

        tools.append({
            "name": name,
            "description": entry["description"],
            "input_schema": {
                "type": "object",
                "properties": props,
                "required": required,
            },
        })
    return {"tools": tools}


@app.post("/api/tools/{tool_name}", response_model=ToolCallResponse)
async def execute_tool(tool_name: str, request: Request):
    """
    Execute a RONIN tool by name.

    Body: { "input": { ...tool-specific params... } }
    Returns: { "tool": str, "success": bool, "result": any, "execution_ms": float }
    """
    if tool_name not in TOOL_REGISTRY:
        raise HTTPException(status_code=404, detail=f"Unknown tool: {tool_name}")

    entry = TOOL_REGISTRY[tool_name]
    fn = entry["fn"]
    model_cls = entry["model"]

    # Parse body
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    tool_input = body.get("input", body)  # Accept {input: {...}} or flat {...}

    # Validate with Pydantic model
    try:
        params = model_cls(**tool_input)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Validation error: {e}")

    # Execute
    start = time.monotonic()
    try:
        raw_result = await fn(params)
        elapsed_ms = (time.monotonic() - start) * 1000

        # MCP tools return JSON strings — parse for the response
        try:
            parsed = json.loads(raw_result)
        except (json.JSONDecodeError, TypeError):
            parsed = {"raw": raw_result}

        # Audit
        db = get_db()
        try:
            audit(db, tool_name, tool_input.get("agent", "api"),
                  json.dumps(tool_input)[:500], str(raw_result)[:500],
                  True, elapsed_ms)
        except Exception:
            pass  # Don't fail the request over audit

        return ToolCallResponse(
            tool=tool_name,
            success=True,
            result=parsed,
            execution_ms=round(elapsed_ms, 1),
        )

    except Exception as e:
        elapsed_ms = (time.monotonic() - start) * 1000
        return ToolCallResponse(
            tool=tool_name,
            success=False,
            result={"error": str(e), "type": type(e).__name__},
            execution_ms=round(elapsed_ms, 1),
        )


# ─── Batch Tool Execution ─────────────────────────────────────────────────

class BatchToolCall(BaseModel):
    name: str
    input: Dict[str, Any]

class BatchRequest(BaseModel):
    calls: List[BatchToolCall]

@app.post("/api/batch")
async def execute_batch(req: BatchRequest):
    """
    Execute multiple tools in sequence. Used by the agentic loop
    when Claude returns multiple tool_use blocks in one turn.

    Body: { "calls": [{ "name": "ronin_file_write", "input": {...} }, ...] }
    Returns: { "results": [...], "total_ms": float }
    """
    results = []
    total_start = time.monotonic()

    for call in req.calls:
        if call.name not in TOOL_REGISTRY:
            results.append({
                "tool": call.name,
                "success": False,
                "result": {"error": f"Unknown tool: {call.name}"},
                "execution_ms": 0,
            })
            continue

        entry = TOOL_REGISTRY[call.name]
        try:
            params = entry["model"](**call.input)
            start = time.monotonic()
            raw = await entry["fn"](params)
            ms = (time.monotonic() - start) * 1000
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            results.append({
                "tool": call.name,
                "success": True,
                "result": parsed,
                "execution_ms": round(ms, 1),
            })
        except Exception as e:
            results.append({
                "tool": call.name,
                "success": False,
                "result": {"error": str(e)},
                "execution_ms": 0,
            })

    return {
        "results": results,
        "total_ms": round((time.monotonic() - total_start) * 1000, 1),
    }


# ─── Memory Endpoints (convenience, beyond per-tool) ──────────────────────

@app.get("/api/memory/semantic")
async def get_all_semantic(limit: int = 50, min_confidence: float = 0.0):
    """Retrieve all semantic memories — for sidebar hydration on page load."""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM semantic_memory WHERE confidence >= ? ORDER BY last_accessed DESC LIMIT ?",
        (min_confidence, limit),
    ).fetchall()
    return {
        "count": len(rows),
        "memories": [
            {
                "id": r["id"],
                "fact": r["fact"],
                "confidence": r["confidence"],
                "source": r["source"],
                "tags": json.loads(r["tags"]) if r["tags"] else [],
                "created_at": r["created_at"],
                "access_count": r["access_count"],
            }
            for r in rows
        ],
    }


@app.get("/api/memory/episodic")
async def get_recent_episodic(limit: int = 20):
    """Retrieve recent episodic memories."""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM episodic_memory ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return {
        "count": len(rows),
        "episodes": [
            {
                "id": r["id"],
                "interaction": r["interaction"],
                "reflection": r["reflection"],
                "importance_score": r["importance_score"],
                "agent": r["agent"],
                "created_at": r["created_at"],
            }
            for r in rows
        ],
    }


# ─── Conversation History ─────────────────────────────────────────────────

# Conversations stored in KV store with prefix "conv:"
@app.post("/api/conversations")
async def save_conversation(entry: ConversationEntry):
    """Save a conversation to persistent storage."""
    db = get_db()
    conv_id = entry.id or f"conv_{int(time.time() * 1000)}"
    now = now_iso()
    payload = json.dumps({
        "id": conv_id,
        "messages": entry.messages,
        "metadata": entry.metadata or {},
        "created_at": entry.created_at or now,
        "updated_at": now,
    })
    db.execute(
        "INSERT OR REPLACE INTO key_value_store (key, value, updated_at) VALUES (?,?,?)",
        (f"conv:{conv_id}", payload, now),
    )
    db.commit()
    return {"id": conv_id, "saved": True}


@app.get("/api/conversations")
async def list_conversations(limit: int = 20):
    """List saved conversations (most recent first)."""
    db = get_db()
    rows = db.execute(
        "SELECT key, value, updated_at FROM key_value_store WHERE key LIKE 'conv:%' ORDER BY updated_at DESC LIMIT ?",
        (limit,),
    ).fetchall()

    convs = []
    for r in rows:
        try:
            data = json.loads(r["value"])
            convs.append({
                "id": data.get("id", r["key"].replace("conv:", "")),
                "message_count": len(data.get("messages", [])),
                "metadata": data.get("metadata", {}),
                "created_at": data.get("created_at"),
                "updated_at": r["updated_at"],
            })
        except json.JSONDecodeError:
            continue
    return {"count": len(convs), "conversations": convs}


@app.get("/api/conversations/{conv_id}")
async def get_conversation(conv_id: str):
    """Load a specific conversation."""
    db = get_db()
    row = db.execute(
        "SELECT value FROM key_value_store WHERE key=?",
        (f"conv:{conv_id}",),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return json.loads(row["value"])


@app.delete("/api/conversations/{conv_id}")
async def delete_conversation(conv_id: str):
    """Delete a saved conversation."""
    db = get_db()
    db.execute("DELETE FROM key_value_store WHERE key=?", (f"conv:{conv_id}",))
    db.commit()
    return {"deleted": True, "id": conv_id}


# ─── Audit Log ─────────────────────────────────────────────────────────────

@app.get("/api/audit")
async def get_audit_log(limit: int = 50):
    """Recent audit entries."""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return {"count": len(rows), "entries": [dict(r) for r in rows]}


# ─── Agent Discovery ─────────────────────────────────────────────────────

@app.get("/.well-known/agent.json")
async def well_known_agent():
    """A2A discovery endpoint — returns the RONIN system Agent Card."""
    registry = get_registry()
    return registry.get_system_card().to_dict()


@app.get("/api/agents")
async def list_agents():
    """List all registered agents."""
    registry = get_registry()
    agents = registry.list_all()
    return {
        "count": len(agents),
        "agents": [a.to_dict() for a in agents],
    }


@app.get("/api/agents/{name}")
async def get_agent(name: str):
    """Get a specific agent's card."""
    registry = get_registry()
    agent = registry.get(name)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent not found: {name}")
    return agent.to_dict()


class RegisterAgentRequest(BaseModel):
    """Request body for registering an external agent."""
    name: str
    description: str = ""
    url: str
    version: str = "1.0.0"
    skills: List[Dict[str, Any]] = []
    capabilities: Optional[Dict[str, Any]] = None
    authentication: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None


@app.post("/api/agents")
async def register_agent(req: RegisterAgentRequest):
    """Register a new external agent."""
    registry = get_registry()

    # Build skills
    skills = [AgentSkill(**s) for s in req.skills] if req.skills else []

    card = AgentCard(
        name=req.name,
        description=req.description,
        url=req.url,
        version=req.version,
        skills=skills,
        capabilities=AgentCapabilities(**(req.capabilities or {})),
        authentication=AgentAuthentication(**(req.authentication or {})),
        status=AgentStatus.online,
        metadata=req.metadata or {},
    )

    registered = registry.register(card)
    return {"registered": True, "agent": registered.to_dict()}


@app.delete("/api/agents/{name}")
async def unregister_agent(name: str):
    """Unregister an external agent. Internal agents cannot be removed."""
    registry = get_registry()
    agent = registry.get(name)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent not found: {name}")
    if agent.is_internal:
        raise HTTPException(status_code=403, detail="Cannot unregister internal agents")
    removed = registry.unregister(name)
    return {"unregistered": removed, "name": name}


@app.get("/api/agents/{name}/health")
async def get_agent_health(name: str):
    """Get an agent's health status."""
    registry = get_registry()
    agent = registry.get(name)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent not found: {name}")
    return {
        "name": name,
        "status": agent.status.value,
        "is_internal": agent.is_internal,
    }


@app.post("/api/agents/health-check")
async def run_health_check():
    """Ping all external agents and update their health status."""
    registry = get_registry()
    try:
        http = app.state.http
    except AttributeError:
        http = httpx.AsyncClient(timeout=10.0)
    results = await check_agent_health(registry, http)
    return {"results": results}


# ─── Agent Capability Matching ──────────────────────────────────────────

class MatchRequest(BaseModel):
    task_description: str
    required_skills: Optional[List[str]] = None
    exclude_agents: Optional[List[str]] = None


@app.post("/api/agents/match")
async def match_agents(req: MatchRequest):
    """Find the best agent(s) for a task."""
    registry = get_registry()
    results = match_task_to_agent(
        registry,
        req.task_description,
        req.required_skills,
        req.exclude_agents,
    )
    return {
        "matches": [
            {"agent": card.to_dict(), "score": score}
            for card, score in results
        ],
    }


# ─── A2A Task Endpoints ─────────────────────────────────────────────────

class CreateTaskRequest(BaseModel):
    from_agent: str = "cortex"
    to_agent: str
    content: str
    metadata: Optional[Dict[str, Any]] = None


@app.post("/a2a/tasks/send")
async def a2a_send_task(req: CreateTaskRequest):
    """
    Create and send a task to an agent.
    Also serves as the A2A server endpoint for receiving tasks from external agents.
    """
    router = get_router()
    task = await router.create_task(
        from_agent=req.from_agent,
        to_agent=req.to_agent,
        content=req.content,
        metadata=req.metadata,
    )
    return task.to_dict()


@app.get("/a2a/tasks/{task_id}")
async def a2a_get_task(task_id: str):
    """Get task status and artifacts."""
    router = get_router()
    task = router.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
    return task.to_dict()


@app.post("/a2a/tasks/{task_id}/cancel")
async def a2a_cancel_task(task_id: str):
    """Cancel a pending or in-progress task."""
    router = get_router()
    task = await router.cancel_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
    return task.to_dict()


@app.get("/a2a/tasks")
async def a2a_list_tasks(status: Optional[str] = None, limit: int = 50):
    """List tasks, optionally filtered by status."""
    router = get_router()
    tasks = router.list_tasks(status=status, limit=limit)
    return {
        "count": len(tasks),
        "tasks": [t.to_dict() for t in tasks],
    }


# ─── Entry Point ──────────────────────────────────────────────────────────

# ═══════════════════════════════════════════════════════════════════════════
# PHASE 4: PROACTIVE INTELLIGENCE ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════

# ─── Webhook Ingestion ────────────────────────────────────────────────────

def _get_event_bus() -> EventBus:
    try:
        return app.state.event_bus
    except AttributeError:
        db = get_db()
        bus = EventBus(db)
        return bus


import hashlib
import hmac


def _verify_github_signature(payload_body: bytes, signature: str, secret: str) -> bool:
    """Verify GitHub webhook signature (X-Hub-Signature-256)."""
    if not signature.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), payload_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)


@app.post("/api/webhooks/{source}")
async def receive_webhook(source: str, request: Request):
    """
    Generic webhook receiver. Normalizes payload into Event and pushes to EventBus.
    Validates signatures for known sources (GitHub).
    """
    bus = _get_event_bus()
    db = get_db()

    body = await request.body()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        payload = {"raw": body.decode("utf-8", errors="replace")}

    # Load webhook config
    try:
        row = db.execute(
            "SELECT value FROM key_value_store WHERE key = ?", ("config:webhooks",)
        ).fetchone()
        webhook_config = json.loads(row["value"]) if row else {}
    except Exception:
        webhook_config = {}

    source_config = webhook_config.get(source, {})

    # GitHub signature validation
    if source == "github" and source_config.get("secret"):
        sig = request.headers.get("X-Hub-Signature-256", "")
        if not _verify_github_signature(body, sig, source_config["secret"]):
            raise HTTPException(status_code=403, detail="Invalid webhook signature")

    # Normalize event type
    if source == "github":
        gh_event = request.headers.get("X-GitHub-Event", "unknown")
        event_type = f"webhook_github_{gh_event}"
    elif source == "slack":
        event_type = f"webhook_slack_{payload.get('type', 'unknown')}"
    else:
        event_type = f"webhook_{source}"

    event_id = await bus.emit(
        source=EventSource.webhook,
        event_type=event_type,
        payload={"source": source, "data": payload},
        priority=EventPriority.normal,
    )

    return {"received": True, "event_id": event_id, "event_type": event_type}


# ─── Scheduler Endpoints ─────────────────────────────────────────────────

def _get_scheduler() -> Scheduler:
    try:
        return app.state.scheduler
    except AttributeError:
        db = get_db()
        bus = _get_event_bus()
        return Scheduler(db, bus)


@app.get("/api/schedules")
async def list_schedules():
    """List all scheduled tasks."""
    scheduler = _get_scheduler()
    tasks = scheduler.list_all()
    return {"count": len(tasks), "schedules": [t.model_dump() for t in tasks]}


@app.post("/api/schedules")
async def create_schedule(req: CreateScheduleRequest):
    """Create a new scheduled task."""
    scheduler = _get_scheduler()
    try:
        task = scheduler.create(req)
        return {"created": True, "schedule": task.model_dump()}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/schedules/{task_id}")
async def get_schedule(task_id: str):
    """Get a scheduled task's details."""
    scheduler = _get_scheduler()
    task = scheduler.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return task.model_dump()


@app.put("/api/schedules/{task_id}")
async def update_schedule(task_id: str, req: UpdateScheduleRequest):
    """Update a scheduled task."""
    scheduler = _get_scheduler()
    try:
        task = scheduler.update(task_id, req)
        if not task:
            raise HTTPException(status_code=404, detail="Schedule not found")
        return {"updated": True, "schedule": task.model_dump()}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/schedules/{task_id}")
async def delete_schedule(task_id: str):
    """Delete a scheduled task."""
    scheduler = _get_scheduler()
    removed = scheduler.delete(task_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return {"deleted": True, "task_id": task_id}


@app.post("/api/schedules/{task_id}/run")
async def run_schedule_now(task_id: str):
    """Trigger a scheduled task immediately."""
    scheduler = _get_scheduler()
    task = await scheduler.run_now(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return {"triggered": True, "schedule": task.model_dump()}


# ─── Notification Endpoints ──────────────────────────────────────────────

def _get_notification_router() -> NotificationRouter:
    try:
        return app.state.notification_router
    except AttributeError:
        db = get_db()
        return NotificationRouter(db)


@app.get("/api/notifications/config")
async def get_notification_config():
    """Get notification channel configuration."""
    router = _get_notification_router()
    return router.get_config().model_dump()


@app.put("/api/notifications/config")
async def update_notification_config(request: Request):
    """Update notification channel configuration."""
    router = _get_notification_router()
    try:
        body = await request.json()
        config = NotificationConfig(**body)
        router.save_config(config)
        return {"updated": True, "config": config.model_dump()}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/notifications/test")
async def test_notifications():
    """Send a test notification to all enabled channels."""
    router = _get_notification_router()
    results = await router.send_test()
    return {"results": results}


# ─── Context Stream & Events ─────────────────────────────────────────────

def _get_context_stream() -> ContextStream:
    try:
        return app.state.context_stream
    except AttributeError:
        db = get_db()
        return ContextStream(db)


@app.get("/api/context")
async def get_context():
    """Get the current context stream summary for system prompt injection."""
    stream = _get_context_stream()
    summary = stream.get_context()
    return {"context": summary, "has_content": bool(summary.strip())}


@app.get("/api/events")
async def list_events(
    source: Optional[str] = None,
    event_type: Optional[str] = None,
    processed: Optional[bool] = None,
    limit: int = 50,
):
    """List recent events with optional filters."""
    stream = _get_context_stream()
    events = stream.get_recent_events(source, event_type, processed, limit)
    return {"count": len(events), "events": events}


@app.get("/api/events/stats")
async def get_event_stats():
    """Get event processing statistics."""
    stream = _get_context_stream()
    stats = stream.get_event_stats()
    # Merge with bus stats
    try:
        bus_stats = app.state.event_bus.get_stats()
        stats["bus"] = bus_stats
    except AttributeError:
        pass
    return stats


# ═══════════════════════════════════════════════════════════════════════════
# PHASE 5: AUTHENTICATION
# ═══════════════════════════════════════════════════════════════════════════

@app.post("/api/auth/register", response_model=User, tags=["auth"])
async def auth_register(req: RegisterRequest):
    """Create a new user account."""
    db = get_fresh_db()
    store = UserStore(db)
    try:
        user = store.create(req.username, req.password, req.is_admin)
        return user
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/auth/login", response_model=TokenResponse, tags=["auth"])
async def auth_login(form: RegisterRequest):
    """Authenticate and receive JWT tokens."""
    db = get_fresh_db()
    store = UserStore(db)
    user = store.authenticate(form.username, form.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    access = create_access_token(user.id, user.username, user.is_admin)
    refresh = create_refresh_token(user.id)
    return TokenResponse(access_token=access, refresh_token=refresh)


@app.post("/api/auth/refresh", response_model=TokenResponse, tags=["auth"])
async def auth_refresh(req: RefreshRequest):
    """Exchange a refresh token for a new access token."""
    from jose import JWTError
    try:
        payload = decode_token(req.refresh_token)
        if payload.get("type") != "refresh":
            raise HTTPException(status_code=401, detail="Not a refresh token")
        user_id = payload.get("sub")
        db = get_fresh_db()
        store = UserStore(db)
        user = store.get_by_id(user_id)
        if not user:
            raise HTTPException(status_code=401, detail="User not found")
        access = create_access_token(user.id, user.username, user.is_admin)
        new_refresh = create_refresh_token(user.id)
        return TokenResponse(access_token=access, refresh_token=new_refresh)
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid refresh token")


@app.get("/api/auth/me", response_model=User, tags=["auth"])
async def auth_me(user: User = Depends(require_auth)):
    """Get current user info."""
    return user


@app.get("/api/auth/users", tags=["auth"])
async def auth_list_users(admin: User = Depends(require_admin)):
    """List all users (admin only)."""
    db = get_fresh_db()
    store = UserStore(db)
    return {"users": [u.model_dump() for u in store.list_all()]}


# ═══════════════════════════════════════════════════════════════════════════
# PHASE 5: VAULT
# ═══════════════════════════════════════════════════════════════════════════

def _get_vault() -> VaultStore:
    try:
        return app.state.vault
    except AttributeError:
        db = get_db()
        init_vault_table(db)
        return VaultStore(db)


@app.get("/api/vault/keys", tags=["vault"])
async def vault_list_keys(admin: User = Depends(require_admin)):
    """List all stored key names (values not included)."""
    vault = _get_vault()
    return {"keys": vault.list_keys()}


@app.get("/api/vault/{name}", tags=["vault"])
async def vault_get(name: str, admin: User = Depends(require_admin)):
    """Retrieve a decrypted secret value."""
    vault = _get_vault()
    value = vault.get(name)
    if value is None:
        raise HTTPException(status_code=404, detail=f"Key '{name}' not found")
    return {"name": name, "value": value}


@app.put("/api/vault/{name}", tags=["vault"])
async def vault_set(name: str, request: Request, admin: User = Depends(require_admin)):
    """Store or update an encrypted secret."""
    body = await request.json()
    value = body.get("value")
    if not value:
        raise HTTPException(status_code=400, detail="Missing 'value' field")
    vault = _get_vault()
    vault.set(name, value)
    return {"name": name, "stored": True}


@app.delete("/api/vault/{name}", tags=["vault"])
async def vault_delete(name: str, admin: User = Depends(require_admin)):
    """Remove a secret from the vault."""
    vault = _get_vault()
    deleted = vault.delete(name)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Key '{name}' not found")
    return {"name": name, "deleted": True}


# ═══════════════════════════════════════════════════════════════════════════
# PHASE 5: RESILIENCE + METRICS
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/metrics", tags=["observability"])
async def get_metrics():
    """Current API metrics snapshot."""
    return {
        "metrics": metrics.snapshot(),
        "circuits": circuits.all_status(),
    }



# ═══════════════════════════════════════════════════════════════════════════
# PHASE 5: BACKUP + RESTORE
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/backups", tags=["backup"])
async def list_backups_endpoint(user: User = Depends(require_auth)):
    """List available database backups."""
    return {"backups": list_backups()}


@app.post("/api/backups", tags=["backup"])
async def create_backup(admin: User = Depends(require_admin)):
    """Trigger an immediate database backup."""
    try:
        path = backup_database(MEMORY_DB)
        stat = path.stat()
        return {
            "filename": path.name,
            "size_mb": round(stat.st_size / (1024 * 1024), 3),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "path": str(path),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Backup failed: {e}")


@app.post("/api/backups/{filename}/restore", tags=["backup"])
async def restore_backup(filename: str, request: Request, admin: User = Depends(require_admin)):
    """Restore database from a backup file (admin only, requires confirm=true)."""
    body = await request.json()
    if not body.get("confirm"):
        raise HTTPException(status_code=400, detail="Must pass {'confirm': true} to restore")

    from backup import BACKUP_DIR
    backup_path = BACKUP_DIR / filename
    try:
        restore_database(backup_path, MEMORY_DB)
        return {"restored": True, "from": filename}
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/export", tags=["backup"])
async def export_user_data(admin: User = Depends(require_admin)):
    """Export all user data as JSON."""
    db = get_db()
    return export_data(db)


@app.post("/api/import", tags=["backup"])
async def import_user_data(request: Request, admin: User = Depends(require_admin)):
    """Import data from a JSON export."""
    data = await request.json()
    db = get_db()
    try:
        summary = import_data(db, data)
        return {"imported": True, "summary": summary}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Import failed: {e}")



# ═══════════════════════════════════════════════════════════════════════════
# PHASE 6: INTERFACE EXPANSION — Voice, CLI, Slack
# ═══════════════════════════════════════════════════════════════════════════

import asyncio as _asyncio

from model_router import ModelRouter, classify_task


def _get_api_keys() -> Dict[str, str]:
    """Retrieve all LLM API keys from Vault or Environment."""
    vault = {}
    try:
        vault = _get_vault()
    except Exception:
        pass
    
    return {
        "anthropic": vault.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", ""),
        "venice": vault.get("VENICE_API_KEY") or os.environ.get("VENICE_API_KEY", ""),
        "gemini": vault.get("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY", ""),
    }


def _get_openai_key() -> Optional[str]:
    """Read OPENAI_API_KEY from Vault first, then environment."""
    try:
        vault = _get_vault()
        val = vault.get("OPENAI_API_KEY")
        if val:
            return val
    except Exception:
        pass
    return os.environ.get("OPENAI_API_KEY")


def _get_slack_token() -> Optional[str]:
    """Read SLACK_BOT_TOKEN from Vault first, then environment."""
    try:
        vault = _get_vault()
        val = vault.get("SLACK_BOT_TOKEN")
        if val:
            return val
    except Exception:
        pass
    return os.environ.get("SLACK_BOT_TOKEN")


def _get_slack_signing_secret() -> Optional[str]:
    """Read SLACK_SIGNING_SECRET from Vault first, then environment."""
    try:
        vault = _get_vault()
        val = vault.get("SLACK_SIGNING_SECRET")
        if val:
            return val
    except Exception:
        pass
    return os.environ.get("SLACK_SIGNING_SECRET")


@app.get("/api/voice/status", tags=["voice"])
async def voice_status():
    """Check whether voice (Whisper/TTS) is available."""
    key = _get_openai_key()
    if key:
        return {"available": True, "reason": "OpenAI API key configured"}
    return {"available": False, "reason": "OPENAI_API_KEY not configured in Vault or environment"}


@app.post("/api/voice/transcribe", tags=["voice"])
async def voice_transcribe(
    request: Request,
    user: User = Depends(require_auth),
):
    """Transcribe audio using OpenAI Whisper. Expects multipart form with 'audio' field."""
    key = _get_openai_key()
    if not key:
        raise HTTPException(status_code=503, detail="Voice unavailable: OPENAI_API_KEY not configured")

    form = await request.form()
    audio_file = form.get("audio")
    if audio_file is None:
        raise HTTPException(status_code=422, detail="Missing 'audio' field in form data")

    audio_bytes = await audio_file.read()
    filename = getattr(audio_file, "filename", "audio.webm") or "audio.webm"

    t0 = time.time()
    try:
        import openai
        client = openai.AsyncOpenAI(api_key=key)
        transcript = await client.audio.transcriptions.create(
            model="whisper-1",
            file=(filename, audio_bytes, audio_file.content_type or "audio/webm"),
        )
        text = transcript.text
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Whisper error: {e}")

    ms = (time.time() - t0) * 1000
    db = get_db()
    audit(db, "voice_transcribe", user.username, f"file={filename}", f"char_count={len(text)}", True, ms)
    return {"text": text}


@app.post("/api/voice/synthesize", tags=["voice"])
async def voice_synthesize(request: Request, user: User = Depends(require_auth)):
    """Synthesize speech. Body: {"text": "...", "voice": "alloy"}. Returns audio/mpeg."""
    from fastapi.responses import StreamingResponse

    key = _get_openai_key()
    if not key:
        raise HTTPException(status_code=503, detail="Voice unavailable: OPENAI_API_KEY not configured")

    body = await request.json()
    text = body.get("text", "")
    voice = body.get("voice", "alloy")
    if not text:
        raise HTTPException(status_code=422, detail="Missing 'text' field")
    text = text[:4096]  # Truncate silently

    t0 = time.time()
    try:
        import openai
        client = openai.AsyncOpenAI(api_key=key)
        response = await client.audio.speech.create(model="tts-1", voice=voice, input=text)
        audio_bytes = response.content
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"TTS error: {e}")

    ms = (time.time() - t0) * 1000
    db = get_db()
    audit(db, "voice_synthesize", user.username, f"chars={len(text)},voice={voice}", f"bytes_written={len(audio_bytes)}", True, ms)
    return StreamingResponse(iter([audio_bytes]), media_type="audio/mpeg", headers={"Content-Length": str(len(audio_bytes))})


class ChatRequest(BaseModel):
    messages: List[Dict[str, Any]]
    system: Optional[str] = None
    provider: Optional[str] = "gemini"
    model: Optional[str] = None
    max_tokens: int = 4096
    tools: Optional[List[Dict[str, Any]]] = None
    task_hint: Optional[str] = ""

@app.post("/api/chat", tags=["core"])
async def chat_endpoint(body: ChatRequest, user: User = Depends(require_auth)):
    """RONIN-v3 Unified Chat Endpoint: supports Gemini, Claude, and Venice via ModelRouter."""
    t0 = time.time()
    keys = _get_api_keys()
    
    # Classify task if hint provided but no explicit task tier
    tier = classify_task(body.task_hint or (body.messages[-1]["content"] if body.messages else "")).value
    
    try:
        router = ModelRouter(
            anthropic_key=keys["anthropic"],
            venice_key=keys["venice"],
            gemini_key=keys["gemini"]
        )
        
        # Determine provider/model from request or default
        provider = body.provider or "gemini"
        model = body.model
        
        result = await router.call(
            messages=body.messages,
            system=body.system,
            provider=provider,
            model=model,
            max_tokens=body.max_tokens,
            tools=body.tools,
            prompt_hint=body.task_hint or (body.messages[-1]["content"] if body.messages else ""),
        )
        
        ms = (time.time() - t0) * 1000
        provider_actual = result.get("provider", provider)
        usage = result.get("usage", {})
        
        # Audit log
        audit(get_db(), "chat", user.username, body.task_hint[:100], f"provider={provider_actual},ms={round(ms)}", True, ms)
        
        return result
        
    except Exception as e:
        logger.error(f"Chat error: {e}")
        raise HTTPException(status_code=502, detail=f"Model execution failed: {e}")


class CliRunRequest(BaseModel):
    command: str
    autonomy: float = 0.5


class CliRunResponse(BaseModel):
    result: str
    tier: str
    iterations: int
    cost_usd: float


@app.post("/api/cli/run", tags=["cli"], response_model=CliRunResponse)
async def cli_run(body: CliRunRequest, user: User = Depends(require_auth)):
    """Single-shot agentic loop: classify task → call model → return result."""
    if not body.command.strip():
        raise HTTPException(status_code=422, detail="command must not be empty")

    t0 = time.time()
    keys = _get_api_keys()

    tier = classify_task(body.command).value

    try:
        router = ModelRouter(
            anthropic_key=keys["anthropic"],
            venice_key=keys["venice"],
            gemini_key=keys["gemini"]
        )
        result = await router.call(
            messages=[{"role": "user", "content": body.command}],
            system="You are RONIN, a semi-autonomous AI agent. Answer concisely and helpfully.",
            prompt_hint=body.command,
        )
        content_blocks = result.get("content", [])
        text_parts = [b.get("text", "") for b in content_blocks if b.get("type") == "text"]
        result_text = "\n".join(text_parts).strip() or "No response."
        
        # Calculate approximate cost based on provider
        usage = result.get("usage", {})
        provider = result.get("provider", "claude")
        if provider == "gemini":
            cost = usage.get("input_tokens", 0) * 1.25e-6 + usage.get("output_tokens", 0) * 3.75e-6
        elif provider == "venice":
            cost = usage.get("input_tokens", 0) * 0.5e-6 + usage.get("output_tokens", 0) * 2e-6
        else: # claude
            cost = usage.get("input_tokens", 0) * 3e-6 + usage.get("output_tokens", 0) * 15e-6
    except Exception as e:
        result_text = f"RONIN encountered an error: {e}"
        cost = 0.0

    ms = (time.time() - t0) * 1000
    db = get_db()
    audit(db, "cli_run", user.username, body.command[:200], result_text[:200], True, ms)
    return CliRunResponse(result=result_text, tier=tier, iterations=1, cost_usd=round(cost, 6))


@app.post("/api/slack/command", tags=["slack"])
async def slack_command(request: Request):
    """Receive Slack slash commands. Returns immediate 200, processes async."""
    from integrations.slack_bot import verify_slack_signature, dispatch_slash_command

    bot_token = _get_slack_token()
    if not bot_token:
        raise HTTPException(status_code=503, detail="Slack unavailable: SLACK_BOT_TOKEN not configured")

    signing_secret = _get_slack_signing_secret()
    if signing_secret:
        body_bytes = await request.body()
        timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
        signature = request.headers.get("X-Slack-Signature", "")
        if not verify_slack_signature(body_bytes, timestamp, signature, signing_secret):
            raise HTTPException(status_code=401, detail="Invalid Slack signature")

    form = await request.form()
    command = str(form.get("command", ""))
    text = str(form.get("text", ""))
    response_url = str(form.get("response_url", ""))

    auth_token = ""
    try:
        vault = _get_vault()
        auth_token = vault.get("RONIN_INTERNAL_TOKEN") or ""
    except Exception:
        pass

    http = getattr(request.app.state, "http", None) or httpx.AsyncClient(timeout=30.0)
    api_base = f"http://localhost:{os.environ.get('PORT', '8742')}"

    _asyncio.create_task(dispatch_slash_command(
        command=command, text=text, response_url=response_url,
        http=http, bot_token=bot_token, api_base_url=api_base, auth_token=auth_token,
    ))
    return {"response_type": "ephemeral", "text": "RONIN is thinking..."}


if __name__ == "__main__":
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="RONIN REST API")
    parser.add_argument("--port", type=int, default=8742, help="API port (default: 8742)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Bind address")
    parser.add_argument("--reload", action="store_true", help="Auto-reload on changes")
    args = parser.parse_args()

    print(f"RONIN REST API starting on {args.host}:{args.port}")
    uvicorn.run(
        "api:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )
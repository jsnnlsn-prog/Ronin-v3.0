"""
RONIN A2A Protocol — Agent-to-Agent Communication
====================================================
Message passing and task lifecycle for inter-agent communication.
Aligned with Google/Linux Foundation A2A v0.3 (opinionated subset).

Internal routing: internal:// → tool executor (no HTTP)
External routing: http(s):// → POST to agent's /a2a/tasks/send endpoint
"""

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

import httpx
from pydantic import BaseModel, Field

from agent_cards import AgentCard, AgentRegistry, AgentStatus


# ═══════════════════════════════════════════════════════════════════════════
# MODELS
# ═══════════════════════════════════════════════════════════════════════════

class MessageType(str, Enum):
    task_request = "task_request"
    task_response = "task_response"
    status_update = "status_update"
    error = "error"


class TaskStatus(str, Enum):
    pending = "pending"
    in_progress = "in_progress"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class ContentPart(BaseModel):
    """A single piece of content in a message."""
    type: str = Field(..., description="Content type: text, json, file")
    data: Any = Field(..., description="The content data")


class A2AMessage(BaseModel):
    """A single message in an agent-to-agent conversation."""
    message_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str = Field(..., description="Groups related messages into a task lifecycle")
    from_agent: str = Field(..., description="Sending agent name")
    to_agent: str = Field(..., description="Receiving agent name")
    type: MessageType = Field(MessageType.task_request)
    content: List[ContentPart] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def text_content(self) -> str:
        """Extract all text parts concatenated."""
        return "\n".join(p.data for p in self.content if p.type == "text" and isinstance(p.data, str))


class TaskArtifact(BaseModel):
    """Output data produced by a task."""
    type: str = Field("text", description="Artifact type: text, json, file")
    name: str = Field("", description="Artifact identifier")
    data: Any = Field(None, description="The artifact data")


class A2ATask(BaseModel):
    """A task lifecycle tracking messages between agents."""
    task_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    status: TaskStatus = Field(TaskStatus.pending)
    from_agent: str = Field(...)
    to_agent: str = Field(...)
    messages: List[A2AMessage] = Field(default_factory=list)
    artifacts: List[TaskArtifact] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "A2ATask":
        return cls.model_validate(data)


# ═══════════════════════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════════════════════

def init_a2a_tables(db: sqlite3.Connection) -> None:
    """Add A2A tables to existing database."""
    db.executescript("""
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
    """)
    db.commit()


# ═══════════════════════════════════════════════════════════════════════════
# INTERNAL AGENT EXECUTOR
# ═══════════════════════════════════════════════════════════════════════════

# Maps internal agent skills to MCP tool names for direct execution.
# When Cortex delegates to an internal agent, we resolve the skill to a tool.
SKILL_TO_TOOL: Dict[str, str] = {
    # Scout skills
    "web_fetch": "ronin_web_fetch",
    "information_extraction": "ronin_web_fetch",  # Same tool, different prompt framing
    # Forge skills
    "code_exec": "ronin_code_exec",
    "shell_exec": "ronin_shell_exec",
    "file_write": "ronin_file_write",
    "file_read": "ronin_file_read",
    "file_list": "ronin_file_list",
    # Aegis skills
    "safety_check": "ronin_safety_check",
    # Prism, Echo, Cortex skills don't map directly to tools —
    # they're reasoning tasks handled by the LLM with a specialized prompt.
}


class InternalExecutionResult(BaseModel):
    """Result from executing a task on an internal agent."""
    success: bool
    result: Any = None
    error: Optional[str] = None


# ═══════════════════════════════════════════════════════════════════════════
# ROUTER
# ═══════════════════════════════════════════════════════════════════════════

class A2ARouter:
    """
    Routes messages between agents.

    - internal:// → resolves via SKILL_TO_TOOL and calls the tool executor
    - http(s):// → POSTs to the external agent's /a2a/tasks/send endpoint
    """

    def __init__(self, db: sqlite3.Connection, registry: AgentRegistry, http_client: Optional[httpx.AsyncClient] = None):
        self.db = db
        self.registry = registry
        self.http = http_client
        init_a2a_tables(db)
        self._tool_executor = None  # Set by the API layer at startup

    def set_tool_executor(self, executor):
        """
        Set the function that executes MCP tools.
        Signature: async executor(tool_name: str, params: dict) -> str (JSON)
        """
        self._tool_executor = executor

    def _save_task(self, task: A2ATask) -> None:
        """Persist task to SQLite."""
        now = datetime.now(timezone.utc).isoformat()
        task.updated_at = now
        self.db.execute(
            "INSERT OR REPLACE INTO a2a_tasks (task_id, task_json, status, from_agent, to_agent, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (task.task_id, json.dumps(task.to_dict()), task.status.value,
             task.from_agent, task.to_agent, task.created_at, now),
        )
        self.db.commit()

    def _load_task(self, task_id: str) -> Optional[A2ATask]:
        """Load task from SQLite."""
        row = self.db.execute(
            "SELECT task_json FROM a2a_tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if not row:
            return None
        return A2ATask.from_dict(json.loads(row["task_json"]))

    async def create_task(
        self,
        from_agent: str,
        to_agent: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> A2ATask:
        """
        Create a new task and send the initial message.
        Returns the task with status updated based on routing result.
        """
        task = A2ATask(
            from_agent=from_agent,
            to_agent=to_agent,
        )

        msg = A2AMessage(
            task_id=task.task_id,
            from_agent=from_agent,
            to_agent=to_agent,
            type=MessageType.task_request,
            content=[ContentPart(type="text", data=content)],
            metadata=metadata or {},
        )

        task.messages.append(msg)
        task.status = TaskStatus.pending
        self._save_task(task)

        # Route the message
        result = await self.send_message(msg)

        # Update task based on result
        if result:
            task.status = TaskStatus.completed
            task.artifacts.append(TaskArtifact(
                type="json" if isinstance(result, dict) else "text",
                name="result",
                data=result,
            ))

            # Add response message
            response_msg = A2AMessage(
                task_id=task.task_id,
                from_agent=to_agent,
                to_agent=from_agent,
                type=MessageType.task_response,
                content=[ContentPart(type="json" if isinstance(result, dict) else "text", data=result)],
            )
            task.messages.append(response_msg)
        else:
            task.status = TaskStatus.failed

        self._save_task(task)
        return task

    async def send_message(self, msg: A2AMessage) -> Any:
        """
        Route a message to the correct agent.
        Returns the response data or None on failure.
        """
        agent = self.registry.get(msg.to_agent)
        if not agent:
            return None

        if agent.is_internal:
            return await self._route_internal(msg, agent)
        else:
            return await self._route_external(msg, agent)

    async def _route_internal(self, msg: A2AMessage, agent: AgentCard) -> Any:
        """
        Route to an internal agent by executing the appropriate MCP tool.
        For reasoning-only agents (Prism, Echo, Cortex), return a structured
        acknowledgment — the actual reasoning happens in the LLM agentic loop.
        """
        text = msg.text_content()

        # Check if any skill maps to a tool we can execute directly
        if self._tool_executor and agent.skills:
            for skill in agent.skills:
                tool_name = SKILL_TO_TOOL.get(skill.id)
                if tool_name:
                    # Try to extract tool params from message metadata
                    params = msg.metadata.get("tool_params", {})
                    if params:
                        try:
                            result = await self._tool_executor(tool_name, params)
                            return json.loads(result) if isinstance(result, str) else result
                        except Exception as e:
                            return {"error": str(e), "agent": agent.name, "skill": skill.id}

        # Reasoning-only agents — return acknowledgment
        return {
            "agent": agent.name,
            "status": "delegated",
            "message": f"Task delegated to {agent.name}: {text[:200]}",
            "requires_llm": True,
        }

    async def _route_external(self, msg: A2AMessage, agent: AgentCard) -> Any:
        """Route to an external agent via HTTP POST."""
        if not self.http:
            return {"error": "No HTTP client configured for external routing"}

        url = agent.url.rstrip("/") + "/a2a/tasks/send"
        payload = {
            "task_id": msg.task_id,
            "message": msg.model_dump(mode="json"),
        }

        headers = {"Content-Type": "application/json"}
        if agent.authentication.type == AuthType.api_key and agent.authentication.credentials:
            headers["X-API-Key"] = agent.authentication.credentials
        elif agent.authentication.type == AuthType.bearer and agent.authentication.credentials:
            headers["Authorization"] = f"Bearer {agent.authentication.credentials}"

        try:
            resp = await self.http.post(url, json=payload, headers=headers, timeout=30.0)
            if resp.status_code == 200:
                return resp.json()
            else:
                return {"error": f"External agent returned {resp.status_code}", "body": resp.text[:500]}
        except httpx.TimeoutException:
            self.registry.update_health(agent.name, AgentStatus.degraded)
            return {"error": f"Timeout connecting to {agent.name} at {url}"}
        except Exception as e:
            self.registry.update_health(agent.name, AgentStatus.offline)
            return {"error": f"Failed to reach {agent.name}: {str(e)}"}

    def get_task(self, task_id: str) -> Optional[A2ATask]:
        """Get a task by ID."""
        return self._load_task(task_id)

    def list_tasks(self, status: Optional[str] = None, limit: int = 50) -> List[A2ATask]:
        """List tasks, optionally filtered by status."""
        if status:
            rows = self.db.execute(
                "SELECT task_json FROM a2a_tasks WHERE status=? ORDER BY updated_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = self.db.execute(
                "SELECT task_json FROM a2a_tasks ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        tasks = []
        for row in rows:
            try:
                tasks.append(A2ATask.from_dict(json.loads(row["task_json"])))
            except Exception:
                pass
        return tasks

    async def cancel_task(self, task_id: str) -> Optional[A2ATask]:
        """Cancel a task if it's still pending or in progress."""
        task = self._load_task(task_id)
        if not task:
            return None
        if task.status in (TaskStatus.pending, TaskStatus.in_progress):
            task.status = TaskStatus.cancelled
            cancel_msg = A2AMessage(
                task_id=task_id,
                from_agent=task.from_agent,
                to_agent=task.to_agent,
                type=MessageType.status_update,
                content=[ContentPart(type="text", data="Task cancelled by requester")],
            )
            task.messages.append(cancel_msg)
            self._save_task(task)
        return task


# ═══════════════════════════════════════════════════════════════════════════
# HEALTH MONITOR
# ═══════════════════════════════════════════════════════════════════════════

async def check_agent_health(
    registry: AgentRegistry,
    http_client: httpx.AsyncClient,
) -> Dict[str, str]:
    """
    Ping all registered external agents and update their health status.
    Internal agents always report online.
    Returns: {agent_name: status}
    """
    results = {}
    for card in registry.list_all():
        if card.is_internal:
            registry.update_health(card.name, AgentStatus.online)
            results[card.name] = "online"
            continue

        url = card.url.rstrip("/") + "/.well-known/agent.json"
        try:
            resp = await http_client.get(url, timeout=10.0)
            if resp.status_code == 200:
                registry.update_health(card.name, AgentStatus.online)
                results[card.name] = "online"
            else:
                registry.update_health(card.name, AgentStatus.degraded)
                results[card.name] = "degraded"
        except httpx.TimeoutException:
            registry.update_health(card.name, AgentStatus.degraded)
            results[card.name] = "degraded"
        except Exception:
            registry.update_health(card.name, AgentStatus.offline)
            results[card.name] = "offline"

    return results

"""
RONIN Agent Cards — A2A Agent Identity & Registry
====================================================
Defines AgentCard schema (aligned with A2A v0.3 under Linux Foundation)
and an SQLite-backed AgentRegistry for discovering and managing agents.

Internal agents use url="internal://{agent_id}" — Cortex recognizes
the scheme and routes via the existing tool executor.
"""

import json
import sqlite3
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ═══════════════════════════════════════════════════════════════════════════
# MODELS
# ═══════════════════════════════════════════════════════════════════════════

class AgentStatus(str, Enum):
    online = "online"
    offline = "offline"
    degraded = "degraded"


class AuthType(str, Enum):
    none = "none"
    api_key = "apiKey"
    bearer = "bearer"


class AgentSkill(BaseModel):
    """A discrete capability an agent can perform."""
    id: str = Field(..., description="Unique skill identifier (e.g., 'web_fetch')")
    name: str = Field(..., description="Human-readable skill name")
    description: str = Field("", description="What this skill does")
    input_schema: Optional[Dict[str, Any]] = Field(None, description="JSON Schema for input")
    output_schema: Optional[Dict[str, Any]] = Field(None, description="JSON Schema for output")


class AgentAuthentication(BaseModel):
    """How to authenticate with this agent."""
    type: AuthType = AuthType.none
    credentials: Optional[str] = Field(None, description="API key or token (never stored in DB for external agents)")


class AgentCapabilities(BaseModel):
    """What communication modes this agent supports."""
    streaming: bool = False
    push_notifications: bool = False


class AgentCard(BaseModel):
    """
    A2A-aligned Agent Card describing an agent's identity and capabilities.

    Internal agents: url starts with 'internal://'
    External agents: url is an http(s) endpoint
    """
    name: str = Field(..., description="Unique agent name", min_length=1, max_length=100)
    description: str = Field("", description="What this agent does")
    url: str = Field(..., description="Agent endpoint (internal://id or https://...)")
    version: str = Field("1.0.0", description="Semver version")
    skills: List[AgentSkill] = Field(default_factory=list)
    capabilities: AgentCapabilities = Field(default_factory=AgentCapabilities)
    authentication: AgentAuthentication = Field(default_factory=AgentAuthentication)
    status: AgentStatus = Field(AgentStatus.online)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @property
    def is_internal(self) -> bool:
        return self.url.startswith("internal://")

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for JSON storage / API responses."""
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AgentCard":
        return cls.model_validate(data)


# ═══════════════════════════════════════════════════════════════════════════
# INTERNAL AGENT DEFINITIONS
# ═══════════════════════════════════════════════════════════════════════════

INTERNAL_AGENTS: List[AgentCard] = [
    AgentCard(
        name="cortex",
        description="Orchestrator agent — decomposes tasks, delegates to specialists, synthesizes results.",
        url="internal://cortex",
        version="3.0.0",
        skills=[
            AgentSkill(id="task_decomposition", name="Task Decomposition",
                       description="Break complex tasks into subtasks for specialist agents"),
            AgentSkill(id="agent_delegation", name="Agent Delegation",
                       description="Route subtasks to the best available agent"),
            AgentSkill(id="plan_synthesis", name="Plan Synthesis",
                       description="Combine results from multiple agents into a coherent response"),
        ],
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        metadata={"icon": "🧠", "color": "#00d4ff", "role": "Orchestrator"},
    ),
    AgentCard(
        name="scout",
        description="Research & intelligence agent — fetches web content, extracts information, performs searches.",
        url="internal://scout",
        version="3.0.0",
        skills=[
            AgentSkill(id="web_fetch", name="Web Fetch",
                       description="Fetch and parse content from URLs",
                       input_schema={"type": "object", "properties": {"url": {"type": "string"}}}),
            AgentSkill(id="information_extraction", name="Information Extraction",
                       description="Extract structured data from unstructured text"),
        ],
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        metadata={"icon": "🔍", "color": "#f59e0b", "role": "Research & Intel"},
    ),
    AgentCard(
        name="forge",
        description="Engineering agent — executes code, manages files, runs shell commands.",
        url="internal://forge",
        version="3.0.0",
        skills=[
            AgentSkill(id="code_exec", name="Code Execution",
                       description="Execute Python, JavaScript, or Bash code in a sandbox",
                       input_schema={"type": "object", "properties": {"language": {"type": "string"}, "code": {"type": "string"}}}),
            AgentSkill(id="shell_exec", name="Shell Execution",
                       description="Run shell commands in the workspace"),
            AgentSkill(id="file_write", name="File Write",
                       description="Create or update files in the workspace"),
            AgentSkill(id="file_read", name="File Read",
                       description="Read file contents from the workspace"),
            AgentSkill(id="file_list", name="File List",
                       description="List files and directories in the workspace"),
        ],
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        metadata={"icon": "⚡", "color": "#818cf8", "role": "Code & Systems"},
    ),
    AgentCard(
        name="prism",
        description="Analysis agent — evaluates data, detects patterns, performs comparisons.",
        url="internal://prism",
        version="3.0.0",
        skills=[
            AgentSkill(id="data_analysis", name="Data Analysis",
                       description="Analyze structured data and produce insights"),
            AgentSkill(id="pattern_detection", name="Pattern Detection",
                       description="Identify patterns, anomalies, and trends in data"),
            AgentSkill(id="comparison", name="Comparison",
                       description="Compare multiple data sources or options"),
        ],
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        metadata={"icon": "📊", "color": "#f43f5e", "role": "Analysis"},
    ),
    AgentCard(
        name="echo",
        description="Communication agent — generates text, summarizes content, drafts messages.",
        url="internal://echo",
        version="3.0.0",
        skills=[
            AgentSkill(id="text_generation", name="Text Generation",
                       description="Generate written content (reports, docs, creative text)"),
            AgentSkill(id="summarization", name="Summarization",
                       description="Condense long content into concise summaries"),
            AgentSkill(id="email_drafting", name="Email Drafting",
                       description="Draft professional emails and messages"),
        ],
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        metadata={"icon": "✍️", "color": "#a78bfa", "role": "Communication"},
    ),
    AgentCard(
        name="aegis",
        description="Safety agent — evaluates risk, enforces policies, checks actions before execution.",
        url="internal://aegis",
        version="3.0.0",
        skills=[
            AgentSkill(id="safety_check", name="Safety Check",
                       description="Evaluate whether an action is safe to execute",
                       input_schema={"type": "object", "properties": {"action": {"type": "string"}, "risk_level": {"type": "string"}}}),
            AgentSkill(id="risk_assessment", name="Risk Assessment",
                       description="Assess overall risk level of a plan or task"),
            AgentSkill(id="policy_enforcement", name="Policy Enforcement",
                       description="Verify compliance with security policies"),
        ],
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        metadata={"icon": "🛡️", "color": "#f97316", "role": "Safety"},
    ),
]


# ═══════════════════════════════════════════════════════════════════════════
# REGISTRY
# ═══════════════════════════════════════════════════════════════════════════

def init_agent_tables(db: sqlite3.Connection) -> None:
    """Add agent_registry table to existing database. Safe to call multiple times."""
    db.executescript("""
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
    """)
    db.commit()


class AgentRegistry:
    """
    In-memory + SQLite-backed agent registry.

    Internal agents are pre-loaded on init. External agents persist in SQLite.
    """

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        init_agent_tables(db)
        self._cache: Dict[str, AgentCard] = {}
        self._load_from_db()
        self._ensure_internal_agents()

    def _load_from_db(self):
        """Load all agents from SQLite into memory cache."""
        rows = self.db.execute("SELECT name, card_json FROM agent_registry").fetchall()
        for row in rows:
            try:
                card = AgentCard.from_dict(json.loads(row["card_json"]))
                self._cache[card.name] = card
            except Exception:
                pass  # Skip malformed entries

    def _ensure_internal_agents(self):
        """Register all internal agents if not already present."""
        now = datetime.now(timezone.utc).isoformat()
        for card in INTERNAL_AGENTS:
            if card.name not in self._cache:
                self.db.execute(
                    "INSERT OR REPLACE INTO agent_registry (name, card_json, is_internal, registered_at, health_status) VALUES (?,?,?,?,?)",
                    (card.name, json.dumps(card.to_dict()), 1, now, "online"),
                )
                self._cache[card.name] = card
            else:
                # Update internal agent definitions (they may have changed)
                existing = self._cache[card.name]
                if existing.is_internal:
                    self.db.execute(
                        "UPDATE agent_registry SET card_json=? WHERE name=? AND is_internal=1",
                        (json.dumps(card.to_dict()), card.name),
                    )
                    self._cache[card.name] = card
        self.db.commit()

    def register(self, card: AgentCard) -> AgentCard:
        """Register or update an agent."""
        now = datetime.now(timezone.utc).isoformat()
        is_internal = 1 if card.is_internal else 0
        self.db.execute(
            "INSERT OR REPLACE INTO agent_registry (name, card_json, is_internal, registered_at, health_status) VALUES (?,?,?,?,?)",
            (card.name, json.dumps(card.to_dict()), is_internal, now, card.status.value),
        )
        self.db.commit()
        self._cache[card.name] = card
        return card

    def unregister(self, agent_name: str) -> bool:
        """Remove an agent. Returns True if found and removed."""
        if agent_name not in self._cache:
            return False
        # Don't allow unregistering internal agents
        if self._cache[agent_name].is_internal:
            return False
        self.db.execute("DELETE FROM agent_registry WHERE name=? AND is_internal=0", (agent_name,))
        self.db.commit()
        del self._cache[agent_name]
        return True

    def get(self, name: str) -> Optional[AgentCard]:
        """Get a specific agent card."""
        return self._cache.get(name)

    def list_all(self) -> List[AgentCard]:
        """Return all registered agents."""
        return list(self._cache.values())

    def find_by_skill(self, skill_id: str) -> List[AgentCard]:
        """Find agents that have a specific skill."""
        return [
            card for card in self._cache.values()
            if any(s.id == skill_id for s in card.skills)
        ]

    def find_by_capability(self, capability: str) -> List[AgentCard]:
        """Filter agents by a capability flag (e.g., 'streaming')."""
        return [
            card for card in self._cache.values()
            if getattr(card.capabilities, capability, False)
        ]

    def update_health(self, agent_name: str, status: AgentStatus) -> bool:
        """Update an agent's health status."""
        if agent_name not in self._cache:
            return False
        card = self._cache[agent_name]
        card.status = status
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            "UPDATE agent_registry SET health_status=?, last_health_check=?, card_json=? WHERE name=?",
            (status.value, now, json.dumps(card.to_dict()), agent_name),
        )
        self.db.commit()
        return True

    def get_system_card(self) -> AgentCard:
        """
        Generate the RONIN system Agent Card for /.well-known/agent.json.
        Cortex is the public-facing agent, listing all delegatable skills.
        """
        all_skills = []
        for card in self._cache.values():
            if card.name != "cortex":
                for skill in card.skills:
                    all_skills.append(AgentSkill(
                        id=f"{card.name}.{skill.id}",
                        name=f"{card.metadata.get('role', card.name)}: {skill.name}",
                        description=skill.description,
                        input_schema=skill.input_schema,
                        output_schema=skill.output_schema,
                    ))

        return AgentCard(
            name="ronin",
            description="RONIN v3.0 — Semi-autonomous AI agent system with 6 specialist agents. "
                        "Cortex orchestrates task decomposition and delegation.",
            url="http://localhost:8742",
            version="3.0.0",
            skills=all_skills,
            capabilities=AgentCapabilities(streaming=False, push_notifications=False),
            authentication=AgentAuthentication(type=AuthType.none),
            status=AgentStatus.online,
            metadata={
                "agents": [c.name for c in self._cache.values()],
                "protocol": "a2a-v0.3-subset",
            },
        )

"""
RONIN Model Router — Dual-Provider Intelligence Layer
========================================================
Routes requests between Claude (Anthropic direct) and Venice AI based on:
  1. Task type (tool-use, orchestration, simple completion)
  2. Privacy requirements (sensitive client data → Venice)
  3. Cost optimization (sub-agent grunt work → Venice)
  4. Safety tier (TT-SI simulation → always Claude)

Venice uses OpenAI-compatible API, so we normalize both into a common interface.

Architecture:
  Cortex (orchestrator) → always Claude
  TT-SI (pre-flight)   → always Claude
  Sub-agents (leaf)     → Venice by default, Claude for tool-use
  Bulk/privacy work     → Venice
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("RoninRouter")

# ─── CONFIGURATION ──────────────────────────────────────────────────────────

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
VENICE_API_URL = "https://api.venice.ai/api/v1/chat/completions"

# Model IDs
CLAUDE_SONNET = "claude-sonnet-4-20250514"
CLAUDE_OPUS = "claude-opus-4-20250514"
VENICE_GLM = "zai-org-glm-4.7"
VENICE_DEEPSEEK_R1 = "deepseek-ai-DeepSeek-R1"
VENICE_QWEN = "qwen3-4b"
VENICE_CLAUDE_SONNET = "claude-sonnet-4-20250514"  # Claude via Venice proxy


class Provider(str, Enum):
    CLAUDE = "claude"
    VENICE = "venice"


class TaskTier(str, Enum):
    """Determines routing priority."""
    ORCHESTRATOR = "orchestrator"   # Cortex — always Claude
    TTSI = "ttsi"                   # Pre-flight sim — always Claude
    TOOL_USE = "tool_use"           # Agentic loop — Claude (tool calling)
    SAFETY = "safety"               # Aegis checks — Claude
    REASONING = "reasoning"         # Deep analysis — Claude or Venice R1
    GENERATION = "generation"       # Writing, drafting — Venice
    SIMPLE = "simple"               # Summaries, formatting — Venice
    PRIVACY = "privacy"             # Sensitive data — Venice (zero retention)
    BULK = "bulk"                   # Batch processing — Venice


# ─── ROUTING TABLE ──────────────────────────────────────────────────────────

ROUTING_TABLE: Dict[TaskTier, Dict] = {
    TaskTier.ORCHESTRATOR: {
        "provider": Provider.CLAUDE,
        "model": CLAUDE_SONNET,
        "reason": "Orchestrator needs strongest tool-use and reasoning",
        "allow_override": False,
    },
    TaskTier.TTSI: {
        "provider": Provider.CLAUDE,
        "model": CLAUDE_SONNET,
        "reason": "TT-SI simulation requires highest reasoning fidelity",
        "allow_override": False,
    },
    TaskTier.TOOL_USE: {
        "provider": Provider.CLAUDE,
        "model": CLAUDE_SONNET,
        "reason": "Multi-turn tool calling is most reliable on Claude",
        "allow_override": False,
    },
    TaskTier.SAFETY: {
        "provider": Provider.CLAUDE,
        "model": CLAUDE_SONNET,
        "reason": "Safety evaluation must not be cost-optimized",
        "allow_override": False,
    },
    TaskTier.REASONING: {
        "provider": Provider.CLAUDE,
        "model": CLAUDE_SONNET,
        "reason": "Deep reasoning benefits from Claude, can fallback to Venice R1",
        "allow_override": True,
        "fallback_model": VENICE_DEEPSEEK_R1,
        "fallback_provider": Provider.VENICE,
    },
    TaskTier.GENERATION: {
        "provider": Provider.VENICE,
        "model": VENICE_GLM,
        "reason": "Content generation is cost-effective on Venice",
        "allow_override": True,
    },
    TaskTier.SIMPLE: {
        "provider": Provider.VENICE,
        "model": VENICE_QWEN,
        "reason": "Simple tasks use cheapest available model",
        "allow_override": True,
    },
    TaskTier.PRIVACY: {
        "provider": Provider.VENICE,
        "model": VENICE_GLM,
        "reason": "Venice has zero data retention for sensitive work",
        "allow_override": False,
    },
    TaskTier.BULK: {
        "provider": Provider.VENICE,
        "model": VENICE_QWEN,
        "reason": "Bulk processing optimized for cost",
        "allow_override": True,
    },
}


# ─── TASK CLASSIFIER ───────────────────────────────────────────────────────

# Keywords that signal each tier — checked in priority order
TIER_SIGNALS: List[tuple] = [
    # (tier, keyword_patterns, requires_all)
    (TaskTier.PRIVACY, [
        "insurance", "client", "confidential", "medical", "ssn", "private",
        "hipaa", "sensitive", "personal data", "biohazard", "report",
    ], False),
    (TaskTier.SAFETY, [
        "safety_check", "risk", "dangerous", "delete", "destroy", "rm -rf",
    ], False),
    (TaskTier.TOOL_USE, [
        "ronin_shell_exec", "ronin_code_exec", "ronin_file_write",
        "ronin_web_fetch", "tool_use", "execute", "run this",
    ], False),
    (TaskTier.REASONING, [
        "analyze", "compare", "evaluate", "trade-off", "architecture",
        "design", "debug", "diagnose", "why does", "root cause",
    ], False),
    (TaskTier.GENERATION, [
        "write", "draft", "compose", "email", "blog", "proposal",
        "document", "letter", "summarize", "rewrite",
    ], False),
    (TaskTier.SIMPLE, [
        "format", "convert", "translate", "list", "define", "what is",
        "how to", "explain briefly",
    ], False),
]


def classify_task(
    prompt: str,
    has_tools: bool = False,
    is_orchestrator: bool = False,
    is_ttsi: bool = False,
    force_privacy: bool = False,
    force_tier: Optional[TaskTier] = None,
) -> TaskTier:
    """
    Classify a task into a routing tier based on signals.
    
    Priority order:
    1. Explicit overrides (force_tier, is_orchestrator, is_ttsi)
    2. Privacy flag
    3. Tool presence
    4. Keyword matching
    5. Default to GENERATION
    """
    if force_tier:
        return force_tier
    if is_orchestrator:
        return TaskTier.ORCHESTRATOR
    if is_ttsi:
        return TaskTier.TTSI
    if force_privacy:
        return TaskTier.PRIVACY
    if has_tools:
        return TaskTier.TOOL_USE

    prompt_lower = prompt.lower()
    for tier, keywords, requires_all in TIER_SIGNALS:
        if requires_all:
            if all(kw in prompt_lower for kw in keywords):
                return tier
        else:
            if any(kw in prompt_lower for kw in keywords):
                return tier

    return TaskTier.GENERATION  # Default


# ─── COST TRACKER ───────────────────────────────────────────────────────────

@dataclass
class UsageRecord:
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0
    tier: str = ""
    timestamp: float = field(default_factory=time.time)

@dataclass
class CostTracker:
    """Track token usage and estimated cost per provider."""
    records: List[UsageRecord] = field(default_factory=list)

    def record(self, rec: UsageRecord):
        self.records.append(rec)

    def summary(self) -> Dict:
        claude_in = sum(r.input_tokens for r in self.records if r.provider == "claude")
        claude_out = sum(r.output_tokens for r in self.records if r.provider == "claude")
        venice_in = sum(r.input_tokens for r in self.records if r.provider == "venice")
        venice_out = sum(r.output_tokens for r in self.records if r.provider == "venice")
        
        # Approximate pricing (per 1M tokens)
        # Claude Sonnet: $3 input, $15 output
        # Venice GLM: ~$0.50 input, ~$2 output (estimated)
        claude_cost = (claude_in * 3 + claude_out * 15) / 1_000_000
        venice_cost = (venice_in * 0.5 + venice_out * 2) / 1_000_000

        return {
            "claude": {"input_tokens": claude_in, "output_tokens": claude_out, "est_cost_usd": round(claude_cost, 4)},
            "venice": {"input_tokens": venice_in, "output_tokens": venice_out, "est_cost_usd": round(venice_cost, 4)},
            "total_est_cost_usd": round(claude_cost + venice_cost, 4),
            "total_requests": len(self.records),
            "routing_split": {
                "claude_pct": round(100 * sum(1 for r in self.records if r.provider == "claude") / max(len(self.records), 1), 1),
                "venice_pct": round(100 * sum(1 for r in self.records if r.provider == "venice") / max(len(self.records), 1), 1),
            }
        }


# ─── API NORMALIZER ─────────────────────────────────────────────────────────

def _strip_none(d: dict) -> dict:
    """Venice rejects null values — strip them recursively."""
    clean = {}
    for k, v in d.items():
        if v is None:
            continue
        if isinstance(v, dict):
            v = _strip_none(v)
        if isinstance(v, list):
            v = [_strip_none(i) if isinstance(i, dict) else i for i in v if i is not None]
        clean[k] = v
    return clean


async def call_claude(
    messages: List[Dict],
    system: str = "",
    model: str = CLAUDE_SONNET,
    max_tokens: int = 4096,
    tools: Optional[List[Dict]] = None,
    api_key: Optional[str] = None,
) -> Dict:
    """Call Anthropic API directly. Native format."""
    key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    headers = {
        "Content-Type": "application/json",
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
    }
    body: Dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system:
        body["system"] = system
    if tools:
        body["tools"] = tools

    start = time.monotonic()
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(ANTHROPIC_API_URL, headers=headers, json=body)
        data = resp.json()

    latency = (time.monotonic() - start) * 1000
    
    # Normalize to common response format
    return {
        "provider": "claude",
        "model": model,
        "content": data.get("content", []),
        "stop_reason": data.get("stop_reason"),
        "usage": data.get("usage", {}),
        "latency_ms": round(latency, 1),
        "error": data.get("error"),
        "raw": data,
    }


async def call_venice(
    messages: List[Dict],
    system: str = "",
    model: str = VENICE_GLM,
    max_tokens: int = 4096,
    tools: Optional[List[Dict]] = None,
    web_search: bool = False,
    api_key: Optional[str] = None,
) -> Dict:
    """Call Venice AI API. OpenAI-compatible format, normalized to our standard."""
    key = api_key or os.environ.get("VENICE_API_KEY", "")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
    }

    # Convert Anthropic-style messages to OpenAI-style
    oai_messages = []
    if system:
        oai_messages.append({"role": "system", "content": system})
    for msg in messages:
        # Handle tool_result blocks (Anthropic format → OpenAI format)
        if isinstance(msg.get("content"), list):
            # Check if it's tool results
            if any(isinstance(c, dict) and c.get("type") == "tool_result" for c in msg["content"]):
                for block in msg["content"]:
                    if block.get("type") == "tool_result":
                        oai_messages.append({
                            "role": "tool",
                            "tool_call_id": block.get("tool_use_id", ""),
                            "content": block.get("content", ""),
                        })
                continue
            # Check if it's assistant content with tool_use blocks
            text_parts = [c.get("text", "") for c in msg["content"] if c.get("type") == "text"]
            if text_parts:
                oai_messages.append({"role": msg["role"], "content": "\n".join(text_parts)})
            continue
        oai_messages.append({"role": msg["role"], "content": msg.get("content", "")})

    body: Dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": oai_messages,
    }
    if tools:
        # Convert Anthropic tool format to OpenAI format
        body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {}),
                }
            }
            for t in tools
        ]
    if web_search:
        body["venice_parameters"] = {"enable_web_search": "auto"}

    # Venice rejects null values
    body = _strip_none(body)

    start = time.monotonic()
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(VENICE_API_URL, headers=headers, json=body)
        data = resp.json()

    latency = (time.monotonic() - start) * 1000

    # Normalize OpenAI response to Anthropic-like format
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message", {})
    oai_usage = data.get("usage", {})

    # Convert to our standard content blocks
    content = []
    if message.get("content"):
        # Strip <think> tags if present (Venice reasoning models)
        text = message["content"]
        think_content = ""
        if "<think>" in text:
            import re
            think_match = re.search(r"<think>(.*?)</think>", text, re.DOTALL)
            if think_match:
                think_content = think_match.group(1).strip()
                text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        content.append({"type": "text", "text": text})
        if think_content:
            content.append({"type": "thinking", "text": think_content})

    # Convert tool calls if present
    if message.get("tool_calls"):
        for tc in message["tool_calls"]:
            fn = tc.get("function", {})
            args = fn.get("arguments", "{}")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            content.append({
                "type": "tool_use",
                "id": tc.get("id", ""),
                "name": fn.get("name", ""),
                "input": args,
            })

    return {
        "provider": "venice",
        "model": model,
        "content": content,
        "stop_reason": "tool_use" if message.get("tool_calls") else "end_turn",
        "usage": {
            "input_tokens": oai_usage.get("prompt_tokens", 0),
            "output_tokens": oai_usage.get("completion_tokens", 0),
        },
        "latency_ms": round(latency, 1),
        "error": data.get("error"),
        "raw": data,
    }


# ─── ROUTER ─────────────────────────────────────────────────────────────────

class ModelRouter:
    """
    Routes requests to the optimal provider based on task classification.
    Tracks costs and provides routing transparency.
    """

    def __init__(
        self,
        anthropic_key: Optional[str] = None,
        venice_key: Optional[str] = None,
    ):
        self.anthropic_key = anthropic_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.venice_key = venice_key or os.environ.get("VENICE_API_KEY", "")
        self.cost_tracker = CostTracker()
        self.venice_available = bool(self.venice_key)
        
        logger.info(f"Router initialized — Claude: {'✓' if self.anthropic_key else '✗'}, Venice: {'✓' if self.venice_available else '✗ (Claude only)'}")

    def route(
        self,
        prompt: str = "",
        has_tools: bool = False,
        is_orchestrator: bool = False,
        is_ttsi: bool = False,
        force_privacy: bool = False,
        force_provider: Optional[Provider] = None,
        force_tier: Optional[TaskTier] = None,
    ) -> Dict:
        """
        Determine provider and model for a request.
        Returns routing decision with full transparency.
        """
        # Classify
        tier = classify_task(
            prompt, has_tools, is_orchestrator, is_ttsi, force_privacy, force_tier
        )
        route_info = ROUTING_TABLE[tier]

        provider = route_info["provider"]
        model = route_info["model"]

        # Force override
        if force_provider:
            provider = force_provider
            if provider == Provider.VENICE and not self.venice_available:
                provider = Provider.CLAUDE
            # Keep model from routing table unless it's incompatible
            if provider == Provider.CLAUDE and model in (VENICE_GLM, VENICE_QWEN, VENICE_DEEPSEEK_R1):
                model = CLAUDE_SONNET
            elif provider == Provider.VENICE and model in (CLAUDE_SONNET, CLAUDE_OPUS):
                model = VENICE_GLM

        # Fallback: if Venice not available, everything goes to Claude
        if provider == Provider.VENICE and not self.venice_available:
            provider = Provider.CLAUDE
            model = CLAUDE_SONNET

        decision = {
            "provider": provider.value,
            "model": model,
            "tier": tier.value,
            "reason": route_info["reason"],
            "fallback_used": provider.value != route_info["provider"].value,
        }

        logger.info(f"Routed [{tier.value}] → {provider.value}/{model}")
        return decision

    async def call(
        self,
        messages: List[Dict],
        system: str = "",
        max_tokens: int = 4096,
        tools: Optional[List[Dict]] = None,
        memories: Optional[List[Dict]] = None, # Added for optimization
        prompt_hint: str = "",
        is_orchestrator: bool = False,
        is_ttsi: bool = False,
        force_privacy: bool = False,
        force_provider: Optional[Provider] = None,
        web_search: bool = False,
        skip_optimization: bool = False, # Opt-out for delicate tasks
    ) -> Dict:
        """
        Route and execute an API call to the appropriate provider.
        Returns normalized response regardless of provider.
        """
        # 1. Determine routing tier (Classification)
        user_prompt = prompt_hint or (messages[-1].get("content", "") if messages else "")
        decision = self.route(
            prompt=user_prompt,
            has_tools=bool(tools),
            is_orchestrator=is_orchestrator,
            is_ttsi=is_ttsi,
            force_privacy=force_privacy,
            force_provider=force_provider,
        )

        provider = decision["provider"]
        model = decision["model"]
        tier_str = decision["tier"]

        # 2. Apply Token Optimization (Phase 6)
        optimization_report = None
        if not skip_optimization:
            import token_optimizer
            system, messages, tools, max_tokens, opt_report = token_optimizer.optimize_request(
                tier=tier_str,
                system_prompt=system,
                messages=messages,
                all_tools=tools or [],
                memories=memories or [],
                user_prompt=user_prompt
            )
            optimization_report = opt_report.to_dict()

        # 3. Execute call
        if provider == "claude":
            result = await call_claude(
                messages=messages,
                system=system,
                model=model,
                max_tokens=max_tokens,
                tools=tools,
                api_key=self.anthropic_key,
            )
        else:
            result = await call_venice(
                messages=messages,
                system=system,
                model=model,
                max_tokens=max_tokens,
                tools=tools if tier_str == "tool_use" else None,
                web_search=web_search,
                api_key=self.venice_key,
            )

        # 4. Track usage
        usage = result.get("usage", {})
        self.cost_tracker.record(UsageRecord(
            provider=provider,
            model=model,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            latency_ms=result.get("latency_ms", 0),
            tier=tier_str,
        ))

        # 5. Attach metadata
        result["routing"] = decision
        if optimization_report:
            result["optimization"] = optimization_report
            
        return result

    def get_cost_summary(self) -> Dict:
        return self.cost_tracker.summary()

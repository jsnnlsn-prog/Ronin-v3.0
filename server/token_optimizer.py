"""
RONIN Token Optimizer — Backend Optimization Engine
=====================================================
5 measurable strategies to reduce token usage on a reasonable baseline.

Optimizations:
  1. Prompt Caching (Anthropic)
  2. Tool Filtering (tier-based)
  3. Conversation Compression (rolling summary)
  4. Memory Relevance Filtering
  5. Token Budgets (per-tier max_tokens)

Every step logs before/after savings.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger("RoninTokenOptimizer")

# 1. PROMPT CACHING
def build_cached_system_prompt(system_text: str) -> List[Dict]:
    return [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}]

def get_cache_headers() -> Dict[str, str]:
    return {"anthropic-beta": "prompt-caching-2024-07-31"}

# 2. DYNAMIC TOOL FILTERING
TIER_TOOL_MAP: Dict[str, Optional[Set[str]]] = {
    "orchestrator": None, "tool_use": None,
    "ttsi": set(), "safety": {"ronin_safety_check", "ronin_system_info"},
    "privacy": {"ronin_file_read", "ronin_file_write", "ronin_memory_store"},
    "reasoning": {"ronin_memory_query", "ronin_system_info"},
    "generation": set(), "simple": set(),
    "bulk": {"ronin_file_write", "ronin_code_exec"},
    "default": None,
}

def filter_tools(all_tools: List[Dict], tier: str) -> List[Dict]:
    allowed = TIER_TOOL_MAP.get(tier)
    if allowed is None: return all_tools
    if not allowed: return []
    return [t for t in all_tools if t.get("name") in allowed]

def measure_tool_savings(all_tools: List[Dict], filtered: List[Dict]) -> Dict:
    avg = 200
    b = len(all_tools) * avg
    a = len(filtered) * avg
    return {"tools_before": len(all_tools), "tools_after": len(filtered),
            "est_tokens_before": b, "est_tokens_after": a, "est_tokens_saved": b - a}

# 3. ROLLING CONVERSATION COMPRESSION
SUMMARY_VERBATIM_COUNT = 4

def compress_conversation(messages: List[Dict], verbatim: int = SUMMARY_VERBATIM_COUNT) -> Tuple[List[Dict], Dict]:
    if len(messages) <= verbatim + 1:
        return messages, {"compressed": False, "reason": "below_threshold"}
    old = messages[:-verbatim]
    recent = messages[-verbatim:]
    est_before = _estimate_tokens(_messages_to_text(old))
    summary = _extractive_compress(old)
    est_after = _estimate_tokens(summary)
    compressed = [{"role": "user", "content": f"[Previous conversation summary]\n{summary}"}] + recent
    savings = {"compressed": True, "messages_before": len(messages), "messages_after": len(compressed),
               "est_tokens_before": est_before,
               "est_tokens_after": est_after + sum(_estimate_tokens(m.get("content", "")) for m in recent),
               "est_tokens_saved": max(0, est_before - est_after)}
    return compressed, savings

def _extractive_compress(messages: List[Dict]) -> str:
    key_points = []
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list): continue
        role = msg.get("role", "")
        if role == "user":
            text = content[:200] if len(content) > 200 else content
            key_points.append(f"User asked: {text}")
        elif role == "assistant":
            phases = re.findall(r'\[(THOUGHT|PLAN|RESULT|REFLECTION)\]\s*(.*?)(?=\[(?:THOUGHT|PLAN|ACTION|RESULT|REFLECTION)\]|$)', content, re.DOTALL)
            for name, text in phases:
                if text.strip(): key_points.append(f"{name}: {text.strip()[:100]}")
            if not phases and content.strip():
                key_points.append(f"Assistant: {content.strip()[:150]}")
    return "\n".join(key_points[-10:]) if key_points else "Previous conversation context was primarily tool execution."

def _messages_to_text(messages: List[Dict]) -> str:
    parts = []
    for m in messages:
        c = m.get("content", "")
        if isinstance(c, str): parts.append(c)
        elif isinstance(c, list):
            for b in c:
                if isinstance(b, dict): parts.append(b.get("text", b.get("content", "")))
    return "\n".join(parts)

def _estimate_tokens(text: str) -> int:
    return len(text) // 4 if isinstance(text, str) else 0

# 4. MEMORY RELEVANCE FILTERING
_STOPWORDS = {"the","a","an","is","are","was","were","be","been","being","have","has","had","do","does","did","will","would","could","should","may","might","can","shall","to","of","in","for","on","with","at","by","from","as","into","through","during","before","after","above","below","between","under","again","further","then","once","here","there","when","where","why","how","all","each","every","both","few","more","most","other","some","such","no","not","only","own","same","so","than","too","very","just","because","but","and","or","if","this","that","these","those","i","me","my","we","our","you","your","it","its","they","them","their","what","which","who"}

def _extract_keywords(text: str) -> Set[str]:
    words = re.findall(r'[a-zA-Z_][a-zA-Z0-9_]*', text.lower())
    return {w for w in words if w not in _STOPWORDS and len(w) > 2}

def filter_relevant_memories(memories: List[Dict], prompt: str, max_mem: int = 4, min_score: float = 0.1) -> Tuple[List[Dict], Dict]:
    if not memories or not prompt:
        return [], {"filtered": False, "reason": "empty_input"}
    prompt_words = _extract_keywords(prompt)
    scored = []
    for mem in memories:
        fact = mem.get("fact", "")
        tags = mem.get("tags", [])
        conf = mem.get("confidence", 0.7)
        mem_words = _extract_keywords(fact)
        tag_words = {w.lower() for t in tags for w in t.split()}
        if not prompt_words:
            score = 0.0
        else:
            overlap = prompt_words & (mem_words | tag_words)
            union = prompt_words | mem_words
            jaccard = len(overlap) / max(len(union), 1)
            score = jaccard * 0.7 + conf * 0.3
        scored.append((score, mem))
    scored.sort(key=lambda x: x[0], reverse=True)
    filtered = [mem for s, mem in scored if s >= min_score][:max_mem]
    savings = {"filtered": True, "memories_before": len(memories), "memories_after": len(filtered),
               "est_tokens_before": len(memories) * 50, "est_tokens_after": len(filtered) * 50,
               "est_tokens_saved": (len(memories) - len(filtered)) * 50,
               "top_scores": [round(s, 3) for s, _ in scored[:5]]}
    return filtered, savings

# 5. RESPONSE TOKEN BUDGETS
TOKEN_BUDGETS: Dict[str, int] = {
    "orchestrator": 4096, "tool_use": 4096, "ttsi": 600, "safety": 400,
    "reasoning": 3000, "generation": 2500, "privacy": 2000,
    "simple": 1000, "bulk": 1500, "default": 4096,
}

def get_token_budget(tier: str) -> int:
    return TOKEN_BUDGETS.get(tier, 4096)

# COMBINED OPTIMIZER
@dataclass
class OptimizationReport:
    caching: Dict = field(default_factory=dict)
    tool_filtering: Dict = field(default_factory=dict)
    conversation: Dict = field(default_factory=dict)
    memory: Dict = field(default_factory=dict)
    budget: Dict = field(default_factory=dict)

    @property
    def total_estimated_savings(self) -> int:
        return (self.tool_filtering.get("est_tokens_saved", 0) +
                self.conversation.get("est_tokens_saved", 0) +
                self.memory.get("est_tokens_saved", 0))

    def to_dict(self) -> Dict:
        return {
            "caching": self.caching, "tool_filtering": self.tool_filtering,
            "conversation": self.conversation, "memory": self.memory,
            "budget": self.budget,
            "total_est_tokens_saved": self.total_estimated_savings,
        }

def optimize_request(
    tier: str,
    system_prompt: str,
    messages: List[Dict],
    all_tools: List[Dict],
    memories: List[Dict],
    user_prompt: str,
) -> Tuple[List[Dict], List[Dict], List[Dict], int, OptimizationReport]:
    report = OptimizationReport()

    # 1. Caching
    opt_system = build_cached_system_prompt(system_prompt)
    report.caching = {
        "enabled": True,
        "system_tokens": _estimate_tokens(system_prompt),
        "cache_savings_pct": 90,
        "est_tokens_saved_per_cache_hit": int(_estimate_tokens(system_prompt) * 0.9),
    }

    # 2. Tools
    opt_tools = filter_tools(all_tools, tier)
    report.tool_filtering = measure_tool_savings(all_tools, opt_tools)

    # 3. Conversation
    opt_messages, conv_savings = compress_conversation(messages)
    report.conversation = conv_savings

    # 4. Memories
    opt_memories, mem_savings = filter_relevant_memories(memories, user_prompt)
    report.memory = mem_savings

    # 5. Budget
    max_tokens = get_token_budget(tier)
    report.budget = {
        "tier": tier, "max_tokens": max_tokens,
        "default_max_tokens": 4096, "output_tokens_saved": 4096 - max_tokens,
    }

    # Inject memories into system
    if opt_memories:
        mem_text = "\n\n[MEMORY]\n" + "\n".join(
            f"- [{int(m.get('confidence', 0.7)*100)}%] {m.get('fact', '')}" for m in opt_memories
        )
        opt_system = build_cached_system_prompt(system_prompt + mem_text)

    logger.info(
        f"Optimized [{tier}]: tools {report.tool_filtering.get('tools_before','?')}→"
        f"{report.tool_filtering.get('tools_after','?')}, msgs {report.conversation.get('messages_before','?')}→"
        f"{report.conversation.get('messages_after','?')}, mem {report.memory.get('memories_before','?')}→"
        f"{report.memory.get('memories_after','?')}, budget {max_tokens}, "
        f"~{report.total_estimated_savings} tokens saved"
    )

    return opt_system, opt_messages, opt_tools, max_tokens, report
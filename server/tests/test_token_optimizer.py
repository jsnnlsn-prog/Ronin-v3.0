"""Tests for Token Optimizer — all 5 strategies."""
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from token_optimizer import (
    build_cached_system_prompt, get_cache_headers,
    filter_tools, measure_tool_savings, TIER_TOOL_MAP,
    compress_conversation,
    filter_relevant_memories,
    get_token_budget, TOKEN_BUDGETS,
    optimize_request, OptimizationReport,
    _extract_keywords, _estimate_tokens,
)


# ─── OPT 1: PROMPT CACHING ─────────────────────────────────────────────────

def test_cached_system_prompt_format():
    result = build_cached_system_prompt("You are RONIN")
    assert len(result) == 1
    assert result[0]["type"] == "text"
    assert result[0]["text"] == "You are RONIN"
    assert result[0]["cache_control"]["type"] == "ephemeral"


def test_cache_headers():
    h = get_cache_headers()
    assert "anthropic-beta" in h
    assert "prompt-caching" in h["anthropic-beta"]


# ─── OPT 2: TOOL FILTERING ─────────────────────────────────────────────────

MOCK_TOOLS = [
    {"name": "ronin_shell_exec"},
    {"name": "ronin_code_exec"},
    {"name": "ronin_file_write"},
    {"name": "ronin_file_read"},
    {"name": "ronin_web_fetch"},
    {"name": "ronin_memory_store"},
    {"name": "ronin_memory_query"},
    {"name": "ronin_safety_check"},
    {"name": "ronin_system_info"},
    {"name": "ronin_episodic_store"},
    {"name": "ronin_kv_get"},
    {"name": "ronin_kv_set"},
]


def test_filter_tools_orchestrator_gets_all():
    result = filter_tools(MOCK_TOOLS, "orchestrator")
    assert len(result) == len(MOCK_TOOLS)


def test_filter_tools_generation_gets_none():
    result = filter_tools(MOCK_TOOLS, "generation")
    assert len(result) == 0


def test_filter_tools_simple_gets_none():
    result = filter_tools(MOCK_TOOLS, "simple")
    assert len(result) == 0


def test_filter_tools_safety_gets_subset():
    result = filter_tools(MOCK_TOOLS, "safety")
    names = {t["name"] for t in result}
    assert "ronin_safety_check" in names
    assert "ronin_system_info" in names
    assert "ronin_shell_exec" not in names


def test_filter_tools_privacy_gets_subset():
    result = filter_tools(MOCK_TOOLS, "privacy")
    names = {t["name"] for t in result}
    assert "ronin_file_read" in names
    assert "ronin_file_write" in names
    assert "ronin_memory_store" in names
    assert "ronin_shell_exec" not in names


def test_measure_tool_savings():
    filtered = filter_tools(MOCK_TOOLS, "generation")
    savings = measure_tool_savings(MOCK_TOOLS, filtered)
    assert savings["tools_before"] == 12
    assert savings["tools_after"] == 0
    assert savings["est_tokens_saved"] == 12 * 200


def test_every_tier_has_tool_map():
    for tier in TIER_TOOL_MAP:
        result = filter_tools(MOCK_TOOLS, tier)
        assert isinstance(result, list)


# ─── OPT 3: CONVERSATION COMPRESSION ───────────────────────────────────────

def test_compress_short_conversation_unchanged():
    msgs = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]
    result, savings = compress_conversation(msgs)
    assert not savings.get("compressed", False)
    assert len(result) == 2


def test_compress_long_conversation():
    msgs = []
    for i in range(10):
        msgs.append({"role": "user", "content": f"Question number {i} about topic {i}"})
        msgs.append({"role": "assistant", "content": f"[THOUGHT] Thinking about {i}\n[RESULT] Answer {i}"})
    
    result, savings = compress_conversation(msgs)
    assert savings["compressed"] is True
    assert savings["messages_after"] < savings["messages_before"]
    assert savings["est_tokens_saved"] > 0
    # Last 4 messages should be preserved verbatim
    assert result[-1]["content"] == msgs[-1]["content"]
    assert result[-2]["content"] == msgs[-2]["content"]


def test_compress_preserves_recent_messages():
    msgs = [
        {"role": "user", "content": "old question 1"},
        {"role": "assistant", "content": "old answer 1"},
        {"role": "user", "content": "old question 2"},
        {"role": "assistant", "content": "old answer 2"},
        {"role": "user", "content": "old question 3"},
        {"role": "assistant", "content": "old answer 3"},
        {"role": "user", "content": "recent question"},
        {"role": "assistant", "content": "recent answer"},
        {"role": "user", "content": "current question"},
        {"role": "assistant", "content": "current answer"},
    ]
    result, _ = compress_conversation(msgs, verbatim_count=4)
    # Should have summary + 4 recent
    assert len(result) == 5
    assert "summary" in result[0]["content"].lower()
    assert result[-1]["content"] == "current answer"


# ─── OPT 4: MEMORY RELEVANCE FILTERING ─────────────────────────────────────

def test_filter_memories_relevant():
    memories = [
        {"fact": "User prefers Python for scripting", "confidence": 0.9, "tags": ["coding"]},
        {"fact": "Client insurance documents are in Google Drive", "confidence": 0.8, "tags": ["client"]},
        {"fact": "Deploy scripts use Docker Compose", "confidence": 0.7, "tags": ["deploy"]},
        {"fact": "User's favorite color is blue", "confidence": 0.5, "tags": ["personal"]},
    ]
    result, savings = filter_relevant_memories(memories, "write a python script to parse data")
    facts = [m["fact"] for m in result]
    assert any("Python" in f for f in facts)
    assert savings["filtered"] is True
    assert savings["memories_after"] <= savings["memories_before"]


def test_filter_memories_empty_prompt():
    memories = [{"fact": "something", "confidence": 0.8, "tags": []}]
    result, savings = filter_relevant_memories(memories, "")
    assert savings.get("reason") == "empty_input"


def test_filter_memories_empty_memories():
    result, savings = filter_relevant_memories([], "some prompt")
    assert savings.get("reason") == "empty_input"


def test_filter_memories_max_cap():
    memories = [{"fact": f"fact about topic {i}", "confidence": 0.9, "tags": []} for i in range(20)]
    result, _ = filter_relevant_memories(memories, "topic", max_mem=4)
    assert len(result) <= 4


# ─── OPT 5: TOKEN BUDGETS ──────────────────────────────────────────────────

def test_token_budget_orchestrator():
    assert get_token_budget("orchestrator") == 4096


def test_token_budget_ttsi():
    assert get_token_budget("ttsi") == 600


def test_token_budget_simple():
    assert get_token_budget("simple") == 1000


def test_token_budget_unknown_gets_default():
    assert get_token_budget("nonexistent_tier") == 4096


def test_all_tiers_have_budgets():
    for tier in ["orchestrator", "tool_use", "ttsi", "safety", "reasoning", "generation", "privacy", "simple", "bulk"]:
        budget = get_token_budget(tier)
        assert 400 <= budget <= 4096, f"Tier {tier} has unreasonable budget: {budget}"


# ─── COMBINED OPTIMIZER ─────────────────────────────────────────────────────

def test_optimize_request_generation():
    system, msgs, tools, max_tok, report = optimize_request(
        tier="generation",
        system_prompt="You are RONIN",
        messages=[{"role": "user", "content": "write a blog post"}],
        all_tools=MOCK_TOOLS,
        memories=[{"fact": "User likes concise writing", "confidence": 0.8, "tags": ["writing"]}],
        user_prompt="write a blog post",
    )
    # Should cache system prompt
    assert system[0]["cache_control"]["type"] == "ephemeral"
    # Should filter out all tools for generation
    assert len(tools) == 0
    # Should use generation budget
    assert max_tok == 2500
    # Report should have savings
    assert report.tool_filtering["est_tokens_saved"] > 0


def test_optimize_request_orchestrator():
    _, _, tools, max_tok, report = optimize_request(
        tier="orchestrator",
        system_prompt="You are RONIN",
        messages=[{"role": "user", "content": "deploy the app"}],
        all_tools=MOCK_TOOLS,
        memories=[],
        user_prompt="deploy the app",
    )
    # Orchestrator gets all tools
    assert len(tools) == len(MOCK_TOOLS)
    # Full budget
    assert max_tok == 4096


def test_optimization_report_total():
    report = OptimizationReport()
    report.tool_filtering = {"est_tokens_saved": 2400}
    report.conversation = {"est_tokens_saved": 3000}
    report.memory = {"est_tokens_saved": 200}
    assert report.total_estimated_savings == 5600


# ─── UTILITY TESTS ──────────────────────────────────────────────────────────

def test_extract_keywords_removes_stopwords():
    kw = _extract_keywords("the quick brown fox jumps over a lazy dog")
    assert "the" not in kw
    assert "over" not in kw
    assert "quick" in kw
    assert "brown" in kw
    assert "jumps" in kw


def test_extract_keywords_removes_short():
    kw = _extract_keywords("I am a go to it")
    assert len(kw) == 0  # all short or stopwords


def test_estimate_tokens():
    assert _estimate_tokens("") == 0
    # ~4 chars per token
    assert 20 <= _estimate_tokens("a" * 100) <= 30

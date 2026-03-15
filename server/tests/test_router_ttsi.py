"""Tests for Model Router and TT-SI modules."""
import json
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ─── MODEL ROUTER TESTS ────────────────────────────────────────────────────

def test_classify_orchestrator():
    from model_router import classify_task, TaskTier
    assert classify_task("anything", is_orchestrator=True) == TaskTier.ORCHESTRATOR


def test_classify_ttsi():
    from model_router import classify_task, TaskTier
    assert classify_task("evaluate plan", is_ttsi=True) == TaskTier.TTSI


def test_classify_tool_use():
    from model_router import classify_task, TaskTier
    assert classify_task("run a command", has_tools=True) == TaskTier.TOOL_USE


def test_classify_privacy():
    from model_router import classify_task, TaskTier
    assert classify_task("process the client insurance documents") == TaskTier.PRIVACY
    assert classify_task("handle HIPAA sensitive records") == TaskTier.PRIVACY
    assert classify_task("biohazard cleanup report for site") == TaskTier.PRIVACY


def test_classify_generation():
    from model_router import classify_task, TaskTier
    assert classify_task("write a proposal for the solar project") == TaskTier.GENERATION
    assert classify_task("draft an email to the client") == TaskTier.GENERATION


def test_classify_reasoning():
    from model_router import classify_task, TaskTier
    assert classify_task("analyze the trade-offs between these approaches") == TaskTier.REASONING
    assert classify_task("debug this authentication issue") == TaskTier.REASONING


def test_classify_simple():
    from model_router import classify_task, TaskTier
    assert classify_task("what is a REST API") == TaskTier.SIMPLE
    assert classify_task("convert this to JSON") == TaskTier.SIMPLE


def test_classify_force_tier():
    from model_router import classify_task, TaskTier
    assert classify_task("anything at all", force_tier=TaskTier.BULK) == TaskTier.BULK


def test_routing_table_completeness():
    from model_router import ROUTING_TABLE, TaskTier
    for tier in TaskTier:
        assert tier in ROUTING_TABLE, f"Missing route for tier: {tier}"


def test_route_returns_expected_keys():
    from model_router import ModelRouter
    router = ModelRouter(anthropic_key="test", venice_key="")
    decision = router.route("hello", is_orchestrator=True)
    assert "provider" in decision
    assert "model" in decision
    assert "tier" in decision
    assert "reason" in decision


def test_route_fallback_without_venice():
    from model_router import ModelRouter
    router = ModelRouter(anthropic_key="test", venice_key="")
    # Privacy would normally go to Venice, but no key → falls back to Claude
    decision = router.route("process confidential insurance records")
    assert decision["provider"] == "claude"
    assert decision.get("fallback_used") or decision["provider"] == "claude"


def test_route_venice_when_available():
    from model_router import ModelRouter
    router = ModelRouter(anthropic_key="test", venice_key="venice-test")
    decision = router.route("write a blog post about solar energy")
    assert decision["provider"] == "venice"
    assert decision["tier"] == "generation"


def test_strip_none():
    from model_router import _strip_none
    assert _strip_none({"a": 1, "b": None, "c": "x"}) == {"a": 1, "c": "x"}
    assert _strip_none({"a": {"b": None, "c": 1}}) == {"a": {"c": 1}}


def test_cost_tracker():
    from model_router import CostTracker, UsageRecord
    tracker = CostTracker()
    tracker.record(UsageRecord(provider="claude", model="test", input_tokens=1000, output_tokens=500, tier="orchestrator"))
    tracker.record(UsageRecord(provider="venice", model="test", input_tokens=2000, output_tokens=1000, tier="generation"))

    summary = tracker.summary()
    assert summary["total_requests"] == 2
    assert summary["claude"]["input_tokens"] == 1000
    assert summary["venice"]["input_tokens"] == 2000
    assert summary["routing_split"]["claude_pct"] == 50.0
    assert summary["routing_split"]["venice_pct"] == 50.0


# ─── TT-SI TESTS ───────────────────────────────────────────────────────────

def test_assess_risk_critical():
    from ttsi import assess_risk
    assert assess_risk("delete database and drop table") == "critical"
    assert assess_risk("rm -rf everything") == "critical"


def test_assess_risk_high():
    from ttsi import assess_risk
    assert assess_risk("deploy to production server") == "high"
    assert assess_risk("send email to client with billing info") == "high"


def test_assess_risk_medium():
    from ttsi import assess_risk
    assert assess_risk("write file to workspace") == "medium"
    assert assess_risk("execute code to test") == "medium"


def test_assess_risk_low():
    from ttsi import assess_risk
    assert assess_risk("what time is it") == "low"
    assert assess_risk("explain how REST works") == "low"


def test_assess_risk_tool_based():
    from ttsi import assess_risk
    assert assess_risk("do the thing", ["ronin_shell_exec"]) == "medium"
    assert assess_risk("do the thing", ["ronin_memory_query"]) == "low"


def test_should_run_ttsi_autonomy_levels():
    from ttsi import should_run_ttsi
    # Autonomy 0 (Manual) → run on everything
    assert should_run_ttsi("low", 0) is True
    # Autonomy 2 (Act+Confirm) → skip low, run medium+
    assert should_run_ttsi("low", 2) is False
    assert should_run_ttsi("medium", 2) is True
    assert should_run_ttsi("high", 2) is True
    # Autonomy 3 (Autonomous) → only run on high+
    assert should_run_ttsi("low", 3) is False
    assert should_run_ttsi("medium", 3) is False
    assert should_run_ttsi("high", 3) is True
    assert should_run_ttsi("critical", 3) is True


def test_ttsi_result_skipped():
    from ttsi import TTSIResult
    result = TTSIResult.skipped_result("low")
    assert result.skipped is True
    assert result.decision.value == "proceed"
    assert result.confidence == 1.0


def test_ttsi_result_to_dict():
    from ttsi import TTSIResult, TTSIDecision
    result = TTSIResult(
        decision=TTSIDecision.MODIFY,
        confidence=0.8,
        reasoning="Found issue",
        failure_modes=["Could timeout"],
        modifications="Add timeout",
        modified_plan="New plan",
        risk_level="medium",
        latency_ms=150.0,
    )
    d = result.to_dict()
    assert d["decision"] == "modify"
    assert d["confidence"] == 0.8
    assert len(d["failure_modes"]) == 1


def test_build_ttsi_prompt():
    from ttsi import build_ttsi_prompt
    prompt = build_ttsi_prompt(
        user_goal="Deploy the API",
        proposed_plan="Build docker image, push, deploy",
        risk_level="high",
        tool_calls=[{"name": "ronin_shell_exec", "input": {"command": "docker build ."}}],
    )
    assert "Deploy the API" in prompt
    assert "ronin_shell_exec" in prompt
    assert "high" in prompt

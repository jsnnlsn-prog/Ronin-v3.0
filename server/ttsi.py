"""
RONIN TT-SI — Test-Time Self-Improvement
===========================================
Pre-flight simulation layer that sits between PLAN and ACTION in the ReAct loop.

For any action classified as medium-risk or above, TT-SI runs a one-shot
simulation asking:
  1. What are the candidate approaches?
  2. What could go wrong with each?
  3. Which path has the best risk-adjusted outcome?

This costs one extra API call per qualifying action but prevents mistakes
that would cost far more to fix. Low-risk actions skip it entirely.

The TT-SI result modifies or gates the original plan before execution proceeds.

Design principles:
  - Always routes to Claude (never cost-optimize the safety net)
  - Single API call, not a loop (bounded cost)
  - Returns structured decision: proceed / modify / abort
  - Attaches reasoning trace for audit trail
"""

import json
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger("RoninTTSI")


class TTSIDecision(str, Enum):
    PROCEED = "proceed"        # Plan looks good, execute as-is
    MODIFY = "modify"          # Plan has issues, here's a better version
    ABORT = "abort"            # Plan is too risky, don't execute
    ESCALATE = "escalate"      # Need human input before proceeding


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# ─── RISK CLASSIFIER ───────────────────────────────────────────────────────

RISK_SIGNALS = {
    RiskLevel.CRITICAL: [
        "delete database", "drop table", "rm -rf", "format disk",
        "send credentials", "expose api key", "production deploy",
        "financial transaction", "legal document", "sign contract",
    ],
    RiskLevel.HIGH: [
        "modify production", "send email", "publish", "deploy",
        "install package", "change permission", "api key",
        "client data", "billing", "payment",
    ],
    RiskLevel.MEDIUM: [
        "write file", "execute code", "shell command", "create",
        "modify", "update", "network request", "fetch url",
        "download", "build",
    ],
    # Everything else is LOW — no signals needed
}


def assess_risk(action_description: str, tool_names: Optional[List[str]] = None) -> RiskLevel:
    """
    Classify risk level of a proposed action.
    Checks description keywords + tool destructiveness.
    """
    desc_lower = action_description.lower()
    tool_names = tool_names or []

    # Check from most dangerous down
    for level in [RiskLevel.CRITICAL, RiskLevel.HIGH, RiskLevel.MEDIUM]:
        signals = RISK_SIGNALS[level]
        if any(s in desc_lower for s in signals):
            return level

    # Tool-based risk assessment
    destructive_tools = {"ronin_shell_exec", "ronin_code_exec", "ronin_file_write"}
    if any(t in destructive_tools for t in tool_names):
        return RiskLevel.MEDIUM

    return RiskLevel.LOW


def should_run_ttsi(risk: RiskLevel, autonomy_level: int = 2) -> bool:
    """
    Decide whether TT-SI should gate this action.
    
    Autonomy levels:
      0 (Manual)      → TT-SI on everything
      1 (Suggest)     → TT-SI on LOW and above  
      2 (Act+Confirm) → TT-SI on MEDIUM and above (default)
      3 (Autonomous)  → TT-SI on HIGH and above only
    """
    thresholds = {
        0: RiskLevel.LOW,
        1: RiskLevel.LOW,
        2: RiskLevel.MEDIUM,
        3: RiskLevel.HIGH,
    }
    threshold = thresholds.get(autonomy_level, RiskLevel.MEDIUM)
    risk_order = [RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL]
    return risk_order.index(risk) >= risk_order.index(threshold)


# ─── TT-SI PROMPT ──────────────────────────────────────────────────────────

TTSI_SYSTEM_PROMPT = """You are the TT-SI (Test-Time Self-Improvement) module of RONIN.
Your job is to evaluate a proposed plan BEFORE it executes and catch problems.

You will receive:
- The original user goal
- The proposed plan (actions, tools, parameters)
- The risk level assessment

Your task:
1. SIMULATE: Walk through each step mentally. What happens at each stage?
2. FAILURE MODES: For each step, what could go wrong? Be specific.
3. ALTERNATIVES: Is there a safer or more effective approach?
4. DECISION: Based on your analysis, choose exactly one:
   - PROCEED: Plan is sound, execute as proposed
   - MODIFY: Plan needs changes (provide the modified plan)
   - ABORT: Plan is too dangerous or fundamentally flawed
   - ESCALATE: Need human confirmation for this specific action

Respond in this exact JSON format:
{
  "decision": "proceed|modify|abort|escalate",
  "confidence": 0.0-1.0,
  "reasoning": "Your step-by-step simulation analysis",
  "failure_modes": ["specific thing that could go wrong", ...],
  "modifications": "If decision is 'modify', describe the changes. Otherwise null.",
  "modified_plan": "If decision is 'modify', the corrected plan. Otherwise null."
}

Be rigorous but not paranoid. Reading a file is low risk. Deleting production data is critical.
Don't block routine operations. DO block genuinely dangerous ones."""


def build_ttsi_prompt(
    user_goal: str,
    proposed_plan: str,
    risk_level: str,
    tool_calls: Optional[List[Dict]] = None,
    context: Optional[str] = None,
) -> str:
    """Build the evaluation prompt for TT-SI."""
    tools_desc = ""
    if tool_calls:
        tools_desc = "\n\nProposed tool calls:\n"
        for i, tc in enumerate(tool_calls, 1):
            tools_desc += f"  {i}. {tc.get('name', 'unknown')}({json.dumps(tc.get('input', {}), indent=2)[:500]})\n"

    ctx_section = f"\n\nAdditional context:\n{context}" if context else ""

    return f"""EVALUATE THIS PLAN:

User goal: {user_goal}

Proposed plan:
{proposed_plan}

Risk assessment: {risk_level}
{tools_desc}{ctx_section}

Analyze this plan and respond with your JSON evaluation."""


# ─── TT-SI RESULT ──────────────────────────────────────────────────────────

@dataclass
class TTSIResult:
    """Structured result from TT-SI evaluation."""
    decision: TTSIDecision
    confidence: float
    reasoning: str
    failure_modes: List[str]
    modifications: Optional[str]
    modified_plan: Optional[str]
    risk_level: str
    latency_ms: float
    skipped: bool = False  # True if TT-SI was skipped (low risk)

    @staticmethod
    def skipped_result(risk_level: str) -> "TTSIResult":
        """Return a pass-through result when TT-SI is skipped."""
        return TTSIResult(
            decision=TTSIDecision.PROCEED,
            confidence=1.0,
            reasoning="TT-SI skipped — risk below threshold",
            failure_modes=[],
            modifications=None,
            modified_plan=None,
            risk_level=risk_level,
            latency_ms=0,
            skipped=True,
        )

    def to_dict(self) -> Dict:
        return {
            "decision": self.decision.value,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "failure_modes": self.failure_modes,
            "modifications": self.modifications,
            "modified_plan": self.modified_plan,
            "risk_level": self.risk_level,
            "latency_ms": self.latency_ms,
            "skipped": self.skipped,
        }


# ─── TT-SI ENGINE ──────────────────────────────────────────────────────────

async def run_ttsi(
    user_goal: str,
    proposed_plan: str,
    tool_calls: Optional[List[Dict]] = None,
    context: Optional[str] = None,
    autonomy_level: int = 2,
    router=None,
) -> TTSIResult:
    """
    Run TT-SI pre-flight evaluation on a proposed plan.
    
    Args:
        user_goal: What the user asked for
        proposed_plan: The plan text from the PLAN phase
        tool_calls: Specific tool calls being proposed
        context: Additional context (memory, previous results)
        autonomy_level: Current autonomy setting (0-3)
        router: ModelRouter instance (uses Claude directly if None)
    
    Returns:
        TTSIResult with decision, reasoning, and any modifications
    """
    # Assess risk
    tool_names = [tc.get("name", "") for tc in (tool_calls or [])]
    risk = assess_risk(proposed_plan + " " + user_goal, tool_names)

    # Check if TT-SI should run
    if not should_run_ttsi(risk, autonomy_level):
        logger.info(f"TT-SI skipped — risk={risk.value}, autonomy={autonomy_level}")
        return TTSIResult.skipped_result(risk.value)

    logger.info(f"TT-SI triggered — risk={risk.value}, evaluating plan...")

    # Build prompt
    eval_prompt = build_ttsi_prompt(
        user_goal=user_goal,
        proposed_plan=proposed_plan,
        risk_level=risk.value,
        tool_calls=tool_calls,
        context=context,
    )

    # Call LLM — always Claude, always via router's TTSI tier
    start = time.monotonic()
    
    try:
        if router:
            response = await router.call(
                messages=[{"role": "user", "content": eval_prompt}],
                system=TTSI_SYSTEM_PROMPT,
                max_tokens=1500,
                is_ttsi=True,
            )
            text_blocks = [b["text"] for b in response.get("content", []) if b.get("type") == "text"]
            raw_text = "\n".join(text_blocks)
        else:
            # Direct import fallback — shouldn't happen in production
            from model_router import call_claude
            response = await call_claude(
                messages=[{"role": "user", "content": eval_prompt}],
                system=TTSI_SYSTEM_PROMPT,
                max_tokens=1500,
            )
            text_blocks = [b["text"] for b in response.get("content", []) if b.get("type") == "text"]
            raw_text = "\n".join(text_blocks)

    except Exception as e:
        logger.error(f"TT-SI API call failed: {e}")
        # On failure, don't block — but log the failure
        return TTSIResult(
            decision=TTSIDecision.PROCEED,
            confidence=0.5,
            reasoning=f"TT-SI evaluation failed ({e}). Proceeding with caution.",
            failure_modes=["TT-SI system error — manual review recommended"],
            modifications=None,
            modified_plan=None,
            risk_level=risk.value,
            latency_ms=(time.monotonic() - start) * 1000,
        )

    latency = (time.monotonic() - start) * 1000

    # Parse JSON response
    try:
        # Extract JSON from response (handle markdown code blocks)
        json_text = raw_text
        if "```json" in json_text:
            json_text = json_text.split("```json")[1].split("```")[0]
        elif "```" in json_text:
            json_text = json_text.split("```")[1].split("```")[0]
        
        parsed = json.loads(json_text.strip())
        
        decision_str = parsed.get("decision", "proceed").lower()
        try:
            decision = TTSIDecision(decision_str)
        except ValueError:
            decision = TTSIDecision.PROCEED

        result = TTSIResult(
            decision=decision,
            confidence=float(parsed.get("confidence", 0.7)),
            reasoning=parsed.get("reasoning", raw_text),
            failure_modes=parsed.get("failure_modes", []),
            modifications=parsed.get("modifications"),
            modified_plan=parsed.get("modified_plan"),
            risk_level=risk.value,
            latency_ms=round(latency, 1),
        )

    except (json.JSONDecodeError, KeyError, IndexError):
        # If we can't parse, treat the raw text as reasoning and proceed
        logger.warning("TT-SI response was not valid JSON — using raw text as reasoning")
        result = TTSIResult(
            decision=TTSIDecision.PROCEED,
            confidence=0.6,
            reasoning=raw_text[:2000],
            failure_modes=["TT-SI output parse error — review reasoning manually"],
            modifications=None,
            modified_plan=None,
            risk_level=risk.value,
            latency_ms=round(latency, 1),
        )

    logger.info(f"TT-SI result: {result.decision.value} (confidence={result.confidence}, {result.latency_ms}ms)")
    return result


# ─── PHASE 5: OUTCOME TRACKING + THRESHOLD AUTO-TUNING ──────────────────────

import os
import sqlite3
from pathlib import Path

_RONIN_HOME = Path(os.environ.get("RONIN_HOME", Path.home() / ".ronin"))
_MEMORY_DB = _RONIN_HOME / "memory.db"


def record_ttsi_outcome(
    ttsi_result: "TTSIResult",
    actual_outcome: str,
    was_correct: bool,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Record a TT-SI prediction vs actual outcome for learning loop."""
    import json as _json
    close_after = False
    if conn is None:
        conn = sqlite3.connect(str(_MEMORY_DB))
        close_after = True

    try:
        conn.execute(
            "INSERT INTO ttsi_outcomes (ttsi_result_json, actual_outcome, was_correct, created_at) VALUES (?,?,?,?)",
            (
                _json.dumps(ttsi_result.to_dict()),
                actual_outcome,
                int(was_correct),
                datetime.now(timezone.utc).isoformat() if "datetime" in dir() else
                __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
            ),
        )
        conn.commit()
    except Exception as e:
        logger.warning(f"Failed to record TT-SI outcome: {e}")
    finally:
        if close_after:
            conn.close()


def get_ttsi_stats(conn: Optional[sqlite3.Connection] = None) -> dict:
    """Return accuracy metrics, threshold history, FP/FN rates."""
    close_after = False
    if conn is None:
        conn = sqlite3.connect(str(_MEMORY_DB))
        conn.row_factory = sqlite3.Row
        close_after = True

    try:
        rows = conn.execute(
            "SELECT ttsi_result_json, actual_outcome, was_correct FROM ttsi_outcomes ORDER BY created_at DESC LIMIT 500"
        ).fetchall()

        total = len(rows)
        if total == 0:
            return {
                "total_outcomes": 0,
                "accuracy": None,
                "false_positive_rate": None,
                "false_negative_rate": None,
                "thresholds": _get_current_thresholds(conn),
            }

        correct = sum(1 for r in rows if r[2])
        accuracy = correct / total

        # False positive: TT-SI said risky (not proceed), action was safe (correct=True if TT-SI was right)
        # Here: was_correct=False + TT-SI decision was NOT proceed = false positive
        fp = 0
        fn = 0
        import json as _json
        for r in rows:
            try:
                result_data = _json.loads(r[0])
                decision = result_data.get("decision", "proceed")
                was_right = bool(r[2])
                if decision != "proceed" and was_right is False:
                    fp += 1  # Said risky, wasn't
                elif decision == "proceed" and was_right is False:
                    fn += 1  # Said safe, wasn't
            except Exception:
                pass

        fp_rate = fp / max(total, 1)
        fn_rate = fn / max(total, 1)

        # Auto-tune thresholds if we have enough data (every 100 outcomes)
        if total % 100 == 0 and total > 0:
            _autotune_thresholds(conn, fp_rate, fn_rate)

        return {
            "total_outcomes": total,
            "correct": correct,
            "accuracy": round(accuracy, 4),
            "false_positive_rate": round(fp_rate, 4),
            "false_negative_rate": round(fn_rate, 4),
            "thresholds": _get_current_thresholds(conn),
        }
    finally:
        if close_after:
            conn.close()


def _get_current_thresholds(conn: sqlite3.Connection) -> dict:
    """Get current TT-SI thresholds from KV store."""
    try:
        row = conn.execute(
            "SELECT value FROM key_value_store WHERE key='config:ttsi_thresholds'"
        ).fetchone()
        if row:
            import json as _json
            return _json.loads(row[0])
    except Exception:
        pass
    return {
        "fp_threshold": 0.20,
        "fn_threshold": 0.05,
        "risk_multiplier": 1.0,
    }


def _autotune_thresholds(conn: sqlite3.Connection, fp_rate: float, fn_rate: float) -> None:
    """Adjust risk thresholds based on observed FP/FN rates."""
    import json as _json
    thresholds = _get_current_thresholds(conn)
    multiplier = thresholds.get("risk_multiplier", 1.0)

    if fp_rate > 0.20:
        multiplier = max(0.5, multiplier * 0.9)  # Relax: TT-SI too cautious
        logger.info(f"TT-SI auto-tune: relaxing (FP rate={fp_rate:.2f}, multiplier→{multiplier:.2f})")
    elif fn_rate > 0.05:
        multiplier = min(2.0, multiplier * 1.1)  # Tighten: TT-SI too permissive
        logger.info(f"TT-SI auto-tune: tightening (FN rate={fn_rate:.2f}, multiplier→{multiplier:.2f})")

    thresholds["risk_multiplier"] = round(multiplier, 3)
    thresholds["last_tuned"] = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc
    ).isoformat()

    try:
        conn.execute(
            "INSERT OR REPLACE INTO key_value_store (key, value, updated_at) VALUES (?,?,?)",
            (
                "config:ttsi_thresholds",
                _json.dumps(thresholds),
                __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
            ),
        )
        conn.commit()
    except Exception as e:
        logger.warning(f"Failed to save TT-SI thresholds: {e}")

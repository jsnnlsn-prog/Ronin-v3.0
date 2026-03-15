"""
JARVIS Capability Matcher — Task-to-Agent Routing
===================================================
Scores registered agents by skill overlap and status to find
the best agent for a given task.
"""

import re
from typing import Dict, List, Optional, Set, Tuple

from agent_cards import AgentCard, AgentRegistry, AgentStatus


# Stopwords for keyword extraction (same set as token_optimizer.py)
_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "shall", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "as", "into", "through", "during",
    "before", "after", "above", "below", "between", "under", "again",
    "further", "then", "once", "here", "there", "when", "where", "why",
    "how", "all", "each", "every", "both", "few", "more", "most", "other",
    "some", "such", "no", "not", "only", "own", "same", "so", "than",
    "too", "very", "just", "because", "but", "and", "or", "if", "this",
    "that", "these", "those", "i", "me", "my", "we", "our", "you", "your",
    "it", "its", "they", "them", "their", "what", "which", "who",
}


def _extract_keywords(text: str) -> Set[str]:
    """Extract meaningful keywords, excluding stopwords."""
    words = re.findall(r'[a-zA-Z_][a-zA-Z0-9_]*', text.lower())
    return {w for w in words if w not in _STOPWORDS and len(w) > 2}


def _score_agent(
    card: AgentCard,
    task_keywords: Set[str],
    required_skills: List[str],
) -> float:
    """
    Score an agent's fit for a task.

    Components:
      - Skill ID exact match (0.5 weight per match)
      - Keyword overlap between task description and agent skills (0.3 weight)
      - Agent status bonus (online=1.0, degraded=0.5, offline=0.0)
    """
    if card.status == AgentStatus.offline:
        return 0.0

    score = 0.0

    # 1. Exact skill ID matches
    agent_skill_ids = {s.id for s in card.skills}
    if required_skills:
        matching = len(agent_skill_ids & set(required_skills))
        total = len(required_skills)
        score += (matching / max(total, 1)) * 0.5

    # 2. Keyword overlap between task and agent description + skill descriptions
    agent_text = f"{card.description} " + " ".join(
        f"{s.name} {s.description}" for s in card.skills
    )
    agent_keywords = _extract_keywords(agent_text)

    if task_keywords and agent_keywords:
        overlap = len(task_keywords & agent_keywords)
        union = len(task_keywords | agent_keywords)
        jaccard = overlap / max(union, 1)
        score += jaccard * 0.3

    # 3. Status bonus
    status_bonus = {"online": 0.2, "degraded": 0.1, "offline": 0.0}
    score += status_bonus.get(card.status.value, 0.0)

    return round(score, 4)


def match_task_to_agent(
    registry: AgentRegistry,
    task_description: str,
    required_skills: Optional[List[str]] = None,
    exclude_agents: Optional[List[str]] = None,
) -> List[Tuple[AgentCard, float]]:
    """
    Find the best agent(s) for a given task.

    Args:
        registry: The agent registry to search
        task_description: Natural language description of the task
        required_skills: Optional list of skill IDs that are required
        exclude_agents: Optional list of agent names to exclude (e.g., "cortex" to avoid self-delegation)

    Returns:
        List of (AgentCard, score) tuples, sorted by score descending.
        Only includes agents with score > 0.
    """
    required_skills = required_skills or []
    exclude_agents = set(exclude_agents or [])
    task_keywords = _extract_keywords(task_description)

    scored: List[Tuple[AgentCard, float]] = []
    for card in registry.list_all():
        if card.name in exclude_agents:
            continue
        if card.status == AgentStatus.offline:
            continue

        s = _score_agent(card, task_keywords, required_skills)
        if s > 0:
            scored.append((card, s))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored

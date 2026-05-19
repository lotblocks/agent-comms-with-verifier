"""
Memory persistence for the agent-comms-with-verifier LangGraph port.

MAPPING FROM ORIGINAL SYSTEM
=============================

Original: agent_memory.py
  - SQLite-backed store for gap reports and reputation tracking
  - store_gap(skill, gap_text) with dedup
  - store_reputation(agent, skill, outcome) with counters
  - claim_guidelines_for_skill(skill) returns accumulated gap knowledge

LangGraph port:
  - Gap reports and reputation are stored in a lightweight in-memory
    dict (for the reference port) that mirrors the SQLite schema
  - In production, these would be persisted via LangGraph's checkpointer
    or a separate database
  - claim_guidelines are rendered as system prompt hints via the
    memory_hints state key

HOW THIS MAPS TO LANGGRAPH CHECKPOINTERS
==========================================

LangGraph provides several checkpointer backends:

  from langgraph.checkpoint.memory import MemorySaver      # in-memory
  from langgraph.checkpoint.sqlite import SqliteSaver       # SQLite
  from langgraph.checkpoint.postgres import PostgresSaver   # Postgres

The checkpointer automatically snapshots the full graph state after
every node execution. This gives you:
  - Replay: re-run from any checkpoint
  - Time-travel: inspect state at any point in execution
  - Persistence: survive process restarts (SQLite/Postgres)

For agent_memory.py's concerns specifically:

  Gap reports:
    - Stored in a custom state key or separate store
    - The checkpointer captures them as part of state snapshots
    - Cross-thread gap dedup requires a shared store (not per-thread state)

  Reputation:
    - Tracks (agent, skill) -> (success_count, failure_count)
    - Needs to persist across graph invocations
    - Best modeled as a separate store alongside the checkpointer

  claim_guidelines:
    - Rendered from accumulated gap reports
    - Injected into state as memory_hints before verifier runs
    - The verifier reads these to focus on historically problematic areas

PRODUCTION PATTERN
==================

For production deployment, replace the in-memory dicts below with:

  checkpointer = SqliteSaver.from_conn_string("./memory.db")
  graph = compiled_graph.with_config(configurable={"checkpointer": checkpointer})

And use a separate table for cross-thread gap/reputation data:

  import sqlite3
  conn = sqlite3.connect("./agent_memory.db")
  # Schema matches agent_memory.py's tables

pip install: langgraph langchain-core
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any


# ---------------------------------------------------------------------------
# In-memory stores (replace with persistent backends in production)
# ---------------------------------------------------------------------------

# Gap reports: skill -> list of gap descriptions (deduped)
# Original: agent_memory.py's gaps table
_gap_store: dict[str, list[str]] = defaultdict(list)

# Reputation: (agent, skill) -> {"verified": int, "failed": int}
# Original: agent_memory.py's reputation table
_reputation_store: dict[tuple[str, str], dict[str, int]] = defaultdict(
    lambda: {"verified": 0, "failed": 0}
)


# ---------------------------------------------------------------------------
# Gap report storage
# ---------------------------------------------------------------------------

def store_gap_report(skill: str, gap_text: str) -> None:
    """Store a gap report for a skill, with dedup.

    MAPPING FROM agent_memory.py:
    ==============================

    Original:
      def store_gap(self, skill: str, gap_text: str) -> None:
          # INSERT OR IGNORE into gaps table (dedup by skill + gap_text hash)
          cursor.execute(
              "INSERT OR IGNORE INTO gaps (skill, gap_hash, gap_text) VALUES (?, ?, ?)",
              (skill, gap_hash, gap_text)
          )

    LangGraph equivalent:
      Gap reports are stored in a dict keyed by skill. Dedup is done
      by checking if the gap text already exists in the list.

    In production, this would write to a SQLite table (via SqliteSaver
    or a separate connection) or to the checkpointer's custom state.

    Args:
        skill: The skill that produced the gap.
        gap_text: Description of the verification gap.
    """
    existing = _gap_store[skill]
    if gap_text not in existing:
        existing.append(gap_text)


def get_gap_reports(skill: str) -> list[str]:
    """Retrieve all gap reports for a skill.

    Original: agent_memory.py's internal query used by claim_guidelines_for_skill.
    """
    return list(_gap_store.get(skill, []))


# ---------------------------------------------------------------------------
# Reputation storage
# ---------------------------------------------------------------------------

def store_reputation(agent: str, skill: str, outcome: str) -> None:
    """Update reputation counters for an (agent, skill) pair.

    MAPPING FROM agent_memory.py:
    ==============================

    Original:
      def store_reputation(self, agent: str, skill: str, outcome: str) -> None:
          # Upsert into reputation table, incrementing the counter
          cursor.execute('''
              INSERT INTO reputation (agent, skill, verified, failed)
              VALUES (?, ?, ?, ?)
              ON CONFLICT(agent, skill) DO UPDATE SET
                  verified = verified + excluded.verified,
                  failed = failed + excluded.failed
          ''', (agent, skill,
                1 if outcome == "verified" else 0,
                1 if outcome == "failed" else 0))

    LangGraph equivalent:
      In-memory dict with (agent, skill) tuple keys. In production,
      this would be a persistent store queried during trust evaluation.

    Args:
        agent: Agent name ("beta", "gamma").
        skill: Skill that was executed.
        outcome: "verified" or "failed".
    """
    key = (agent, skill)
    if outcome in ("verified", "failed"):
        _reputation_store[key][outcome] += 1


def get_reputation(agent: str, skill: str) -> dict[str, int]:
    """Get reputation counters for an (agent, skill) pair.

    Returns:
        Dict with "verified" and "failed" counts.
    """
    key = (agent, skill)
    return dict(_reputation_store[key])


def get_reputation_score(agent: str, skill: str) -> float:
    """Compute a reputation score between 0.0 and 1.0.

    Score = verified / (verified + failed), or 0.5 if no data.
    This is used by the trust_check node to modulate its policy.

    Original: agent_memory.py doesn't compute a score directly,
    but Alpha's trust policy implicitly uses reputation data.
    """
    rep = _reputation_store.get((agent, skill))
    if rep is None:
        return 0.5  # neutral prior
    total = rep["verified"] + rep["failed"]
    if total == 0:
        return 0.5
    return rep["verified"] / total


# ---------------------------------------------------------------------------
# Claim guidelines (memory hints for the verifier)
# ---------------------------------------------------------------------------

def get_memory_hints(skill: str) -> list[str]:
    """Render accumulated gap knowledge as verifier hints.

    MAPPING FROM agent_memory.py:
    ==============================

    Original:
      def claim_guidelines_for_skill(self, skill: str) -> list[str]:
          # Query gaps table for this skill
          # Format each gap as a guideline string
          # Return list of guidelines to inject into verifier prompt

    LangGraph equivalent:
      Read from the gap store and format as natural-language hints.
      These are placed in state["memory_hints"] and read by the
      verifier tool to focus validation on historically problematic areas.

    The key insight: past verification failures become future verification
    priorities. If "negative" claims about data absence failed in previous
    runs, the verifier will pay extra attention to negative claims next time.

    Args:
        skill: The skill to get guidelines for.

    Returns:
        List of guideline strings for the verifier prompt.
    """
    gaps = _gap_store.get(skill, [])
    if not gaps:
        return []

    hints: list[str] = []
    for gap in gaps:
        # Extract claim type from gap text if present
        # Gap format: "[claim_type] claim_text: failure_reason"
        if gap.startswith("["):
            claim_type = gap.split("]")[0].strip("[")
            hints.append(
                f"PRIOR GAP ({claim_type}): {gap}. "
                f"Pay extra attention to {claim_type} claims for this skill."
            )
        else:
            hints.append(f"PRIOR GAP: {gap}. Verify this area carefully.")

    return hints


def render_memory_hints_as_system_prompt(hints: list[str]) -> str:
    """Format memory hints as a system prompt section.

    This is injected into the verifier's prompt to guide claim validation.

    Original: agent_memory.py returns raw guidelines; the verifier
    formats them into its prompt. Here we provide the formatting.
    """
    if not hints:
        return ""

    lines = ["## Verification Guidelines from Prior Runs", ""]
    lines.append(
        "The following gaps were identified in previous verifications of "
        "this skill. Pay extra attention to these areas:"
    )
    lines.append("")
    for hint in hints:
        lines.append(f"- {hint}")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Checkpointer integration pattern
# ---------------------------------------------------------------------------

def create_memory_checkpointer():
    """Create a checkpointer configured for agent-comms memory.

    PRODUCTION PATTERN:
    ===================

    In production, you would use SqliteSaver or PostgresSaver:

        from langgraph.checkpoint.sqlite import SqliteSaver

        checkpointer = SqliteSaver.from_conn_string("./agent_comms.db")

        # The checkpointer automatically captures:
        # - Full graph state after every node execution
        # - Thread-level isolation (each thread_id gets its own history)
        # - Time-travel: inspect/restore any prior state

        # For cross-thread memory (gap reports, reputation), use a
        # separate store that all threads can read from:

        from langgraph.store.memory import InMemoryStore
        # or: from langgraph.store.postgres import PostgresStore

        cross_thread_store = InMemoryStore()

        graph = compiled_graph.compile(
            checkpointer=checkpointer,
            store=cross_thread_store,
        )

    For this reference port, we use MemorySaver (in-memory, no persistence):
    """
    from langgraph.checkpoint.memory import MemorySaver
    return MemorySaver()


# ---------------------------------------------------------------------------
# State key helpers
# ---------------------------------------------------------------------------

def extract_memory_state(state: dict[str, Any]) -> dict[str, Any]:
    """Extract memory-related keys from graph state.

    Useful for debugging and logging. Returns just the memory-relevant
    portions of the state.
    """
    return {
        "memory_hints": state.get("memory_hints", []),
        "chain_summary": state.get("chain_summary"),
        "verification_status": state.get("verification_status"),
        "trust_decision": state.get("trust_decision"),
    }

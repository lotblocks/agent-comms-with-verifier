"""
State schema for the agent-comms-with-verifier LangGraph port.

MAPPING FROM ORIGINAL SYSTEM
=============================

In the original system, state is distributed across several mechanisms:
  - bus.py messages carry payloads between agents (dict with task, data, etc.)
  - run_skill_verified.py returns VerificationRecord dicts
  - verification_chain.py composes chain_summary dicts
  - agent_memory.py maintains gap reports and reputation in SQLite

In LangGraph, ALL of this becomes a single TypedDict that flows through
the graph. Each node reads what it needs and writes what it produces.
The checkpointer snapshots this state after every node execution.

KEY DESIGN DECISIONS
====================

1. We use TypedDict rather than Pydantic models for LangGraph compatibility.
   LangGraph's StateGraph expects TypedDict for channel-based state management.

2. The `messages` field uses LangGraph's built-in message list handling,
   which supports append semantics via the `add_messages` reducer.

3. Complex nested structures (chain_summary, verification records) are
   typed as dicts with documented schemas rather than deeply nested
   TypedDicts — this keeps the state readable in LangGraph Studio.

4. The `memory_hints` field replaces agent_memory.py's
   `claim_guidelines_for_skill()` — these are injected into the verifier
   prompt rather than maintained in a separate SQLite store.

pip install: langgraph langchain-core
"""

from __future__ import annotations

import operator
from typing import Any, Literal, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph import add_messages


# ---------------------------------------------------------------------------
# Verification record schema (returned by verifier_tool.py)
# Maps to: run_skill_verified.py's VerificationRecord
# ---------------------------------------------------------------------------

class VerificationRecord(TypedDict, total=False):
    """Result of running the verifier on a single builder output.

    Original: This is what run_skill_verified() returns after claim
    extraction, validation, remediation, and gap reporting.
    """
    skill: str                        # which skill/builder was verified
    status: str                       # "verified" | "partial" | "failed"
    claims_checked: int               # total claims extracted
    claims_passed: int                # claims that passed validation
    claims_failed: int                # claims that failed validation
    claim_details: list[dict]         # per-claim breakdown
    remediation_applied: bool         # whether remediation was attempted
    gap_report: list[str]             # unresolvable gaps found
    cost_usd: float                   # verification cost
    duration_ms: int                  # verification wall-clock time
    builder_model: str                # model that produced the output
    verifier_model: str               # model that verified it


# ---------------------------------------------------------------------------
# Chain summary schema (composed by verification_chain.py logic)
# Maps to: verification_chain.py's build_chain_summary()
# ---------------------------------------------------------------------------

class HopTrace(TypedDict, total=False):
    """One hop in a multi-agent verification chain.

    Original: Each entry in walk_chain()'s output list.
    """
    agent: str                        # "beta" | "gamma"
    skill: str                        # what skill this hop executed
    status: str                       # "verified" | "partial" | "failed"
    cost_usd: float
    duration_ms: int
    gap_report: list[str]


class ChainSummary(TypedDict, total=False):
    """Aggregated verification result across all hops.

    Original: verification_chain.py build_chain_summary() output.
    The chain_status is the WEAKEST link — if any hop is "failed",
    the whole chain is "failed". If any is "partial", chain is "partial".
    """
    chain_status: str                 # weakest-link: verified|partial|failed
    total_cost_usd: float             # sum across all hops
    total_duration_ms: int            # sum across all hops
    hops: list[HopTrace]             # per-hop detail, ordered root-to-leaf
    merged_gap_report: list[str]      # union of all per-hop gaps, deduped


# ---------------------------------------------------------------------------
# Main graph state
# ---------------------------------------------------------------------------

class AgentCommsState(TypedDict, total=False):
    """Top-level state for the agent-comms-with-verifier graph.

    FIELD MAPPING:
    ==============

    messages
        LangGraph standard — accumulates LLM message history.
        Uses `add_messages` reducer for automatic append semantics.
        Original: No direct equivalent (bus messages were fire-and-forget).

    request
        The task that Alpha creates and passes to downstream agents.
        Original: The payload in bus.send_direct("beta", {...}).
        Contains: task (str), skill (str), payload (dict), intent (str).

    beta_result
        Beta's builder output plus its self-verification record.
        Original: Beta's response message on the bus, containing
        the builder output and the chain_summary for its single hop.

    gamma_result
        Gamma's composed output plus its multi-hop chain_summary.
        Original: Gamma's response message, which includes upstream
        data from Beta composed with Gamma's own builder output.

    chain_summary
        The fully composed verification chain across all hops.
        Original: verification_chain.py build_chain_summary() output.
        This is what Alpha's trust policy evaluates.

    memory_hints
        Prior gap reports and claim guidelines retrieved from memory.
        Original: agent_memory.py claim_guidelines_for_skill(skill).
        These are injected into the verifier's system prompt to focus
        verification on historically problematic areas.

    verification_status
        Roll-up status from the chain_summary.
        Original: The chain_status field from build_chain_summary().
        One of: "verified", "partial", "failed".

    trust_decision
        Alpha's final disposition after evaluating the chain.
        Original: Alpha's trust policy in agent_alpha.py.
        One of: "accept", "reject", "escalate".
    """

    # -- LangGraph message accumulator --
    messages: list[BaseMessage]

    # -- Request from Alpha --
    request: dict[str, Any]

    # -- Beta's output --
    beta_result: dict[str, Any]

    # -- Gamma's output (three-agent graph only) --
    gamma_result: dict[str, Any]

    # -- Composed verification chain --
    chain_summary: ChainSummary

    # -- Memory-derived hints for the verifier --
    memory_hints: list[str]

    # -- Roll-up verification status --
    verification_status: Literal["verified", "partial", "failed", "pending"]

    # -- Alpha's trust decision --
    trust_decision: Literal["accept", "reject", "escalate", "pending"]


# ---------------------------------------------------------------------------
# State reducer configuration
# ---------------------------------------------------------------------------
# LangGraph uses "channels" to define how each state key is updated.
# The default is last-writer-wins. For `messages`, we use `add_messages`
# which appends rather than replacing.
#
# In graph.py, this is configured as:
#
#   graph = StateGraph(AgentCommsState)
#
# With the annotation:
#   from typing import Annotated
#   messages: Annotated[list[BaseMessage], add_messages]
#
# For chain_summary, we use a custom reducer that performs weakest-link
# merge when a new hop is added. See graph.py for the reducer function.
# ---------------------------------------------------------------------------


def merge_chain_summary(
    existing: ChainSummary | None,
    new_hop: HopTrace,
) -> ChainSummary:
    """Reducer: merge a new hop into the chain summary.

    This implements the weakest-link aggregation from
    verification_chain.py's build_chain_summary().

    The status hierarchy is: verified > partial > failed.
    The chain_status is always the weakest (worst) status
    across all hops.

    Args:
        existing: Current chain summary, or None for the first hop.
        new_hop: The new hop trace to merge in.

    Returns:
        Updated ChainSummary with the new hop incorporated.
    """
    STATUS_RANK = {"verified": 2, "partial": 1, "failed": 0}

    if existing is None:
        return ChainSummary(
            chain_status=new_hop.get("status", "pending"),
            total_cost_usd=new_hop.get("cost_usd", 0.0),
            total_duration_ms=new_hop.get("duration_ms", 0),
            hops=[new_hop],
            merged_gap_report=list(new_hop.get("gap_report", [])),
        )

    hops = list(existing.get("hops", []))
    hops.append(new_hop)

    # Weakest-link status
    all_statuses = [h.get("status", "failed") for h in hops]
    weakest = min(all_statuses, key=lambda s: STATUS_RANK.get(s, -1))

    # Sum costs and durations
    total_cost = sum(h.get("cost_usd", 0.0) for h in hops)
    total_duration = sum(h.get("duration_ms", 0) for h in hops)

    # Merge gap reports with dedup
    seen_gaps: set[str] = set()
    merged_gaps: list[str] = []
    for hop in hops:
        for gap in hop.get("gap_report", []):
            if gap not in seen_gaps:
                seen_gaps.add(gap)
                merged_gaps.append(gap)

    return ChainSummary(
        chain_status=weakest,
        total_cost_usd=total_cost,
        total_duration_ms=total_duration,
        hops=hops,
        merged_gap_report=merged_gaps,
    )


# ---------------------------------------------------------------------------
# Annotated state for use with StateGraph
# ---------------------------------------------------------------------------
# LangGraph supports Annotated types for specifying reducers per field.
# This version uses add_messages for the messages field. Other fields
# use default last-writer-wins semantics (nodes write the full value).
# ---------------------------------------------------------------------------

from typing import Annotated


class AnnotatedAgentCommsState(TypedDict, total=False):
    """State with LangGraph reducer annotations.

    Use this as the state type when building the StateGraph:
        graph = StateGraph(AnnotatedAgentCommsState)

    The Annotated[..., add_messages] on `messages` tells LangGraph
    to append new messages rather than replacing the list.
    """
    messages: Annotated[list[BaseMessage], add_messages]
    request: dict[str, Any]
    beta_result: dict[str, Any]
    gamma_result: dict[str, Any]
    chain_summary: ChainSummary
    memory_hints: list[str]
    verification_status: Literal["verified", "partial", "failed", "pending"]
    trust_decision: Literal["accept", "reject", "escalate", "pending"]

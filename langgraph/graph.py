"""
Main graph definition for the agent-comms-with-verifier LangGraph port.

This file defines two graph variants:
  1. Two-agent graph:   Alpha --> Beta --> trust_check --> routing
  2. Three-agent graph: Alpha --> Beta --> Gamma --> trust_check --> routing

Both use conditional edges for trust-based routing after verification.

MAPPING FROM ORIGINAL SYSTEM
=============================

Original flow (bus-based):
  Alpha --(bus.send_direct)--> Beta
  Beta runs builder, verifies, responds via bus
  Alpha --(bus.receive)--> inspects chain_summary
  Alpha applies trust policy

LangGraph flow (state-graph):
  Alpha node writes request to state
  State flows along edge to Beta node
  Beta reads request, builds, verifies, writes result to state
  State flows to trust_check node
  trust_check evaluates chain_summary, sets trust_decision
  Conditional edge routes to accept/reject/escalate

The bus is GONE. The SQLite message queue, agent registration, topic
pub/sub, and atomic claim-locking are all replaced by state flow along
graph edges. Each node is a pure function: state in, state updates out.

RUNNING THIS FILE
=================

Mock mode (no API keys needed):
    python graph.py

This exercises the full graph with mock builder and verifier backends,
demonstrating:
  - State flow through all nodes
  - Verification with claim extraction and validation
  - Chain summary composition with weakest-link aggregation
  - Trust policy evaluation with conditional routing
  - Memory hint injection from prior gap reports

pip install: langgraph langchain-core langchain-anthropic
"""

from __future__ import annotations

import json
import sys
from typing import Any

from langgraph.graph import END, StateGraph

from state import AnnotatedAgentCommsState, ChainSummary
from nodes import (
    alpha_node,
    beta_node,
    gamma_node,
    trust_check,
    should_continue,
)
from memory_saver import create_memory_checkpointer


# ---------------------------------------------------------------------------
# Graph 1: Two-Agent (Alpha --> Beta)
# ---------------------------------------------------------------------------

def build_two_agent_graph() -> StateGraph:
    """Build the simple two-agent graph: Alpha delegates to Beta.

    TOPOLOGY:
    =========

        alpha --> beta --> trust_check --+--> accept --> END
                                        |
                                        +--> reject --> END
                                        |
                                        +--> escalate --> END

    MAPPING FROM ORIGINAL:
    ======================

    This models the simplest case in the original system: Alpha sends
    a task directly to Beta, Beta verifies and responds, Alpha evaluates.

    In bus terms:
      1. bus.send_direct("beta", task)       --> edge: alpha -> beta
      2. Beta runs + verifies                --> beta node
      3. bus.send_direct("alpha", response)  --> edge: beta -> trust_check
      4. Alpha evaluates chain_summary       --> trust_check node
      5. Alpha decides                       --> should_continue conditional

    Returns:
        Compiled StateGraph ready for invocation.
    """
    graph = StateGraph(AnnotatedAgentCommsState)

    # --- Add nodes ---
    # Each node is a function that takes state and returns state updates.
    # Original: Each agent is an independent process on the bus.
    # LangGraph: Each agent is a pure function in the graph.
    graph.add_node("alpha", alpha_node)
    graph.add_node("beta", beta_node)
    graph.add_node("trust_check", trust_check)

    # Terminal nodes for each trust decision outcome
    # These are pass-through nodes that just mark the decision in state.
    graph.add_node("accept", _accept_node)
    graph.add_node("reject", _reject_node)
    graph.add_node("escalate", _escalate_node)

    # --- Add edges ---
    # Original: bus.send_direct() between agents
    # LangGraph: Explicit edges define the communication topology
    graph.set_entry_point("alpha")
    graph.add_edge("alpha", "beta")
    graph.add_edge("beta", "trust_check")

    # --- Conditional edge: trust routing ---
    # Original: Alpha's trust policy loop with if/elif/else
    # LangGraph: Conditional edge that reads trust_decision from state
    graph.add_conditional_edges(
        "trust_check",
        should_continue,
        {
            "accept": "accept",
            "reject": "reject",
            "escalate": "escalate",
        },
    )

    # Terminal edges
    graph.add_edge("accept", END)
    graph.add_edge("reject", END)
    graph.add_edge("escalate", END)

    return graph


# ---------------------------------------------------------------------------
# Graph 2: Three-Agent (Alpha --> Beta --> Gamma)
# ---------------------------------------------------------------------------

def build_three_agent_graph() -> StateGraph:
    """Build the three-agent graph: Alpha delegates to Gamma, which peers with Beta.

    TOPOLOGY:
    =========

        alpha --> beta --> gamma --> trust_check --+--> accept --> END
                                                  |
                                                  +--> reject --> END
                                                  |
                                                  +--> escalate --> END

    MAPPING FROM ORIGINAL:
    ======================

    This models the multi-hop case: Alpha sends to Gamma, Gamma peers
    with Beta for upstream data, Gamma composes and verifies, Alpha
    evaluates the full chain.

    In bus terms:
      1. bus.send_direct("gamma", task)       --> Alpha creates request
      2. Gamma: bus.send_direct("beta", ...)  --> Beta runs first (graph edge)
      3. Beta runs + verifies                 --> beta node
      4. Gamma receives Beta's response       --> gamma reads beta_result from state
      5. Gamma composes + verifies            --> gamma node
      6. bus.send_direct("alpha", response)   --> edge: gamma -> trust_check
      7. Alpha evaluates multi-hop chain      --> trust_check node

    SUBGRAPH COMPOSITION NOTE:
    ==========================

    In the original system, Gamma actively sends a message to Beta and
    waits for the response. In LangGraph, we model this as Beta running
    BEFORE Gamma in the graph topology. The edge alpha->beta->gamma
    ensures Beta's result is in state when Gamma executes.

    An alternative LangGraph pattern is to make Beta a subgraph that
    Gamma invokes:

        beta_subgraph = build_beta_subgraph().compile()
        def gamma_node_with_subgraph(state):
            beta_result = beta_subgraph.invoke(state)
            # ... compose with beta_result

    Both approaches preserve the invariant that Beta's output is verified
    before Gamma uses it. We use the simpler flat topology here.

    Returns:
        Compiled StateGraph ready for invocation.
    """
    graph = StateGraph(AnnotatedAgentCommsState)

    # --- Add nodes ---
    graph.add_node("alpha", alpha_node)
    graph.add_node("beta", beta_node)
    graph.add_node("gamma", gamma_node)
    graph.add_node("trust_check", trust_check)
    graph.add_node("accept", _accept_node)
    graph.add_node("reject", _reject_node)
    graph.add_node("escalate", _escalate_node)

    # --- Add edges ---
    # Alpha creates the request, Beta runs first for upstream data,
    # Gamma composes with Beta's result, then trust evaluation.
    graph.set_entry_point("alpha")
    graph.add_edge("alpha", "beta")
    graph.add_edge("beta", "gamma")
    graph.add_edge("gamma", "trust_check")

    # --- Conditional edge ---
    graph.add_conditional_edges(
        "trust_check",
        should_continue,
        {
            "accept": "accept",
            "reject": "reject",
            "escalate": "escalate",
        },
    )

    graph.add_edge("accept", END)
    graph.add_edge("reject", END)
    graph.add_edge("escalate", END)

    return graph


# ---------------------------------------------------------------------------
# Terminal nodes
# ---------------------------------------------------------------------------

def _accept_node(state: AnnotatedAgentCommsState) -> dict[str, Any]:
    """Terminal node for accepted results.

    Original: Alpha logs the accepted result and returns it to the user.
    """
    from langchain_core.messages import AIMessage

    chain = state.get("chain_summary", {})
    return {
        "messages": [
            AIMessage(content=(
                f"ACCEPTED. Chain verified across {len(chain.get('hops', []))} "
                f"hop(s). Total cost: ${chain.get('total_cost_usd', 0):.4f}. "
                f"Total duration: {chain.get('total_duration_ms', 0)}ms."
            )),
        ],
    }


def _reject_node(state: AnnotatedAgentCommsState) -> dict[str, Any]:
    """Terminal node for rejected results.

    Original: Alpha logs the rejection with gap details and optionally retries.
    """
    from langchain_core.messages import AIMessage

    chain = state.get("chain_summary", {})
    gaps = chain.get("merged_gap_report", [])
    return {
        "messages": [
            AIMessage(content=(
                f"REJECTED. Chain status: {chain.get('chain_status', 'unknown')}. "
                f"Unresolved gaps ({len(gaps)}): "
                + "; ".join(gaps[:3])
                + ("..." if len(gaps) > 3 else "")
            )),
        ],
    }


def _escalate_node(state: AnnotatedAgentCommsState) -> dict[str, Any]:
    """Terminal node for escalated results.

    Original: Alpha flags the result for human review, preserving
    the chain_summary so the reviewer can see exactly what's uncertain.
    """
    from langchain_core.messages import AIMessage

    chain = state.get("chain_summary", {})
    return {
        "messages": [
            AIMessage(content=(
                f"ESCALATED for human review. Chain status: "
                f"{chain.get('chain_status', 'unknown')}. "
                f"Gaps requiring human judgment: "
                + "; ".join(chain.get("merged_gap_report", [])[:3])
            )),
        ],
    }


# ---------------------------------------------------------------------------
# Compilation helpers
# ---------------------------------------------------------------------------

def compile_two_agent_graph():
    """Compile the two-agent graph with memory checkpointer.

    Returns a compiled graph ready for invocation:
        result = compiled.invoke(initial_state, config={"configurable": {"thread_id": "1"}})
    """
    graph = build_two_agent_graph()
    checkpointer = create_memory_checkpointer()
    return graph.compile(checkpointer=checkpointer)


def compile_three_agent_graph():
    """Compile the three-agent graph with memory checkpointer."""
    graph = build_three_agent_graph()
    checkpointer = create_memory_checkpointer()
    return graph.compile(checkpointer=checkpointer)


# ---------------------------------------------------------------------------
# Pretty-print helpers
# ---------------------------------------------------------------------------

def _format_state(state: dict[str, Any]) -> str:
    """Format final state for readable output."""
    lines = []
    lines.append("=" * 70)

    # Trust decision
    decision = state.get("trust_decision", "unknown")
    lines.append(f"  Trust Decision: {decision.upper()}")
    lines.append(f"  Verification Status: {state.get('verification_status', 'unknown')}")

    # Chain summary
    chain = state.get("chain_summary")
    if chain:
        lines.append("")
        lines.append(f"  Chain Status: {chain.get('chain_status', 'unknown')}")
        lines.append(f"  Total Cost: ${chain.get('total_cost_usd', 0):.4f}")
        lines.append(f"  Total Duration: {chain.get('total_duration_ms', 0)}ms")
        lines.append(f"  Hops: {len(chain.get('hops', []))}")

        for i, hop in enumerate(chain.get("hops", [])):
            lines.append(f"    Hop {i+1}: agent={hop.get('agent')}, "
                        f"skill={hop.get('skill')}, "
                        f"status={hop.get('status')}, "
                        f"gaps={len(hop.get('gap_report', []))}")

        gaps = chain.get("merged_gap_report", [])
        if gaps:
            lines.append(f"  Merged Gaps ({len(gaps)}):")
            for gap in gaps[:5]:
                lines.append(f"    - {gap[:80]}{'...' if len(gap) > 80 else ''}")

    # Memory hints
    hints = state.get("memory_hints", [])
    if hints:
        lines.append(f"\n  Memory Hints ({len(hints)}):")
        for hint in hints[:3]:
            lines.append(f"    - {hint[:80]}{'...' if len(hint) > 80 else ''}")

    # Messages (last 3)
    messages = state.get("messages", [])
    if messages:
        lines.append(f"\n  Messages (last 3 of {len(messages)}):")
        for msg in messages[-3:]:
            content = msg.content if hasattr(msg, "content") else str(msg)
            lines.append(f"    [{type(msg).__name__}] {content[:90]}{'...' if len(content) > 90 else ''}")

    lines.append("=" * 70)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main: demo invocation
# ---------------------------------------------------------------------------

def main():
    """Run the demo: invoke both graphs with mock backends.

    This demonstrates the full flow:
      1. Two-agent graph (Alpha -> Beta)
      2. Three-agent graph (Alpha -> Beta -> Gamma)

    Both use mock builder and verifier backends, so no API keys are needed.
    The output shows state flow, verification results, chain summary
    composition, and trust policy decisions.
    """
    print("=" * 70)
    print("  LangGraph Reference Port: Agent-Comms-with-Verifier")
    print("  Mock backends (no API keys needed)")
    print("=" * 70)

    # --- Two-agent graph ---
    print("\n\n--- GRAPH 1: Two-Agent (Alpha -> Beta) ---\n")

    two_agent = compile_two_agent_graph()

    # Initial state with a request
    initial_state: dict[str, Any] = {
        "request": {
            "task": "analyze_quarterly_data",
            "skill": "data_analysis",
            "payload": {"dataset": "q4_2025_revenue"},
            "intent": "Analyze Q4 2025 revenue data and identify trends",
        },
        "messages": [],
    }

    # Invoke the graph
    # Original: Alpha sends task via bus, Beta processes, Alpha evaluates
    # LangGraph: Single invoke() call runs the full pipeline
    config = {"configurable": {"thread_id": "two-agent-demo"}}
    result_2 = two_agent.invoke(initial_state, config=config)

    print("Final State (Two-Agent):")
    print(_format_state(result_2))

    # --- Three-agent graph ---
    print("\n\n--- GRAPH 2: Three-Agent (Alpha -> Beta -> Gamma) ---\n")

    three_agent = compile_three_agent_graph()

    initial_state_3: dict[str, Any] = {
        "request": {
            "task": "compose_multi_source_analysis",
            "skill": "data_analysis",
            "payload": {
                "dataset": "q4_2025_revenue",
                "cross_reference": "market_benchmark",
            },
            "intent": "Compose multi-source analysis of Q4 revenue vs market",
        },
        "messages": [],
    }

    config_3 = {"configurable": {"thread_id": "three-agent-demo"}}
    result_3 = three_agent.invoke(initial_state_3, config=config_3)

    print("Final State (Three-Agent):")
    print(_format_state(result_3))

    # --- Second invocation (shows memory hints from prior gaps) ---
    print("\n\n--- GRAPH 1 (Second Run): Memory Hints from Prior Gaps ---\n")

    # Re-run the two-agent graph. If the first run produced gap reports,
    # this run will have memory_hints populated, showing how past
    # verification failures inform future verification.
    config_2b = {"configurable": {"thread_id": "two-agent-demo-round-2"}}
    result_2b = two_agent.invoke(initial_state, config=config_2b)

    print("Final State (Two-Agent, Round 2):")
    print(_format_state(result_2b))

    hints = result_2b.get("memory_hints", [])
    if hints:
        print(f"\n  Memory hints were active ({len(hints)} hints from prior gaps).")
        print("  This demonstrates claim_guidelines_for_skill() in action:")
        print("  past failures focus future verification.\n")
    else:
        print("\n  No memory hints (first run had no gaps, or skill name differs).\n")

    print("=" * 70)
    print("  Demo complete. See README.md for architecture mapping details.")
    print("=" * 70)


if __name__ == "__main__":
    main()

"""
Node functions for the agent-comms-with-verifier LangGraph port.

MAPPING FROM ORIGINAL SYSTEM
=============================

Each node function corresponds to an agent in the original system:

  alpha_node    <-->  agent_alpha.py   (requester / orchestrator)
  beta_node     <-->  agent_beta.py    (leaf worker)
  gamma_node    <-->  agent_gamma.py   (intermediate worker)
  trust_check   <-->  Alpha's trust policy loop
  should_continue <-->  Alpha's accept/reject/escalate decision

ORIGINAL FLOW (bus-based):
  1. Alpha registers on bus, sends task to Beta (or Gamma)
  2. Beta receives task, runs builder, calls run_skill_verified(), responds
  3. Alpha receives response, inspects chain_summary, applies trust policy
  4. If Gamma is involved: Gamma receives from Alpha, peers with Beta for
     upstream data, composes multi-hop chain_summary, responds to Alpha

LANGGRAPH FLOW (state-graph):
  1. alpha_node writes request into state
  2. beta_node reads request, produces builder output, verifies, writes result
  3. trust_check reads chain_summary, sets trust_decision
  4. should_continue routes to END (accept/reject) or escalation
  5. For three-agent: gamma_node sits between alpha and beta, composes chains

KEY DIFFERENCE: In the original system, each agent is an independent process
polling the bus. In LangGraph, they are pure functions that read from and
write to state. The graph topology defines the communication pattern.

pip install: langgraph langchain-core langchain-anthropic
"""

from __future__ import annotations

import time
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from state import (
    AgentCommsState,
    ChainSummary,
    HopTrace,
    VerificationRecord,
    merge_chain_summary,
)
from verifier_tool import verify_output
from memory_saver import get_memory_hints, store_gap_report, store_reputation


# ---------------------------------------------------------------------------
# Helper: mock builder call
# ---------------------------------------------------------------------------

def _mock_builder(skill: str, payload: dict[str, Any], intent: str) -> str:
    """Simulate a builder LLM call.

    Original: Each agent calls its builder (an LLM with skill-specific
    prompts) to produce output. In a real deployment, this would be:

        from langchain_anthropic import ChatAnthropic
        llm = ChatAnthropic(model="claude-sonnet-4-20250514")
        response = llm.invoke([SystemMessage(content=skill_prompt), ...])

    For Haiku leaf work (Beta):
        llm = ChatAnthropic(model="claude-3-5-haiku-20241022")

    This mock returns deterministic output for demo purposes.
    """
    return (
        f"Builder output for skill '{skill}' addressing intent: {intent}. "
        f"The analysis found three key results present in the data. "
        f"Revenue contains a 15% increase over the prior quarter. "
        f"When adjusted for seasonality, this results in a 12% normalized gain. "
        f"The dataset does not contain any anomalous entries. "
        f"The methodology equals standard industry practice."
    )


# ---------------------------------------------------------------------------
# Alpha node
# ---------------------------------------------------------------------------

def alpha_node(state: AgentCommsState) -> dict[str, Any]:
    """Alpha: the requester/orchestrator agent.

    MAPPING FROM agent_alpha.py:
    ============================

    Original Alpha:
      1. Registers on bus as "alpha"
      2. Constructs a task with skill, payload, intent
      3. Sends task to the downstream agent (beta or gamma) via bus
      4. Polls bus.receive() waiting for the response
      5. Inspects chain_summary in the response
      6. Applies trust policy: accept / reject / escalate

    LangGraph Alpha:
      1. Reads any existing state (for multi-turn scenarios)
      2. Constructs the request dict and writes it to state
      3. The graph edge carries the request to the next node
      4. Trust evaluation happens in a separate trust_check node
         (this separation makes the conditional edge cleaner)

    Alpha does NOT call the builder — it delegates to Beta or Gamma.
    Alpha does NOT verify — it evaluates the chain_summary from downstream.

    Model recommendation: Claude Sonnet 4 (strong reasoning for trust
    evaluation, cost-effective for orchestration).
    """
    # In a real deployment, Alpha might use an LLM to formulate the request
    # based on user input. For this reference port, we construct it directly.
    request = state.get("request")

    if request is None:
        # Default request for demo purposes
        request = {
            "task": "analyze_quarterly_data",
            "skill": "data_analysis",
            "payload": {
                "dataset": "q4_2025_revenue",
                "metrics": ["revenue", "growth_rate", "anomalies"],
            },
            "intent": "Analyze Q4 2025 revenue data and identify trends",
            "requested_by": "alpha",
            "timestamp_ms": int(time.time() * 1000),
        }

    return {
        "request": request,
        "messages": [
            SystemMessage(content=(
                "You are Alpha, the orchestrator agent. You create tasks, "
                "delegate to downstream agents, and evaluate verification "
                "chains using trust policy."
            )),
            HumanMessage(content=(
                f"Creating request: task={request['task']}, "
                f"skill={request['skill']}, "
                f"intent={request['intent']}"
            )),
        ],
        "verification_status": "pending",
        "trust_decision": "pending",
    }


# ---------------------------------------------------------------------------
# Beta node
# ---------------------------------------------------------------------------

def beta_node(state: AgentCommsState) -> dict[str, Any]:
    """Beta: the leaf worker agent.

    MAPPING FROM agent_beta.py:
    ===========================

    Original Beta:
      1. Registers on bus as "beta"
      2. Polls bus.receive() for incoming tasks
      3. Runs the builder (LLM with skill prompts) to produce output
      4. Calls run_skill_verified() on its OWN output (self-verification)
      5. Composes a single-hop chain_summary
      6. Responds to the requester via bus.send_direct()

    LangGraph Beta:
      1. Reads request from state
      2. Retrieves memory hints for the skill (prior gap guidelines)
      3. Runs the builder to produce output
      4. Calls verify_output() on the builder output (self-verification)
      5. Composes a single-hop chain_summary
      6. Writes beta_result and chain_summary to state

    The critical invariant preserved: Beta verifies its own output BEFORE
    it becomes visible to any downstream node. This is the
    "verify-before-respond" pattern.

    Model recommendation: Claude Haiku 3.5 for the builder (high throughput,
    low cost for leaf work). The verifier should be cross-family (e.g., GPT-4o).
    """
    request = state["request"]
    skill = request["skill"]
    intent = request["intent"]
    payload = request.get("payload", {})

    # --- Retrieve memory hints ---
    # Original: agent_memory.py claim_guidelines_for_skill(skill)
    # These are prior gap reports that inform the verifier where to focus.
    memory_hints = get_memory_hints(skill)

    # --- Run builder ---
    # Original: Beta calls its builder LLM with skill-specific prompts.
    # Model: Claude Haiku 3.5 for cost-effective leaf work.
    start_ms = int(time.time() * 1000)
    builder_output = _mock_builder(skill, payload, intent)
    builder_duration_ms = int(time.time() * 1000) - start_ms

    # --- Self-verification ---
    # Original: Beta calls run_skill_verified() on its own output.
    # This is the core "verify-before-respond" pattern.
    verification_record: VerificationRecord = verify_output(
        builder_output=builder_output,
        skill=skill,
        skill_docs=f"Documentation for {skill}: standard analysis methodology.",
        intent=intent,
        memory_hints=memory_hints,
        builder_model="claude-3-5-haiku-20241022",  # recommended for Beta
    )

    # --- Compose single-hop chain_summary ---
    # Original: Beta builds a chain_summary with just its own hop.
    hop = HopTrace(
        agent="beta",
        skill=skill,
        status=verification_record["status"],
        cost_usd=verification_record.get("cost_usd", 0.0),
        duration_ms=verification_record.get("duration_ms", 0) + builder_duration_ms,
        gap_report=verification_record.get("gap_report", []),
    )

    chain_summary = merge_chain_summary(
        existing=state.get("chain_summary"),
        new_hop=hop,
    )

    # --- Store gaps in memory ---
    # Original: agent_memory.py store_gap() for each gap found
    for gap in verification_record.get("gap_report", []):
        store_gap_report(skill, gap)

    beta_result = {
        "builder_output": builder_output,
        "verification_record": dict(verification_record),
        "agent": "beta",
        "skill": skill,
    }

    return {
        "beta_result": beta_result,
        "chain_summary": chain_summary,
        "memory_hints": memory_hints,
        "verification_status": chain_summary["chain_status"],
        "messages": [
            AIMessage(content=(
                f"Beta completed task '{request['task']}' with skill '{skill}'. "
                f"Verification status: {verification_record['status']}. "
                f"Claims checked: {verification_record['claims_checked']}, "
                f"passed: {verification_record['claims_passed']}, "
                f"failed: {verification_record['claims_failed']}."
            )),
        ],
    }


# ---------------------------------------------------------------------------
# Gamma node
# ---------------------------------------------------------------------------

def gamma_node(state: AgentCommsState) -> dict[str, Any]:
    """Gamma: the intermediate worker agent.

    MAPPING FROM agent_gamma.py:
    ============================

    Original Gamma:
      1. Registers on bus as "gamma"
      2. Receives task from Alpha via bus
      3. Peers with Beta via bus.send_direct("beta", upstream_request)
         to get upstream data that Gamma needs for its own work
      4. Receives Beta's response (with Beta's chain_summary)
      5. Runs its own builder using upstream data + original task
      6. Calls run_skill_verified() on its own output
      7. Composes a MULTI-HOP chain_summary that includes Beta's hop
      8. Responds to Alpha via bus.send_direct("alpha", response)

    LangGraph Gamma:
      1. Reads request from state
      2. Invokes the beta subgraph to get upstream data
         (In the full graph.py, this is modeled as Beta running first,
          then Gamma reading beta_result from state)
      3. Runs its own builder with upstream data + original request
      4. Calls verify_output() on its own output
      5. Extends the chain_summary with its own hop (Beta's hop is
         already there from step 2)
      6. Writes gamma_result and updated chain_summary to state

    The key difference: In the original system, Gamma actively peers with
    Beta via the bus. In LangGraph, the graph topology ensures Beta runs
    before Gamma, and Gamma reads Beta's result from state. The effect
    is the same — Gamma has access to verified upstream data.

    Model recommendation: Claude Sonnet 4 (multi-hop composition
    requires strong reasoning, similar complexity to Alpha).
    """
    request = state["request"]
    skill = request["skill"]
    intent = request["intent"]

    # --- Read upstream data from Beta ---
    # Original: Gamma sends a peer request to Beta via bus and waits.
    # LangGraph: Beta has already run (graph topology), result is in state.
    beta_result = state.get("beta_result")
    if beta_result is None:
        # Beta hasn't run yet — this shouldn't happen in a correctly
        # wired graph, but we handle it gracefully.
        return {
            "gamma_result": {
                "error": "No upstream data from Beta",
                "agent": "gamma",
            },
            "verification_status": "failed",
            "messages": [
                AIMessage(content="Gamma: No upstream data from Beta available."),
            ],
        }

    upstream_output = beta_result["builder_output"]
    upstream_verification = beta_result["verification_record"]

    # --- Retrieve memory hints ---
    gamma_skill = f"{skill}_composition"  # Gamma uses a composition skill
    memory_hints = get_memory_hints(gamma_skill)

    # --- Run Gamma's own builder with upstream data ---
    # Original: Gamma calls its builder with both the original task
    # AND Beta's upstream data, composing a richer output.
    start_ms = int(time.time() * 1000)
    gamma_builder_output = (
        f"Gamma composition for skill '{gamma_skill}' using upstream data. "
        f"Upstream analysis from Beta is verified with status: "
        f"{upstream_verification['status']}. "
        f"Building on upstream findings, the composed analysis includes "
        f"cross-referencing with additional data sources. "
        f"The combined results contain both direct measurements and "
        f"derived metrics that were not available in isolation."
    )
    builder_duration_ms = int(time.time() * 1000) - start_ms

    # --- Self-verification ---
    # Gamma verifies its OWN output, not Beta's (Beta already verified its own)
    verification_record: VerificationRecord = verify_output(
        builder_output=gamma_builder_output,
        skill=gamma_skill,
        skill_docs=(
            f"Documentation for {gamma_skill}: composition methodology. "
            f"Upstream data from Beta (skill: {skill}) is pre-verified."
        ),
        intent=f"Compose multi-source analysis: {intent}",
        memory_hints=memory_hints,
        builder_model="claude-sonnet-4-20250514",  # recommended for Gamma
    )

    # --- Compose multi-hop chain_summary ---
    # Original: Gamma calls build_chain_summary() which walks the chain
    # from Beta's hop through Gamma's hop, computing weakest-link status.
    #
    # LangGraph: We use merge_chain_summary() to add Gamma's hop to the
    # existing chain (which already contains Beta's hop from beta_node).
    gamma_hop = HopTrace(
        agent="gamma",
        skill=gamma_skill,
        status=verification_record["status"],
        cost_usd=verification_record.get("cost_usd", 0.0),
        duration_ms=verification_record.get("duration_ms", 0) + builder_duration_ms,
        gap_report=verification_record.get("gap_report", []),
    )

    chain_summary = merge_chain_summary(
        existing=state.get("chain_summary"),
        new_hop=gamma_hop,
    )

    # --- Store gaps in memory ---
    for gap in verification_record.get("gap_report", []):
        store_gap_report(gamma_skill, gap)

    gamma_result = {
        "builder_output": gamma_builder_output,
        "upstream_output": upstream_output,
        "verification_record": dict(verification_record),
        "agent": "gamma",
        "skill": gamma_skill,
    }

    return {
        "gamma_result": gamma_result,
        "chain_summary": chain_summary,
        "memory_hints": memory_hints,
        "verification_status": chain_summary["chain_status"],
        "messages": [
            AIMessage(content=(
                f"Gamma completed composition with skill '{gamma_skill}'. "
                f"Chain status: {chain_summary['chain_status']}. "
                f"Total hops: {len(chain_summary['hops'])}. "
                f"Gamma verification: {verification_record['status']}. "
                f"Merged gap count: {len(chain_summary['merged_gap_report'])}."
            )),
        ],
    }


# ---------------------------------------------------------------------------
# Trust check node
# ---------------------------------------------------------------------------

def trust_check(state: AgentCommsState) -> dict[str, Any]:
    """Evaluate the chain_summary and set trust_decision.

    MAPPING FROM agent_alpha.py's trust policy:
    =============================================

    Original: After receiving a response, Alpha inspects the chain_summary
    and applies its trust policy:
      - If chain_status is "verified" and all hops passed: ACCEPT
      - If chain_status is "failed" or gap_report is severe: REJECT
      - If chain_status is "partial": ESCALATE for human review

    LangGraph: This is a separate node (not part of alpha_node) because
    LangGraph's conditional edges need a clean decision point. The
    should_continue function reads trust_decision to route.

    Additional trust signals evaluated:
      - Number of unresolved gaps vs total claims
      - Whether remediation was applied (reduces confidence)
      - Reputation of the agents involved (from memory)
    """
    chain_summary = state.get("chain_summary")

    if chain_summary is None:
        return {
            "trust_decision": "reject",
            "messages": [
                AIMessage(content="Trust check: No chain_summary found. Rejecting."),
            ],
        }

    chain_status = chain_summary.get("chain_status", "failed")
    gap_count = len(chain_summary.get("merged_gap_report", []))
    total_hops = len(chain_summary.get("hops", []))

    # --- Apply trust policy ---
    # Original: agent_alpha.py's trust evaluation logic

    if chain_status == "verified" and gap_count == 0:
        # Clean verification — full trust
        decision = "accept"
        reasoning = (
            f"Chain fully verified across {total_hops} hop(s) with no gaps."
        )
    elif chain_status == "failed":
        # Hard failure — reject
        decision = "reject"
        reasoning = (
            f"Chain verification failed. {gap_count} unresolved gap(s) across "
            f"{total_hops} hop(s)."
        )
        # Store negative reputation
        for hop in chain_summary.get("hops", []):
            if hop.get("status") == "failed":
                store_reputation(
                    agent=hop["agent"],
                    skill=hop["skill"],
                    outcome="failed",
                )
    elif chain_status == "partial":
        if gap_count <= 1 and total_hops <= 2:
            # Minor partial — might be acceptable, but escalate to be safe
            decision = "escalate"
            reasoning = (
                f"Partial verification with {gap_count} gap(s). "
                f"Escalating for human review."
            )
        else:
            # Significant partial — reject
            decision = "reject"
            reasoning = (
                f"Partial verification with {gap_count} gap(s) across "
                f"{total_hops} hop(s). Too many gaps to accept."
            )
    else:
        decision = "escalate"
        reasoning = f"Unexpected chain_status: {chain_status}. Escalating."

    # Store positive reputation for accepted results
    if decision == "accept":
        for hop in chain_summary.get("hops", []):
            store_reputation(
                agent=hop["agent"],
                skill=hop["skill"],
                outcome="verified",
            )

    return {
        "trust_decision": decision,
        "messages": [
            AIMessage(content=(
                f"Trust check complete. Decision: {decision}. "
                f"Reasoning: {reasoning}"
            )),
        ],
    }


# ---------------------------------------------------------------------------
# Conditional edge function
# ---------------------------------------------------------------------------

def should_continue(state: AgentCommsState) -> str:
    """Conditional edge: route based on trust_decision.

    MAPPING FROM agent_alpha.py:
    ============================

    Original: Alpha's main loop decides what to do after evaluating
    the chain_summary:
      - accept: Use the result, respond to the user
      - reject: Discard the result, possibly retry or report failure
      - escalate: Flag for human review

    LangGraph: This function returns a string that maps to a named
    edge in the graph. The graph definition wires these strings to
    target nodes (or END).

    Returns one of: "accept", "reject", "escalate"
    """
    decision = state.get("trust_decision", "reject")

    if decision == "accept":
        return "accept"
    elif decision == "reject":
        return "reject"
    else:
        return "escalate"

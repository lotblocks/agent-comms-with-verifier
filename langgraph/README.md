# LangGraph Reference Port: Agent-Comms-with-Verifier

> A translation guide showing how peer-to-peer agent communication with
> claim verification maps to LangGraph's state-graph model.

## Architecture Mapping

### Bus-Based Messaging --> State-Graph Edges

The original system uses a SQLite message bus (`bus.py`) where agents
register, send direct messages, publish to topics, and receive with
atomic claim-locking. In LangGraph, this entire layer disappears and is
replaced by **state flow along graph edges**:

| Original (bus.py)             | LangGraph Equivalent                        |
|-------------------------------|---------------------------------------------|
| `bus.register("alpha")`       | Node named `"alpha"` in `StateGraph`        |
| `bus.send_direct("beta", m)`  | Edge from `"alpha"` to `"beta"`             |
| `bus.publish("topic", m)`     | State key visible to all downstream nodes   |
| `bus.receive("beta")`         | `"beta"` node reads from `state["request"]` |
| Atomic claim-lock on message  | Single-writer guarantee per node invocation  |

The bus's SQLite storage is replaced by LangGraph's checkpointer, which
snapshots the full state after every node execution. This gives you replay,
time-travel debugging, and persistence for free.

### Verify-Before-Respond --> Conditional Edge Pattern

In the original system, Beta and Gamma call `run_skill_verified()` on their
own output before responding. The caller (Alpha) then inspects the
`chain_summary` to decide trust. In LangGraph this becomes:

```
beta_node --> trust_check --> should_continue (conditional edge)
                                |         |          |
                              accept    reject    escalate
                                |         |          |
                               END       END     human_review
```

The `should_continue` function is a **conditional edge** that reads
`state["trust_decision"]` and routes accordingly. This is the direct
equivalent of Alpha's trust-policy loop in `agent_alpha.py`.

### Chain Summary Composition --> State Reducers

The original `verification_chain.py` provides `walk_chain` and
`build_chain_summary` with weakest-link aggregation. In LangGraph, each
node writes its verification record into state, and the `trust_check` node
performs the same aggregation:

- Each hop appends to `state["chain_summary"]["hops"]`
- `chain_status` is the weakest link across all hops
- `total_cost_usd` and `total_duration_ms` are summed
- `merged_gap_report` is the union of all per-hop gap reports

The state reducer for `chain_summary` uses a custom merge function rather
than the default append/replace semantics.

### Memory --> Checkpointers + Custom State

The original `agent_memory.py` provides:
- `store_gap(skill, gap)` with dedup
- `store_reputation(agent, skill, outcome)`
- `claim_guidelines_for_skill(skill)` returning prior gaps as hints

In LangGraph:
- Gap reports persist in checkpointer state across invocations
- Reputation is a custom state key updated by the trust_check node
- `claim_guidelines` are rendered into the system prompt via `memory_hints`
  state key, which the verifier tool reads before validating claims

See `memory_saver.py` for the full mapping.

## Model Recommendations

| Role        | Recommended Model          | Rationale                                    |
|-------------|----------------------------|----------------------------------------------|
| Alpha       | Claude Sonnet 4            | Orchestration, trust reasoning, cost control |
| Beta        | Claude Haiku 3.5           | Leaf builder work, high throughput, low cost  |
| Gamma       | Claude Sonnet 4            | Multi-hop composition needs strong reasoning |
| Verifier    | Cross-family (e.g. GPT-4o) | Independence from builder model family       |

Cross-family verification is critical: if the builder uses Claude, the
verifier should use a different model family to avoid correlated failures.
The `verifier_tool.py` supports swapping backends via environment variables.

## What's Preserved vs What's Different

### Preserved
- **Verification-before-response**: Every agent verifies its own output
  before passing it downstream. This is the core invariant.
- **Chain summary composition**: Multi-hop verification chains with
  weakest-link aggregation work identically.
- **Trust policy**: Alpha's accept/reject/escalate logic is preserved
  as a conditional edge.
- **Claim taxonomy**: All six claim types (existential, structural,
  behavioral, factual, semantic, negative) are preserved in the verifier.
- **Gap report accumulation**: Memory of past verification failures
  informs future verification via `claim_guidelines`.

### Different
- **No message bus**: State flow replaces pub/sub messaging entirely.
  This is simpler but loses the decoupled topology of the bus.
- **No agent registration**: Agents are graph nodes, not registered
  bus participants. The graph structure is fixed at compile time.
- **Synchronous by default**: LangGraph executes nodes sequentially
  along edges. The original bus supports async receive with polling.
  LangGraph can parallelize via fan-out, but the default is sequential.
- **Checkpointing replaces SQLite memory**: The memory store is now
  part of the graph's checkpoint, not a separate SQLite database.
- **Subgraph composition**: Gamma's peer call to Beta is modeled as a
  subgraph invocation, which is more structured than the original
  direct-message approach.

## Installation

```bash
pip install langgraph langchain-core langchain-anthropic langchain-openai
```

For the mock demo (no API keys needed):
```bash
cd langgraph/
python graph.py
```

For real LLM backends, set environment variables:
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
export OPENAI_API_KEY="sk-..."       # for cross-family verifier
python graph.py --live
```

## File Overview

| File               | Original Equivalent                        | Purpose                                      |
|--------------------|--------------------------------------------|----------------------------------------------|
| `state.py`         | Message schemas in bus.py                  | TypedDict state schema for the graph         |
| `verifier_tool.py` | `run_skill_verified.py`                    | Verification tool compatible with LangGraph  |
| `nodes.py`         | `agent_alpha.py`, `agent_beta.py`, `agent_gamma.py` | Node functions for each agent role |
| `memory_saver.py`  | `agent_memory.py`                          | Gap/reputation persistence via checkpointer  |
| `graph.py`         | Main orchestration + `verification_chain.py` | Graph definition, compilation, invocation  |
| `README.md`        | (this file)                                | Mapping guide and reference documentation    |

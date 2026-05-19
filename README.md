# agent-comms-with-verifier

A small substrate for trustworthy multi-agent systems that learn over time. Three primitives that compose by convention:

1. **A peer-to-peer message bus** — agents register, address each other directly or via role topics, exchange envelopes with conversation tracking, TTL, atomic claim-locking for safe concurrency.
2. **A verifier primitive** — wraps every worker's output with adversarial claim validation, structured remediation, and gap-report flywheel that surfaces documentation improvements.
3. **An agent memory store** — gap reports and reputation accumulate across runs. The LLM verifier consumes prior gaps as hints in its system prompt. Repeated observations reinforce, not duplicate.

Together they're a working model for multi-agent systems where workers verify before publishing, receivers trust-check before accepting, and the whole system gets smarter the more it runs.

## What's in the repo

```
skills/
  verifier/                  # Verifier primitive — mock + LLM backends
demos/
  comms-with-verifier/       # Reference agents (Alpha, Beta, Gamma), demos, memory, dashboard
langgraph/
  README.md                  # LangGraph reference port — mapping guide
  graph.py                   # State graph with Alpha/Beta/Gamma as nodes
  nodes.py                   # Node functions (verify-before-respond pattern)
  state.py                   # TypedDict state schema
  verifier_tool.py           # Verifier as LangGraph ToolNode
  memory_saver.py            # Memory persistence via checkpointer
docs/
  01-agent-comms.html        # Reference architecture for peer-to-peer agent comms
  02-verifier-primitive.html # Design spec for the verifier
  03-composed-system.html    # How they compose end-to-end
  04-operators-manual.html   # Comprehensive operator's manual (13 sections)
  05-agent-specs.html        # Portable build sheets per agent + all 22 scripts inline
  KNOWLEDGE.md               # Agent-readable knowledge base
  AGENT_SPECS.md             # Agent-readable build sheets (markdown)
```

## Quick start

```bash
cd demos/comms-with-verifier

# Two-agent: Alpha asks Beta directly
python3 run_demo.py

# Three-agent: Alpha asks Gamma; Gamma asks Beta peer-to-peer
python3 run_three_agent_demo.py

# Concurrent worker pool: 5 alphas, 2 gammas, 2 betas
python3 run_concurrent_demo.py

# Learning demo: same task twice, watch memories accumulate
python3 run_learning_demo.py

# Generate the static observability dashboard
python3 build_dashboard.py
```

## Using the comms-operator (Hyperagent)

A pre-configured **comms-operator** agent is available on [Hyperagent](https://hyperagent.com). It knows the full system — every script, every spec, every pattern — and can:

- **Explain** any component (bus, verifier, memory, chain composition) conversationally
- **Run demos** in its sandbox (all six demo runners work out of the box)
- **Generate dashboards** from demo runs using `build_dashboard.py`
- **Port agents** to other platforms (LangGraph, AutoGen, raw Python) using the embedded specs
- **Extend the system** — add new agent roles, new verifier backends, new demo workflows
- **Manage the repo** — commit changes, create branches, push updates

### How to start a thread

1. In Hyperagent, expand the **Agents** section in the sidebar
2. Click **comms-operator**
3. Start a new thread — the agent loads with both skills (verifier + agent-bus) and all project memories pre-attached

### What's pre-loaded

| Resource | Type | What it knows |
|----------|------|---------------|
| verifier | Skill | RunSkillVerified orchestrator, 3 backends, claim types, gap-report flow |
| agent-bus | Skill | Bus API, agent lifecycle, memory store, reputation dispatch |
| Architecture memory | Memory | Repo structure, 3 design reference docs, system overview |
| Operator's manual memory | Memory | 13-section knowledge base covering all components |
| Agent specs memory | Memory | Build sheets for every agent + all 22 scripts inline |
| File locations memory | Memory | Exact paths in workspace and GitHub |
| Runbook memory | Memory | Every demo command, flags, expected output |
| Design lessons memory | Memory | Architectural decisions and patterns |

## LangGraph reference port

The `langgraph/` directory contains a reference implementation that maps each agent to a LangGraph node. This is not a drop-in replacement — it's a translation guide showing how the same patterns (verify-before-respond, chain composition, memory accumulation) express in LangGraph's state-graph model.

```bash
cd langgraph
pip install langgraph langchain-core langchain-anthropic
python graph.py
```

### What maps to what

| This repo | LangGraph equivalent |
|-----------|---------------------|
| `bus.py` (SQLite message bus) | State graph edges + `MessagesState` |
| `agent_alpha.py` (requester) | `alpha_node` — injects request, evaluates trust |
| `agent_beta.py` (leaf worker) | `beta_node` — runs verifier tool, responds |
| `agent_gamma.py` (intermediate) | `gamma_node` — peers with beta, composes chain |
| `agent_memory.py` (memory store) | `MemorySaver` checkpointer + custom state keys |
| `verification_chain.py` | Reducer function on state's `chain_summary` key |
| `run_skill_verified.py` | `VerifierTool` — LangGraph ToolNode wrapping the orchestrator |

See `langgraph/README.md` for the full mapping guide.

## Verified properties

- Peer-to-peer agent communication, no orchestrator
- Workers verify their own output before publishing (mock or LLM verifier)
- Verification metadata travels with every message (chain_summary in payload)
- Multi-hop trust composition via weakest-link aggregation
- Linked sub-conversations via parent_conversation_id
- End-to-end cost and duration tracked across all hops
- Merged gap reports deduped across the chain
- Worker pools with atomic claim-locking (5/5 verified at concurrency 2/tier)
- Role-based discovery (no hardcoded agent ids)
- Graceful lifecycle (SIGTERM/SIGINT shutdown, --serve-forever)
- Loop prevention (TTL, hop_count, MAX_HOP_COUNT=8)
- **Memory accumulation across runs — gap reports become per-skill knowledge; reinforcement instead of duplication**
- Full observability via static HTML dashboard rendered from SQLite

## Production swap-in path

| Demo | Production |
|------|------------|
| SQLite bus | Postgres LISTEN/NOTIFY or Redis pub/sub |
| Mock verifier | LLM verifier with `ANTHROPIC_API_KEY` (`--backend llm`) |
| Subprocess builders | Real skills via RunWithCredentials |
| `--max-requests` | `--serve-forever` with proper supervision |

The agent code does not change.

## What this isn't (deferred)

- No human-in-the-loop attestation for high-stakes claims
- No cross-tenant isolation on a shared bus
- No streaming / incremental verification
- Single-call LLM verifier (no tool use yet)
- No chain-of-skill verification beyond a single hop pattern
- Reputation scoring is raw success/total; no decay, no stake-weighting

## Design docs

Architecture documented in three reference docs under `docs/`. Open them in a browser:

- `01-agent-comms.html` — eight primitives, four topologies, three reference designs, five failure modes
- `02-verifier-primitive.html` — verifier API, claim contract, gap-report flywheel, six failure modes
- `03-composed-system.html` — system + sequence diagrams, eight pros, four honest limits

## Commit history

| # | SHA | Description |
|---|-----|-------------|
| 7 | _latest_ | README update + LangGraph reference port |
| 6 | `1e92b4c` | Embed all 22 scripts inline in agent specs doc |
| 5 | `59f96a2` | Agent specifications (Ref 05) — portable build sheets |
| 4 | `057f36c` | Operator's manual (Ref 04) + KNOWLEDGE.md |
| 3 | `1a19e55` | Replicate backend + reputation-weighted dispatch |
| 2 | `02d8006` | Agent memory layer + learning demo |
| 1 | `bd2ef78` | Initial — 28 files, bus + verifier + 3 agents + 5 demos + 3 design docs |

## License

MIT.

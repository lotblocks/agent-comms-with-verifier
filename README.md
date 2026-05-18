# agent-comms-with-verifier

A small substrate for trustworthy multi-agent systems that learn over time. Three primitives that compose by convention:

1. **A peer-to-peer message bus** — agents register, address each other directly or via role topics, exchange envelopes with conversation tracking, TTL, atomic claim-locking for safe concurrency.
2. **A verifier primitive** — wraps every worker's output with adversarial claim validation, structured remediation, and gap-report flywheel that surfaces documentation improvements.
3. **An agent memory store** — gap reports and reputation accumulate across runs. The LLM verifier consumes prior gaps as hints in its system prompt. Repeated observations reinforce, not duplicate.

Together they're a working model for multi-agent systems where workers verify before publishing, receivers trust-check before accepting, and the whole system gets smarter the more it runs.

## What's in the repo

```
skills/
  verifier/                 # Verifier primitive — mock + LLM backends
demos/
  comms-with-verifier/      # Reference agents (Alpha, Beta, Gamma), demos, memory, dashboard
docs/
  01-agent-comms.html       # Reference architecture for peer-to-peer agent comms
  02-verifier-primitive.html # Design spec for the verifier
  03-composed-system.html   # How they compose end-to-end
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

## License

MIT.

# agent-comms-with-verifier

A small substrate for trustworthy multi-agent systems. Two primitives that compose by convention:

1. **A peer-to-peer message bus** — agents register, address each other directly or via role topics, exchange envelopes with conversation tracking, TTL, and atomic claim-locking for safe concurrency.
2. **A verifier primitive** — wraps every worker's output with adversarial claim validation, structured remediation, and gap-report flywheel that surfaces documentation improvements.

Together they're a working model for multi-agent systems where workers verify before publishing and receivers trust-check before accepting. Bus carries the conversation; verifier gates the trust.

## What's in the repo

```
skills/
  verifier/                 # The verifier primitive — mock + LLM backends
demos/
  comms-with-verifier/      # Reference agents (Alpha, Beta, Gamma) + demos + dashboard
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

# Concurrent worker pool: 5 alphas × 2 gammas × 2 betas
python3 run_concurrent_demo.py

# Generate the static observability dashboard from the bus
python3 build_dashboard.py
```

## Verified properties

- Peer-to-peer agent communication with no orchestrator
- Workers verify their own output before publishing (mock or LLM verifier)
- Verification metadata travels with every message (chain_summary in payload)
- Multi-hop trust composition via weakest-link aggregation
- Linked sub-conversations via parent_conversation_id
- End-to-end cost and duration tracked across all hops
- Merged gap reports deduped across the chain
- Worker pools with atomic claim-locking (5/5 verified at concurrency=2/tier)
- Role-based discovery (no hardcoded agent ids)
- Graceful lifecycle (SIGTERM/SIGINT shutdown, --serve-forever mode)
- Loop prevention (TTL, hop_count, MAX_HOP_COUNT=8)
- Full observability via static HTML dashboard rendered from SQLite

## Production swap-in path

| Demo | Production |
|------|------------|
| SQLite bus | Postgres LISTEN/NOTIFY or Redis pub/sub (same signatures) |
| Mock verifier | LLM verifier with ANTHROPIC_API_KEY (--backend llm) |
| Subprocess builders | Real Hyperagent skills via RunWithCredentials |
| --max-requests / --idle-timeout | --serve-forever with proper supervision |

The agent code does not change.

## What this isn't (deferred)

- No human-in-the-loop attestation for high-stakes claims
- No cross-tenant isolation on a shared bus
- No streaming / incremental verification
- Single-call LLM verifier (no tool use yet)
- No chain-of-skill verification beyond a single hop pattern

## Design docs

The architecture is documented in three reference docs under `docs/`. Open them in a browser:

- `01-agent-comms.html` — the architecture primer (eight primitives, four topologies, three reference designs, five failure modes)
- `02-verifier-primitive.html` — the design spec for the verifier (API, claim contract, gap-report flywheel, six failure modes)
- `03-composed-system.html` — how the two primitives compose, with system + sequence diagrams

## License

MIT.

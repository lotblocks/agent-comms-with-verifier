# comms-with-verifier — Reference multi-agent system

A working demo of the architecture from Reference No. 01 (agent comms) + No. 02 (verifier) + No. 03 (composition). Two primitives that compose by convention: a peer-to-peer message bus and a worker-side verifier wrapper. Workers verify before publishing. Receivers trust-check before accepting. Multi-hop chains keep that property all the way through. Memory accumulates across runs so the system learns.

## Run it

```bash
# Two-agent flow: Alpha asks Beta directly
python3 run_demo.py

# Three-agent flow: Alpha asks Gamma; Gamma asks Beta peer-to-peer
python3 run_three_agent_demo.py

# Concurrent worker pool: 5 alphas, 2 gammas, 2 betas
python3 run_concurrent_demo.py

# Learning demo: same task twice, watch memories accumulate
python3 run_learning_demo.py

# Generate the static observability dashboard from the SQLite bus
python3 build_dashboard.py
```

## Files

| File | Role |
|------|------|
| `bus.py` | SQLite-backed peer-to-peer message bus with envelope, parent_conversation_id linkage, role-based discovery, topic-fanout |
| `verification_chain.py` | Walks nested verification records; emits end-to-end chain summaries (weakest-link trust, total cost, merged gap reports) |
| `agent_lifecycle.py` | Shared SIGTERM/SIGINT handling for graceful shutdown |
| `agent_memory.py` | SQLite-backed memory store (global / per-agent / per-skill scopes); stores gap reports + reputation; renders claim_guidelines for the LLM verifier; dedup via reinforcement |
| `agent_alpha.py` | Reference requester (target-id or target-role); reads chain_summary; trust-checks |
| `agent_beta.py` | Reference leaf worker; consults memory; verifies own work; emits chain_summary |
| `agent_gamma.py` | Reference intermediate worker; discovers upstream by role; nests verification; consults memory |
| `_compute_total.py` | Toy builder for Beta |
| `_write_report.py` | Toy builder for Gamma |
| `run_demo.py` | Two-agent demo runner |
| `run_three_agent_demo.py` | Three-agent demo runner |
| `run_concurrent_demo.py` | Worker-pool stress test with assertion-based verification |
| `run_learning_demo.py` | Same task twice; demonstrates memory accumulation and reinforcement |
| `build_dashboard.py` | Static HTML observability dashboard generator |

## What the demos prove

1. **Peer-to-peer agent communication** without an orchestrator
2. **Workers verify before publishing** — verification metadata travels with every reply
3. **Multi-hop trust composition** via weakest-link aggregation in `chain_summary`
4. **Linked sub-conversations** via `parent_conversation_id` on the envelope
5. **End-to-end cost / duration / attempts** aggregated across all hops
6. **Merged gap reports** deduped across the chain
7. **Worker pools with atomic claim-locking** — verified 5/5 at concurrency 2/tier
8. **Role-based discovery** — no hardcoded agent ids
9. **Graceful lifecycle** (SIGTERM/SIGINT shutdown, `--serve-forever`)
10. **Loop prevention** (TTL, hop_count, MAX_HOP_COUNT=8)
11. **Memory accumulation across runs** — gap reports become per-skill knowledge; reputation tracks per-agent success rates; dedup reinforces rather than duplicates
12. **Full observability** via static HTML dashboard rendered from SQLite

## Memory: how agents learn

Enable by setting `AGENT_MEMORY_DB` env var or `--memory-db` CLI flag on the agents:

```bash
AGENT_MEMORY_DB=./memory.sqlite python3 run_three_agent_demo.py
```

Agents will:
- On startup, print the current memory count
- Before each verifier invocation, recall per-skill memories and append them as MEMORY HINTS to skill_doc (the LLM verifier uses these in its system prompt)
- After verification, store gap reports as per-skill memories with importance 0.7
- Record per-agent reputation (success/total counts) over time

Repeated observations dedup and reinforce — `use_count` increments and `importance` bumps by 0.05 (capped at 1.0) instead of creating a duplicate row. This keeps the memory store from bloating on repeated runs of the same scenario.

## Production swap-in

| Demo | Production swap |
|------|-----------------|
| SQLite bus | Postgres LISTEN/NOTIFY or Redis pub-sub (same function signatures) |
| Mock verifier | LLM verifier via `--backend llm` and `ANTHROPIC_API_KEY` |
| Subprocess builders | Real Hyperagent skills invoked via `RunWithCredentials` |
| `--max-requests` / `--idle-timeout-sec` | `--serve-forever` with proper supervision (systemd, k8s, etc.) |

The substrate doesn't change. Only the layer beneath it.

## What still isn't done (deferred for v2)

- Human-in-the-loop attestation for high-stakes claims
- Cross-tenant isolation on a shared bus
- Streaming / incremental verification
- Cross-skill chain verification beyond the simple Alpha→Gamma→Beta case
- Smarter reputation scoring (currently raw success/total; no decay, no weighting by stakes)

See Reference No. 03 (`docs/03-composed-system.html`) for the full deferred list.

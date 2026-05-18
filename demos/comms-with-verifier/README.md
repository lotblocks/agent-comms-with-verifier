# comms-with-verifier — Reference multi-agent system

A working demo of the architecture from Reference No. 01 (agent comms) + No. 02 (verifier) + No. 03 (composition). Two primitives that compose by convention: a peer-to-peer message bus and a worker-side verifier wrapper. Workers verify before publishing. Receivers trust-check before accepting. Multi-hop chains keep that property all the way through.

## Run it

```bash
# Two-agent flow: Alpha asks Beta directly
python3 run_demo.py

# Three-agent flow: Alpha asks Gamma → Gamma asks Beta peer-to-peer
python3 run_three_agent_demo.py

# Generate the static observability dashboard from the SQLite bus
python3 build_dashboard.py
```

## Files

| File | Role |
|------|------|
| `bus.py` | SQLite-backed peer-to-peer message bus with envelope + parent_conversation_id linkage |
| `verification_chain.py` | Walks nested verification records; emits end-to-end chain summaries (weakest-link trust, total cost, merged gap reports) |
| `agent_lifecycle.py` | Shared SIGTERM/SIGINT handling for graceful shutdown |
| `agent_alpha.py` | Reference requester — sends a task, reads chain_summary, trust-checks before accepting |
| `agent_beta.py` | Reference leaf worker — verifies own work, emits chain_summary (single hop) |
| `agent_gamma.py` | Reference intermediate worker — calls Beta peer-to-peer (linked sub-conversation), nests Beta's verification inside its own, emits composed chain_summary |
| `_compute_total.py` | Toy builder for Beta — reads REMEDIATION_PROMPT, produces complete output when remediated |
| `_write_report.py` | Toy builder for Gamma — synthesizes a report paragraph from upstream data |
| `run_demo.py` | Two-agent demo runner |
| `run_three_agent_demo.py` | Three-agent demo runner |
| `build_dashboard.py` | Generates static HTML observability dashboard from the SQLite bus |

## What the demo proves

1. **Peer-to-peer agent communication** — no orchestrator. Gamma calls Beta directly.
2. **Workers verify before publishing** — `agent_beta` and `agent_gamma` both run their builders through the verifier orchestrator before replying.
3. **Verification metadata travels with messages** — every response payload contains a `verification` record and a `chain_summary`.
4. **Multi-hop trust composition** — Alpha sees `chain_status` (weakest-link rule across all verification hops), not just Gamma's local status.
5. **Linked sub-conversations** — Gamma's request to Beta carries `parent_conversation_id` so the bus can render the whole chain as one story.
6. **End-to-end cost and duration** — `chain_summary` aggregates across all hops; budget-tracking does not require walking the chain manually.
7. **Merged gap reports across the chain** — unverifiable claims from any hop surface in one deduped report so documentation improvements roll up.
8. **Graceful lifecycle** — agents accept `--serve-forever`, shut down cleanly on SIGTERM/SIGINT, and emit clear status on startup.

## Lifecycle modes

```bash
# Demo mode: handle one request then exit
python3 agent_beta.py --db ./_demo_bus.sqlite --max-requests 1

# Service mode: run until killed (kill -TERM <pid> shuts down cleanly)
python3 agent_beta.py --db ./_demo_bus.sqlite --serve-forever
```

## Promotion to production

| Demo | Production swap |
|------|-----------------|
| SQLite bus | Postgres LISTEN/NOTIFY or Redis pub-sub (same function signatures) |
| Mock verifier | LLM verifier via `--backend llm` and ANTHROPIC_API_KEY |
| Subprocess builder | Real Hyperagent skill invoked via RunWithCredentials |
| Hardcoded agent ids | Role-based discovery via the registry |
| Single-process agents | Containers or skill workers with proper supervision |

The substrate doesn't change. Only the layer beneath it.

## What still isn't done

- Worker pool stress test (multiple Betas, claim-locking under load)
- Human-in-the-loop attestation for high-stakes claims
- Streaming / incremental verification
- Cross-tenant isolation on a shared bus

See Reference No. 03 § 06 for the full list.

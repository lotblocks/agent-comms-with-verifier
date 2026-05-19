# Knowledge: agent-comms-with-verifier

Canonical knowledge document for agents and operators working with this system. Companion to the four reference webpages in `docs/`.

## TL;DR

A multi-agent system with three primitives that compose:

1. **Bus** — SQLite-backed peer-to-peer message broker
2. **Verifier** — wraps any builder with claim validation (mock | Anthropic | Replicate backends)
3. **Memory** — durable per-skill / per-agent learning store

Plus three reference agents (Alpha, Beta, Gamma), a static HTML dashboard, and four design docs.

**Repo:** https://github.com/lotblocks/agent-comms-with-verifier
**License:** MIT
**Dependencies:** Python 3.9+ stdlib only

## The four design docs

| Ref | File | Subject |
|-----|------|---------|
| 01 | `docs/01-agent-comms.html` | Architecture for peer-to-peer agent comms |
| 02 | `docs/02-verifier-primitive.html` | Verifier design spec (claim contract, gap reports) |
| 03 | `docs/03-composed-system.html` | How bus + verifier compose with diagrams |
| 04 | `docs/04-operators-manual.html` | This document, fully rendered with diagrams |

## The five components

| Component | File | Purpose |
|-----------|------|---------|
| Bus | `demos/comms-with-verifier/bus.py` | SQLite-backed message broker with role-based discovery, topic fanout, atomic claim-locking, conversation chain tracking |
| Verifier | `skills/verifier/run_skill_verified.py` | Pluggable verifier orchestrator with mock/llm/replicate backends |
| Memory | `demos/comms-with-verifier/agent_memory.py` | SQLite-backed learning store with global/agent/skill scopes |
| Agents | `demos/comms-with-verifier/agent_*.py` | Reference Alpha (requester) + Beta (worker) + Gamma (intermediate worker) |
| Dashboard | `demos/comms-with-verifier/build_dashboard.py` | Static HTML observability generator |

Supporting:
- `verification_chain.py` — multi-hop chain aggregation (weakest-link status, merged gap reports)
- `agent_lifecycle.py` — graceful SIGTERM/SIGINT handling

## End-to-end message flow

```
Alpha → Bus → Beta → consults memory → invokes verifier → builder runs
                  ↓                         ↓
                  (claim-locking)       (decompose, remediate if needed)
                                            ↓
                                        verification record + gap report
                                            ↓
                                        store gap memory + reputation
                                            ↓
                                        build chain_summary
                                            ↓
Beta → Bus → Alpha (reads chain_status, trust-checks, accepts/refuses)
```

## How memory works

Three scopes:
- **global** — shared facts ("skills returning PII must include redaction summary")
- **skill.<name>** — per-skill gaps and failure patterns
- **agent.<id>** — per-agent reputation (success/total counts)

The flywheel:
1. First run on a skill — verifier emits gap_report
2. Worker stores it via `memory.store_gap(skill, gap_report)`
3. Next run — worker recalls memories, appends them to skill_doc as "MEMORY HINTS"
4. LLM verifier sees the hints in its system prompt, produces tighter claims
5. Repeated observations dedup + reinforce (use_count++, importance += 0.05)

Persistence:
- Bus DB and memory DB are separate SQLite files
- Bus is per-run (wiped between demos)
- Memory is long-lived (survives across runs)
- Memory enabled via `AGENT_MEMORY_DB` env var or `--memory-db` flag

Key methods (in `agent_memory.py`):
```python
store_gap(skill_name, gap_report)           # dedups by (scope, subject, statement)
store_failure_pattern(skill_name, claim_id, ...)
store_reputation(agent_id, success)         # 1 bump per run
reputation_summary(agent_id) → dict          # {successes, total, success_rate}
claim_guidelines_for_skill(skill_name) → str # rendered for verifier system prompt
recall(scope, category?, subject?, limit) → list[Memory]
```

## Three dispatch modes

| Mode | CLI | Use when |
|------|-----|----------|
| Direct addressing | `--target-id agent_beta_1` | You know the specific replica |
| Topic fanout | `--target-role worker` | Worker pool, atomic claim-locking distributes load |
| Reputation-aware pick | `--target-role worker --reputation-min 0.6` | Routing around known-flaky workers |

Probation: replicas with fewer than `min_reputation_samples` (default 3) get a probation pass — new agents aren't filtered out just because they haven't proven themselves yet.

## Three verifier backends

| Backend | Cost | Quality | Setup |
|---------|------|---------|-------|
| `mock` | free | deterministic stub | none |
| `llm` (Anthropic) | $0.003-0.015/call | high (Claude Sonnet/Opus) | ANTHROPIC_API_KEY |
| `replicate` | ~$0.0002/call | good (Llama-3, Mistral, etc.) | REPLICATE_API_TOKEN |

Selected via `--backend` flag or `VERIFIER_BACKEND` env var.

Default Anthropic model: `claude-sonnet-4-5-20250929` (override with `VERIFIER_MODEL`).
Default Replicate model: `meta/meta-llama-3-8b-instruct` (override with `VERIFIER_REPLICATE_MODEL`).

End-to-end tested: real Replicate Llama-3 call returns 7-claim VerificationRecord parsed cleanly.

## Model recommendations (builder + verifier pairings)

| Builder | Verifier | Backend | Why |
|---------|----------|---------|-----|
| claude-opus | gpt-4.5 or llama-3-70b | llm or replicate | Cross-family |
| claude-sonnet | llama-3-70b-instruct | replicate | Strong, cheap independent reasoning |
| gpt-4.5 | claude-sonnet/opus | llm | Anthropic catches what GPT misses |
| llama-3-8b | llama-3-70b or claude-haiku | replicate or llm | Step up to stronger reasoner |
| any (testing) | mock | mock | Orchestration testing only |

**Always cross-family.** Same-family verifier shares blind spots with the builder.

## Five run commands

```bash
cd demos/comms-with-verifier

python3 run_demo.py                  # 2-agent: Alpha → Beta direct
python3 run_three_agent_demo.py      # 3-agent: Alpha → Gamma → Beta peer-to-peer
python3 run_concurrent_demo.py       # Worker pool: 5 alphas × 2 gammas × 2 betas
python3 run_learning_demo.py         # Memory accumulation across two runs
python3 run_reputation_demo.py       # Reputation-weighted dispatch routes around flaky replica
python3 build_dashboard.py           # Generate static HTML observability snapshot
```

Tests (in `skills/verifier/`):
```bash
python3 test_smoke.py        # 20 assertions, mock orchestration
python3 test_llm_parsing.py  # 16 assertions, LLM parser logic (no network)
```

## Production patterns

**Pattern A — single worker:**
```bash
python3 agent_beta.py --db ./bus.sqlite --my-id agent_beta \
    --serve-forever --memory-db ./memory.sqlite
```

**Pattern B — worker pool:**
```bash
python3 agent_beta.py --db ./bus.sqlite --my-id agent_beta_1 --serve-forever &
python3 agent_beta.py --db ./bus.sqlite --my-id agent_beta_2 --serve-forever &
# Clients use --target-role worker; atomic claim-locking distributes
```

**Pattern C — reputation-weighted:**
```bash
python3 agent_alpha.py --db ./bus.sqlite \
    --target-role worker --reputation-min 0.95 \
    --memory-db ./memory.sqlite \
    --task compute_total --intent "..."
```

## Environment variables

| Variable | Effect |
|----------|--------|
| `VERIFIER_BACKEND` | mock / llm / replicate (default mock) |
| `VERIFIER_MODEL` | Anthropic model override |
| `VERIFIER_REPLICATE_MODEL` | Replicate model override |
| `ANTHROPIC_API_KEY` | Required for `--backend llm` |
| `REPLICATE_API_TOKEN` | Required for `--backend replicate` |
| `AGENT_MEMORY_DB` | Path to MemoryStore SQLite — enables learning |
| `REMEDIATION_PROMPT` | Set by orchestrator on retry; builders read to know what to fix |
| `DATA_INPUT` | Set by Gamma when passing upstream data to its builder |

## Production swap-in path

| Demo | Production |
|------|------------|
| SQLite bus | Postgres LISTEN/NOTIFY or Redis pub-sub (same signatures) |
| Mock verifier | LLM verifier with real key |
| Subprocess builders | Real Hyperagent skills via RunWithCredentials |
| `--max-requests` / `--idle-timeout-sec` | `--serve-forever` with supervision (systemd, k8s) |

The agent code does not change.

## Data shapes (the contracts)

### Envelope (every bus message)
```json
{
  "id": "msg_<12 hex>",
  "conversation_id": "cnv_<12 hex>",
  "parent_conversation_id": "cnv_<12 hex>",
  "from_agent": "agent_alpha",
  "to_agent": "agent_gamma",
  "topic": null,
  "msg_type": "request | response | event",
  "reply_to": null,
  "hop_count": 0,
  "ttl_seconds": 300,
  "created_at": 1779140000.0,
  "payload": { ... }
}
```

### VerificationRecord
```json
{
  "status": "verified | failed | partial",
  "claims": [Claim],
  "gap_report": GapReport | null,
  "verifier_model": "meta/meta-llama-3-8b-instruct",
  "duration_ms": 8089,
  "cost_usd": 0.0002,
  "upstream_verification": VerificationRecord | null
}
```

### ChainSummary (embedded in response payloads)
```json
{
  "chain_status": "verified",
  "hop_count": 2,
  "total_attempts": 4,
  "total_cost_usd": 0.0004,
  "total_duration_ms": 12340,
  "per_hop": [{"verifier_model": "...", "status": "...", ...}],
  "merged_gap_report": GapReport | null
}
```

### Claim
```json
{
  "id": "claim_001",
  "type": "existential | structural | behavioral | factual | semantic | negative",
  "statement": "...",
  "evidence_required": "...",
  "evidence_collected": { ... } | null,
  "verdict": "pass | fail | unverifiable",
  "confidence": 0.95,
  "reasoning": "..."
}
```

### Memory
```json
{
  "id": "mem_<12 hex>",
  "scope": "skill.demo-compute-total | global | agent.<id>",
  "category": "gap | failure_pattern | reputation | fact",
  "subject": "claim_004",
  "content": { ... },
  "importance": 0.7,
  "use_count": 3,
  "created_at": 1779140000.0,
  "last_used_at": 1779143000.0
}
```

## What's deferred (v2 list)

- Agentic verifier with tool access (current is single-call)
- Human-in-the-loop attestation for high-stakes claims
- Cross-skill chain verification
- Streaming / incremental verification
- Reputation decay (currently no time-weighting)
- Stake-weighted reputation
- Cross-tenant isolation
- Postgres/Redis transport for sub-100ms latency
- Rubric integration for eval data

## Verified properties

- 20 assertions on mock orchestration
- 16 assertions on LLM verifier parser logic
- 2-agent demo: chain_status=verified, hops=1
- 3-agent demo: chain_status=verified, hops=2 (peer-to-peer Gamma↔Beta)
- Concurrent demo: 6 assertions pass with 5 reqs × 2 gammas × 2 betas
- Learning demo: memory accumulates with dedup reinforcement
- Reputation demo: 3 assertions, flaky replica filtered at reputation_min=0.6
- Real Replicate LLM call → 7-claim VerificationRecord (Llama-3 8b, $0.0002)

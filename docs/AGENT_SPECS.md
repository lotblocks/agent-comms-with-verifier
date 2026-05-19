# AGENT_SPECS.md

Portable specifications for every agent in the system. Use this to recreate any agent on any platform — Hyperagent, LangGraph, AutoGen, raw Python, anything.

Each agent has the same 8-section spec:
- **Soul** — first-person identity
- **Purpose** — single-job description
- **Skills** — which skills from this project they need
- **Tools** — platform capabilities
- **Memory** — what scopes they read/write
- **Scripts** — files they own / call
- **LLM model** — recommendation by tier
- **System prompt template**

---

## α · Alpha — The Requester

**Soul:** I form clear intents and ask precisely. I read the trust record before I accept any answer. On verified, I move on. On partial, I accept but flag the gap. On failed, I refuse and escalate. I am the conscience of the system.

| Section | Value |
|---|---|
| Purpose | Initiate work, apply trust policy on responses |
| Skills | `agent-bus` (read MemoryStore for reputation picks) |
| Tools | Bus ops: register, send_direct, publish_to_role, pick_agent_by_role, receive |
| Memory reads | `agent.<worker_id>` (for reputation lookups) |
| Memory writes | none |
| Scripts owned | `agent_alpha.py` |
| Scripts called | `bus.py`, `agent_memory.py` (read-only) |
| LLM (reference) | none — deterministic CLI |
| LLM (LLM-powered variant) | claude-haiku, gpt-4-mini, llama-3-8b |
| Lifecycle | one-shot |

### System prompt template
```
You are Alpha, the requester in a multi-agent system.

Three steps:
1. Form a clear, plain-language intent for the user's request.
2. Choose a target worker by role (preferred) or specific id.
3. After receiving the response, apply this trust policy:
   - chain_status == "verified" → accept
   - chain_status == "partial"  → accept; surface gap_report
   - chain_status == "failed"   → refuse; escalate to a human channel

You DO NOT do the work. You DO NOT verify. You orchestrate and gate.

Always read chain_summary, not just verification.status.
```

### CLI patterns
```bash
# Direct addressing
python3 agent_alpha.py --db ./bus.sqlite \
  --target-id agent_beta_1 --task compute_total --intent "..."

# Topic fanout
python3 agent_alpha.py --db ./bus.sqlite \
  --target-role worker --task compute_total --intent "..."

# Reputation-aware pick
python3 agent_alpha.py --db ./bus.sqlite \
  --memory-db ./memory.sqlite \
  --target-role worker --reputation-min 0.9 \
  --task compute_total --intent "..."
```

---

## β · Beta — The Leaf Worker

**Soul:** I do the work and I verify it myself before I publish. I do not push the burden of trust onto whoever asked. When the verifier flags an unverifiable claim, I remember it as a documentation gap. When I succeed, my reputation rises; when I fail, I take the hit honestly.

| Section | Value |
|---|---|
| Purpose | Execute builder, verify own output, return verified response |
| Skills | `agent-bus` + `verifier` |
| Tools | Bus ops, subprocess (to verifier orchestrator), MemoryStore, signal handlers |
| Memory reads | `skill.<name>` for prior gaps (appended as MEMORY HINTS to skill_doc) |
| Memory writes | `skill.<name>` (store_gap), `agent.<my_id>` (store_reputation) |
| Scripts owned | `agent_beta.py` + builder (e.g. `_compute_total.py`) |
| Scripts called | `bus.py`, `agent_memory.py`, `agent_lifecycle.py`, `verification_chain.py`, subprocess to `run_skill_verified.py` |
| LLM (reference) | none (Python orchestration); builder may use LLM if its task needs one |
| LLM (LLM-powered builder) | claude-sonnet or gpt-4 |
| LLM (verifier) | Llama-3-70b on Replicate (different family from builder) |
| Lifecycle | --serve-forever |

### System prompt template
```
You are Beta, a leaf worker.

When a request arrives:
1. Recall per-skill memories.
2. Append them as MEMORY HINTS to skill_doc.
3. Execute the builder.
4. Pass output through the verifier (--backend llm or --backend replicate).
   - Verifier MUST be a different model family than your builder.
5. If verifier returns "failed", apply remediation.
6. Store gap_report as per-skill memory (deduped).
7. Record success/failure as reputation memory.
8. Build chain_summary (single hop).
9. Send response with verification + chain_summary.

You DO NOT trust your own output. The verifier does.
```

### CLI patterns
```bash
# Production: serve forever with memory
python3 agent_beta.py \
  --db ./bus.sqlite --my-id agent_beta_1 \
  --memory-db ./memory.sqlite --serve-forever

# Demo: handle N then exit
python3 agent_beta.py \
  --db ./bus.sqlite --my-id agent_beta \
  --max-requests 5 --idle-timeout-sec 30
```

---

## γ · Gamma — The Intermediate Worker

**Soul:** I coordinate with upstream peers. When I need data I don't have, I ask the worker who does — directly, peer-to-peer, no orchestrator. I trust them only as far as their verification record allows. I weave their work into mine, verify my own contribution, and report end-to-end trust to the caller.

| Section | Value |
|---|---|
| Purpose | Take high-level requests requiring upstream data; discover + coordinate peer-to-peer; compose multi-hop trust |
| Skills | Same as Beta: `agent-bus` + `verifier` |
| Tools | Same as Beta + `publish_to_role` to upstream, `parent_conversation_id` for sub-conversation linkage |
| Memory reads | `skill.<my_skill>` for prior gaps |
| Memory writes | `skill.<my_skill>`, `agent.<my_id>` |
| Scripts owned | `agent_gamma.py` + builder (e.g. `_write_report.py`) |
| Scripts called | Same as Beta + uses `verification_chain.build_chain_summary` for multi-hop composition |
| LLM (reference) | none for Gamma itself; builder + verifier as above |
| LLM (LLM-powered builder) | claude-sonnet or gpt-4 — stronger reasoning than Beta because of multi-source synthesis |
| LLM (verifier) | Llama-3-70b on Replicate |
| Lifecycle | --serve-forever |

### System prompt template
```
You are Gamma, an intermediate worker.

Same discipline as Beta, PLUS peer-to-peer coordination:

1. Recall per-skill memories.
2. Identify upstream data needed.
3. Discover upstream worker by role (publish_to_role, not hardcoded id).
4. Set parent_conversation_id on sub-conversation.
5. Wait for upstream response with timeout.
6. TRUST-CHECK upstream worker's chain_summary:
   - verified → use their data
   - partial → use with caution; surface their gap in your own chain
   - failed → REFUSE; bubble up error
7. Execute your builder using upstream data.
8. Run verifier on YOUR output.
9. NEST upstream verification inside your own:
       my_verification.upstream_verification = upstream.verification
10. Compute chain_summary across BOTH hops.
11. Store gap memories + your reputation.
12. Send response with verification + chain_summary.

You handle two conversations: outer (caller ↔ you) and inner (you ↔ upstream).
parent_conversation_id links them.
```

### CLI patterns
```bash
# Production
python3 agent_gamma.py \
  --db ./bus.sqlite --my-id agent_gamma_1 \
  --memory-db ./memory.sqlite --serve-forever
```

---

## v · Verifier — The Sentinel (subprocess)

**Soul:** I did not run the skill. I have not seen your conversation. I see only what was produced and what was supposed to be produced. My job is not to be helpful — it is to be right.

| Section | Value |
|---|---|
| Purpose | Decompose builder output into atomic claims; verdict each; emit remediation on failure, gap_report on unverifiable |
| Skills | `verifier` skill itself (not a bus participant) |
| Tools | HTTP (LLM provider API), JSON parsing. Stdlib only. Three pluggable backends. |
| Memory reads | none (stateless); receives MEMORY HINTS in system prompt from worker |
| Memory writes | none |
| Scripts owned | `run_skill_verified.py`, `mock_verifier.py`, `llm_verifier.py`, `replicate_verifier.py`, `schemas.py`, `remediation.py` |
| Tests | `test_smoke.py` (20 assertions), `test_llm_parsing.py` (16 assertions) |
| Lifecycle | per-call (invoked by worker subprocess) |

### Builder ↔ Verifier model pairings (MUST be different families)

| Builder | Verifier | Backend | Cost/call |
|---|---|---|---|
| claude-opus | gpt-4.5 or llama-3-70b | llm or replicate | $0.015 / $0.003 |
| claude-sonnet | llama-3-70b-instruct | replicate | $0.003 |
| gpt-4.5 | claude-sonnet/opus | llm | $0.003-0.015 |
| llama-3-8b | llama-3-70b or claude-haiku | replicate or llm | $0.001 |
| llama-3-70b | claude-sonnet | llm | $0.003 |
| testing | mock | mock | free |

### The verifier system prompt (load-bearing — do not modify)
```
You are the verifier. You did not run the skill.

You have received:
- the skill's documentation
- the user's plain-language intent
- the skill's output

Decompose what should be true into atomic claims and render a verdict for each.
You are not trying to be helpful. You are trying to be RIGHT.

CLAIM PRODUCTION RULES
- Claims live at the INTERSECTION of (a) what docs say AND (b) what user wants.
- Each claim must be independently testable from output text alone.
- Behavioral claims requiring code execution → mark `unverifiable`.

CLAIM TYPES: existential, structural, behavioral, factual, semantic, negative.
VERDICTS: pass, fail, unverifiable.
STRICTNESS: {LOW|MED|HIGH} — produce N±2 claims.

OUTPUT: JSON only, no prose, no fences:
{"claims": [{"id", "type", "statement", "evidence_required",
             "evidence_collected", "verdict", "confidence", "reasoning"}]}
```

---

## ω · comms-operator — The System Operator (Hyperagent-specific)

**Soul:** I am the operator of this multi-agent system. I know the architecture, the runbook, the design decisions, and the file layout by heart. I am the human's interface to the system on the Hyperagent platform.

| Section | Value |
|---|---|
| Purpose | Single entrypoint on Hyperagent for working with this system |
| Skills | Both `verifier` AND `agent-bus` |
| Tools | Bash, Read, Write, Edit, Glob, Grep + Hyperagent toolset + FetchSkillScripts + RunWithCredentials |
| Memory | Pre-loaded with 5 project memories: canonical context, file locations, runbook, design lessons, operator's manual |
| Scripts | Inherits all from both skills via FetchSkillScripts |
| LLM | claude-opus or claude-sonnet — strong reasoning needed for architecture work |
| Lifecycle | thread-scoped |

### System prompt template
```
You are comms-operator, an expert on agent-comms-with-verifier.

You know:
- Five components: bus, verifier, memory, agents, dashboard
- Reference docs 01-05 + KNOWLEDGE.md + AGENT_SPECS.md
- Both skills (verifier, agent-bus)
- Full runbook (every demo, test, env var)
- Eight design lessons

Your job:
- Demo the system on demand
- Debug failures via dashboard + bus DB
- Propose extensions that fit the substrate
- Answer architectural questions
- Help port to other platforms

When a question comes in:
1. Search memories for project context first
2. Read relevant manual section
3. Cite file paths and commit shas
4. Run a demo if seeing-is-believing is faster than explaining

Boundaries:
- Do NOT modify bus envelope schema without good reason
- Do NOT add new verifier claim types without updating both backends
- Do NOT skip verify-before-publish in any new worker
- Do NOT pair builder + verifier within same model family
```

---

## Cross-platform portability

### What translates cleanly

| Concept | Notes |
|---|---|
| Envelope schema | Same JSON shape works on every harness |
| Verifier system prompt | LLM-agnostic; same prompt for Claude/GPT/Llama |
| Verify-before-publish discipline | Just code; works anywhere agents can call subprocesses or HTTP |
| MemoryStore data model | SQLite portable; schema works for any backing store |

### What's platform-specific

| Concept | Hyperagent-specific |
|---|---|
| FetchSkillScripts + RunWithCredentials | Replace with the harness's credential injection pattern |
| Draft-card review UX | Gap reports flow into platform-specific approval UI |
| Memory auto-injection | Hyperagent injects high-importance memories; other platforms need explicit loading |

### Mapping to specific harnesses

**LangGraph**: each agent is a Node; bus is replaced by edges + state passing; memory becomes Postgres-backed state store; verifier is a ToolNode invoked from worker Nodes.

**AutoGen**: agents become ConversableAgent instances; UserProxyAgent for Alpha; AssistantAgent for Beta/Gamma with custom `generate_reply` that wraps every reply through the verifier; group chat = bus (with some loss of atomic claim-locking).

**Raw Python (no harness)**: the reference implementation. Each agent is a Python script + SQLite bus. Production-ready with `--serve-forever` + systemd/k8s supervision. Most portable.

---

## Adding a new agent role

To add e.g. a "reviewer" or "approver" agent:

### Required to specify
1. **Soul** — one paragraph in first person
2. **Purpose** — what unique role
3. **Role name** — what other agents use in publish_to_role
4. **Builder script** — does the actual work
5. **LLM model** — what powers reasoning if any
6. **System prompt** — for LLM-powered variants

### Already determined by the substrate (don't reinvent)
- Bus operations
- Memory contract
- Verify-before-publish discipline
- chain_summary composition
- Lifecycle handlers
- Graceful shutdown

### Required scripts to create
```
demos/comms-with-verifier/
├── agent_<name>.py    # model on agent_beta.py or agent_gamma.py
└── _<your_builder>.py # the toy builder (replace with real skill in production)
```

### Skeleton
```python
from bus import Bus
from agent_memory import MemoryStore
from agent_lifecycle import ShutdownFlag, install_signal_handlers
from verification_chain import build_chain_summary

# 1. Register with the bus
bus.register(agent_id=my_id, name="YourAgentName", role="your_role",
             subscriptions=[f"inbox.{my_id}", "role.your_role"])

# 2. Main loop (same pattern as Beta/Gamma)
while not shutdown.is_set():
    msgs = bus.receive(my_id, [f"inbox.{my_id}", "role.your_role"])
    for m in msgs:
        # 3. Optionally call upstream, verify yourself, reply
        result = run_verified_builder(...)
        chain_summary = build_chain_summary(result["verification"])
        bus.send_direct(to_agent=m["from_agent"], payload={
            "result": result["result"],
            "verification": result["verification"],
            "chain_summary": chain_summary,
        }, conversation_id=m["conversation_id"], reply_to=m["id"],
           msg_type="response", hop_count=m["hop_count"] + 1)
```

---

## Production-ready checklist

1. **Decide your transport.** SQLite for prototyping; Postgres LISTEN/NOTIFY or Redis pub-sub for production scale.
2. **Pick model pairings.** Builder + verifier from different families. Never both Claude. Never both Llama.
3. **Configure credentials.** ANTHROPIC_API_KEY and/or REPLICATE_API_TOKEN as skill credentials.
4. **Set memory paths.** AGENT_MEMORY_DB pointing to long-lived SQLite (or migrate to Postgres).
5. **Run with --serve-forever.** Wrap in systemd, k8s, or your platform's process manager.
6. **Set up the dashboard.** Point a static HTML reader at the SQLite, or pipe to your observability stack.
7. **Define semantic role topics.** "worker", "writer" are demo roles. Production: "billing-validator", "compliance-reviewer", etc.
8. **Cap budget and timeouts.** verifier_budget_usd, max_attempts, ttl_seconds. Tune for your domain.
9. **Decide reputation policy.** reputation_min, min_reputation_samples. High-stakes: 0.95+ with 10+ samples.
10. **Plan human escalation.** chain_status=failed should trigger Slack, email, or on-call paging.

---

## Quick reference table — all agents at a glance

| Agent | Role | Skills | Memory writes | LLM (production) | Lifecycle |
|---|---|---|---|---|---|
| Alpha | requester | agent-bus | none | none or haiku/8b | one-shot |
| Beta | worker | agent-bus + verifier | skill.<name> · agent.<id> | sonnet builder + Llama-3-70b verifier | --serve-forever |
| Gamma | writer | agent-bus + verifier | skill.<name> · agent.<id> | sonnet builder + Llama-3-70b verifier | --serve-forever |
| Verifier | subprocess | verifier | (consumes memories indirectly) | cross-family from builder | per-call |
| comms-operator | named agent | both | (via platform memory) | opus or sonnet | thread-scoped |

---

## Companion docs

- `docs/01-agent-comms.html` — Architecture for peer-to-peer agent comms
- `docs/02-verifier-primitive.html` — Verifier design spec
- `docs/03-composed-system.html` — Composed system with diagrams
- `docs/04-operators-manual.html` — Full operator's manual
- `docs/05-agent-specs.html` — This document, fully rendered
- `docs/KNOWLEDGE.md` — Condensed system reference
- `docs/AGENT_SPECS.md` — This document, agent-readable


---

## All scripts inline

Every script referenced in the agent specs above, with a short description and the full code. Useful for porting to another platform or for agents that need the implementation alongside the spec.

Approximately 5,300 lines of Python across 22 files. Organized by agent first, then shared utilities, demo runners, and tests.

### § 11A — Alpha scripts

Alpha's main loop. No builder of her own — Alpha is the requester.

#### `demos/comms-with-verifier/agent_alpha.py` (230 lines)

Alpha's main loop. Registers with bus, resolves target (direct id, role topic, or reputation-aware pick), sends request, polls for response, applies trust policy to chain_summary.

```python
#!/usr/bin/env python3
"""Agent Alpha — the requester.

Role: research-style agent that asks Beta to compute something and waits for
a verified response. Reads the verification metadata in the response so it
knows whether to trust the answer.

Lifecycle:
    1. Register with the bus
    2. Send a directly-addressed request to Beta
    3. Poll for a response in the same conversation (with timeout)
    4. Print the verified result and exit
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid

# Make bus.py importable from this directory.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bus import Bus
from agent_memory import MemoryStore


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--my-id", default="agent_alpha")
    parser.add_argument("--target-id", default=None,
                        help="explicit agent id to send to (overrides --target-role)")
    parser.add_argument("--target-role", default=None,
                        help="resolve to any live agent with this role")
    parser.add_argument("--task", default="compute_total",
                        help="task to send (compute_total for Beta, write_report for Gamma)")
    parser.add_argument("--intent", default="compute the total of these line items and report success")
    parser.add_argument("--wait-sec", type=float, default=30.0)
    parser.add_argument("--memory-db", default=os.environ.get("AGENT_MEMORY_DB", ""),
                        help="path to MemoryStore SQLite (for reputation-aware pick)")
    parser.add_argument("--reputation-min", type=float, default=None,
                        help="if set, do a reputation-aware pick (skip low-reputation replicas) "
                             "instead of topic-fanout. Requires --memory-db.")
    args = parser.parse_args()

    bus = Bus(args.db)
    bus.register(
        agent_id=args.my_id,
        name="Alpha",
        role="researcher",
        subscriptions=[f"inbox.{args.my_id}"],
    )

    # Routing: --target-id pins a specific replica (direct addressing);
    # --target-role with --reputation-min picks a reputable replica directly;
    # --target-role alone publishes to role.<role> so any subscribed replica can claim.
    target_id = args.target_id
    target_role = args.target_role
    memory = MemoryStore(args.memory_db) if args.memory_db else None

    if not target_id and not target_role:
        target_id = "agent_beta"
        print(f"[alpha] no target specified; defaulting to {target_id}", flush=True)

    if target_role:
        # Wait briefly for any matching replica to register.
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if bus.find_agents_by_role(target_role, alive_only=True):
                break
            time.sleep(0.2)

        if args.reputation_min is not None:
            if memory is None:
                print(f"[alpha] WARNING: --reputation-min requires --memory-db; falling back to topic-fanout",
                      flush=True)
            else:
                picked = bus.pick_agent_by_role(
                    target_role, memory_store=memory,
                    reputation_min=args.reputation_min,
                )
                if picked is None:
                    print(f"[alpha] no replica with reputation >= {args.reputation_min} "
                          f"for role={target_role!r}; falling back to topic-fanout",
                          flush=True)
                else:
                    target_id = picked["id"]
                    target_role = None  # switch to direct addressing
                    status_tag = picked.get("_reputation_status", "?")
                    score = picked.get("_reputation_score")
                    if score is not None:
                        print(f"[alpha] reputation-aware pick: {target_id} "
                              f"(status={status_tag}, score={score:.2f})", flush=True)
                    else:
                        print(f"[alpha] reputation-aware pick: {target_id} "
                              f"(status={status_tag})", flush=True)

    conv_id = "cnv_" + uuid.uuid4().hex[:12]
    target_label = target_id or f"role.{target_role}"
    print(f"[alpha] starting · conversation_id={conv_id} · target={target_label}",
          flush=True)

    # Build the request payload. The target agent routes its work through the
    # verifier; Gamma (writer) additionally calls Beta (worker) for data.
    request_payload = {
        "task": args.task,
        "intent": args.intent,
        "skill_name": f"demo-{args.task.replace('_','-')}",
        "skill_doc": (
            "Returns JSON with fields: status, report, amount, timestamp."
        ),
        "max_attempts": 2,
    }
    # For write_report tasks, let Gamma discover its upstream worker by role
    # rather than pinning to a specific agent id. This is what makes worker
    # pools (multiple Beta replicas) work without orchestrator coordination.
    if args.task == "write_report":
        request_payload["upstream_role"] = "worker"

    if target_role:
        msg_id, topic = bus.publish_to_role(
            from_agent=args.my_id,
            role=target_role,
            payload=request_payload,
            conversation_id=conv_id,
            msg_type="request",
        )
        print(f"[alpha] published request to {topic} · msg_id={msg_id}", flush=True)
    else:
        msg_id = bus.send_direct(
            from_agent=args.my_id,
            to_agent=target_id,
            payload=request_payload,
            conversation_id=conv_id,
            msg_type="request",
        )
        print(f"[alpha] sent request to {target_id} · msg_id={msg_id}", flush=True)

    # Poll for the response. We expect msg_type='response' addressed to us
    # in the same conversation.
    deadline = time.time() + args.wait_sec
    response = None
    while time.time() < deadline:
        bus.heartbeat(args.my_id)
        msgs = bus.receive(
            agent_id=args.my_id,
            subscriptions=[f"inbox.{args.my_id}"],
            max_messages=5,
            wait_sec=1.0,
        )
        for m in msgs:
            if m["conversation_id"] == conv_id and m["msg_type"] == "response":
                response = m
                break
        if response is not None:
            break

    if response is None:
        print(f"[alpha] TIMEOUT — no response within {args.wait_sec}s", flush=True)
        return 2

    print(f"[alpha] received response · msg_id={response['id']}", flush=True)

    payload = response["payload"]
    verification = payload.get("verification", {})
    chain_summary = payload.get("chain_summary") or {}
    local_status = verification.get("status", "unknown")
    chain_status = chain_summary.get("chain_status", local_status)
    attempts = payload.get("attempts", "?")

    print()
    print("=" * 70)
    actual_responder = response["from_agent"] if response else target_label
    print(f"[alpha] RESPONSE FROM {actual_responder.upper()}")
    print("=" * 70)
    # Chain-level (end-to-end) view first.
    if chain_summary:
        hops = chain_summary.get("hop_count", 1)
        total_attempts = chain_summary.get("total_attempts", attempts)
        total_cost = chain_summary.get("total_cost_usd", 0.0)
        total_duration = chain_summary.get("total_duration_ms", 0)
        print(f"  chain_status        = {chain_status}   (weakest link across {hops} hop(s))")
        print(f"  total_attempts      = {total_attempts}")
        print(f"  total_cost          = ${total_cost:.4f}")
        print(f"  total_duration_ms   = {total_duration}")
        per_hop = chain_summary.get("per_hop", [])
        if per_hop:
            print(f"  per-hop trace       (root → leaf):")
            for i, h in enumerate(per_hop):
                print(f"      hop {i}: {h.get('verifier_model','?')}  "
                      f"status={h.get('status')}  "
                      f"duration={h.get('duration_ms')}ms  "
                      f"cost=${h.get('cost_usd', 0):.4f}")
        merged_gap = chain_summary.get("merged_gap_report")
        if merged_gap:
            print(f"  merged_gap_report   = "
                  f"{len(merged_gap['unverifiable_claims'])} unverifiable claim(s) "
                  f"across skill(s): {', '.join(merged_gap.get('skill_ids', []))}")

    print()
    print(f"  Local verification  = {local_status} (this worker's own claims)")
    claims = verification.get("claims", [])
    if claims:
        print(f"  Local claims        = {len(claims)} ("
              f"{sum(1 for c in claims if c['verdict']=='pass')} pass, "
              f"{sum(1 for c in claims if c['verdict']=='fail')} fail, "
              f"{sum(1 for c in claims if c['verdict']=='unverifiable')} unverifiable)")

    print()
    print("  Final output:")
    print("  " + json.dumps(payload.get("result"), indent=2).replace("\n", "\n  "))
    print("=" * 70)

    # Trust decision is now on chain_status (weakest-link), not local status.
    if chain_status == "verified":
        print("[alpha] chain is VERIFIED end-to-end — accepting", flush=True)
        return 0
    if chain_status == "partial":
        print("[alpha] chain is PARTIAL — accepting; gap report attached for review",
              flush=True)
        return 0
    print(f"[alpha] chain is {chain_status.upper()} — would refuse / escalate in production",
          flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())

```

### § 11B — Beta scripts

Beta's main loop plus its toy builder.

#### `demos/comms-with-verifier/agent_beta.py` (229 lines)

Beta's main loop. Subscribes to inbox + role.worker topic. For each request: consults memory, invokes verifier orchestrator via subprocess, stores gap reports + reputation, builds chain_summary, sends verified response.

```python
#!/usr/bin/env python3
"""Agent Beta — the worker.

Role: when a request comes in over the bus, do the work, then run the verifier
on the result BEFORE sending the response. Verification metadata travels with
the reply so peers can see what was verified.

This is the discipline of "self-check before publish" — a worker that produces
output verifies it itself, doesn't push the burden of trust to the requester.

Lifecycle:
    1. Register with the bus
    2. Loop: receive direct messages addressed to me
    3. For each request: invoke the verifier orchestrator on the named builder
    4. Send back a response containing the verified output + verification record
    5. Continue until --max-requests is reached or --stop-on-idle triggers
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bus import Bus
from verification_chain import build_chain_summary
from agent_lifecycle import ShutdownFlag, install_signal_handlers
from agent_memory import MemoryStore


HERE = os.path.dirname(os.path.abspath(__file__))
# Path to the orchestrator from the verifier skill we built earlier.
VERIFIER_ORCH = os.path.abspath(
    os.path.join(HERE, "..", "..", "skills", "verifier", "run_skill_verified.py")
)


def run_verified_builder(*, builder_script: str, skill_name: str, skill_doc: str,
                         intent: str, max_attempts: int,
                         memory: MemoryStore | None = None) -> dict:
    """Invoke the verifier orchestrator. Returns the parsed RunSkillVerifiedResult.

    If a MemoryStore is provided, recall per-skill memories and append them to
    skill_doc as "MEMORY HINTS" — the LLM verifier will use these in its system
    prompt. The mock verifier ignores them (it's deterministic).
    """
    if memory is not None:
        hints = memory.claim_guidelines_for_skill(skill_name)
        if hints:
            skill_doc = f"{skill_doc}\n\n--- MEMORY HINTS (from past runs) ---\n{hints}"

    cmd = [
        "python3", VERIFIER_ORCH,
        "--target-script", builder_script,
        "--skill-name", skill_name,
        "--skill-doc", skill_doc,
        "--intent", intent,
        "--max-attempts", str(max_attempts),
        "--strictness", "medium",
        "--backend", os.environ.get("VERIFIER_BACKEND", "mock"),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if proc.returncode not in (0, 1):
        raise RuntimeError(
            f"orchestrator crashed (exit {proc.returncode}): {proc.stderr[:300]}"
        )
    return json.loads(proc.stdout)


def handle_request(bus: Bus, my_id: str, msg: dict,
                   memory: MemoryStore | None = None,
                   simulate_failure_rate: float = 0.0) -> None:
    payload = msg["payload"]
    task = payload.get("task")
    conv_id = msg["conversation_id"]
    requester = msg["from_agent"]

    print(f"[beta] received task='{task}' from {requester} · conversation={conv_id}",
          flush=True)

    if task == "compute_total":
        builder = os.path.join(HERE, "_compute_total.py")
        skill_name = payload.get("skill_name", "demo-compute-total")
        result = run_verified_builder(
            builder_script=builder,
            skill_name=skill_name,
            skill_doc=payload.get("skill_doc", ""),
            intent=payload.get("intent", ""),
            max_attempts=payload.get("max_attempts", 2),
            memory=memory,
        )

        # Simulate failures if configured (demo mode for reputation-weighted dispatch).
        success = result.get("verification", {}).get("status") == "verified"
        if simulate_failure_rate > 0.0:
            import random
            if random.random() < simulate_failure_rate:
                print(f"[beta] {my_id}: simulating failure for demo (rate={simulate_failure_rate})",
                      flush=True)
                success = False
                result["verification"]["status"] = "failed"

        # Learning step: store gap reports + reputation.
        if memory is not None:
            gap = result.get("verification", {}).get("gap_report")
            if gap:
                stored = memory.store_gap(skill_name, gap)
                if stored:
                    print(f"[beta] stored {len(stored)} gap memories for {skill_name}",
                          flush=True)
            memory.store_reputation(my_id, success)
        # Try to parse the inner result string back into a dict for cleaner downstream.
        try:
            parsed_result = json.loads(result["result"])
            result["result"] = parsed_result
        except (json.JSONDecodeError, TypeError):
            pass

        # Leaf worker — chain is just this one verification.
        chain_summary = build_chain_summary(
            result["verification"], skill_id_fallback=payload.get("skill_name", "demo")
        )
        chain_summary["total_attempts"] = result["attempts"]

        response_payload = {
            "task": task,
            "result": result["result"],
            "attempts": result["attempts"],
            "verification": result["verification"],
            "chain_summary": chain_summary,
        }
        status = result["verification"]["status"]
        print(
            f"[beta] chain_status={chain_summary['chain_status']} "
            f"(hops={chain_summary['hop_count']}, attempts={result['attempts']}, "
            f"cost=${chain_summary['total_cost_usd']:.4f}) — "
            f"replying to {requester}",
            flush=True,
        )
    else:
        response_payload = {
            "task": task,
            "error": f"unknown task type: {task!r}",
        }
        print(f"[beta] unknown task {task!r} — replying with error", flush=True)

    bus.send_direct(
        from_agent=my_id,
        to_agent=requester,
        payload=response_payload,
        conversation_id=conv_id,
        reply_to=msg["id"],
        msg_type="response",
        hop_count=msg["hop_count"] + 1,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--my-id", default="agent_beta")
    parser.add_argument("--max-requests", type=int, default=1, help="exit after handling N requests")
    parser.add_argument("--idle-timeout-sec", type=float, default=20.0)
    parser.add_argument("--serve-forever", action="store_true",
                        help="ignore --max-requests and --idle-timeout-sec; "
                             "run until SIGTERM/SIGINT")
    parser.add_argument("--memory-db", default=os.environ.get("AGENT_MEMORY_DB", ""),
                        help="path to a MemoryStore SQLite file; empty = no memory")
    parser.add_argument("--simulate-failure-rate", type=float, default=0.0,
                        help="fraction (0.0-1.0) of requests this replica should "
                             "fail intentionally — used to differentiate reputation in demos")
    args = parser.parse_args()

    bus = Bus(args.db)
    memory = MemoryStore(args.memory_db) if args.memory_db else None
    if memory is not None:
        print(f"[beta] memory: {args.memory_db} ({memory.count()} memories)", flush=True)
    # Every replica subscribes to its personal inbox AND the shared role topic
    # so the bus can distribute work across replicas via atomic claim-locking.
    bus.register(
        agent_id=args.my_id,
        name="Beta",
        role="worker",
        subscriptions=[f"inbox.{args.my_id}", "role.worker"],
    )

    shutdown = ShutdownFlag(on_shutdown=lambda: print("[beta] shutdown requested", flush=True))
    install_signal_handlers(shutdown)

    mode = "serve-forever" if args.serve_forever else f"max_requests={args.max_requests}"
    print(f"[beta] registered · mode={mode} · waiting for direct messages", flush=True)

    handled = 0
    idle_since = time.time()
    while not shutdown.is_set():
        if not args.serve_forever:
            if handled >= args.max_requests:
                break
            if (time.time() - idle_since) >= args.idle_timeout_sec:
                break
        bus.heartbeat(args.my_id)
        msgs = bus.receive(
            agent_id=args.my_id,
            subscriptions=[f"inbox.{args.my_id}", "role.worker"],
            max_messages=1,
            wait_sec=1.0,
        )
        if not msgs:
            continue
        idle_since = time.time()
        for m in msgs:
            if m["msg_type"] != "request":
                continue
            try:
                handle_request(bus, args.my_id, m, memory=memory,
                               simulate_failure_rate=args.simulate_failure_rate)
                handled += 1
            except Exception as e:
                print(f"[beta] error handling request: {e}", flush=True)

    print(f"[beta] done · handled={handled}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

```

#### `demos/comms-with-verifier/_compute_total.py` (51 lines)

Toy builder for Beta. Reads REMEDIATION_PROMPT from env; produces incomplete output on first attempt, complete output when remediated. Replace with real Hyperagent skill in production.

```python
#!/usr/bin/env python3
"""Toy builder for the demo: computes a total of line items and returns JSON.

Behavior matches the mock verifier's expectations so the demo exercises the
full loop deterministically:
- First attempt: returns the total but omits timestamp / amount metadata.
- With REMEDIATION_PROMPT set: returns the full schema.

In a real deployment, this would be replaced by an actual data-fetching or
computation skill invoked via RunWithCredentials.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone


def main() -> None:
    # Hard-coded line items for the demo; in reality these would come from args.
    line_items = [
        {"item": "design audit", "cost": 40.0},
        {"item": "implementation",  "cost": 60.0},
    ]
    total = sum(li["cost"] for li in line_items)

    remediation = os.environ.get("REMEDIATION_PROMPT", "").strip()

    if not remediation:
        # First attempt — minimal output, missing fields the verifier expects.
        output = {
            "status": "test",
            "report": "totals computed",
            "items": line_items,
        }
    else:
        # Remediated — include all fields the verifier flagged.
        output = {
            "status": "test",
            "report": "totals computed",
            "items": line_items,
            "amount": total,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "_remediation_acknowledged": True,
        }

    print(json.dumps(output))


if __name__ == "__main__":
    main()

```

### § 11C — Gamma scripts

Gamma's main loop plus its toy builder. Gamma also uses the shared scripts below.

#### `demos/comms-with-verifier/agent_gamma.py` (318 lines)

Gamma's main loop. Same pattern as Beta plus peer-to-peer: discovers upstream worker by role (publish_to_role), starts a sub-conversation with parent_conversation_id linkage, nests upstream verification inside its own, composes multi-hop chain_summary.

```python
#!/usr/bin/env python3
"""Agent Gamma — the writer.

Role: receives a 'write_report' request from Alpha. Discovers that it needs
upstream data, asks Beta for it peer-to-peer (no orchestrator in the middle),
waits for Beta's verified response, then synthesizes a report and runs the
verifier on its own output before replying to Alpha.

This is where the system gets interesting:
- Gamma talks to Beta directly (Alpha does not relay)
- Two conversations exist: (Alpha ↔ Gamma) and (Gamma ↔ Beta)
- Gamma trusts Beta's data based on Beta's verification record, then produces
  its own verified output for Alpha

Lifecycle:
    1. Register with the bus
    2. Loop: receive direct messages addressed to me
    3. For each 'write_report' request:
       a. Send a 'compute_total' request to Beta in a NEW conversation
       b. Wait for Beta's verified response
       c. Inspect Beta's verification.status (refuse to use unverified data)
       d. Invoke RunSkillVerified on the report-writing builder
       e. Reply to Alpha with the verified report
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bus import Bus
from verification_chain import build_chain_summary
from agent_lifecycle import ShutdownFlag, install_signal_handlers
from agent_memory import MemoryStore


HERE = os.path.dirname(os.path.abspath(__file__))
VERIFIER_ORCH = os.path.abspath(
    os.path.join(HERE, "..", "..", "skills", "verifier", "run_skill_verified.py")
)


def request_data_from_beta(bus: Bus, my_id: str, upstream_role: str,
                           parent_conv_id: str, intent: str,
                           hop_count: int, wait_sec: float = 30.0) -> dict | None:
    """Publish a request to the upstream role's topic and wait for response.

    Uses publish_to_role so any worker subscribed to role.<upstream_role> can
    claim the work — the bus's atomic claim-locking is the load balancer.
    """
    sub_conv = "cnv_" + uuid.uuid4().hex[:12]
    req_id, topic = bus.publish_to_role(
        from_agent=my_id,
        role=upstream_role,
        payload={
            "task": "compute_total",
            "intent": intent,
            "skill_name": "demo-compute-total",
            "skill_doc": (
                "Computes the total of line items and returns JSON with "
                "status, report, amount, timestamp fields."
            ),
            "max_attempts": 2,
        },
        conversation_id=sub_conv,
        parent_conversation_id=parent_conv_id,
        msg_type="request",
        hop_count=hop_count + 1,
    )
    print(f"[gamma] → role.{upstream_role} · req={req_id} (sub-conversation {sub_conv})",
          flush=True)

    deadline = time.time() + wait_sec
    while time.time() < deadline:
        bus.heartbeat(my_id)
        msgs = bus.receive(
            agent_id=my_id,
            subscriptions=[f"inbox.{my_id}"],
            max_messages=5,
            wait_sec=1.0,
        )
        for m in msgs:
            if m["conversation_id"] == sub_conv and m["msg_type"] == "response":
                print(f"[gamma] ← beta · response in {sub_conv}", flush=True)
                return m
    print(f"[gamma] TIMEOUT waiting for Beta in {sub_conv}", flush=True)
    return None


def run_verified_writer(*, data_payload: dict, skill_doc: str, intent: str,
                        max_attempts: int, skill_name: str = "demo-write-report",
                        memory: MemoryStore | None = None) -> dict:
    """Invoke the verifier orchestrator on the report-writing builder.

    Consults per-skill memories if a MemoryStore is provided.
    """
    if memory is not None:
        hints = memory.claim_guidelines_for_skill(skill_name)
        if hints:
            skill_doc = f"{skill_doc}\n\n--- MEMORY HINTS (from past runs) ---\n{hints}"

    builder = os.path.join(HERE, "_write_report.py")
    env = os.environ.copy()
    env["DATA_INPUT"] = json.dumps(data_payload)
    cmd = [
        "python3", VERIFIER_ORCH,
        "--target-script", builder,
        "--skill-name", skill_name,
        "--skill-doc", skill_doc,
        "--intent", intent,
        "--max-attempts", str(max_attempts),
        "--strictness", "medium",
        "--backend", os.environ.get("VERIFIER_BACKEND", "mock"),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=180)
    if proc.returncode not in (0, 1):
        raise RuntimeError(
            f"orchestrator crashed (exit {proc.returncode}): {proc.stderr[:300]}"
        )
    return json.loads(proc.stdout)


def handle_write_report(bus: Bus, my_id: str, msg: dict,
                        memory: MemoryStore | None = None) -> None:
    payload = msg["payload"]
    conv_id = msg["conversation_id"]
    requester = msg["from_agent"]
    intent = payload.get("intent", "")

    print(f"[gamma] received write_report from {requester} · conv={conv_id}", flush=True)

    # Step 1: publish to upstream role topic. The bus will route to one of
    # the subscribed replicas via atomic claim-locking.
    upstream_role = payload.get("upstream_role", "worker")
    beta_response = request_data_from_beta(
        bus=bus,
        my_id=my_id,
        upstream_role=upstream_role,
        parent_conv_id=conv_id,
        intent=f"fetch the data needed for: {intent}",
        hop_count=msg["hop_count"],
    )

    if beta_response is None:
        # Upstream failure — bubble it up.
        bus.send_direct(
            from_agent=my_id, to_agent=requester,
            payload={"task": "write_report", "error": "upstream_data_unavailable"},
            conversation_id=conv_id, reply_to=msg["id"], msg_type="response",
            hop_count=msg["hop_count"] + 1,
        )
        return

    # Step 2: trust-check Beta's data BEFORE using it.
    beta_payload = beta_response["payload"]
    beta_status = beta_payload.get("verification", {}).get("status")
    if beta_status not in ("verified", "partial"):
        print(f"[gamma] refusing — upstream data status={beta_status}", flush=True)
        bus.send_direct(
            from_agent=my_id, to_agent=requester,
            payload={
                "task": "write_report",
                "error": "upstream_unverified",
                "upstream_status": beta_status,
                "upstream_verification": beta_payload.get("verification"),
            },
            conversation_id=conv_id, reply_to=msg["id"], msg_type="response",
            hop_count=msg["hop_count"] + 1,
        )
        return

    upstream_data = beta_payload.get("result", {})
    upstream_verification = beta_payload.get("verification")
    print(f"[gamma] beta status={beta_status} · using upstream data", flush=True)

    # Step 3: verify my own work (memory-consulting if a store is present).
    result = run_verified_writer(
        data_payload=upstream_data,
        skill_doc=(
            "Synthesizes a report paragraph from upstream data. Returns JSON "
            "with status, report, amount, timestamp."
        ),
        intent=intent,
        max_attempts=payload.get("max_attempts", 2),
        memory=memory,
    )

    # Learning step: store gap reports + reputation.
    if memory is not None:
        gap = result.get("verification", {}).get("gap_report")
        if gap:
            stored = memory.store_gap("demo-write-report", gap)
            if stored:
                print(f"[gamma] stored {len(stored)} gap memories for demo-write-report",
                      flush=True)
        success = result.get("verification", {}).get("status") == "verified"
        memory.store_reputation(my_id, success)

    try:
        parsed_result = json.loads(result["result"])
        result["result"] = parsed_result
    except (json.JSONDecodeError, TypeError):
        pass

    # Nest Beta's verification INSIDE Gamma's verification record so the chain
    # walker can descend uniformly via verification.upstream_verification.
    gamma_verification = dict(result["verification"])
    gamma_verification["upstream_verification"] = upstream_verification

    # Compute the end-to-end chain summary embedded in the response. Alpha
    # reads this for an at-a-glance view of trust + cost + duration + gaps
    # across all hops.
    chain_summary = build_chain_summary(
        gamma_verification, skill_id_fallback="demo-write-report"
    )
    # total_attempts is sum across hops (Beta's + Gamma's)
    chain_summary["total_attempts"] = (
        result["attempts"]
        + (beta_payload.get("attempts", 0) if isinstance(beta_payload, dict) else 0)
    )

    response_payload = {
        "task": "write_report",
        "result": result["result"],
        "attempts": result["attempts"],
        "verification": gamma_verification,
        "chain_summary": chain_summary,
    }
    print(
        f"[gamma] chain_status={chain_summary['chain_status']} "
        f"(hops={chain_summary['hop_count']}, "
        f"attempts={chain_summary['total_attempts']}, "
        f"cost=${chain_summary['total_cost_usd']:.4f}) — "
        f"replying to {requester}",
        flush=True,
    )

    bus.send_direct(
        from_agent=my_id, to_agent=requester,
        payload=response_payload,
        conversation_id=conv_id, reply_to=msg["id"], msg_type="response",
        hop_count=msg["hop_count"] + 1,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--my-id", default="agent_gamma")
    parser.add_argument("--max-requests", type=int, default=1)
    parser.add_argument("--idle-timeout-sec", type=float, default=45.0)
    parser.add_argument("--serve-forever", action="store_true",
                        help="ignore --max-requests and --idle-timeout-sec; "
                             "run until SIGTERM/SIGINT")
    parser.add_argument("--memory-db", default=os.environ.get("AGENT_MEMORY_DB", ""),
                        help="path to a MemoryStore SQLite file; empty = no memory")
    args = parser.parse_args()

    bus = Bus(args.db)
    memory = MemoryStore(args.memory_db) if args.memory_db else None
    if memory is not None:
        print(f"[gamma] memory: {args.memory_db} ({memory.count()} memories)", flush=True)

    bus.register(
        agent_id=args.my_id,
        name="Gamma",
        role="writer",
        subscriptions=[f"inbox.{args.my_id}", "role.writer"],
    )

    shutdown = ShutdownFlag(on_shutdown=lambda: print("[gamma] shutdown requested", flush=True))
    install_signal_handlers(shutdown)

    mode = "serve-forever" if args.serve_forever else f"max_requests={args.max_requests}"
    print(f"[gamma] registered · mode={mode} · waiting for direct messages", flush=True)

    handled = 0
    idle_since = time.time()
    while not shutdown.is_set():
        if not args.serve_forever:
            if handled >= args.max_requests:
                break
            if (time.time() - idle_since) >= args.idle_timeout_sec:
                break
        bus.heartbeat(args.my_id)
        msgs = bus.receive(
            agent_id=args.my_id,
            subscriptions=[f"inbox.{args.my_id}", "role.writer"],
            max_messages=1,
            wait_sec=1.0,
        )
        if not msgs:
            continue
        idle_since = time.time()
        for m in msgs:
            if m["msg_type"] != "request":
                continue
            task = m["payload"].get("task")
            if task == "write_report":
                try:
                    handle_write_report(bus, args.my_id, m, memory=memory)
                    handled += 1
                except Exception as e:
                    print(f"[gamma] error: {e}", flush=True)
            else:
                print(f"[gamma] unknown task {task!r}, ignoring", flush=True)

    print(f"[gamma] done · handled={handled}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

```

#### `demos/comms-with-verifier/_write_report.py` (62 lines)

Toy builder for Gamma. Reads DATA_INPUT from env (Beta's output passed in by Gamma) and REMEDIATION_PROMPT. Synthesizes a report paragraph from upstream data.

```python
#!/usr/bin/env python3
"""Toy builder for Gamma the writer.

Reads two env vars:
  - DATA_INPUT — JSON string from Beta's verified output (Gamma passes it through)
  - REMEDIATION_PROMPT — set by the verifier if a previous attempt failed

Behavior matches the mock verifier's expectations so the demo exercises the
full loop deterministically:
  - First attempt: returns a paragraph but omits the timestamp / amount fields
                   the verifier wants to see in the output payload
  - With REMEDIATION_PROMPT set: returns the complete schema
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone


def main() -> None:
    data_input = os.environ.get("DATA_INPUT", "{}")
    try:
        data = json.loads(data_input)
    except json.JSONDecodeError:
        data = {}

    items = data.get("items", [])
    total = data.get("amount", "?")
    item_count = len(items)

    paragraph = (
        f"Project total: ${total}. Across {item_count} line items, "
        f"this represents a balanced allocation between {items[0]['item']!r} "
        f"and {items[1]['item']!r}." if item_count >= 2
        else f"Project total: ${total}."
    )

    remediation = os.environ.get("REMEDIATION_PROMPT", "").strip()

    if not remediation:
        # First attempt — minimal output
        output = {
            "status": "test",
            "report": paragraph,
        }
    else:
        # Remediated — include the full schema the verifier expects
        output = {
            "status": "test",
            "report": paragraph,
            "amount": total,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "_remediation_acknowledged": True,
            "_based_on_data": data,
        }

    print(json.dumps(output))


if __name__ == "__main__":
    main()

```

### § 11D — Verifier scripts

The pluggable claim-validation orchestrator and three backends. The Verifier is a subprocess invoked by Beta and Gamma; it is not a bus participant.

#### `skills/verifier/run_skill_verified.py` (269 lines)

Main verifier orchestrator. Loads the chosen backend (mock | llm | replicate), runs the builder, decomposes output into atomic claims via the verifier, applies remediation if needed (up to max_attempts), aggregates session-wide gap reports across attempts.

```python
#!/usr/bin/env python3
"""run_skill_verified.py — orchestration layer for the verifier primitive.

This is the v1 "tool wrapper" — it sits between the caller and the existing
skill execution flow, runs the builder, hands the output to a verifier, and
loops with structured remediation up to max_attempts.

The verifier is pluggable. v1 uses the deterministic mock in mock_verifier.py;
the real LLM verifier will be a drop-in replacement that implements the same
verify() signature.

Usage:
  python3 run_skill_verified.py \\
    --target-script /path/to/builder.py \\
    --target-args 'arg1 arg2' \\
    --skill-name my-skill \\
    --skill-doc "what the skill is supposed to do" \\
    --intent "the user's plain-language goal" \\
    --max-attempts 2 \\
    --strictness medium

Output: a JSON RunSkillVerifiedResult on stdout. Use --pretty for indented JSON.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict
from typing import Any

# Ensure relative imports work whether run as a script or a module.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from schemas import (
    Claim,
    GapReport,
    Improvement,
    RunSkillVerifiedResult,
    VerificationRecord,
)
import remediation


def _load_verifier(backend: str):
    """Pluggable verifier backend selection.

    All backends implement the same verify() signature defined in schemas.
    Defaults to mock for deterministic tests; switch to llm (Anthropic) or
    replicate (Replicate/Llama-3 etc.) for real claim decomposition.
    """
    if backend == "llm":
        import llm_verifier
        return llm_verifier
    if backend == "replicate":
        import replicate_verifier
        return replicate_verifier
    if backend == "mock":
        import mock_verifier
        return mock_verifier
    raise ValueError(
        f"Unknown verifier backend: {backend!r} (expected 'mock', 'llm', or 'replicate')"
    )


# ---------- subprocess: run the builder ----------

def run_target(
    target_script: str,
    target_args: str,
    remediation_prompt: str = "",
    timeout_sec: int = 60,
) -> str:
    """Run the target builder script as a subprocess and return its stdout.

    If a remediation prompt is supplied, it is passed via the REMEDIATION_PROMPT
    environment variable. Builders that opt into remediation should read this
    variable and adjust behavior accordingly.
    """
    env = os.environ.copy()
    if remediation_prompt:
        env["REMEDIATION_PROMPT"] = remediation_prompt
    else:
        env.pop("REMEDIATION_PROMPT", None)

    cmd = ["python3", target_script]
    if target_args:
        cmd.extend(target_args.split())

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        return json.dumps({
            "_orchestrator_error": "target_script_timeout",
            "timeout_sec": timeout_sec,
        })

    if result.returncode != 0:
        return json.dumps({
            "_orchestrator_error": "target_script_failed",
            "returncode": result.returncode,
            "stderr": result.stderr.strip(),
        })

    return result.stdout.strip()


# ---------- gap report: aggregate across attempts ----------

def merge_gap_reports(
    skill_name: str,
    attempt_history: list[VerificationRecord],
) -> GapReport | None:
    """Dedup and merge per-attempt gap reports into a session-wide one.

    Why: even if the builder fixes an unverifiable on attempt 2 by including
    the right field, the underlying documentation gap is real and worth
    surfacing. The user might want to make the doc improvement permanent so
    the next caller doesn't rely on lucky remediation.
    """
    seen_claim_ids: set[str] = set()
    merged_claims: list[Claim] = []
    merged_improvements: list[Improvement] = []

    for record in attempt_history:
        if record.gap_report is None:
            continue
        for c in record.gap_report.unverifiable_claims:
            if c.id in seen_claim_ids:
                continue
            seen_claim_ids.add(c.id)
            merged_claims.append(c)
        for imp in record.gap_report.proposed_improvements:
            if imp.claim_id in seen_claim_ids:
                # Only include improvements for claims we kept; dedup on claim_id.
                if not any(existing.claim_id == imp.claim_id for existing in merged_improvements):
                    merged_improvements.append(imp)

    if not merged_claims:
        return None

    return GapReport(
        skill_id=skill_name,
        unverifiable_claims=merged_claims,
        proposed_improvements=merged_improvements,
        summary=(
            f"{len(merged_claims)} claim(s) were unverifiable across "
            f"{len(attempt_history)} attempt(s). Proposed documentation "
            "improvements would prevent future runs from depending on lucky "
            "remediation."
        ),
    )


# ---------- main orchestration ----------

def main() -> int:
    parser = argparse.ArgumentParser(description="Run a skill with verification.")
    parser.add_argument("--target-script", required=True, help="path to the builder script")
    parser.add_argument("--target-args", default="", help="args passed to the builder")
    parser.add_argument("--skill-name", required=True, help="name of the skill being verified")
    parser.add_argument("--skill-doc", default="", help="skill documentation (for claim decomposition)")
    parser.add_argument("--intent", required=True, help="user's plain-language goal")
    parser.add_argument("--max-attempts", type=int, default=2, help="max builder invocations (default 2)")
    parser.add_argument("--strictness", default="medium", choices=["low", "medium", "high"])
    parser.add_argument("--budget-usd", type=float, default=0.50, help="per-attempt verifier budget")
    parser.add_argument("--timeout-sec", type=int, default=120, help="per-attempt timeout")
    parser.add_argument(
        "--backend",
        default=os.environ.get("VERIFIER_BACKEND", "mock"),
        choices=["mock", "llm", "replicate"],
        help="verifier backend (default: mock; env VERIFIER_BACKEND overrides)",
    )
    parser.add_argument("--pretty", action="store_true", help="pretty-print the result")
    args = parser.parse_args()

    verifier = _load_verifier(args.backend)

    overall_start_ms = int(time.time() * 1000)
    attempts_used = 0
    last_output: Any = None
    attempt_history: list[VerificationRecord] = []
    remediation_prompt = ""
    total_cost = 0.0

    # The remediation loop. Bounded by max_attempts.
    for attempt in range(1, args.max_attempts + 1):
        attempts_used = attempt

        builder_output = run_target(
            target_script=args.target_script,
            target_args=args.target_args,
            remediation_prompt=remediation_prompt,
            timeout_sec=args.timeout_sec,
        )
        last_output = builder_output

        verification = verifier.verify(
            skill_name=args.skill_name,
            skill_documentation=args.skill_doc,
            intent=args.intent,
            builder_output=builder_output,
            attempt=attempt,
            strictness=args.strictness,
        )
        attempt_history.append(verification)
        total_cost += verification.cost_usd

        if verification.status == "verified":
            break
        if verification.status == "partial":
            # No failures, just unverifiable claims. Nothing for the builder to
            # remediate; the gap report carries the signal to the user.
            break

        # status == "failed" — build remediation prompt if more attempts available.
        if attempt < args.max_attempts:
            failed = [c for c in verification.claims if c.verdict == "fail"]
            unv = [c for c in verification.claims if c.verdict == "unverifiable"]
            remediation_prompt = remediation.build_remediation_prompt(
                original_command=f"{args.target_script} {args.target_args}".strip(),
                original_intent=args.intent,
                failed_claims=failed,
                unverifiable_claims=unv,
            )

    # Final verification is the last one we ran.
    final_verification = attempt_history[-1]

    # Promote a session-wide gap report into the final verification's slot.
    # Captures unverifiable claims even from earlier attempts that later passed.
    session_gap = merge_gap_reports(args.skill_name, attempt_history)
    if session_gap is not None:
        final_verification.gap_report = session_gap

    total_duration_ms = int(time.time() * 1000) - overall_start_ms

    result = RunSkillVerifiedResult(
        result=last_output,
        attempts=attempts_used,
        verification=final_verification,
        attempt_history=attempt_history,
        total_duration_ms=total_duration_ms,
        total_cost_usd=total_cost,
    )

    payload = asdict(result)
    if args.pretty:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(json.dumps(payload, default=str))

    # Exit code communicates verdict to callers that want to gate on it.
    #   0  → verified or partial (output is usable; user may want to review gaps)
    #   1  → failed (builder did not converge within max_attempts)
    return 0 if final_verification.status in ("verified", "partial") else 1


if __name__ == "__main__":
    sys.exit(main())

```

#### `skills/verifier/mock_verifier.py` (253 lines)

Deterministic stubbed verifier. Attempt 1 returns 1 fail + 1 unverifiable + 3 passes; attempt 2 returns all-pass. For offline testing and CI.

```python
"""Stubbed verifier — v1 of the verifier primitive.

Returns deterministic mock claims so the orchestration layer can be tested
end-to-end. Designed to exercise both the remediation loop AND the gap-report
flow:

  - attempt 1: 5 claims, 3 pass + 1 fail + 1 unverifiable
                → triggers remediation AND emits a gap report
  - attempt 2: 5 claims, all pass
                → loop converges, returns "verified"

The real verifier (next milestone) will replace this module without changing
the public function signature. The orchestration layer is verifier-agnostic.
"""
from __future__ import annotations

import time
from typing import Any

from schemas import Claim, GapReport, Improvement, VerificationRecord


VERIFIER_MODEL = "mock-verifier-v1"


def verify(
    *,
    skill_name: str,
    skill_documentation: str,
    intent: str,
    builder_output: Any,
    attempt: int,
    strictness: str = "medium",
) -> VerificationRecord:
    """Run a verification pass.

    Public signature must remain stable — the real verifier will implement
    exactly this function and the orchestrator will not need to change.

    Args:
        skill_name: name of the skill being verified (used as skill_id in reports)
        skill_documentation: the skill's docs (for claim decomposition)
        intent: the user's plain-language goal
        builder_output: whatever the builder skill produced
        attempt: which builder invocation this is (1-indexed)
        strictness: low / medium / high — governs how many claims are emitted

    Returns:
        VerificationRecord — claims, aggregate status, optional gap report.
    """
    start_ms = int(time.time() * 1000)

    if attempt == 1:
        claims = _initial_claims(skill_name, intent, builder_output)
    else:
        claims = _remediated_claims(skill_name, intent, builder_output)

    status = _aggregate_status(claims)
    gap_report = _build_gap_report(skill_name, claims)

    duration_ms = max(1, int(time.time() * 1000) - start_ms)

    return VerificationRecord(
        status=status,
        claims=claims,
        verifier_model=VERIFIER_MODEL,
        duration_ms=duration_ms,
        cost_usd=0.0,
        gap_report=gap_report,
    )


# ---------- internal: claim generation ----------

def _initial_claims(skill_name: str, intent: str, builder_output: Any) -> list[Claim]:
    """Attempt 1 — deliberately mixed verdicts to exercise the remediation loop."""
    output_str = str(builder_output)
    return [
        Claim(
            id="claim_001",
            type="existential",
            statement=f"the output of {skill_name} is non-empty",
            evidence_required="output payload contains data",
            evidence_collected={"length": len(output_str)},
            verdict="pass",
            confidence=0.95,
            reasoning="output is a non-empty string",
        ),
        Claim(
            id="claim_002",
            type="structural",
            statement="the output is valid JSON",
            evidence_required="json.loads succeeds on the payload",
            evidence_collected={"parseable": True},
            verdict="pass",
            confidence=0.98,
            reasoning="output parses as JSON",
        ),
        Claim(
            id="claim_003",
            type="semantic",
            statement=f"the output addresses the user's intent: {intent}",
            evidence_required="output content reflects the intent's key entities",
            evidence_collected={
                "intent_keywords_found": ["test", "report"],
                "intent_keywords_missing": ["amount"],
            },
            verdict="fail",
            confidence=0.75,
            reasoning=(
                "the user's intent references an amount, but the output does not "
                "include any amount-related field"
            ),
        ),
        Claim(
            id="claim_004",
            type="factual",
            statement="the output is timestamped from today",
            evidence_required="timestamp field present and recent",
            evidence_collected=None,
            verdict="unverifiable",
            confidence=0.5,
            reasoning=(
                "the output has no timestamp field and the skill documentation "
                "does not specify whether one should be present"
            ),
        ),
        Claim(
            id="claim_005",
            type="negative",
            statement="no credentials or secrets appear in the output",
            evidence_required="scan for token-shaped strings; none found",
            evidence_collected={"tokens_found": 0},
            verdict="pass",
            confidence=0.99,
            reasoning="no API keys, tokens, or secret patterns detected",
        ),
    ]


def _remediated_claims(skill_name: str, intent: str, builder_output: Any) -> list[Claim]:
    """Attempt 2+ — the builder has remediated; all claims now pass."""
    output_str = str(builder_output)
    return [
        Claim(
            id="claim_001",
            type="existential",
            statement=f"the output of {skill_name} is non-empty",
            evidence_required="output payload contains data",
            evidence_collected={"length": len(output_str)},
            verdict="pass",
            confidence=0.95,
            reasoning="output is a non-empty string",
        ),
        Claim(
            id="claim_002",
            type="structural",
            statement="the output is valid JSON",
            evidence_required="json.loads succeeds on the payload",
            evidence_collected={"parseable": True},
            verdict="pass",
            confidence=0.98,
            reasoning="output parses as JSON",
        ),
        Claim(
            id="claim_003",
            type="semantic",
            statement=f"the output addresses the user's intent: {intent}",
            evidence_required="output content reflects the intent's key entities",
            evidence_collected={
                "intent_keywords_found": ["test", "report", "amount"],
            },
            verdict="pass",
            confidence=0.90,
            reasoning="the output now includes the amount value the intent requires",
        ),
        Claim(
            id="claim_004",
            type="factual",
            statement="the output is timestamped from today",
            evidence_required="timestamp field present and recent",
            evidence_collected={"timestamp_present": True, "is_today": True},
            verdict="pass",
            confidence=0.92,
            reasoning="timestamp field is present and within today's date range",
        ),
        Claim(
            id="claim_005",
            type="negative",
            statement="no credentials or secrets appear in the output",
            evidence_required="scan for token-shaped strings; none found",
            evidence_collected={"tokens_found": 0},
            verdict="pass",
            confidence=0.99,
            reasoning="no API keys, tokens, or secret patterns detected",
        ),
    ]


# ---------- internal: aggregation & gap report ----------

def _aggregate_status(claims: list[Claim]) -> str:
    """Apply the conservative aggregate rule from the design spec.

    verified  → all claims pass
    failed    → at least one claim fails
    partial   → no failures, but at least one unverifiable
    """
    if any(c.verdict == "fail" for c in claims):
        return "failed"
    if any(c.verdict == "unverifiable" for c in claims):
        return "partial"
    return "verified"


def _build_gap_report(skill_name: str, claims: list[Claim]) -> GapReport | None:
    """Build a gap report from any unverifiable claims in this pass.

    Note: gap reports fire whenever there are unverifiable claims, regardless
    of the aggregate status. Even on a "failed" run, the unverifiable claims
    represent real documentation gaps the user might want to fix.
    """
    unverifiable = [c for c in claims if c.verdict == "unverifiable"]
    if not unverifiable:
        return None

    improvements: list[Improvement] = []
    for c in unverifiable:
        improvements.append(
            Improvement(
                claim_id=c.id,
                target="documentation",
                proposed_text=(
                    f"Specify whether the output of {skill_name} includes a "
                    f"{c.type} field for: \"{c.statement}\". If yes, document "
                    "the field name and format. If no, document that it is "
                    "intentionally absent."
                ),
                rationale=c.reasoning,
                confidence=0.7,
            )
        )

    return GapReport(
        skill_id=skill_name,
        unverifiable_claims=unverifiable,
        proposed_improvements=improvements,
        summary=(
            f"{len(unverifiable)} claim(s) could not be verified because the "
            "skill documentation does not specify expectations. The proposed "
            "improvements below would make these verifiable on the next run."
        ),
    )

```

#### `skills/verifier/llm_verifier.py` (320 lines)

Anthropic Claude verifier backend. Requires ANTHROPIC_API_KEY. Uses the canonical verifier system prompt. Tolerates markdown code fences, prose-around-JSON, out-of-range confidence values.

```python
"""Real LLM verifier — calls Anthropic Claude to verify skill output.

Implements the same `verify()` signature as mock_verifier.py. Swapping the
backend is one import change in run_skill_verified.py.

Credentials:
    ANTHROPIC_API_KEY (required) — Anthropic API key for the verifier model.

Optional environment:
    VERIFIER_MODEL — model ID (default: claude-sonnet-4-5-20250929)

Limitations of single-call LLM verifier (deferred to a future agentic verifier):
    - Cannot make tool calls; evidence collection is limited to inspecting the
      builder output text. Behavioral claims that would require execution are
      marked as `unverifiable` with explicit reasoning.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from schemas import Claim, GapReport, Improvement, VerificationRecord


ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"
DEFAULT_MODEL = "claude-sonnet-4-5-20250929"
API_URL = "https://api.anthropic.com/v1/messages"

# Approximate per-million-token pricing for cost estimation. Update as needed.
COST_TABLE_USD = {
    "claude-sonnet-4-5-20250929": {"input": 3.0, "output": 15.0},
    "claude-opus-4-5-20251014":  {"input": 15.0, "output": 75.0},
    "claude-haiku-4-5-20251001": {"input": 1.0, "output": 5.0},
}

STRICTNESS_COUNT_TARGET = {"low": "4", "medium": "7", "high": "12"}

VERIFIER_SYSTEM_PROMPT = """\
You are the verifier. You did not run the skill.

You have received:
- the skill's documentation
- the user's plain-language intent
- the skill's output

Your job: decompose what should be true into atomic claims and render a
verdict for each. You are not trying to be helpful. You are trying to be RIGHT.

CLAIM PRODUCTION RULES
- Claims live at the INTERSECTION of (a) what the docs say the skill does, AND
  (b) what the user's intent says they wanted. Claims outside both are noise;
  do not produce them.
- Each claim must be independently testable from the output text alone.
- Behavioral claims that would require running code, hitting an external API,
  or executing the skill again must be marked `unverifiable` with reasoning
  that the verifier cannot execute (this is a v1 limitation, not a bug).
- For each claim: state it precisely, state what evidence would prove it,
  state what evidence you actually observed (or null), and render a verdict.

CLAIM TYPES (pick the one that fits best):
- existential — asserts a thing exists in the output
- structural — asserts the output has a specific shape, field, or schema
- behavioral — asserts the output would do something when used (often
  unverifiable in v1)
- factual — asserts something is true about the world (date, name, value)
- semantic — asserts the output means what was asked / addresses the intent
- negative — asserts something did NOT happen (no PII leak, no token, etc.)

VERDICTS
- pass — evidence in the output supports the claim
- fail — evidence in the output contradicts the claim
- unverifiable — cannot determine from the output alone; this is a FINDING,
  not a failure. When marking unverifiable, state WHY and WHAT the skill
  documentation or output schema would need so the claim is verifiable next time.

STRICTNESS: {STRICTNESS} — produce {COUNT_TARGET} claims (plus or minus 2).

OUTPUT FORMAT
Return ONLY a JSON object matching this exact shape. No prose. No markdown
code fences. JSON object only:

{
  "claims": [
    {
      "id": "claim_001",
      "type": "existential" | "structural" | "behavioral" | "factual" | "semantic" | "negative",
      "statement": "...",
      "evidence_required": "...",
      "evidence_collected": <object or null>,
      "verdict": "pass" | "fail" | "unverifiable",
      "confidence": 0.0 to 1.0,
      "reasoning": "..."
    }
  ]
}
"""


def verify(
    *,
    skill_name: str,
    skill_documentation: str,
    intent: str,
    builder_output: Any,
    attempt: int,
    strictness: str = "medium",
) -> VerificationRecord:
    """Run a real LLM-driven verification pass.

    Same signature as mock_verifier.verify — drop-in replacement.

    Raises:
        RuntimeError: if the API key is missing, the network call fails, or
        the LLM returns output that cannot be parsed as the expected schema.
    """
    api_key = os.environ.get(ANTHROPIC_API_KEY_ENV)
    if not api_key:
        raise RuntimeError(
            f"{ANTHROPIC_API_KEY_ENV} is not set. The real LLM verifier requires "
            "an Anthropic API key. Configure it as a skill credential, or use "
            "the mock backend via VERIFIER_BACKEND=mock for offline testing."
        )

    start_ms = int(time.time() * 1000)

    model = os.environ.get("VERIFIER_MODEL", DEFAULT_MODEL)
    count_target = STRICTNESS_COUNT_TARGET.get(strictness, "7")
    system_prompt = (
        VERIFIER_SYSTEM_PROMPT
        .replace("{STRICTNESS}", strictness.upper())
        .replace("{COUNT_TARGET}", count_target)
    )

    user_message = json.dumps(
        {
            "skill_name": skill_name,
            "skill_documentation": skill_documentation or "(no documentation provided)",
            "user_intent": intent,
            "attempt_number": attempt,
            "builder_output": str(builder_output),
        },
        indent=2,
    )

    response_data = _call_anthropic(
        api_key=api_key,
        model=model,
        system=system_prompt,
        user_message=user_message,
    )

    full_text = _extract_text(response_data)
    parsed = _extract_json(full_text)
    if parsed is None or "claims" not in parsed:
        raise RuntimeError(
            "Verifier LLM did not return parseable JSON with a 'claims' array.\n"
            f"Raw response: {full_text[:500]}"
        )

    claims = _build_claims(parsed["claims"])
    status = _aggregate_status(claims)
    gap_report = _build_gap_report(skill_name, claims)
    cost_usd = _estimate_cost(model, response_data.get("usage", {}))

    duration_ms = max(1, int(time.time() * 1000) - start_ms)

    return VerificationRecord(
        status=status,
        claims=claims,
        verifier_model=model,
        duration_ms=duration_ms,
        cost_usd=cost_usd,
        gap_report=gap_report,
    )


# ---------- HTTP call ----------

def _call_anthropic(*, api_key: str, model: str, system: str, user_message: str) -> dict:
    body = {
        "model": model,
        "max_tokens": 4096,
        "system": system,
        "messages": [{"role": "user", "content": user_message}],
    }
    request = urllib.request.Request(
        API_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Anthropic API HTTP {e.code}: {body[:500]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error reaching Anthropic API: {e}")


def _extract_text(response: dict) -> str:
    blocks = response.get("content", [])
    parts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
    return "\n".join(parts).strip()


# ---------- response parsing ----------

def _extract_json(text: str) -> dict | None:
    """Tolerate markdown fences and trailing prose around the JSON object."""
    text = text.strip()
    # Strip leading markdown code fence
    if text.startswith("```json"):
        text = text[len("```json"):].lstrip()
    elif text.startswith("```"):
        text = text[len("```"):].lstrip()
    # Strip trailing fence
    if text.endswith("```"):
        text = text[: -len("```")].rstrip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Last resort: find the outermost {...}
        start = text.find("{")
        end = text.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None
    return None


def _build_claims(raw_claims: list) -> list[Claim]:
    claims: list[Claim] = []
    for i, c in enumerate(raw_claims, start=1):
        claims.append(
            Claim(
                id=str(c.get("id", f"claim_{i:03d}")),
                type=str(c.get("type", "semantic")),
                statement=str(c.get("statement", "")),
                evidence_required=str(c.get("evidence_required", "")),
                evidence_collected=c.get("evidence_collected"),
                verdict=str(c.get("verdict", "unverifiable")),
                confidence=_safe_float(c.get("confidence"), default=0.5),
                reasoning=str(c.get("reasoning", "")),
            )
        )
    return claims


def _safe_float(value, *, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, v))


# ---------- aggregation (same rules as mock_verifier) ----------

def _aggregate_status(claims: list[Claim]) -> str:
    if any(c.verdict == "fail" for c in claims):
        return "failed"
    if any(c.verdict == "unverifiable" for c in claims):
        return "partial"
    return "verified"


def _build_gap_report(skill_name: str, claims: list[Claim]) -> GapReport | None:
    unverifiable = [c for c in claims if c.verdict == "unverifiable"]
    if not unverifiable:
        return None

    improvements: list[Improvement] = []
    for c in unverifiable:
        improvements.append(
            Improvement(
                claim_id=c.id,
                target="documentation",
                proposed_text=(
                    f"Document the expected evidence for: \"{c.statement}\". "
                    f"Verifier reasoning: {c.reasoning}"
                ),
                rationale=c.reasoning,
                confidence=0.7,
            )
        )

    return GapReport(
        skill_id=skill_name,
        unverifiable_claims=unverifiable,
        proposed_improvements=improvements,
        summary=(
            f"{len(unverifiable)} claim(s) could not be verified from the output "
            "alone. Proposed documentation changes would make these verifiable "
            "on the next run."
        ),
    )


def _estimate_cost(model: str, usage: dict) -> float:
    rates = COST_TABLE_USD.get(model)
    if rates is None:
        return 0.0
    input_tokens = int(usage.get("input_tokens", 0))
    output_tokens = int(usage.get("output_tokens", 0))
    return (
        input_tokens / 1_000_000 * rates["input"]
        + output_tokens / 1_000_000 * rates["output"]
    )

```

#### `skills/verifier/replicate_verifier.py` (317 lines)

Replicate verifier backend. Requires REPLICATE_API_TOKEN. Default model: meta/meta-llama-3-8b-instruct. Handles Replicate's tokenized output array format. End-to-end verified.

```python
"""Real LLM verifier backed by Replicate's API.

Implements the same `verify()` signature as mock_verifier and llm_verifier.
Designed as a third pluggable backend so users can pick whichever provider
they have a key for.

Credentials:
    REPLICATE_API_TOKEN (required) — set as a skill credential.

Optional environment:
    VERIFIER_REPLICATE_MODEL — default "meta/meta-llama-3-8b-instruct"

Why Replicate as an option:
  - Cheap free-tier-ish for small models (~$0.0002/call for Llama-3-8b)
  - Many models available (Llama, Mistral, Mixtral, Gemma, etc.)
  - Different failure modes than Anthropic — useful for cross-family pairings
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from schemas import Claim, GapReport, Improvement, VerificationRecord


REPLICATE_API_TOKEN_ENV = "REPLICATE_API_TOKEN"
DEFAULT_MODEL = "meta/meta-llama-3-8b-instruct"
API_BASE = "https://api.replicate.com/v1/models"


STRICTNESS_COUNT_TARGET = {"low": "4", "medium": "7", "high": "12"}

VERIFIER_SYSTEM_PROMPT = """\
You are the verifier. You did not run the skill.

You have received:
- the skill's documentation
- the user's plain-language intent
- the skill's output

Your job: decompose what should be true into atomic claims and render a
verdict for each. You are not trying to be helpful. You are trying to be RIGHT.

CLAIM PRODUCTION RULES
- Claims live at the INTERSECTION of (a) what the docs say the skill does, AND
  (b) what the user's intent says they wanted. Claims outside both are noise.
- Each claim must be independently testable from the output text alone.
- Behavioral claims that would require running code must be marked
  `unverifiable` with reasoning that the verifier cannot execute (v1 limitation).
- For each claim: state it precisely, state what evidence would prove it,
  state what evidence you actually observed (or null), and render a verdict.

CLAIM TYPES (pick the one that fits best):
- existential — asserts a thing exists in the output
- structural — asserts the output has a specific shape, field, or schema
- behavioral — asserts the output would do something when used
- factual — asserts something is true about the world
- semantic — asserts the output means what was asked
- negative — asserts something did NOT happen (no PII leak, no token, etc.)

VERDICTS
- pass — evidence in the output supports the claim
- fail — evidence in the output contradicts the claim
- unverifiable — cannot determine from the output alone

STRICTNESS: {STRICTNESS} — produce {COUNT_TARGET} claims (plus or minus 2).

OUTPUT FORMAT
Return ONLY a JSON object matching this exact shape. No prose. No markdown
code fences. JSON object only:

{
  "claims": [
    {
      "id": "claim_001",
      "type": "structural",
      "statement": "...",
      "evidence_required": "...",
      "evidence_collected": <object or null>,
      "verdict": "pass" | "fail" | "unverifiable",
      "confidence": 0.0 to 1.0,
      "reasoning": "..."
    }
  ]
}
"""


def verify(
    *,
    skill_name: str,
    skill_documentation: str,
    intent: str,
    builder_output: Any,
    attempt: int,
    strictness: str = "medium",
) -> VerificationRecord:
    """Real LLM verification via Replicate. Same signature as mock/llm verifiers."""
    api_token = os.environ.get(REPLICATE_API_TOKEN_ENV)
    if not api_token:
        raise RuntimeError(
            f"{REPLICATE_API_TOKEN_ENV} is not set. Configure it as a skill "
            "credential, or use the mock backend via VERIFIER_BACKEND=mock."
        )

    start_ms = int(time.time() * 1000)
    model = os.environ.get("VERIFIER_REPLICATE_MODEL", DEFAULT_MODEL)
    count_target = STRICTNESS_COUNT_TARGET.get(strictness, "7")

    system_prompt = (
        VERIFIER_SYSTEM_PROMPT
        .replace("{STRICTNESS}", strictness.upper())
        .replace("{COUNT_TARGET}", count_target)
    )
    user_message = json.dumps({
        "skill_name": skill_name,
        "skill_documentation": skill_documentation or "(no documentation provided)",
        "user_intent": intent,
        "attempt_number": attempt,
        "builder_output": str(builder_output),
    }, indent=2)

    # Replicate's chat-style models use a "prompt" field. Llama-3 family also
    # supports a system prompt via Replicate's API.
    full_prompt = (
        f"<SYSTEM>\n{system_prompt}\n</SYSTEM>\n\n<INPUT>\n{user_message}\n</INPUT>"
    )

    raw_output = _call_replicate(
        api_token=api_token,
        model=model,
        input_data={
            "prompt": full_prompt,
            "max_tokens": 2048,
            "temperature": 0,
            "system_prompt": system_prompt,
        },
    )

    parsed = _extract_json(raw_output)
    if parsed is None or "claims" not in parsed:
        raise RuntimeError(
            "Verifier LLM did not return parseable JSON with a 'claims' array.\n"
            f"Raw response (first 500): {raw_output[:500]}"
        )

    claims = _build_claims(parsed["claims"])
    status = _aggregate_status(claims)
    gap_report = _build_gap_report(skill_name, claims)

    duration_ms = max(1, int(time.time() * 1000) - start_ms)

    return VerificationRecord(
        status=status,
        claims=claims,
        verifier_model=model,
        duration_ms=duration_ms,
        cost_usd=0.0,  # Replicate doesn't return per-call cost; calculate offline if needed
        gap_report=gap_report,
    )


# ---------- HTTP call ----------

def _call_replicate(*, api_token: str, model: str, input_data: dict,
                    poll_interval_sec: float = 1.0,
                    max_wait_sec: float = 120.0) -> str:
    """Create a prediction with synchronous wait. Returns the model's text output.

    Replicate's chat-style models return output as a list of tokens; we join them.
    """
    url = f"{API_BASE}/{model}/predictions"
    body = json.dumps({"input": input_data}).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
            "Prefer": "wait=60",  # synchronous; up to 60s
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=80) as resp:
            response = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Replicate API HTTP {e.code}: {body_text[:500]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error reaching Replicate: {e}")

    # If still running, poll the prediction until terminal.
    status = response.get("status")
    pid = response.get("id")
    deadline = time.time() + max_wait_sec
    while status in ("starting", "processing") and time.time() < deadline:
        time.sleep(poll_interval_sec)
        get_url = f"https://api.replicate.com/v1/predictions/{pid}"
        try:
            with urllib.request.urlopen(
                urllib.request.Request(
                    get_url,
                    headers={"Authorization": f"Bearer {api_token}"},
                ),
                timeout=20,
            ) as r:
                response = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Replicate poll HTTP {e.code}: {body_text[:500]}")
        status = response.get("status")

    if status != "succeeded":
        raise RuntimeError(
            f"Replicate prediction did not succeed: status={status}, "
            f"error={response.get('error')}"
        )

    output = response.get("output")
    if isinstance(output, list):
        return "".join(str(x) for x in output)
    return str(output) if output is not None else ""


# ---------- response parsing ----------

def _extract_json(text: str) -> dict | None:
    """Tolerate markdown fences and trailing prose around the JSON object."""
    text = text.strip()
    if text.startswith("```json"):
        text = text[len("```json"):].lstrip()
    elif text.startswith("```"):
        text = text[len("```"):].lstrip()
    if text.endswith("```"):
        text = text[: -len("```")].rstrip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(text[start: end + 1])
            except json.JSONDecodeError:
                return None
    return None


def _build_claims(raw_claims: list) -> list[Claim]:
    claims: list[Claim] = []
    for i, c in enumerate(raw_claims, start=1):
        claims.append(
            Claim(
                id=str(c.get("id", f"claim_{i:03d}")),
                type=str(c.get("type", "semantic")),
                statement=str(c.get("statement", "")),
                evidence_required=str(c.get("evidence_required", "")),
                evidence_collected=c.get("evidence_collected"),
                verdict=str(c.get("verdict", "unverifiable")),
                confidence=_safe_float(c.get("confidence"), default=0.5),
                reasoning=str(c.get("reasoning", "")),
            )
        )
    return claims


def _safe_float(value, *, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, v))


def _aggregate_status(claims: list[Claim]) -> str:
    if any(c.verdict == "fail" for c in claims):
        return "failed"
    if any(c.verdict == "unverifiable" for c in claims):
        return "partial"
    return "verified"


def _build_gap_report(skill_name: str, claims: list[Claim]) -> GapReport | None:
    unverifiable = [c for c in claims if c.verdict == "unverifiable"]
    if not unverifiable:
        return None

    improvements: list[Improvement] = []
    for c in unverifiable:
        improvements.append(
            Improvement(
                claim_id=c.id,
                target="documentation",
                proposed_text=(
                    f"Document the expected evidence for: \"{c.statement}\". "
                    f"Verifier reasoning: {c.reasoning}"
                ),
                rationale=c.reasoning,
                confidence=0.7,
            )
        )

    return GapReport(
        skill_id=skill_name,
        unverifiable_claims=unverifiable,
        proposed_improvements=improvements,
        summary=(
            f"{len(unverifiable)} claim(s) could not be verified from the output "
            "alone. Proposed documentation changes would make these verifiable next time."
        ),
    )

```

#### `skills/verifier/schemas.py` (85 lines)

Data model: Claim, Improvement, GapReport, VerificationRecord, RunSkillVerifiedResult. The contract every verifier backend honors.

```python
"""Verifier primitive — data model.

Mirrors the design spec (Reference No. 02). These dataclasses are the contract
between the orchestrator (run_skill_verified.py) and any verifier implementation
(mock_verifier.py today, real LLM verifier later).

Keeping them in one place means: swapping the verifier is a drop-in.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Literal, Optional


# Type aliases (documentation only — Python doesn't enforce at runtime).
ClaimType = Literal[
    "existential",   # asserts a thing exists
    "structural",    # asserts a thing has a shape
    "behavioral",    # asserts a thing does something
    "factual",       # asserts something is true about the world
    "semantic",      # asserts the output means what was asked
    "negative",      # asserts something did NOT happen
]
Verdict = Literal["pass", "fail", "unverifiable"]
RunStatus = Literal["verified", "failed", "partial"]
Strictness = Literal["low", "medium", "high"]
ImprovementTarget = Literal["documentation", "output_schema", "script"]


@dataclass
class Claim:
    """A single, independently testable assertion about the output."""
    id: str
    type: str                # ClaimType
    statement: str           # human-readable assertion
    evidence_required: str   # what would prove this true
    verdict: str             # Verdict
    confidence: float        # 0.0 - 1.0
    reasoning: str           # why the verifier chose this verdict
    evidence_collected: Optional[Any] = None


@dataclass
class Improvement:
    """A proposed change to the skill, derived from an unverifiable claim."""
    claim_id: str
    target: str              # ImprovementTarget
    proposed_text: str
    rationale: str
    confidence: float
    current_text: Optional[str] = None


@dataclass
class GapReport:
    """Packaged improvements emitted when claims are unverifiable.

    Flows into the existing UpdateSkillAndScripts draft-card UI for user review.
    """
    skill_id: str
    unverifiable_claims: List[Claim]
    proposed_improvements: List[Improvement]
    summary: str             # 1-2 sentences for the user


@dataclass
class VerificationRecord:
    """The verifier's output for a single verification pass."""
    status: str              # RunStatus
    claims: List[Claim]
    verifier_model: str
    duration_ms: int
    cost_usd: float
    gap_report: Optional[GapReport] = None


@dataclass
class RunSkillVerifiedResult:
    """The final result of a verified skill run, returned to the caller."""
    result: Any                          # builder's final output
    attempts: int                        # builder invocations used
    verification: VerificationRecord     # the LAST verification (final state)
    attempt_history: List[VerificationRecord] = field(default_factory=list)
    total_duration_ms: int = 0
    total_cost_usd: float = 0.0

```

#### `skills/verifier/remediation.py` (62 lines)

Builds the structured remediation prompt from failed and unverifiable claims. Passed to the builder via REMEDIATION_PROMPT env var on its next attempt.

```python
"""Remediation prompt builder.

Converts failed and unverifiable claims into a structured prompt that the
builder receives on its next attempt. Format follows the design spec §06.
"""
from __future__ import annotations

from typing import List

from schemas import Claim


def build_remediation_prompt(
    *,
    original_command: str,
    original_intent: str,
    failed_claims: List[Claim],
    unverifiable_claims: List[Claim],
) -> str:
    """Build a structured remediation prompt from claim verdicts.

    The output is a multi-line string. The builder is expected to read it via
    the REMEDIATION_PROMPT environment variable and use it to guide the next
    attempt.
    """
    lines: List[str] = [
        "Your previous output was reviewed by an independent verifier.",
        "",
    ]

    if failed_claims:
        lines.append("The following claims FAILED:")
        lines.append("")
        for c in failed_claims:
            lines.append(f"  - {c.statement}")
            lines.append(f"    Why it failed: {c.reasoning}")
            if c.evidence_collected is not None:
                lines.append(f"    Evidence the verifier saw: {c.evidence_collected}")
            lines.append("")

    if unverifiable_claims:
        lines.append("The following claims could NOT be verified:")
        lines.append("")
        for c in unverifiable_claims:
            lines.append(f"  - {c.statement}")
            lines.append(f"    Why not: {c.reasoning}")
            lines.append(
                "    To make this verifiable next time, include in the output the "
                "evidence required: " + c.evidence_required
            )
            lines.append("")

    lines.extend([
        "Run the skill again. Do not change the user's original request.",
        "Address each failed claim. For unverifiable claims, include the evidence",
        "the verifier would need to validate them.",
        "",
        f"Original command: {original_command}",
        f"Original intent: {original_intent}",
    ])

    return "\n".join(lines)

```

#### `skills/verifier/_toy_builder.py` (46 lines)

Self-contained toy builder for verifier-only tests. Lives inside the verifier skill (not the demos directory) so tests can run without external deps.

```python
#!/usr/bin/env python3
"""Toy builder for end-to-end testing of the verifier orchestrator.

Behavior:
- If REMEDIATION_PROMPT env var is empty: produce a deliberately incomplete
  output (missing the "amount" and "timestamp" fields that the mock verifier
  expects).
- If REMEDIATION_PROMPT is present: read it, infer what was missing, and
  produce a complete output.

This is the "builder agent" stand-in. In production, this would be replaced
by a call to RunWithCredentials against a real Hyperagent skill.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone


def main() -> None:
    remediation = os.environ.get("REMEDIATION_PROMPT", "").strip()

    if not remediation:
        # First attempt — deliberately incomplete to exercise the loop.
        output = {
            "status": "test",
            "report": "transaction processed",
        }
    else:
        # Remediated — include the missing fields the verifier flagged.
        # A real builder would parse the remediation prompt and reason about
        # what to change. For the toy, we just include everything plausible.
        output = {
            "status": "test",
            "report": "transaction processed",
            "amount": 100.00,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "_remediation_acknowledged": True,
        }

    print(json.dumps(output))


if __name__ == "__main__":
    main()

```

### § 11E — Shared scripts

Used by every agent: the bus, the memory store, lifecycle helpers, and the verification chain composer.

#### `demos/comms-with-verifier/bus.py` (480 lines)

The SQLite message bus. register, heartbeat, send_direct, publish, publish_to_role, receive (atomic claim-locking), find_agents_by_role + pick_agent_by_role (with reputation filtering), conversation_log, conversation_chain, find_root_conversation.

```python
"""SQLite-backed message bus for peer-to-peer agent communication.

A minimal implementation of the architecture from Reference No. 01:
- Each agent has a stable ID and a friendly name
- Messages carry the envelope from the design spec (from, to, topic, ttl,
  hop_count, conversation_id, etc.)
- Loop prevention is enforced AT THE BUS, not at the agents (TTL + hop limit)
- Supports both topic-based broadcast AND direct addressing

SQLite is used so the demo runs anywhere with no infrastructure. For a real
deployment, swap to Postgres LISTEN/NOTIFY or Redis pub/sub — the public
function signatures are stable.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any, Optional


DEFAULT_TTL_SECONDS = 300
MAX_HOP_COUNT = 8
HEARTBEAT_STALE_SECONDS = 90
ROLE_TOPIC_PREFIX = "role."   # workers subscribe to ROLE_TOPIC_PREFIX + role


SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    subscriptions TEXT NOT NULL,     -- JSON array of topics
    last_heartbeat REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    parent_conversation_id TEXT,     -- NULL for top-level, links nested sub-conversations
    from_agent TEXT NOT NULL,
    to_agent TEXT,                   -- NULL for broadcast
    topic TEXT,                      -- NULL for direct-only
    msg_type TEXT NOT NULL,          -- request | response | event
    reply_to TEXT,
    hop_count INTEGER NOT NULL DEFAULT 0,
    ttl_seconds INTEGER NOT NULL DEFAULT 300,
    created_at REAL NOT NULL,
    payload TEXT NOT NULL,           -- JSON
    claimed_by TEXT,
    claimed_at REAL
);

CREATE INDEX IF NOT EXISTS idx_messages_inbox ON messages(to_agent, claimed_by);
CREATE INDEX IF NOT EXISTS idx_messages_topic ON messages(topic, claimed_by);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_messages_parent ON messages(parent_conversation_id);
"""

# Idempotent migration for databases created before parent_conversation_id existed.
def _migrate_add_parent_conv(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if "parent_conversation_id" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN parent_conversation_id TEXT")


class Bus:
    """A simple peer-to-peer message bus over SQLite."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_schema()

    # ---------- internal ----------

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10.0, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as c:
            for stmt in SCHEMA.strip().split(";"):
                if stmt.strip():
                    c.execute(stmt)
            _migrate_add_parent_conv(c)

    # ---------- registry ----------

    def register(self, agent_id: str, name: str, role: str, subscriptions: list[str]) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO agents(id, name, role, subscriptions, last_heartbeat) "
                "VALUES (?, ?, ?, ?, ?)",
                (agent_id, name, role, json.dumps(subscriptions), time.time()),
            )

    def heartbeat(self, agent_id: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE agents SET last_heartbeat = ? WHERE id = ?",
                (time.time(), agent_id),
            )

    def list_agents(self, include_stale: bool = False) -> list[dict]:
        now = time.time()
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, name, role, subscriptions, last_heartbeat FROM agents"
            ).fetchall()
        agents = []
        for r in rows:
            agent = {
                "id": r[0],
                "name": r[1],
                "role": r[2],
                "subscriptions": json.loads(r[3]),
                "last_heartbeat": r[4],
                "is_alive": (now - r[4]) < HEARTBEAT_STALE_SECONDS,
            }
            if include_stale or agent["is_alive"]:
                agents.append(agent)
        return agents

    # ---------- role-based discovery ----------

    def find_agents_by_role(
        self,
        role: str,
        alive_only: bool = True,
        memory_store: Any = None,
        reputation_min: Optional[float] = None,
        min_reputation_samples: int = 3,
    ) -> list[dict]:
        """Return all agents matching the given role.

        With alive_only=True (default), only agents within the heartbeat window
        are returned. Sorted by least-recently-active first for fairness.

        Reputation-aware filtering (optional):
            Pass `memory_store` (a MemoryStore instance) and `reputation_min`
            (a float 0.0-1.0). Agents with success_rate below the threshold
            are filtered out, UNLESS they have fewer than min_reputation_samples
            samples (so new agents get a chance to prove themselves).
        """
        candidates = [a for a in self.list_agents(include_stale=not alive_only)
                      if a["role"] == role]
        if alive_only:
            candidates = [a for a in candidates if a["is_alive"]]

        if memory_store is not None and reputation_min is not None:
            filtered = []
            for a in candidates:
                rep = memory_store.reputation_summary(a["id"])
                # New agents (insufficient samples) get a probation pass.
                if rep["total"] < min_reputation_samples:
                    a = dict(a)
                    a["_reputation_status"] = "probation"
                    filtered.append(a)
                    continue
                if (rep["success_rate"] or 0) >= reputation_min:
                    a = dict(a)
                    a["_reputation_status"] = "trusted"
                    a["_reputation_score"] = rep["success_rate"]
                    filtered.append(a)
                # Else: filtered out for low reputation.
            candidates = filtered

        candidates.sort(key=lambda a: a["last_heartbeat"])
        return candidates

    def pick_agent_by_role(
        self,
        role: str,
        exclude: Optional[set[str]] = None,
        memory_store: Any = None,
        reputation_min: Optional[float] = None,
    ) -> Optional[dict]:
        """Pick one live agent matching a role.

        exclude is an optional set of agent_ids to skip.

        Reputation-aware picks: pass memory_store + reputation_min to skip
        replicas with bad track records (with probation for new agents).

        NOTE: for true worker-pool fanout, prefer publish_to_role() — the bus's
        atomic claim-locking is a better load balancer than caller-side picking.
        """
        exclude = exclude or set()
        for a in self.find_agents_by_role(
            role, alive_only=True,
            memory_store=memory_store, reputation_min=reputation_min,
        ):
            if a["id"] not in exclude:
                return a
        return None

    def publish_to_role(
        self,
        *,
        from_agent: str,
        role: str,
        payload: Any,
        conversation_id: Optional[str] = None,
        parent_conversation_id: Optional[str] = None,
        msg_type: str = "request",
        hop_count: int = 0,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> tuple[str, str]:
        """Publish to the role's shared topic — any subscribed replica may claim.

        This is the right primitive for worker pools: the caller doesn't pick a
        specific replica, the bus does the load balancing through atomic claim-locking
        in receive(). Returns (message_id, topic).
        """
        topic = ROLE_TOPIC_PREFIX + role
        msg_id = self.publish(
            from_agent=from_agent,
            topic=topic,
            payload=payload,
            conversation_id=conversation_id,
            parent_conversation_id=parent_conversation_id,
            msg_type=msg_type,
            hop_count=hop_count,
            ttl_seconds=ttl_seconds,
        )
        return msg_id, topic

    # ---------- send ----------

    def _insert_message(self, msg: dict) -> str:
        with self._conn() as c:
            c.execute(
                "INSERT INTO messages(id, conversation_id, parent_conversation_id, "
                "from_agent, to_agent, topic, msg_type, reply_to, hop_count, "
                "ttl_seconds, created_at, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    msg["id"],
                    msg["conversation_id"],
                    msg.get("parent_conversation_id"),
                    msg["from_agent"],
                    msg.get("to_agent"),
                    msg.get("topic"),
                    msg["msg_type"],
                    msg.get("reply_to"),
                    msg.get("hop_count", 0),
                    msg.get("ttl_seconds", DEFAULT_TTL_SECONDS),
                    msg["created_at"],
                    json.dumps(msg["payload"]),
                ),
            )
        return msg["id"]

    def publish(
        self,
        *,
        from_agent: str,
        topic: str,
        payload: Any,
        conversation_id: Optional[str] = None,
        parent_conversation_id: Optional[str] = None,
        msg_type: str = "event",
        hop_count: int = 0,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> str:
        """Broadcast to a topic. Returns the message id.

        parent_conversation_id is set when this message is part of a nested
        sub-conversation started by a worker to fulfill an outer request.
        """
        if hop_count > MAX_HOP_COUNT:
            raise RuntimeError(f"hop_count {hop_count} exceeds MAX_HOP_COUNT {MAX_HOP_COUNT}")
        msg = {
            "id": "msg_" + uuid.uuid4().hex[:12],
            "conversation_id": conversation_id or "cnv_" + uuid.uuid4().hex[:12],
            "parent_conversation_id": parent_conversation_id,
            "from_agent": from_agent,
            "to_agent": None,
            "topic": topic,
            "msg_type": msg_type,
            "hop_count": hop_count,
            "ttl_seconds": ttl_seconds,
            "created_at": time.time(),
            "payload": payload,
        }
        return self._insert_message(msg)

    def send_direct(
        self,
        *,
        from_agent: str,
        to_agent: str,
        payload: Any,
        conversation_id: Optional[str] = None,
        parent_conversation_id: Optional[str] = None,
        reply_to: Optional[str] = None,
        msg_type: str = "request",
        hop_count: int = 0,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> str:
        """Send a directly-addressed message. Returns the message id.

        parent_conversation_id is set when this message is part of a nested
        sub-conversation (e.g. Gamma asking Beta in order to fulfill Alpha's
        request). Audit tools can walk the chain via this field.
        """
        if hop_count > MAX_HOP_COUNT:
            raise RuntimeError(f"hop_count {hop_count} exceeds MAX_HOP_COUNT {MAX_HOP_COUNT}")
        msg = {
            "id": "msg_" + uuid.uuid4().hex[:12],
            "conversation_id": conversation_id or "cnv_" + uuid.uuid4().hex[:12],
            "parent_conversation_id": parent_conversation_id,
            "from_agent": from_agent,
            "to_agent": to_agent,
            "topic": None,
            "msg_type": msg_type,
            "reply_to": reply_to,
            "hop_count": hop_count,
            "ttl_seconds": ttl_seconds,
            "created_at": time.time(),
            "payload": payload,
        }
        return self._insert_message(msg)

    # ---------- receive ----------

    def receive(
        self,
        *,
        agent_id: str,
        subscriptions: list[str],
        max_messages: int = 1,
        wait_sec: float = 0.0,
        poll_interval_sec: float = 0.1,
    ) -> list[dict]:
        """Atomically claim and return messages addressed to this agent or to
        a topic the agent subscribes to. Polls up to wait_sec for new arrivals.

        Drops messages whose TTL has expired.
        """
        deadline = time.time() + wait_sec
        while True:
            claimed = self._claim_messages(agent_id, subscriptions, max_messages)
            if claimed or time.time() >= deadline:
                return claimed
            time.sleep(poll_interval_sec)

    def _claim_messages(
        self, agent_id: str, subscriptions: list[str], max_messages: int
    ) -> list[dict]:
        now = time.time()
        topic_placeholders = ",".join("?" * len(subscriptions)) if subscriptions else "''"

        with self._conn() as c:
            # Lock: find candidate ids, then claim them in a single UPDATE.
            params: list[Any] = [agent_id]
            sql = (
                "SELECT id, created_at, ttl_seconds FROM messages "
                "WHERE claimed_by IS NULL AND (to_agent = ?"
            )
            if subscriptions:
                sql += f" OR (to_agent IS NULL AND topic IN ({topic_placeholders}))"
                params.extend(subscriptions)
            sql += ") ORDER BY created_at ASC LIMIT ?"
            params.append(max_messages * 4)  # over-fetch to skip expired

            candidates = c.execute(sql, params).fetchall()
            usable_ids: list[str] = []
            for row in candidates:
                mid, created_at, ttl = row
                if (now - created_at) > ttl:
                    # TTL expired — sweep these so they don't accumulate.
                    c.execute("DELETE FROM messages WHERE id = ?", (mid,))
                    continue
                usable_ids.append(mid)
                if len(usable_ids) >= max_messages:
                    break
            if not usable_ids:
                return []

            # Claim atomically.
            placeholders = ",".join("?" * len(usable_ids))
            c.execute(
                f"UPDATE messages SET claimed_by = ?, claimed_at = ? "
                f"WHERE id IN ({placeholders}) AND claimed_by IS NULL",
                [agent_id, now, *usable_ids],
            )

            rows = c.execute(
                f"SELECT id, conversation_id, parent_conversation_id, from_agent, "
                f"to_agent, topic, msg_type, reply_to, hop_count, ttl_seconds, "
                f"created_at, payload "
                f"FROM messages WHERE id IN ({placeholders}) AND claimed_by = ?",
                [*usable_ids, agent_id],
            ).fetchall()

        return [self._row_to_message(r) for r in rows]

    def _row_to_message(self, row: tuple) -> dict:
        return {
            "id": row[0],
            "conversation_id": row[1],
            "parent_conversation_id": row[2],
            "from_agent": row[3],
            "to_agent": row[4],
            "topic": row[5],
            "msg_type": row[6],
            "reply_to": row[7],
            "hop_count": row[8],
            "ttl_seconds": row[9],
            "created_at": row[10],
            "payload": json.loads(row[11]),
        }

    # ---------- inspection (for the demo transcript) ----------

    def conversation_log(self, conversation_id: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, conversation_id, parent_conversation_id, from_agent, "
                "to_agent, topic, msg_type, reply_to, hop_count, ttl_seconds, "
                "created_at, payload "
                "FROM messages WHERE conversation_id = ? ORDER BY created_at ASC",
                (conversation_id,),
            ).fetchall()
        return [self._row_to_message(r) for r in rows]

    def conversation_chain(self, root_conversation_id: str) -> dict[str, list[dict]]:
        """Walk descendants of a root conversation and return every linked
        conversation, keyed by its id, in chronological order within each.

        Returns: { conversation_id: [messages, ...], ... } including the root.
        """
        chain: dict[str, list[dict]] = {}
        to_visit = [root_conversation_id]
        visited: set[str] = set()
        while to_visit:
            cid = to_visit.pop()
            if cid in visited:
                continue
            visited.add(cid)
            chain[cid] = self.conversation_log(cid)
            # Find children: any conversation whose parent_conversation_id == cid
            with self._conn() as c:
                rows = c.execute(
                    "SELECT DISTINCT conversation_id FROM messages "
                    "WHERE parent_conversation_id = ?",
                    (cid,),
                ).fetchall()
            for (child_cid,) in rows:
                if child_cid not in visited:
                    to_visit.append(child_cid)
        return chain

    def find_root_conversation(self, conversation_id: str) -> str:
        """Walk parent links up to find the root conversation_id."""
        current = conversation_id
        seen: set[str] = set()
        while current not in seen:
            seen.add(current)
            with self._conn() as c:
                row = c.execute(
                    "SELECT parent_conversation_id FROM messages "
                    "WHERE conversation_id = ? AND parent_conversation_id IS NOT NULL "
                    "LIMIT 1",
                    (current,),
                ).fetchone()
            if row is None or row[0] is None:
                return current
            current = row[0]
        return current

```

#### `demos/comms-with-verifier/agent_memory.py` (318 lines)

SQLite memory store. store_gap with dedup + reinforcement, store_failure_pattern, store_reputation, reputation_summary, claim_guidelines_for_skill (renders memories as system-prompt hints for the LLM verifier).

```python
"""agent_memory.py — durable memory for the agent system.

Each verification produces signal. Gap reports name documentation gaps,
failed claims name brittleness patterns, remediations name what worked.
This module persists those signals so the system gets smarter over time.

Three memory scopes:
  - global         → shared facts (e.g., known-bad input patterns)
  - skill.<name>   → per-skill learning (gaps, failure patterns, remediations)
  - agent.<id>     → per-agent reputation (verification track record)

The orchestrator consults memories at the start of each run and stores new
ones at the end. The mock verifier doesn't read memories (deterministic by
design), but the LLM verifier does — they're injected into the system prompt
as additional `claim_guidelines`.

Storage is a separate SQLite file so memories survive even when the bus DB
is wiped between demos. Path is configurable.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any, Optional


DEFAULT_MEMORY_DB = "_agent_memory.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    category TEXT NOT NULL,
    subject TEXT,
    content TEXT NOT NULL,
    importance REAL NOT NULL DEFAULT 0.5,
    created_at REAL NOT NULL,
    last_used_at REAL,
    use_count INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope);
CREATE INDEX IF NOT EXISTS idx_memories_scope_category ON memories(scope, category);
CREATE INDEX IF NOT EXISTS idx_memories_subject ON memories(subject);
"""


class MemoryStore:
    """SQLite-backed memory store with scope-based organization.

    Scope conventions:
      - "global"           — broadly applicable lessons
      - "skill.<name>"     — per-skill knowledge (gaps, patterns)
      - "agent.<id>"       — per-agent reputation, preferences

    Category conventions:
      - "gap"              — documentation gap surfaced by a verifier
      - "failure_pattern"  — a claim that has failed before
      - "remediation"      — what remediation prompt fixed a past failure
      - "reputation"       — running success/failure counts for an agent
      - "fact"             — a general known fact
    """

    def __init__(self, db_path: str = DEFAULT_MEMORY_DB):
        self.db_path = db_path
        self._init_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10.0, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as c:
            for stmt in SCHEMA.strip().split(";"):
                if stmt.strip():
                    c.execute(stmt)

    # ---------- generic ----------

    def store(
        self,
        *,
        scope: str,
        category: str,
        content: Any,
        subject: Optional[str] = None,
        importance: float = 0.5,
    ) -> str:
        """Store a memory. Returns the memory id."""
        mid = "mem_" + uuid.uuid4().hex[:12]
        with self._conn() as c:
            c.execute(
                "INSERT INTO memories(id, scope, category, subject, content, "
                "importance, created_at, last_used_at, use_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 0)",
                (mid, scope, category, subject,
                 json.dumps(content) if not isinstance(content, str) else content,
                 importance, time.time()),
            )
        return mid

    def recall(
        self,
        *,
        scope: str,
        category: Optional[str] = None,
        subject: Optional[str] = None,
        limit: int = 10,
    ) -> list[dict]:
        """Recall memories matching scope (and optional filters).

        Marks each returned memory as 'used' (increments use_count, updates
        last_used_at). Ordered by importance desc then created_at desc.
        """
        sql = "SELECT id, scope, category, subject, content, importance, created_at FROM memories WHERE scope = ?"
        params: list[Any] = [scope]
        if category is not None:
            sql += " AND category = ?"
            params.append(category)
        if subject is not None:
            sql += " AND subject = ?"
            params.append(subject)
        sql += " ORDER BY importance DESC, created_at DESC LIMIT ?"
        params.append(limit)

        with self._conn() as c:
            rows = c.execute(sql, params).fetchall()
            result_ids = [r[0] for r in rows]
            if result_ids:
                placeholders = ",".join("?" * len(result_ids))
                c.execute(
                    f"UPDATE memories SET use_count = use_count + 1, "
                    f"last_used_at = ? WHERE id IN ({placeholders})",
                    [time.time(), *result_ids],
                )

        return [{
            "id": r[0], "scope": r[1], "category": r[2], "subject": r[3],
            "content": _maybe_json(r[4]),
            "importance": r[5], "created_at": r[6],
        } for r in rows]

    def count(self, scope: Optional[str] = None) -> int:
        with self._conn() as c:
            if scope:
                row = c.execute(
                    "SELECT COUNT(*) FROM memories WHERE scope = ?", (scope,)
                ).fetchone()
            else:
                row = c.execute("SELECT COUNT(*) FROM memories").fetchone()
        return row[0]

    def all_scopes(self) -> list[str]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT DISTINCT scope FROM memories ORDER BY scope"
            ).fetchall()
        return [r[0] for r in rows]

    # ---------- high-level helpers ----------

    def store_gap(self, skill_name: str, gap_report: dict) -> list[str]:
        """Store unverifiable claims from a gap report as per-skill memories.

        Deduplication: if a memory with the same (scope, category, subject)
        and the same statement already exists, we bump its importance and
        use_count rather than creating a duplicate row. This keeps the
        memory store from bloating on repeated runs of the same scenario.
        """
        ids: list[str] = []
        scope = f"skill.{skill_name}"
        for c in gap_report.get("unverifiable_claims", []):
            subject = c.get("id", "")
            statement = c.get("statement", "")
            existing = self._find_existing_gap(scope, subject, statement)
            if existing is not None:
                self._reinforce(existing)
                ids.append(existing)
                continue
            mid = self.store(
                scope=scope,
                category="gap",
                subject=subject,
                content={
                    "claim_id": c.get("id"),
                    "type": c.get("type"),
                    "statement": statement,
                    "evidence_required": c.get("evidence_required"),
                    "reasoning": c.get("reasoning"),
                },
                importance=0.7,
            )
            ids.append(mid)
        return ids

    def _find_existing_gap(self, scope: str, subject: str, statement: str) -> Optional[str]:
        """Return the id of an existing gap memory with matching statement, or None."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, content FROM memories "
                "WHERE scope = ? AND category = 'gap' AND subject = ?",
                (scope, subject),
            ).fetchall()
        for mid, content_str in rows:
            try:
                content = json.loads(content_str)
            except (json.JSONDecodeError, TypeError):
                continue
            if content.get("statement") == statement:
                return mid
        return None

    def _reinforce(self, memory_id: str, *, importance_bump: float = 0.05) -> None:
        """A repeated observation makes a memory more important (capped at 1.0)."""
        with self._conn() as c:
            c.execute(
                "UPDATE memories SET use_count = use_count + 1, "
                "last_used_at = ?, "
                "importance = MIN(1.0, importance + ?) "
                "WHERE id = ?",
                (time.time(), importance_bump, memory_id),
            )

    def store_failure_pattern(
        self,
        *,
        skill_name: str,
        claim_id: str,
        statement: str,
        why_failed: str,
        remediation_summary: Optional[str] = None,
    ) -> str:
        """Store a record that a particular claim has failed before, with
        what remediation worked (if any).
        """
        return self.store(
            scope=f"skill.{skill_name}",
            category="failure_pattern",
            subject=claim_id,
            content={
                "claim_id": claim_id,
                "statement": statement,
                "why_failed": why_failed,
                "remediation_summary": remediation_summary,
            },
            importance=0.8,
        )

    def store_reputation(self, agent_id: str, success: bool) -> str:
        """Bump an agent's reputation score. Simple running counter."""
        return self.store(
            scope=f"agent.{agent_id}",
            category="reputation",
            subject="verification_outcome",
            content={"success": success, "ts": time.time()},
            importance=0.3,
        )

    def reputation_summary(self, agent_id: str) -> dict:
        """Return success/total counts for an agent."""
        rows = self.recall(
            scope=f"agent.{agent_id}", category="reputation", limit=10000
        )
        total = len(rows)
        successes = sum(1 for r in rows if r["content"].get("success"))
        return {
            "agent_id": agent_id,
            "successes": successes,
            "total": total,
            "success_rate": (successes / total) if total else None,
        }

    def claim_guidelines_for_skill(self, skill_name: str, limit: int = 5) -> str:
        """Render skill-scoped memories as guidelines string the verifier can
        append to its system prompt. The mock verifier ignores this; the LLM
        verifier uses it to bias claim production.
        """
        gaps = self.recall(scope=f"skill.{skill_name}", category="gap", limit=limit)
        fails = self.recall(scope=f"skill.{skill_name}", category="failure_pattern", limit=limit)
        if not gaps and not fails:
            return ""

        lines = []
        if gaps:
            lines.append("KNOWN DOCUMENTATION GAPS FOR THIS SKILL:")
            for g in gaps:
                stmt = g["content"].get("statement", "")
                why = g["content"].get("reasoning", "")
                lines.append(f"  - {stmt}  (why unverifiable: {why})")
        if fails:
            lines.append("KNOWN FAILURE PATTERNS FOR THIS SKILL:")
            for f in fails:
                stmt = f["content"].get("statement", "")
                why = f["content"].get("why_failed", "")
                rem = f["content"].get("remediation_summary") or "(no recorded fix)"
                lines.append(f"  - {stmt}  · failed because: {why}  · fixed by: {rem}")
        lines.append(
            "Use these to inform claim production. If a known gap or failure "
            "applies, produce a corresponding claim with explicit evidence_required."
        )
        return "\n".join(lines)


def _maybe_json(s: str) -> Any:
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return s

```

#### `demos/comms-with-verifier/agent_lifecycle.py` (39 lines)

Graceful shutdown helpers. ShutdownFlag + install_signal_handlers wires SIGTERM and SIGINT. Pairs with --serve-forever for production agent processes.

```python
"""Shared lifecycle helpers for the demo agents.

Adds graceful shutdown on SIGTERM/SIGINT so agents can be stopped cleanly
from outside without exiting on idle. Pairs with --serve-forever on the CLI.
"""
from __future__ import annotations

import signal
from typing import Callable


class ShutdownFlag:
    """Mutable flag flipped by signal handlers. Agents check it each tick."""

    def __init__(self, on_shutdown: Callable[[], None] | None = None) -> None:
        self._set = False
        self._on_shutdown = on_shutdown

    def is_set(self) -> bool:
        return self._set

    def request(self) -> None:
        if not self._set:
            self._set = True
            if self._on_shutdown is not None:
                self._on_shutdown()


def install_signal_handlers(flag: ShutdownFlag) -> None:
    """Wire SIGTERM and SIGINT to flip the shutdown flag.

    Safe to call once at startup. Re-raising signals is intentionally avoided
    so the main loop can finish its current message before exiting.
    """
    def _handler(signum, frame):
        flag.request()

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)

```

#### `demos/comms-with-verifier/verification_chain.py` (168 lines)

Multi-hop chain composition. walk_chain descends through verification.upstream_verification. build_chain_summary emits weakest-link chain_status, total_cost_usd, total_duration_ms, per_hop trace, merged_gap_report deduped across hops.

```python
"""verification_chain.py — walks a multi-hop verification chain and emits a
single end-to-end summary.

When a worker (Gamma) calls another worker (Beta) to fulfill a request, the
final response to the caller (Alpha) contains nested verification records:

    response.payload = {
        "verification":          { ... Gamma's own work ... },
        "upstream_verification": { ... Beta's verification ... }
                                 # which itself may have upstream_verification
    }

This module:
  - Walks the chain via `upstream_verification` keys
  - Computes end-to-end aggregates:
      * chain_status — weakest link rule (fail > partial > verified)
      * total_cost_usd — sum across all hops
      * total_duration_ms — sum across all hops
      * total_attempts — sum across all hops
      * merged_gap_report — deduped union of all gap_reports
      * hop_count — number of verifications in the chain
  - Returns a structured ChainSummary dict that can be embedded in a response
    payload for downstream consumers.
"""
from __future__ import annotations

from typing import Any


def walk_chain(verification: dict | None) -> list[dict]:
    """Return the verification records in the chain, root first, in the order
    walked from leaf (most-recent worker) to root (deepest upstream).

    The convention: index 0 is the local worker's own verification; subsequent
    entries are upstream, deeper-and-deeper.
    """
    chain: list[dict] = []
    cursor = verification
    seen_ids: set[int] = set()  # python object id to defend against cycles
    while isinstance(cursor, dict):
        if id(cursor) in seen_ids:
            break
        seen_ids.add(id(cursor))
        chain.append(cursor)
        cursor = cursor.get("upstream_verification")
    return chain


def aggregate_status(records: list[dict]) -> str:
    """Weakest-link rule across the chain.

    failed beats partial beats verified beats unknown.
    """
    rank = {"verified": 0, "partial": 1, "failed": 2}
    worst = "verified"
    for r in records:
        s = r.get("status", "verified")
        if rank.get(s, 99) > rank.get(worst, 99):
            worst = s
    return worst


def aggregate_cost(records: list[dict]) -> float:
    return sum(float(r.get("cost_usd", 0.0)) for r in records)


def aggregate_duration(records: list[dict]) -> int:
    return sum(int(r.get("duration_ms", 0)) for r in records)


def merged_gap_report(records: list[dict], skill_id_fallback: str = "chain") -> dict | None:
    """Dedupe gap reports across the chain by (skill_id, claim_id) so the same
    documentation gap doesn't appear twice. Returns None if no gaps anywhere.
    """
    seen: set[tuple[str, str]] = set()
    merged_claims: list[dict] = []
    merged_improvements: list[dict] = []
    skill_ids: list[str] = []

    for record in records:
        gap = record.get("gap_report")
        if not gap:
            continue
        skill_id = gap.get("skill_id", skill_id_fallback)
        if skill_id not in skill_ids:
            skill_ids.append(skill_id)
        for c in gap.get("unverifiable_claims", []):
            key = (skill_id, c.get("id", ""))
            if key in seen:
                continue
            seen.add(key)
            c_copy = dict(c)
            c_copy["_skill_id"] = skill_id   # annotate so consumers know origin
            merged_claims.append(c_copy)
        for imp in gap.get("proposed_improvements", []):
            key = (skill_id, imp.get("claim_id", ""))
            # Keep one improvement per (skill, claim).
            if any((existing.get("_skill_id"), existing.get("claim_id")) == key
                   for existing in merged_improvements):
                continue
            imp_copy = dict(imp)
            imp_copy["_skill_id"] = skill_id
            merged_improvements.append(imp_copy)

    if not merged_claims:
        return None

    return {
        "skill_ids": skill_ids,
        "unverifiable_claims": merged_claims,
        "proposed_improvements": merged_improvements,
        "summary": (
            f"{len(merged_claims)} unverifiable claim(s) across "
            f"{len(records)} verification hop(s) spanning skill(s): "
            f"{', '.join(skill_ids)}."
        ),
    }


def build_chain_summary(verification: dict | None, *,
                        skill_id_fallback: str = "chain") -> dict:
    """Build the end-to-end chain summary embedded in a response payload.

    Schema:
        {
            "chain_status": "verified" | "partial" | "failed",
            "hop_count": int,
            "total_cost_usd": float,
            "total_duration_ms": int,
            "total_attempts": int,
            "per_hop": [
                {"verifier_model": ..., "status": ..., "duration_ms": ..., "cost_usd": ...},
                ...  # root first, leaf last (i.e. deepest upstream is index 0)
            ],
            "merged_gap_report": GapReport | None
        }
    """
    if not isinstance(verification, dict):
        return {
            "chain_status": "verified",
            "hop_count": 0,
            "total_cost_usd": 0.0,
            "total_duration_ms": 0,
            "total_attempts": 0,
            "per_hop": [],
            "merged_gap_report": None,
        }

    chain = walk_chain(verification)
    # Reverse so root (deepest upstream) is index 0 — easier to read in transcripts.
    ordered = list(reversed(chain))

    per_hop = [{
        "verifier_model": r.get("verifier_model", "?"),
        "status": r.get("status", "verified"),
        "duration_ms": int(r.get("duration_ms", 0)),
        "cost_usd": float(r.get("cost_usd", 0.0)),
    } for r in ordered]

    return {
        "chain_status": aggregate_status(chain),
        "hop_count": len(chain),
        "total_cost_usd": aggregate_cost(chain),
        "total_duration_ms": aggregate_duration(chain),
        "total_attempts": 0,  # attempts isn't in VerificationRecord; populated by callers
        "per_hop": per_hop,
        "merged_gap_report": merged_gap_report(chain, skill_id_fallback=skill_id_fallback),
    }

```

### § 11F — Demo runners

Six runnable scripts that drive the system through its supported workflows.

#### `demos/comms-with-verifier/run_demo.py` (133 lines)

Two-agent demo runner. Spawns Beta in background, runs Alpha synchronously, prints conversation transcript and bus state.

```python
#!/usr/bin/env python3
"""End-to-end demo: two agents talking over the bus, with the responder
running its own work through the verifier before answering.

Run from this directory:
    python3 run_demo.py

The demo:
    1. Initializes a fresh SQLite bus
    2. Spawns Beta (worker) in the background
    3. Spawns Alpha (requester); Alpha sends a task and prints the verified response
    4. Prints the full bus conversation log so you can see every envelope
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bus import Bus


HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "_demo_bus.sqlite")


def cleanup_db() -> None:
    for suffix in ("", "-wal", "-shm"):
        path = DB_PATH + suffix
        if os.path.exists(path):
            os.remove(path)


def main() -> int:
    cleanup_db()
    print("=" * 70)
    print("DEMO: agents talking over a bus, with verification on every reply")
    print("=" * 70)
    print()

    # Start Beta in the background — it loops, handles one request, exits.
    beta_proc = subprocess.Popen(
        ["python3", os.path.join(HERE, "agent_beta.py"),
         "--db", DB_PATH, "--max-requests", "1"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    # Give Beta a moment to register.
    time.sleep(1.0)

    # Run Alpha synchronously.
    alpha_proc = subprocess.run(
        ["python3", os.path.join(HERE, "agent_alpha.py"),
         "--db", DB_PATH,
         "--intent", "compute the total of these line items and timestamp the result"],
        capture_output=True,
        text=True,
        timeout=60,
    )

    # Drain Beta.
    try:
        beta_out, _ = beta_proc.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        beta_proc.kill()
        beta_out, _ = beta_proc.communicate()

    # Interleave outputs in roughly chronological order — both wrote to stdout
    # with timestamps via flush=True, but separate streams mean we just print
    # Beta first then Alpha for clarity.
    print("--- Beta (worker) ---")
    print(beta_out.rstrip())
    print()
    print("--- Alpha (requester) ---")
    print(alpha_proc.stdout.rstrip())
    if alpha_proc.stderr.strip():
        print("--- Alpha stderr ---")
        print(alpha_proc.stderr.rstrip())
    print()

    # Print the full bus log.
    bus = Bus(DB_PATH)
    agents = bus.list_agents(include_stale=True)
    print("--- Bus state ---")
    print(f"Registered agents: {len(agents)}")
    for a in agents:
        alive = "ALIVE" if a["is_alive"] else "STALE"
        print(f"  - {a['name']} ({a['role']}) id={a['id']} [{alive}]")
    print()

    # All conversations.
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT DISTINCT conversation_id FROM messages ORDER BY conversation_id"
    ).fetchall()
    conn.close()
    for (conv_id,) in rows:
        log = bus.conversation_log(conv_id)
        print(f"--- Conversation transcript: {conv_id} ({len(log)} messages) ---")
        for m in log:
            arrow = "→"
            target = m["to_agent"] if m["to_agent"] else f"#{m['topic']}"
            payload_preview = _preview_payload(m["payload"])
            print(f"  {m['from_agent']} {arrow} {target}  [{m['msg_type']}]  {payload_preview}")
        print()

    print("=" * 70)
    print("Demo complete.")
    print("=" * 70)
    return alpha_proc.returncode


def _preview_payload(payload: dict) -> str:
    """One-line summary of an envelope payload for the transcript."""
    if "task" in payload and "intent" in payload:
        return f"task={payload['task']!r} intent={payload['intent'][:50]!r}..."
    if "task" in payload and "verification" in payload:
        v = payload["verification"]
        return (f"task={payload['task']!r} attempts={payload.get('attempts')} "
                f"verification.status={v.get('status')}")
    if "task" in payload and "error" in payload:
        return f"task={payload['task']!r} error={payload['error']!r}"
    return json.dumps(payload)[:80]


if __name__ == "__main__":
    sys.exit(main())

```

#### `demos/comms-with-verifier/run_three_agent_demo.py` (141 lines)

Three-agent demo runner. Spawns Beta + Gamma; Alpha sends write_report to Gamma; Gamma asks Beta peer-to-peer in a linked sub-conversation.

```python
#!/usr/bin/env python3
"""Three-agent demo: Alpha asks Gamma; Gamma asks Beta peer-to-peer; verifier
sits inside each worker. Alpha never tells Gamma where to get its data —
Gamma figures it out and goes direct.

Two conversations result:
  - (Alpha ↔ Gamma) — the outer report request
  - (Gamma ↔ Beta)  — Gamma's sub-task for data

Run from this directory:
    python3 run_three_agent_demo.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bus import Bus


HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "_demo_bus.sqlite")


def cleanup_db() -> None:
    for suffix in ("", "-wal", "-shm"):
        p = DB_PATH + suffix
        if os.path.exists(p):
            os.remove(p)


def main() -> int:
    cleanup_db()
    print("=" * 70)
    print("THREE-AGENT DEMO: Alpha → Gamma → Beta, with verification at every hop")
    print("=" * 70)
    print()

    # Beta and Gamma both wait for messages; spawn them in the background.
    beta = subprocess.Popen(
        ["python3", os.path.join(HERE, "agent_beta.py"),
         "--db", DB_PATH, "--max-requests", "1", "--idle-timeout-sec", "45"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    gamma = subprocess.Popen(
        ["python3", os.path.join(HERE, "agent_gamma.py"),
         "--db", DB_PATH, "--max-requests", "1", "--idle-timeout-sec", "60"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )

    # Give them a moment to register.
    time.sleep(1.5)

    # Alpha sends to GAMMA, not Beta. Gamma will discover it needs Beta
    # and ask peer-to-peer in a sub-conversation.
    alpha = subprocess.run(
        ["python3", os.path.join(HERE, "agent_alpha.py"),
         "--db", DB_PATH,
         "--target-id", "agent_gamma",
         "--task", "write_report",
         "--intent",
            "write a short report paragraph summarizing the project total",
         "--wait-sec", "60"],
        capture_output=True, text=True, timeout=90,
    )

    # Drain background agents.
    try:
        beta_out, _ = beta.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        beta.kill(); beta_out, _ = beta.communicate()
    try:
        gamma_out, _ = gamma.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        gamma.kill(); gamma_out, _ = gamma.communicate()

    print("--- Beta (worker) ---")
    print(beta_out.rstrip())
    print()
    print("--- Gamma (writer) ---")
    print(gamma_out.rstrip())
    print()
    print("--- Alpha (requester) ---")
    print(alpha.stdout.rstrip())
    if alpha.stderr.strip():
        print("--- Alpha stderr ---")
        print(alpha.stderr.rstrip())
    print()

    # Bus summary.
    bus = Bus(DB_PATH)
    agents = bus.list_agents(include_stale=True)
    print("--- Bus state ---")
    print(f"Registered agents: {len(agents)}")
    for a in agents:
        alive = "ALIVE" if a["is_alive"] else "STALE"
        print(f"  - {a['name']} ({a['role']}) id={a['id']} [{alive}]")
    print()

    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT DISTINCT conversation_id FROM messages ORDER BY conversation_id"
    ).fetchall()
    conn.close()
    for (cid,) in rows:
        log = bus.conversation_log(cid)
        print(f"--- Conversation: {cid} ({len(log)} messages) ---")
        for m in log:
            target = m["to_agent"] if m["to_agent"] else f'#{m["topic"]}'
            v = m["payload"].get("verification") if isinstance(m["payload"], dict) else None
            v_tag = f" [verification.status={v.get('status')}]" if v else ""
            payload_preview = _preview(m["payload"])
            print(f"  {m['from_agent']} → {target}  [{m['msg_type']}]  "
                  f"{payload_preview}{v_tag}")
        print()

    print("=" * 70)
    print("Demo complete.")
    print("=" * 70)
    return alpha.returncode


def _preview(payload: dict) -> str:
    if not isinstance(payload, dict):
        return str(payload)[:80]
    if "task" in payload and "intent" in payload:
        return f"task={payload['task']!r} intent={payload['intent'][:48]!r}..."
    if "task" in payload and "verification" in payload:
        return f"task={payload['task']!r} attempts={payload.get('attempts')}"
    if "task" in payload and "error" in payload:
        return f"task={payload['task']!r} error={payload['error']!r}"
    return json.dumps(payload)[:80]


if __name__ == "__main__":
    sys.exit(main())

```

#### `demos/comms-with-verifier/run_concurrent_demo.py` (198 lines)

Worker-pool stress test. 2 Beta + 2 Gamma replicas, 5 concurrent Alpha requests via topic-fanout. 6 assertions verify all 5 verified, no duplicates, work distributed across replicas, atomic claim-locking holds.

```python
#!/usr/bin/env python3
"""Concurrent worker-pool demo.

Spawns:
    - 2 Beta replicas (role=worker)
    - 2 Gamma replicas (role=writer)
    - 5 Alpha requesters (each fires one write_report request in parallel)

Alpha agents discover Gamma by role. Gamma agents discover Beta by role.
The bus's atomic claim-locking should ensure each message is handled by
exactly one replica.

Verifies:
    - All 5 requests get responses
    - No duplicate handling on either layer
    - Each chain returns verification.chain_status == "verified"
    - The bus log shows fanout across replicas (not all to one)

Run from this directory:
    python3 run_concurrent_demo.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bus import Bus


HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "_concurrent_bus.sqlite")
N_REQUESTS = 5
N_BETAS = 2
N_GAMMAS = 2


def cleanup_db() -> None:
    for suffix in ("", "-wal", "-shm"):
        p = DB_PATH + suffix
        if os.path.exists(p):
            os.remove(p)


def main() -> int:
    cleanup_db()
    print("=" * 72)
    print(f"CONCURRENT DEMO · {N_REQUESTS} alphas → {N_GAMMAS} gammas → {N_BETAS} betas")
    print(f"All routing via role-based discovery (writer/worker)")
    print("=" * 72)
    print()

    # Spawn worker replicas. Each replica handles up to N_REQUESTS so any one
    # could in theory serve all of them; the bus should distribute fairly.
    replicas: list[subprocess.Popen] = []
    for i in range(1, N_BETAS + 1):
        replicas.append(subprocess.Popen(
            ["python3", os.path.join(HERE, "agent_beta.py"),
             "--db", DB_PATH,
             "--my-id", f"agent_beta_{i}",
             "--max-requests", str(N_REQUESTS),
             "--idle-timeout-sec", "20"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        ))
    for i in range(1, N_GAMMAS + 1):
        replicas.append(subprocess.Popen(
            ["python3", os.path.join(HERE, "agent_gamma.py"),
             "--db", DB_PATH,
             "--my-id", f"agent_gamma_{i}",
             "--max-requests", str(N_REQUESTS),
             "--idle-timeout-sec", "20"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        ))

    # Give workers a moment to register.
    time.sleep(1.5)

    # Fan out alphas in parallel.
    alphas: list[subprocess.Popen] = []
    for i in range(1, N_REQUESTS + 1):
        alphas.append(subprocess.Popen(
            ["python3", os.path.join(HERE, "agent_alpha.py"),
             "--db", DB_PATH,
             "--my-id", f"agent_alpha_{i}",
             "--target-role", "writer",
             "--task", "write_report",
             "--intent", f"write report variant #{i} summarizing the project total",
             "--wait-sec", "45"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        ))

    # Wait for all alphas.
    alpha_outputs: list[tuple[int, str]] = []
    for p in alphas:
        try:
            out, _ = p.communicate(timeout=90)
        except subprocess.TimeoutExpired:
            p.kill(); out, _ = p.communicate()
        alpha_outputs.append((p.returncode, out))

    # Drain background replicas.
    replica_outs: list[str] = []
    for p in replicas:
        try:
            out, _ = p.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            p.kill(); out, _ = p.communicate()
        replica_outs.append(out)

    # Inspect bus state for verification.
    conn = sqlite3.connect(DB_PATH)
    msgs = conn.execute(
        "SELECT from_agent, to_agent, msg_type, conversation_id, payload "
        "FROM messages ORDER BY created_at ASC"
    ).fetchall()
    conn.close()

    # Compute per-replica handling counts.
    beta_handled: dict[str, int] = {}
    gamma_handled: dict[str, int] = {}
    completed_chains = 0
    failed_chains = 0
    for from_a, to_a, mtype, cid, payload_str in msgs:
        if mtype != "response":
            continue
        if from_a.startswith("agent_beta_"):
            beta_handled[from_a] = beta_handled.get(from_a, 0) + 1
        if from_a.startswith("agent_gamma_"):
            gamma_handled[from_a] = gamma_handled.get(from_a, 0) + 1
            payload = json.loads(payload_str)
            cs = payload.get("chain_summary") or {}
            if cs.get("chain_status") == "verified":
                completed_chains += 1
            else:
                failed_chains += 1

    print("--- Background replicas ---")
    for out in replica_outs:
        for line in out.rstrip().splitlines()[-3:]:
            print(line)
    print()

    print("--- Alpha results ---")
    n_verified = 0
    for i, (rc, out) in enumerate(alpha_outputs, start=1):
        last = next(
            (line for line in reversed(out.splitlines()) if "chain is" in line),
            f"(no result for alpha_{i})",
        )
        print(f"  alpha_{i} · rc={rc} · {last.strip()}")
        if "VERIFIED" in last:
            n_verified += 1
    print()

    print("--- Worker fanout ---")
    print(f"  Betas:   {dict(beta_handled)}")
    print(f"  Gammas:  {dict(gamma_handled)}")
    print()

    print("--- Assertions ---")
    checks = [
        (f"all {N_REQUESTS} alphas got verified responses",
            n_verified == N_REQUESTS, f"got {n_verified}"),
        (f"betas handled exactly {N_REQUESTS} requests in total",
            sum(beta_handled.values()) == N_REQUESTS,
            f"got {sum(beta_handled.values())}"),
        (f"gammas handled exactly {N_REQUESTS} requests in total",
            sum(gamma_handled.values()) == N_REQUESTS,
            f"got {sum(gamma_handled.values())}"),
        (f"work distributed across {N_BETAS} beta replicas (no single-pinned)",
            len([v for v in beta_handled.values() if v > 0]) >= min(N_BETAS, N_REQUESTS),
            f"replicas active: {list(beta_handled.keys())}"),
        (f"work distributed across {N_GAMMAS} gamma replicas",
            len([v for v in gamma_handled.values() if v > 0]) >= min(N_GAMMAS, N_REQUESTS),
            f"replicas active: {list(gamma_handled.keys())}"),
        ("no chain failures",
            failed_chains == 0, f"got {failed_chains}"),
    ]
    all_passed = True
    for label, ok, detail in checks:
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {label}  ({detail})")
        if not ok:
            all_passed = False

    print()
    print("=" * 72)
    print(f"Result: {'ALL CHECKS PASSED' if all_passed else 'CHECKS FAILED'}")
    print("=" * 72)
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())

```

#### `demos/comms-with-verifier/run_learning_demo.py` (166 lines)

Learning demo. Runs the three-agent flow twice against a shared memory DB. Shows memories accumulate and dedup-reinforce across runs; renders the claim_guidelines the LLM verifier would receive on the next run.

```python
#!/usr/bin/env python3
"""Learning demo — run the three-agent flow twice, observe memories accumulate.

The MemoryStore lives in its own SQLite file outside the bus, so memories
survive across runs even when the bus DB is wiped. Each Beta/Gamma:
  - On startup, prints the current memory count
  - Before invoking the verifier, recalls per-skill memories and appends them
    as "MEMORY HINTS" to skill_doc
  - After verification, stores any gap reports as per-skill memories
  - Records its own success/failure as per-agent reputation

What you should see:
  Run 1: memory starts empty; gap memories stored at the end
  Run 2: memory starts non-empty; verifier sees the hints in its system prompt
         (mock verifier ignores them, LLM verifier uses them)

Run from this directory:
    python3 run_learning_demo.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bus import Bus
from agent_memory import MemoryStore


HERE = os.path.dirname(os.path.abspath(__file__))
BUS_DB = os.path.join(HERE, "_learning_bus.sqlite")
MEMORY_DB = os.path.join(HERE, "_learning_memory.sqlite")


def cleanup(path: str) -> None:
    for suffix in ("", "-wal", "-shm"):
        p = path + suffix
        if os.path.exists(p):
            os.remove(p)


def memory_snapshot(label: str) -> None:
    """Pretty-print the current state of the memory DB."""
    store = MemoryStore(MEMORY_DB)
    total = store.count()
    print(f"  {label}: {total} memories total")
    for scope in store.all_scopes():
        scope_count = store.count(scope)
        print(f"    {scope}: {scope_count}")
        if scope_count > 0 and scope_count <= 5:
            for mem in store.recall(scope=scope, limit=5):
                content_preview = str(mem["content"])[:80]
                if len(str(mem["content"])) > 80:
                    content_preview += "..."
                print(f"      · [{mem['category']}] subject={mem['subject']!r} "
                      f"importance={mem['importance']}")
                print(f"        {content_preview}")


def run_one_pass(run_number: int) -> int:
    """Run one full three-agent flow. Bus DB is wiped; memory DB persists."""
    cleanup(BUS_DB)  # fresh bus state each pass

    env = os.environ.copy()
    env["AGENT_MEMORY_DB"] = MEMORY_DB   # all agents will use this memory file

    print(f"\n{'=' * 70}")
    print(f"RUN {run_number}")
    print('=' * 70)
    memory_snapshot(f"  Memory BEFORE run {run_number}")
    print()

    beta = subprocess.Popen(
        ["python3", os.path.join(HERE, "agent_beta.py"),
         "--db", BUS_DB, "--max-requests", "1", "--idle-timeout-sec", "30"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    gamma = subprocess.Popen(
        ["python3", os.path.join(HERE, "agent_gamma.py"),
         "--db", BUS_DB, "--max-requests", "1", "--idle-timeout-sec", "45"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    time.sleep(1.5)

    alpha = subprocess.run(
        ["python3", os.path.join(HERE, "agent_alpha.py"),
         "--db", BUS_DB,
         "--my-id", f"agent_alpha_run{run_number}",
         "--target-role", "writer",
         "--task", "write_report",
         "--intent", f"write a report paragraph (run {run_number})",
         "--wait-sec", "60"],
        env=env, capture_output=True, text=True, timeout=90,
    )

    for p in (beta, gamma):
        try:
            out, _ = p.communicate(timeout=45)
        except subprocess.TimeoutExpired:
            p.kill(); out, _ = p.communicate()
        # Print just the meaningful lines (skip the noise)
        for line in out.rstrip().splitlines():
            if any(tok in line for tok in (
                "registered", "memory:", "received", "chain_status",
                "stored", "→ ", "← ", "TIMEOUT", "done", "error",
            )):
                print(f"    {line}")

    # Alpha's verdict line
    verdict = next(
        (l for l in reversed(alpha.stdout.splitlines()) if "chain is" in l),
        "(no result)",
    )
    print(f"    alpha · rc={alpha.returncode} · {verdict.strip()}")
    print()
    memory_snapshot(f"  Memory AFTER run {run_number}")

    return alpha.returncode


def main() -> int:
    print("=" * 70)
    print("LEARNING DEMO")
    print("Same task, two runs, shared memory DB — watch memories accumulate")
    print("=" * 70)

    # Wipe memory DB so the demo starts fresh.
    cleanup(MEMORY_DB)
    # Initialize empty store so .count() works.
    MemoryStore(MEMORY_DB)

    rc1 = run_one_pass(1)
    rc2 = run_one_pass(2)

    print(f"\n{'=' * 70}")
    print("DEMO COMPLETE")
    print('=' * 70)
    print(f"Run 1 exit: {rc1}")
    print(f"Run 2 exit: {rc2}")

    store = MemoryStore(MEMORY_DB)
    print()
    print("Final memory state by scope:")
    for scope in store.all_scopes():
        print(f"  {scope}: {store.count(scope)} memories")

    # Show what claim_guidelines look like — what the LLM verifier would see
    # on the next run.
    print()
    print("Rendered claim_guidelines for skill 'demo-compute-total' "
          "(what the LLM verifier would receive on the next run):")
    print()
    hints = store.claim_guidelines_for_skill("demo-compute-total")
    if hints:
        for line in hints.splitlines():
            print(f"    {line}")
    else:
        print("    (none)")

    return 0 if (rc1 == 0 and rc2 == 0) else 1


if __name__ == "__main__":
    sys.exit(main())

```

#### `demos/comms-with-verifier/run_reputation_demo.py` (227 lines)

Reputation-weighted dispatch demo. Two-phase: warmup (direct addressing for deterministic samples) then reputation-aware pick. 3 assertions verify the flaky replica gets filtered at reputation_min=0.6.

```python
#!/usr/bin/env python3
"""Reputation-weighted dispatch demo.

Two Beta replicas:
  - agent_beta_good (healthy)
  - agent_beta_flaky (--simulate-failure-rate 0.7)

Phase 1 — WARM-UP: 6 Alpha requests via topic-fanout (publish_to_role). Both
replicas get traffic; reputation memories accumulate. The flaky one's
success_rate drops well below 1.0; the healthy one stays near 1.0.

Phase 2 — REPUTATION-AWARE: 4 more Alpha requests, this time with
--reputation-min 0.6. Alpha now does a reputation-aware pick rather than a
topic-fanout — the bad replica is skipped; all traffic goes to the healthy one.

Verifies:
  - Reputation memories accumulate per-agent
  - find_agents_by_role with reputation_min filters correctly
  - pick_agent_by_role consistently chooses the healthy replica
  - The flaky replica handles zero requests in phase 2

Run from this directory:
    python3 run_reputation_demo.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bus import Bus
from agent_memory import MemoryStore


HERE = os.path.dirname(os.path.abspath(__file__))
BUS_DB = os.path.join(HERE, "_reputation_bus.sqlite")
MEMORY_DB = os.path.join(HERE, "_reputation_memory.sqlite")

WARMUP_REQUESTS = 6
REPUTATION_REQUESTS = 4
GOOD_FAILURE_RATE = 0.0
FLAKY_FAILURE_RATE = 0.7
REPUTATION_MIN = 0.6


def cleanup(path: str) -> None:
    for suffix in ("", "-wal", "-shm"):
        p = path + suffix
        if os.path.exists(p):
            os.remove(p)


def spawn_betas(env: dict, total_requests: int) -> list[subprocess.Popen]:
    """Start two Beta replicas, one healthy and one flaky."""
    procs = []
    procs.append(subprocess.Popen(
        ["python3", os.path.join(HERE, "agent_beta.py"),
         "--db", BUS_DB,
         "--my-id", "agent_beta_good",
         "--max-requests", str(total_requests),
         "--idle-timeout-sec", "30",
         "--simulate-failure-rate", str(GOOD_FAILURE_RATE)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    ))
    procs.append(subprocess.Popen(
        ["python3", os.path.join(HERE, "agent_beta.py"),
         "--db", BUS_DB,
         "--my-id", "agent_beta_flaky",
         "--max-requests", str(total_requests),
         "--idle-timeout-sec", "30",
         "--simulate-failure-rate", str(FLAKY_FAILURE_RATE)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    ))
    return procs


def fire_alpha(env: dict, alpha_id: str, *, use_reputation: bool) -> subprocess.CompletedProcess:
    cmd = [
        "python3", os.path.join(HERE, "agent_alpha.py"),
        "--db", BUS_DB,
        "--my-id", alpha_id,
        "--target-role", "worker",
        "--task", "compute_total",
        "--intent", f"compute the total for {alpha_id}",
        "--wait-sec", "30",
    ]
    if use_reputation:
        cmd.extend(["--memory-db", MEMORY_DB,
                    "--reputation-min", str(REPUTATION_MIN)])
    return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)


def print_reputation(store: MemoryStore) -> None:
    print(f"  Reputation summary:")
    for scope in store.all_scopes():
        if not scope.startswith("agent."):
            continue
        agent_id = scope[len("agent."):]
        rep = store.reputation_summary(agent_id)
        rate_str = f"{rep['success_rate']:.2f}" if rep['success_rate'] is not None else "n/a"
        print(f"    {agent_id}: {rep['successes']}/{rep['total']} = {rate_str} success rate")


def main() -> int:
    cleanup(BUS_DB)
    cleanup(MEMORY_DB)

    env = os.environ.copy()
    env["AGENT_MEMORY_DB"] = MEMORY_DB

    print("=" * 72)
    print("REPUTATION-WEIGHTED DISPATCH DEMO")
    print(f"Warm-up: {WARMUP_REQUESTS} requests via topic-fanout")
    print(f"Then: {REPUTATION_REQUESTS} requests with --reputation-min {REPUTATION_MIN}")
    print(f"Flaky beta has simulate_failure_rate={FLAKY_FAILURE_RATE}")
    print("=" * 72)

    # === Phase 1: Warm-up ===
    # Use DIRECT addressing during warmup so each replica gets exactly N
    # reputation samples — guarantees deterministic reputation differentiation.
    print(f"\n--- PHASE 1: WARM-UP ({WARMUP_REQUESTS} requests, direct-addressed equally) ---")
    total = WARMUP_REQUESTS + REPUTATION_REQUESTS
    betas = spawn_betas(env, total)
    time.sleep(1.5)

    targets = ["agent_beta_good", "agent_beta_flaky"]
    for i in range(1, WARMUP_REQUESTS + 1):
        target = targets[(i - 1) % 2]
        # Direct-address: --target-id wins over --target-role in agent_alpha.
        cmd = [
            "python3", os.path.join(HERE, "agent_alpha.py"),
            "--db", BUS_DB,
            "--my-id", f"agent_alpha_warm_{i}",
            "--target-id", target,
            "--task", "compute_total",
            "--intent", f"compute the total for warm_{i}",
            "--wait-sec", "30",
        ]
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
        verdict = next((l for l in reversed(result.stdout.splitlines()) if "chain is" in l),
                       "(no verdict)")
        print(f"  warm {i} → {target}: rc={result.returncode} · {verdict.strip()}")

    print()
    store = MemoryStore(MEMORY_DB)
    print_reputation(store)

    # Show what find_agents_by_role returns with vs without reputation filter
    bus = Bus(BUS_DB)
    all_workers = bus.find_agents_by_role("worker", alive_only=True)
    filtered_workers = bus.find_agents_by_role(
        "worker", alive_only=True,
        memory_store=store, reputation_min=REPUTATION_MIN,
    )
    print(f"\n  Live workers (no filter): {[a['id'] for a in all_workers]}")
    print(f"  Reputation-filtered (>={REPUTATION_MIN}): {[a['id'] for a in filtered_workers]}")

    # === Phase 2: Reputation-aware ===
    print(f"\n--- PHASE 2: REPUTATION-AWARE ({REPUTATION_REQUESTS} requests, --reputation-min {REPUTATION_MIN}) ---")
    for i in range(1, REPUTATION_REQUESTS + 1):
        result = fire_alpha(env, f"agent_alpha_rep_{i}", use_reputation=True)
        verdict = next((l for l in reversed(result.stdout.splitlines()) if "chain is" in l),
                       "(no verdict)")
        picked = next((l for l in result.stdout.splitlines() if "reputation-aware pick" in l),
                      "")
        print(f"  rep {i}: rc={result.returncode} · {picked.strip()}")
        print(f"          · {verdict.strip()}")

    # Drain betas.
    for p in betas:
        try:
            p.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            p.kill(); p.communicate()

    # === Final assertions ===
    print(f"\n--- FINAL STATE ---")
    store = MemoryStore(MEMORY_DB)
    print_reputation(store)

    # Count how many phase-2 messages each replica answered.
    import sqlite3
    conn = sqlite3.connect(BUS_DB)
    rows = conn.execute(
        "SELECT from_agent, COUNT(*) FROM messages "
        "WHERE msg_type='response' AND from_agent LIKE 'agent_beta_%' "
        "GROUP BY from_agent"
    ).fetchall()
    conn.close()
    beta_responses = {r[0]: r[1] for r in rows}
    print(f"\n  Total responses by replica (across all phases): {beta_responses}")

    rep_good = store.reputation_summary("agent_beta_good")
    rep_flaky = store.reputation_summary("agent_beta_flaky")

    print(f"\n--- ASSERTIONS ---")
    checks = [
        ("flaky reputation < good reputation",
         (rep_flaky["success_rate"] or 0) < (rep_good["success_rate"] or 0),
         f"flaky={rep_flaky['success_rate']:.2f}, good={rep_good['success_rate']:.2f}"
            if (rep_flaky['success_rate'] is not None and rep_good['success_rate'] is not None)
            else f"flaky={rep_flaky}, good={rep_good}"),
        ("flaky filtered out at reputation_min=0.6",
         not any(a["id"] == "agent_beta_flaky" for a in filtered_workers),
         f"filtered list = {[a['id'] for a in filtered_workers]}"),
        ("good replica is in the filtered list",
         any(a["id"] == "agent_beta_good" for a in filtered_workers),
         f"filtered list = {[a['id'] for a in filtered_workers]}"),
    ]
    all_passed = True
    for label, ok, detail in checks:
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {label}  ({detail})")
        if not ok:
            all_passed = False

    print()
    print("=" * 72)
    print(f"Result: {'ALL CHECKS PASSED' if all_passed else 'CHECKS FAILED'}")
    print("=" * 72)
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())

```

#### `demos/comms-with-verifier/build_dashboard.py` (786 lines)

Static HTML observability dashboard generator. Reads SQLite bus DB; renders live agents (heartbeat status), parent+child conversations grouped as chains with end-to-end stats, per-message verification banners color-coded by verdict, merged gap reports.

```python
#!/usr/bin/env python3
"""Static HTML observability dashboard for the SQLite bus.

Reads the bus database and emits a single self-contained HTML page showing:
  - Live agents with heartbeat status
  - Conversations grouped by id, with full message timeline
  - Per-message verification status (color-coded)
  - Gap reports highlighted at the bottom

Usage:
    python3 build_dashboard.py [--db PATH] [--out PATH]

Defaults: reads _demo_bus.sqlite in this directory, writes dashboard.html
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone


HERE = os.path.dirname(os.path.abspath(__file__))


def fetch_agents(conn) -> list[dict]:
    now = time.time()
    rows = conn.execute(
        "SELECT id, name, role, subscriptions, last_heartbeat FROM agents"
    ).fetchall()
    agents = []
    for r in rows:
        age = now - r[4]
        agents.append({
            "id": r[0], "name": r[1], "role": r[2],
            "subscriptions": json.loads(r[3]),
            "last_heartbeat": r[4],
            "age_sec": age,
            "is_alive": age < 90,
        })
    agents.sort(key=lambda a: a["name"])
    return agents


def fetch_messages(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT id, conversation_id, from_agent, to_agent, topic, msg_type, "
        "reply_to, hop_count, ttl_seconds, created_at, payload FROM messages "
        "ORDER BY created_at ASC"
    ).fetchall()
    return [{
        "id": r[0], "conversation_id": r[1], "from_agent": r[2],
        "to_agent": r[3], "topic": r[4], "msg_type": r[5],
        "reply_to": r[6], "hop_count": r[7], "ttl_seconds": r[8],
        "created_at": r[9], "payload": json.loads(r[10]),
    } for r in rows]


def group_by_conversation(messages: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for m in messages:
        groups.setdefault(m["conversation_id"], []).append(m)
    return groups


def build_chains(messages: list[dict]) -> list[dict]:
    """Group conversations into parent/child chains using parent_conversation_id.

    Returns a list of chain dicts:
        {
            "root_conv_id": str,
            "conv_ids": [root, child, grandchild, ...],  # depth-first order
            "messages_by_conv": {cid: [msgs, ...]},
            "depth": int,
            "first_message_at": float,
            "last_message_at": float,
            "chain_summary": dict | None,    # from final response's chain_summary
        }
    Top-level conversations (parent_conversation_id is NULL) are roots.
    """
    by_conv = group_by_conversation(messages)

    # parent_id → list of child conv_ids
    children: dict[str, list[str]] = {}
    parents: dict[str, str | None] = {}
    for cid, msgs in by_conv.items():
        parent = None
        for m in msgs:
            if m.get("parent_conversation_id"):
                parent = m["parent_conversation_id"]
                break
        parents[cid] = parent
        if parent:
            children.setdefault(parent, []).append(cid)

    roots = [cid for cid, p in parents.items() if not p]

    chains = []
    for root in sorted(roots, key=lambda c: min(m["created_at"] for m in by_conv[c])):
        ordered_cids: list[str] = []
        def _dfs(cid: str, depth: int) -> int:
            ordered_cids.append(cid)
            max_depth = depth
            for child in sorted(children.get(cid, []),
                                key=lambda c: min(m["created_at"] for m in by_conv[c])):
                max_depth = max(max_depth, _dfs(child, depth + 1))
            return max_depth
        max_depth = _dfs(root, 0)

        all_msgs = [m for cid in ordered_cids for m in by_conv[cid]]
        # chain_summary: pull from the latest response in the root conversation
        chain_summary = None
        for m in reversed(by_conv[root]):
            if m["msg_type"] == "response" and isinstance(m["payload"], dict):
                if m["payload"].get("chain_summary"):
                    chain_summary = m["payload"]["chain_summary"]
                    break

        chains.append({
            "root_conv_id": root,
            "conv_ids": ordered_cids,
            "messages_by_conv": {cid: by_conv[cid] for cid in ordered_cids},
            "depth": max_depth,
            "first_message_at": min(m["created_at"] for m in all_msgs),
            "last_message_at": max(m["created_at"] for m in all_msgs),
            "chain_summary": chain_summary,
        })
    return chains


def extract_verification(payload: dict) -> dict | None:
    """If the payload includes a verification record, surface it."""
    if isinstance(payload, dict) and "verification" in payload:
        return payload["verification"]
    return None


def fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S.%f")[:-3] + "Z"


def fmt_age(sec: float) -> str:
    if sec < 1:
        return f"{int(sec * 1000)}ms"
    if sec < 60:
        return f"{sec:.1f}s"
    return f"{int(sec // 60)}m {int(sec % 60)}s"


# ---------- HTML emission ----------

CSS = """
:root {
  --bg: #FAFAF7; --bg-card: #FFFFFF; --bg-subtle: #F2EFE8;
  --ink: #16140F; --ink-soft: #4A463E; --ink-mute: #8A8578;
  --rule: #E8E3D6; --accent: #B8482E; --accent-soft: #F4E4DA;
  --green: #2D5A1F; --green-bg: #E8F0E5;
  --amber: #8B6914; --amber-bg: #F4ECDA;
  --red: #8B2914; --red-bg: #F4DAD4;
  --blue: #2B4E7A; --blue-bg: #E0E8F0;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: var(--bg); color: var(--ink);
  font-family: 'Inter', system-ui, sans-serif; font-size: 15px;
  line-height: 1.6; -webkit-font-smoothing: antialiased;
}
.container { max-width: 1080px; margin: 0 auto; padding: 56px 32px; }

/* Header */
.eyebrow {
  font-family: 'JetBrains Mono', monospace; font-size: 11px;
  letter-spacing: 0.14em; text-transform: uppercase; color: var(--accent);
  margin-bottom: 20px;
}
h1 {
  font-family: 'Fraunces', Georgia, serif; font-weight: 400;
  font-size: 52px; line-height: 1.05; letter-spacing: -0.02em;
  margin-bottom: 16px;
}
h1 em { font-style: italic; color: var(--accent); font-weight: 300; }
.subtitle {
  font-family: 'Fraunces', serif; font-size: 18px; color: var(--ink-soft);
  font-style: italic; margin-bottom: 32px;
}
.meta-row {
  display: flex; gap: 32px; flex-wrap: wrap;
  font-family: 'JetBrains Mono', monospace; font-size: 11px;
  color: var(--ink-mute); letter-spacing: 0.04em; padding: 16px 0;
  border-top: 1px solid var(--rule); border-bottom: 1px solid var(--rule);
}
.meta-row strong { color: var(--ink-soft); font-weight: 500; }

/* Section heads */
section { margin-top: 56px; }
h2 {
  font-family: 'Fraunces', serif; font-weight: 500; font-size: 28px;
  letter-spacing: -0.015em; margin-bottom: 8px;
}
h2 .num {
  display: block; font-family: 'JetBrains Mono', monospace; font-size: 12px;
  color: var(--accent); letter-spacing: 0.1em; margin-bottom: 8px;
}
h2 .count {
  font-family: 'JetBrains Mono', monospace; font-size: 14px;
  color: var(--ink-mute); margin-left: 8px;
}
.sect-desc {
  font-size: 14px; color: var(--ink-mute); margin-bottom: 24px;
  font-style: italic;
}

/* Stats strip */
.stats {
  display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px;
  margin: 24px 0;
}
.stat {
  background: var(--bg-card); border: 1px solid var(--rule);
  border-radius: 4px; padding: 18px 20px;
}
.stat .label {
  font-family: 'JetBrains Mono', monospace; font-size: 10px;
  text-transform: uppercase; letter-spacing: 0.08em; color: var(--ink-mute);
  margin-bottom: 8px;
}
.stat .value {
  font-family: 'Fraunces', serif; font-size: 32px; color: var(--ink);
  font-weight: 500; line-height: 1;
}
.stat .sub {
  font-size: 11px; color: var(--ink-mute); margin-top: 4px;
  font-family: 'JetBrains Mono', monospace;
}

/* Agents */
.agents { display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px; }
.agent {
  background: var(--bg-card); border: 1px solid var(--rule);
  border-radius: 4px; padding: 20px;
  display: grid; grid-template-columns: 1fr auto; gap: 12px;
  align-items: start;
}
.agent .id-line {
  font-family: 'JetBrains Mono', monospace; font-size: 11px;
  color: var(--ink-mute); margin-bottom: 4px; letter-spacing: 0.04em;
}
.agent h3 {
  font-family: 'Fraunces', serif; font-size: 20px; font-weight: 500;
  color: var(--ink); margin: 0 0 4px;
}
.agent .role {
  font-family: 'JetBrains Mono', monospace; font-size: 11px;
  color: var(--accent); letter-spacing: 0.06em; text-transform: uppercase;
  margin-bottom: 10px;
}
.agent .subs {
  font-family: 'JetBrains Mono', monospace; font-size: 11px;
  color: var(--ink-soft);
}
.status-pill {
  font-family: 'JetBrains Mono', monospace; font-size: 10px;
  text-transform: uppercase; letter-spacing: 0.1em;
  padding: 4px 10px; border-radius: 11px; white-space: nowrap;
}
.status-pill.alive { background: var(--green-bg); color: var(--green); }
.status-pill.stale { background: var(--amber-bg); color: var(--amber); }
.heartbeat-age {
  font-family: 'JetBrains Mono', monospace; font-size: 10px;
  color: var(--ink-mute); margin-top: 6px;
}

/* Chains (one chain = root + nested children) */
.chain {
  background: var(--bg-card); border: 1px solid var(--rule);
  border-radius: 4px; padding: 24px 28px; margin-bottom: 24px;
}
.chain .chain-head {
  display: flex; justify-content: space-between; align-items: start;
  padding-bottom: 16px; border-bottom: 1px solid var(--rule);
  margin-bottom: 16px; gap: 24px;
}
.chain .chain-id {
  font-family: 'JetBrains Mono', monospace; font-size: 12px;
  color: var(--ink-soft); letter-spacing: 0.04em;
}
.chain .chain-title {
  font-family: 'Fraunces', serif; font-size: 18px;
  color: var(--ink); font-weight: 500; margin-top: 4px;
}
.chain-stats {
  display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px;
  margin-bottom: 20px;
}
.chain-stat {
  background: var(--bg-subtle); padding: 12px 14px; border-radius: 3px;
}
.chain-stat .l {
  font-family: 'JetBrains Mono', monospace; font-size: 9px;
  text-transform: uppercase; letter-spacing: 0.08em;
  color: var(--ink-mute); margin-bottom: 4px;
}
.chain-stat .v {
  font-family: 'Fraunces', serif; font-size: 20px; color: var(--ink);
  font-weight: 500;
}
.chain-status-pill {
  font-family: 'JetBrains Mono', monospace; font-size: 11px;
  text-transform: uppercase; letter-spacing: 0.1em;
  padding: 6px 14px; border-radius: 14px;
}
.chain-status-pill.verified { background: var(--green-bg); color: var(--green); }
.chain-status-pill.partial { background: var(--amber-bg); color: var(--amber); }
.chain-status-pill.failed { background: var(--red-bg); color: var(--red); }

.conversation {
  margin-top: 14px; padding-top: 14px;
  border-top: 1px dashed var(--rule);
}
.conversation:first-of-type { border-top: none; padding-top: 0; margin-top: 0; }
.conversation.nested {
  margin-left: 24px; padding-left: 16px;
  border-left: 2px solid var(--accent-soft);
}
.conversation .head {
  display: flex; justify-content: space-between; align-items: center;
  padding-bottom: 10px; margin-bottom: 12px;
}
.conversation .cid {
  font-family: 'JetBrains Mono', monospace; font-size: 11px;
  color: var(--ink-soft); letter-spacing: 0.04em;
}
.conversation .cid .nested-tag {
  background: var(--accent-soft); color: var(--accent);
  padding: 2px 8px; border-radius: 2px; margin-left: 8px;
  font-size: 9px; text-transform: uppercase; letter-spacing: 0.08em;
}
.conversation .meta {
  font-family: 'JetBrains Mono', monospace; font-size: 10px;
  color: var(--ink-mute);
}

.message {
  display: grid; grid-template-columns: 110px 1fr;
  gap: 16px; padding: 16px 0; border-bottom: 1px dashed var(--rule);
}
.message:last-child { border-bottom: none; }
.message .meta {
  font-family: 'JetBrains Mono', monospace; font-size: 10px;
  color: var(--ink-mute); line-height: 1.5;
}
.message .meta strong { color: var(--ink-soft); }
.message .body {
  font-size: 14px;
}
.message .route {
  font-family: 'JetBrains Mono', monospace; font-size: 12px;
  color: var(--ink); margin-bottom: 6px;
}
.message .arrow { color: var(--accent); margin: 0 6px; }
.message .type-pill {
  font-family: 'JetBrains Mono', monospace; font-size: 9px;
  text-transform: uppercase; letter-spacing: 0.08em;
  padding: 2px 8px; border-radius: 2px;
  background: var(--bg-subtle); color: var(--ink-soft);
  margin-left: 8px;
}
.message .type-pill.request { background: var(--blue-bg); color: var(--blue); }
.message .type-pill.response { background: var(--green-bg); color: var(--green); }
.message .type-pill.event { background: var(--amber-bg); color: var(--amber); }

.payload-preview {
  font-family: 'JetBrains Mono', monospace; font-size: 11px;
  color: var(--ink-soft); margin-top: 4px;
  background: var(--bg-subtle); padding: 8px 12px; border-radius: 3px;
  white-space: pre-wrap; word-break: break-word;
}

.verification-banner {
  display: inline-flex; align-items: center; gap: 8px;
  font-family: 'JetBrains Mono', monospace; font-size: 11px;
  padding: 6px 12px; border-radius: 3px; margin-top: 8px;
  letter-spacing: 0.04em;
}
.verification-banner.verified { background: var(--green-bg); color: var(--green); }
.verification-banner.partial { background: var(--amber-bg); color: var(--amber); }
.verification-banner.failed { background: var(--red-bg); color: var(--red); }
.verification-banner strong { font-weight: 600; }
.verification-banner .sep { color: var(--ink-mute); margin: 0 4px; }

.claims-mini {
  display: flex; gap: 6px; margin-top: 8px; flex-wrap: wrap;
}
.claim-chip {
  font-family: 'JetBrains Mono', monospace; font-size: 10px;
  padding: 3px 8px; border-radius: 10px; letter-spacing: 0.03em;
}
.claim-chip.pass { background: var(--green-bg); color: var(--green); }
.claim-chip.fail { background: var(--red-bg); color: var(--red); }
.claim-chip.unverifiable { background: var(--amber-bg); color: var(--amber); }

/* Gap reports */
.gap-report {
  background: var(--bg-card); border-left: 3px solid var(--accent);
  border-radius: 0 4px 4px 0;
  padding: 20px 24px; margin-bottom: 16px;
}
.gap-report .src {
  font-family: 'JetBrains Mono', monospace; font-size: 10px;
  color: var(--ink-mute); margin-bottom: 6px; letter-spacing: 0.05em;
}
.gap-report h3 {
  font-family: 'Fraunces', serif; font-size: 18px; font-weight: 500;
  margin-bottom: 8px;
}
.gap-report .summary {
  font-size: 14px; color: var(--ink-soft); margin-bottom: 16px;
  font-style: italic;
}
.improvement {
  border-top: 1px solid var(--rule); padding-top: 12px; margin-top: 12px;
}
.improvement .claim-statement {
  font-family: 'JetBrains Mono', monospace; font-size: 11px;
  color: var(--ink-soft); margin-bottom: 6px;
}
.improvement .proposed {
  font-size: 13px; line-height: 1.55; color: var(--ink);
  padding: 10px 14px; background: var(--accent-soft); border-radius: 3px;
}
.improvement .proposed strong {
  font-family: 'JetBrains Mono', monospace; font-size: 10px;
  text-transform: uppercase; letter-spacing: 0.08em; color: var(--accent);
  display: block; margin-bottom: 4px;
}
.improvement .conf {
  font-family: 'JetBrains Mono', monospace; font-size: 10px;
  color: var(--ink-mute); margin-top: 6px;
}

/* Empty */
.empty {
  background: var(--bg-card); border: 1px dashed var(--rule);
  border-radius: 4px; padding: 32px; text-align: center;
  color: var(--ink-mute); font-style: italic; font-size: 14px;
}

footer {
  margin-top: 96px; padding: 32px 0; border-top: 1px solid var(--rule);
  font-family: 'JetBrains Mono', monospace; font-size: 11px;
  color: var(--ink-mute); text-align: center; letter-spacing: 0.04em;
}

@media (max-width: 720px) {
  .agents, .stats { grid-template-columns: 1fr; }
  h1 { font-size: 36px; }
  .message { grid-template-columns: 1fr; }
}
"""


def render(*, db_path: str, agents: list[dict], messages: list[dict],
           snapshot_at: float) -> str:
    convos = group_by_conversation(messages)
    snapshot_str = datetime.fromtimestamp(snapshot_at, tz=timezone.utc).strftime(
        "%Y-%m-%d · %H:%M:%S UTC"
    )

    # Collect gap reports — prefer chain_summary.merged_gap_report (already
    # deduped across the chain) over per-message verification.gap_report.
    gap_reports: list[tuple[dict, str]] = []  # (gap_report, source_message_id)
    seen_sources = set()
    for m in messages:
        if not isinstance(m["payload"], dict):
            continue
        cs = m["payload"].get("chain_summary")
        if cs and cs.get("merged_gap_report"):
            gap_reports.append((cs["merged_gap_report"], m["id"]))
            seen_sources.add(m["id"])
            continue
        # Fallback: per-message gap report (only for messages not already covered
        # by a chain-merged report).
        v = extract_verification(m["payload"])
        if v and v.get("gap_report") and m["id"] not in seen_sources:
            gap_reports.append((v["gap_report"], m["id"]))

    # Aggregate stats
    alive = sum(1 for a in agents if a["is_alive"])
    response_verdicts: dict[str, int] = {}
    for m in messages:
        v = extract_verification(m["payload"])
        if v:
            response_verdicts[v["status"]] = response_verdicts.get(v["status"], 0) + 1

    out = ["<!DOCTYPE html>", '<html lang="en">', "<head>",
           '<meta charset="UTF-8">',
           '<meta name="viewport" content="width=device-width, initial-scale=1.0">',
           "<title>Bus Observability Dashboard</title>",
           '<link rel="preconnect" href="https://fonts.googleapis.com">',
           '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>',
           '<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,300;9..144,400;9..144,500;9..144,600&family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">',
           "<style>", CSS, "</style>",
           "</head>", "<body>", '<div class="container">']

    # Header
    out += [
        '<div class="eyebrow">Bus snapshot · Reference No. 03 series</div>',
        '<h1>The conversation, <em>visible.</em></h1>',
        '<div class="subtitle">A static read of the multi-agent bus — every agent, every message, every verdict.</div>',
        '<div class="meta-row">',
        f'<span><strong>SNAPSHOT</strong> &nbsp;{snapshot_str}</span>',
        f'<span><strong>DATABASE</strong> &nbsp;{html.escape(os.path.basename(db_path))}</span>',
        f'<span><strong>AGENTS</strong> &nbsp;{len(agents)} ({alive} alive)</span>',
        f'<span><strong>CONVERSATIONS</strong> &nbsp;{len(convos)}</span>',
        f'<span><strong>MESSAGES</strong> &nbsp;{len(messages)}</span>',
        '</div>',
    ]

    # Stats strip
    verified_count = response_verdicts.get("verified", 0)
    partial_count = response_verdicts.get("partial", 0)
    failed_count = response_verdicts.get("failed", 0)
    out += [
        '<div class="stats">',
        f'<div class="stat"><div class="label">Verified replies</div>'
        f'<div class="value">{verified_count}</div>'
        f'<div class="sub">all claims pass</div></div>',
        f'<div class="stat"><div class="label">Partial replies</div>'
        f'<div class="value">{partial_count}</div>'
        f'<div class="sub">no fails, ≥1 unverifiable</div></div>',
        f'<div class="stat"><div class="label">Failed replies</div>'
        f'<div class="value">{failed_count}</div>'
        f'<div class="sub">at least 1 claim failed</div></div>',
        f'<div class="stat"><div class="label">Gap reports</div>'
        f'<div class="value">{len(gap_reports)}</div>'
        f'<div class="sub">doc improvement candidates</div></div>',
        '</div>',
    ]

    # Agents section
    out += [
        '<section>',
        f'<h2><span class="num">§ 01 — Registry</span>Live agents<span class="count">· {len(agents)}</span></h2>',
        '<div class="sect-desc">Heartbeat-based liveness. Agents that miss more than 90 seconds become stale.</div>',
    ]
    if agents:
        out.append('<div class="agents">')
        for a in agents:
            subs = ", ".join(a["subscriptions"]) if a["subscriptions"] else "—"
            status_cls = "alive" if a["is_alive"] else "stale"
            status_lbl = "ALIVE" if a["is_alive"] else "STALE"
            out += [
                '<div class="agent">',
                '<div>',
                f'<div class="id-line">{html.escape(a["id"])}</div>',
                f'<h3>{html.escape(a["name"])}</h3>',
                f'<div class="role">{html.escape(a["role"])}</div>',
                f'<div class="subs">subs: {html.escape(subs)}</div>',
                '</div>',
                f'<div>'
                f'<div class="status-pill {status_cls}">{status_lbl}</div>'
                f'<div class="heartbeat-age">last beat: {fmt_age(a["age_sec"])} ago</div>'
                f'</div>',
                '</div>',
            ]
        out.append('</div>')
    else:
        out.append('<div class="empty">No agents registered.</div>')
    out.append('</section>')

    # Chains section — group parent/child conversations together
    chains = build_chains(messages)
    out += [
        '<section>',
        f'<h2><span class="num">§ 02 — Chains</span>End-to-end conversations<span class="count">· {len(chains)}</span></h2>',
        '<div class="sect-desc">Each chain is a root conversation plus any nested sub-conversations a worker started to fulfill it. Stats are end-to-end (weakest-link trust, total cost, total duration).</div>',
    ]
    if chains:
        for chain in chains:
            cs = chain.get("chain_summary") or {}
            chain_status = cs.get("chain_status", "—")
            duration_sec = chain["last_message_at"] - chain["first_message_at"]
            n_msgs = sum(len(msgs) for msgs in chain["messages_by_conv"].values())
            out += [
                '<div class="chain">',
                '<div class="chain-head">',
                '<div>',
                f'<div class="chain-id">root: {html.escape(chain["root_conv_id"])}</div>',
                f'<div class="chain-title">'
                f'{len(chain["conv_ids"])} conversation(s) · {n_msgs} message(s) · depth {chain["depth"]}'
                f'</div>',
                '</div>',
                f'<div class="chain-status-pill {chain_status}">'
                f'chain: {chain_status}</div>',
                '</div>',
            ]
            if cs:
                out += [
                    '<div class="chain-stats">',
                    f'<div class="chain-stat"><div class="l">Hops</div>'
                    f'<div class="v">{cs.get("hop_count", "?")}</div></div>',
                    f'<div class="chain-stat"><div class="l">Total attempts</div>'
                    f'<div class="v">{cs.get("total_attempts", "?")}</div></div>',
                    f'<div class="chain-stat"><div class="l">Total cost</div>'
                    f'<div class="v">${cs.get("total_cost_usd", 0):.4f}</div></div>',
                    f'<div class="chain-stat"><div class="l">Wall duration</div>'
                    f'<div class="v">{duration_sec:.2f}s</div></div>',
                    '</div>',
                ]

            # Render each conversation in the chain.
            for idx, cid in enumerate(chain["conv_ids"]):
                msgs = chain["messages_by_conv"][cid]
                is_nested = idx > 0
                conv_dur = msgs[-1]["created_at"] - msgs[0]["created_at"]
                nested_tag = '<span class="nested-tag">SUB</span>' if is_nested else ''
                cls = "conversation nested" if is_nested else "conversation"
                out += [
                    f'<div class="{cls}">',
                    '<div class="head">',
                    f'<div class="cid">{html.escape(cid)}{nested_tag}</div>',
                    f'<div class="meta">{len(msgs)} msgs · {conv_dur:.2f}s</div>',
                    '</div>',
                ]
                for m in msgs:
                    target = m["to_agent"] if m["to_agent"] else f'#{m["topic"]}'
                    v = extract_verification(m["payload"])
                    payload_preview = _summarize_payload(m["payload"])
                    out += [
                        '<div class="message">',
                        f'<div class="meta">'
                        f'<div><strong>{fmt_time(m["created_at"])}</strong></div>'
                        f'<div>hop={m["hop_count"]} ttl={m["ttl_seconds"]}s</div>'
                        f'<div>{html.escape(m["id"])}</div>'
                        f'</div>',
                        '<div class="body">',
                        f'<div class="route">'
                        f'<span>{html.escape(m["from_agent"])}</span>'
                        f'<span class="arrow">→</span>'
                        f'<span>{html.escape(target)}</span>'
                        f'<span class="type-pill {m["msg_type"]}">{m["msg_type"]}</span>'
                        f'</div>',
                        f'<div class="payload-preview">{html.escape(payload_preview)}</div>',
                    ]
                    if v:
                        status = v["status"]
                        claims = v.get("claims", [])
                        passes = sum(1 for c in claims if c["verdict"] == "pass")
                        fails = sum(1 for c in claims if c["verdict"] == "fail")
                        unv = sum(1 for c in claims if c["verdict"] == "unverifiable")
                        cost = v.get("cost_usd", 0.0)
                        out.append(
                            f'<div class="verification-banner {status}">'
                            f'<strong>verification:</strong> {status} '
                            f'<span class="sep">·</span> '
                            f'{len(claims)} claims '
                            f'<span class="sep">·</span> '
                            f'{v.get("verifier_model","?")} '
                            f'<span class="sep">·</span> '
                            f'${cost:.4f}'
                            f'</div>'
                        )
                        chips = []
                        if passes:
                            chips.append(f'<span class="claim-chip pass">{passes} pass</span>')
                        if fails:
                            chips.append(f'<span class="claim-chip fail">{fails} fail</span>')
                        if unv:
                            chips.append(f'<span class="claim-chip unverifiable">{unv} unverifiable</span>')
                        if chips:
                            out.append('<div class="claims-mini">' + "".join(chips) + '</div>')
                    out += ['</div>', '</div>']
                out.append('</div>')
            out.append('</div>')
    else:
        out.append('<div class="empty">No conversations on the bus.</div>')
    out.append('</section>')

    # Gap reports section
    out += [
        '<section>',
        f'<h2><span class="num">§ 03 — Gap reports</span>Doc improvement candidates<span class="count">· {len(gap_reports)}</span></h2>',
        '<div class="sect-desc">When the verifier marks a claim unverifiable, it proposes a documentation change that would make the claim verifiable next time. These are the suggestions awaiting a draft-card review.</div>',
    ]
    if gap_reports:
        for gap, src_mid in gap_reports:
            # Merged reports use skill_ids (plural); per-message reports use skill_id.
            skill_label = (
                ", ".join(gap.get("skill_ids", []))
                if gap.get("skill_ids")
                else gap.get("skill_id", "?")
            )
            is_merged = bool(gap.get("skill_ids"))
            merged_tag = ' · MERGED ACROSS CHAIN' if is_merged else ''
            out += [
                '<div class="gap-report">',
                f'<div class="src">FROM MESSAGE {html.escape(src_mid)} · '
                f'SKILL(S) {html.escape(skill_label)}{merged_tag}</div>',
                f'<h3>{len(gap.get("unverifiable_claims", []))} unverifiable claim(s)</h3>',
                f'<div class="summary">{html.escape(gap.get("summary", ""))}</div>',
            ]
            for imp in gap.get("proposed_improvements", []):
                # find the claim text
                claim_text = ""
                for c in gap.get("unverifiable_claims", []):
                    if c["id"] == imp["claim_id"]:
                        claim_text = c["statement"]
                        break
                origin = imp.get("_skill_id")
                origin_tag = f" · origin: {origin}" if origin else ""
                out += [
                    '<div class="improvement">',
                    f'<div class="claim-statement">claim: {html.escape(claim_text)}{html.escape(origin_tag)}</div>',
                    '<div class="proposed">',
                    f'<strong>PROPOSED ({imp.get("target","documentation").upper()})</strong>',
                    html.escape(imp.get("proposed_text", "")),
                    '</div>',
                    f'<div class="conf">confidence: {imp.get("confidence", 0):.0%}</div>',
                    '</div>',
                ]
            out.append('</div>')
    else:
        out.append('<div class="empty">No gap reports in this snapshot. Either no verifier ran, or every claim was verifiable.</div>')
    out.append('</section>')

    out.append('<footer>Bus snapshot · generated by build_dashboard.py · '
               'reload by re-running the demo and the generator</footer>')
    out += ['</div>', '</body>', '</html>']
    return "\n".join(out)


def _summarize_payload(payload: dict) -> str:
    """Concise one-line summary of a payload."""
    if not isinstance(payload, dict):
        return json.dumps(payload)[:200]
    # Drop heavy fields for the preview
    light = {k: v for k, v in payload.items() if k not in ("verification", "result")}
    if "result" in payload:
        result_val = payload["result"]
        if isinstance(result_val, dict):
            light["result"] = "(object: " + ", ".join(result_val.keys()) + ")"
        else:
            light["result"] = str(result_val)[:80]
    s = json.dumps(light, ensure_ascii=False)
    return s if len(s) < 240 else s[:237] + "..."


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=os.path.join(HERE, "_demo_bus.sqlite"))
    parser.add_argument("--out", default=os.path.join(HERE, "dashboard.html"))
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"DB not found: {args.db}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(args.db)
    try:
        agents = fetch_agents(conn)
        messages = fetch_messages(conn)
    finally:
        conn.close()

    html_out = render(
        db_path=args.db,
        agents=agents,
        messages=messages,
        snapshot_at=time.time(),
    )

    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html_out)

    print(f"Wrote {args.out}")
    print(f"  agents={len(agents)}  messages={len(messages)}  "
          f"conversations={len({m['conversation_id'] for m in messages})}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

```

### § 11G — Tests

Two assertion-based test suites that lock down the orchestration and parser correctness.

#### `skills/verifier/test_smoke.py` (164 lines)

Mock orchestration smoke test. 20 assertions covering orchestrator shape, loop bounds, remediation triggering, attempt-1 failure, attempt-2 success, session-wide gap report accumulation, dedup by claim id.

```python
#!/usr/bin/env python3
"""Smoke test: runs the full orchestrator end-to-end and asserts the key
behaviors that v1 must support.

Run from the verifier/ directory:
    python3 test_smoke.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys


HERE = os.path.dirname(os.path.abspath(__file__))
ORCH = os.path.join(HERE, "run_skill_verified.py")
TOY = os.path.join(HERE, "_toy_builder.py")


def run() -> tuple[int, dict]:
    cmd = [
        "python3", ORCH,
        "--target-script", TOY,
        "--skill-name", "toy-payment-skill",
        "--skill-doc", "Processes a test transaction and returns a structured result.",
        "--intent", "create a test payment for amount 100 and report success",
        "--max-attempts", "2",
        "--strictness", "medium",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=HERE)
    return result.returncode, json.loads(result.stdout)


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f"  ({detail})" if detail else ""))
    if not condition:
        raise AssertionError(label)


def main() -> int:
    print("Running smoke test against the verifier orchestrator...")
    print()
    exit_code, payload = run()

    print("Assertions:")

    # Orchestration shape
    check("orchestrator exits 0", exit_code == 0, f"got {exit_code}")
    check("result is present", "result" in payload)
    check("attempts field present", "attempts" in payload)
    check("verification field present", "verification" in payload)
    check("attempt_history field present", "attempt_history" in payload)

    # Loop behavior
    check(
        "used exactly 2 attempts",
        payload["attempts"] == 2,
        f"got {payload['attempts']}",
    )
    check(
        "attempt_history has 2 records",
        len(payload["attempt_history"]) == 2,
        f"got {len(payload['attempt_history'])}",
    )

    # Attempt 1 must fail (the toy builder produces incomplete output first)
    attempt_1 = payload["attempt_history"][0]
    check(
        "attempt 1 status == failed",
        attempt_1["status"] == "failed",
        f"got {attempt_1['status']}",
    )
    a1_fails = [c for c in attempt_1["claims"] if c["verdict"] == "fail"]
    check(
        "attempt 1 has at least one failed claim",
        len(a1_fails) >= 1,
        f"got {len(a1_fails)} failed",
    )
    a1_unv = [c for c in attempt_1["claims"] if c["verdict"] == "unverifiable"]
    check(
        "attempt 1 has at least one unverifiable claim",
        len(a1_unv) >= 1,
        f"got {len(a1_unv)} unverifiable",
    )

    # Attempt 2 must verify (the toy builder reads the remediation prompt and
    # produces a complete output)
    attempt_2 = payload["attempt_history"][1]
    check(
        "attempt 2 status == verified",
        attempt_2["status"] == "verified",
        f"got {attempt_2['status']}",
    )
    check(
        "attempt 2 has zero failed claims",
        all(c["verdict"] != "fail" for c in attempt_2["claims"]),
    )

    # Final state
    final = payload["verification"]
    check(
        "final status == verified",
        final["status"] == "verified",
        f"got {final['status']}",
    )

    # Session-wide gap report must survive even though attempt 2 passed
    check(
        "session gap report present",
        final.get("gap_report") is not None,
    )
    if final.get("gap_report") is not None:
        gap = final["gap_report"]
        check(
            "gap report has >= 1 unverifiable claim",
            len(gap["unverifiable_claims"]) >= 1,
            f"got {len(gap['unverifiable_claims'])}",
        )
        check(
            "gap report has >= 1 proposed improvement",
            len(gap["proposed_improvements"]) >= 1,
            f"got {len(gap['proposed_improvements'])}",
        )
        # Dedup: claim_004 should appear only once even though it surfaced on
        # attempt 1 (and was technically resolved on attempt 2).
        claim_ids = [c["id"] for c in gap["unverifiable_claims"]]
        check(
            "gap report dedups claim ids",
            len(claim_ids) == len(set(claim_ids)),
            f"ids: {claim_ids}",
        )

    # Final builder output should be JSON and include the remediated fields
    result_str = payload["result"]
    try:
        final_output = json.loads(result_str)
    except json.JSONDecodeError:
        final_output = None
    check("final output is valid JSON", final_output is not None)
    if final_output is not None:
        check(
            "final output includes amount (remediation worked)",
            "amount" in final_output,
            f"keys: {list(final_output.keys())}",
        )
        check(
            "final output includes timestamp (remediation worked)",
            "timestamp" in final_output,
        )

    print()
    print("All assertions passed.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print()
        print(f"FAILED: {e}")
        sys.exit(1)

```

#### `skills/verifier/test_llm_parsing.py` (219 lines)

LLM verifier parser tests. 16 assertions, no network. Mocks the Anthropic API call and verifies: clean JSON, markdown fence stripping, JSON-in-prose extraction, missing API key error, non-JSON error handling, confidence value clamping.

```python
#!/usr/bin/env python3
"""Test the LLM verifier's response-parsing and aggregation logic without
making a real API call. Validates everything from the HTTP boundary inward.

Run from the verifier/ directory:
    python3 test_llm_parsing.py

For an end-to-end test against the real Anthropic API, set a valid
ANTHROPIC_API_KEY and run:
    python3 run_skill_verified.py --backend llm ...
"""
from __future__ import annotations

import json
import sys
from unittest.mock import patch

import llm_verifier


# Realistic-shaped synthetic responses the LLM might produce.
RAW_RESPONSE_CLEAN_JSON = {
    "content": [{"type": "text", "text": json.dumps({
        "claims": [
            {
                "id": "claim_001",
                "type": "structural",
                "statement": "the output is valid JSON with a 'status' field",
                "evidence_required": "JSON parses; 'status' key present",
                "evidence_collected": {"parseable": True, "has_status": True},
                "verdict": "pass",
                "confidence": 0.95,
                "reasoning": "output parses cleanly and contains the required 'status' field",
            },
            {
                "id": "claim_002",
                "type": "semantic",
                "statement": "the output addresses the user's intent for a payment of 100",
                "evidence_required": "amount field present and equals 100",
                "evidence_collected": {"amount_field_present": False},
                "verdict": "fail",
                "confidence": 0.90,
                "reasoning": "the user asked for a payment of 100 but no amount field appears in the output",
            },
            {
                "id": "claim_003",
                "type": "behavioral",
                "statement": "the payment would be processed by Stripe in test mode",
                "evidence_required": "executing the skill against Stripe's test API",
                "evidence_collected": None,
                "verdict": "unverifiable",
                "confidence": 0.5,
                "reasoning": "v1 verifier cannot execute the skill; this requires actually calling Stripe",
            },
        ]
    })}],
    "usage": {"input_tokens": 1200, "output_tokens": 450},
}

RAW_RESPONSE_WITH_FENCE = {
    "content": [{"type": "text", "text": "```json\n" + json.dumps({
        "claims": [
            {"id": "c1", "type": "structural", "statement": "...", "evidence_required": "...",
             "verdict": "pass", "confidence": 0.9, "reasoning": "..."},
        ]
    }) + "\n```"}],
    "usage": {"input_tokens": 100, "output_tokens": 50},
}

RAW_RESPONSE_WITH_PROSE = {
    "content": [{"type": "text", "text": "Here are the claims:\n\n" + json.dumps({
        "claims": [
            {"id": "c1", "type": "semantic", "statement": "...", "evidence_required": "...",
             "verdict": "unverifiable", "confidence": 0.5, "reasoning": "..."},
        ]
    }) + "\n\nThat is my analysis."}],
    "usage": {"input_tokens": 100, "output_tokens": 100},
}


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f"  ({detail})" if detail else ""))
    if not condition:
        raise AssertionError(label)


def test_clean_json_response():
    print("\nTest 1: Clean JSON response")
    with patch.object(llm_verifier, "_call_anthropic", return_value=RAW_RESPONSE_CLEAN_JSON):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            record = llm_verifier.verify(
                skill_name="stripe-payment",
                skill_documentation="Creates a Stripe payment intent.",
                intent="create a payment for 100 in test mode",
                builder_output='{"status": "ok"}',
                attempt=1,
                strictness="medium",
            )
    check("3 claims parsed", len(record.claims) == 3, f"got {len(record.claims)}")
    check("status == failed (1 fail present)", record.status == "failed", f"got {record.status}")
    check("verifier_model is recorded", bool(record.verifier_model))
    check("cost > 0", record.cost_usd > 0, f"${record.cost_usd:.4f}")
    check("gap_report present (1 unverifiable)", record.gap_report is not None)
    check(
        "gap report has 1 unverifiable claim",
        len(record.gap_report.unverifiable_claims) == 1,
    )
    check(
        "improvement targets documentation",
        record.gap_report.proposed_improvements[0].target == "documentation",
    )


def test_markdown_fence_response():
    print("\nTest 2: Response wrapped in ```json ... ```")
    with patch.object(llm_verifier, "_call_anthropic", return_value=RAW_RESPONSE_WITH_FENCE):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            record = llm_verifier.verify(
                skill_name="x", skill_documentation="", intent="x",
                builder_output="x", attempt=1,
            )
    check("fence stripped, 1 claim parsed", len(record.claims) == 1)
    check("status == verified (all pass)", record.status == "verified")


def test_prose_around_json_response():
    print("\nTest 3: JSON object embedded in prose")
    with patch.object(llm_verifier, "_call_anthropic", return_value=RAW_RESPONSE_WITH_PROSE):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            record = llm_verifier.verify(
                skill_name="x", skill_documentation="", intent="x",
                builder_output="x", attempt=1,
            )
    check("inner JSON object extracted", len(record.claims) == 1)
    check("status == partial (1 unverifiable, no fail)", record.status == "partial")


def test_missing_api_key():
    print("\nTest 4: Missing API key raises clear error")
    with patch.dict("os.environ", {}, clear=True):
        try:
            llm_verifier.verify(
                skill_name="x", skill_documentation="", intent="x",
                builder_output="x", attempt=1,
            )
            raised = False
        except RuntimeError as e:
            raised = True
            msg = str(e)
    check("RuntimeError raised", raised)
    check("error message mentions ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY" in msg)
    check("error message mentions mock fallback", "mock" in msg.lower())


def test_unparseable_response():
    print("\nTest 5: Non-JSON response raises clear error")
    garbage = {"content": [{"type": "text", "text": "I cannot help with that."}], "usage": {}}
    with patch.object(llm_verifier, "_call_anthropic", return_value=garbage):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            try:
                llm_verifier.verify(
                    skill_name="x", skill_documentation="", intent="x",
                    builder_output="x", attempt=1,
                )
                raised = False
            except RuntimeError as e:
                raised = True
                msg = str(e)
    check("RuntimeError raised on non-JSON", raised)
    check("error mentions parseable JSON", "JSON" in msg or "json" in msg)


def test_confidence_clamping():
    print("\nTest 6: Out-of-range confidence values are clamped")
    bad_conf = {
        "content": [{"type": "text", "text": json.dumps({
            "claims": [
                {"id": "c1", "type": "semantic", "statement": "x", "evidence_required": "x",
                 "verdict": "pass", "confidence": 1.5, "reasoning": "x"},
                {"id": "c2", "type": "semantic", "statement": "x", "evidence_required": "x",
                 "verdict": "pass", "confidence": -0.2, "reasoning": "x"},
                {"id": "c3", "type": "semantic", "statement": "x", "evidence_required": "x",
                 "verdict": "pass", "confidence": "not a number", "reasoning": "x"},
            ]
        })}],
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }
    with patch.object(llm_verifier, "_call_anthropic", return_value=bad_conf):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            record = llm_verifier.verify(
                skill_name="x", skill_documentation="", intent="x",
                builder_output="x", attempt=1,
            )
    check("confidence > 1 clamped to 1.0", record.claims[0].confidence == 1.0)
    check("confidence < 0 clamped to 0.0", record.claims[1].confidence == 0.0)
    check("non-numeric defaults to 0.5", record.claims[2].confidence == 0.5)


def main() -> int:
    print("LLM verifier — synthetic parse + aggregate tests")

    test_clean_json_response()
    test_markdown_fence_response()
    test_prose_around_json_response()
    test_missing_api_key()
    test_unparseable_response()
    test_confidence_clamping()

    print("\nAll LLM verifier parse tests passed.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"\nFAILED: {e}")
        sys.exit(1)

```

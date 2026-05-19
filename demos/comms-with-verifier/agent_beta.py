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

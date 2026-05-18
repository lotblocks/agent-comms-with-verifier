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
                        max_attempts: int) -> dict:
    """Invoke the verifier orchestrator on the report-writing builder."""
    builder = os.path.join(HERE, "_write_report.py")
    env = os.environ.copy()
    env["DATA_INPUT"] = json.dumps(data_payload)
    cmd = [
        "python3", VERIFIER_ORCH,
        "--target-script", builder,
        "--skill-name", "demo-write-report",
        "--skill-doc", skill_doc,
        "--intent", intent,
        "--max-attempts", str(max_attempts),
        "--strictness", "medium",
        "--backend", "mock",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=180)
    if proc.returncode not in (0, 1):
        raise RuntimeError(
            f"orchestrator crashed (exit {proc.returncode}): {proc.stderr[:300]}"
        )
    return json.loads(proc.stdout)


def handle_write_report(bus: Bus, my_id: str, msg: dict) -> None:
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

    # Step 3: verify my own work.
    result = run_verified_writer(
        data_payload=upstream_data,
        skill_doc=(
            "Synthesizes a report paragraph from upstream data. Returns JSON "
            "with status, report, amount, timestamp."
        ),
        intent=intent,
        max_attempts=payload.get("max_attempts", 2),
    )

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
    args = parser.parse_args()

    bus = Bus(args.db)
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
                    handle_write_report(bus, args.my_id, m)
                    handled += 1
                except Exception as e:
                    print(f"[gamma] error: {e}", flush=True)
            else:
                print(f"[gamma] unknown task {task!r}, ignoring", flush=True)

    print(f"[gamma] done · handled={handled}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
    args = parser.parse_args()

    bus = Bus(args.db)
    bus.register(
        agent_id=args.my_id,
        name="Alpha",
        role="researcher",
        subscriptions=[f"inbox.{args.my_id}"],
    )

    # Routing: --target-id pins a specific replica (direct addressing);
    # --target-role publishes to role.<role> so any subscribed replica can claim.
    target_id = args.target_id
    target_role = args.target_role
    if not target_id and not target_role:
        # Default fallback for backwards-compat with the two-agent demo.
        target_id = "agent_beta"
        print(f"[alpha] no target specified; defaulting to {target_id}", flush=True)
    # Optional: wait briefly for role-subscribed replicas to come up.
    if target_role:
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if bus.find_agents_by_role(target_role, alive_only=True):
                break
            time.sleep(0.2)

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

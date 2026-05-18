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

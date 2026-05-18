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

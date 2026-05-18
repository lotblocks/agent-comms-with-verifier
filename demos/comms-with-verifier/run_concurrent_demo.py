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

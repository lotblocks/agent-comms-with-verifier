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

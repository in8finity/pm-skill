#!/usr/bin/env python3
"""Stress test: spawn N parallel claim attempts on a single task and
verify exactly one survives the post-append recheck.

This is the runtime regression guard for blind spot #2
(`reports/cache-staleness-investigation.md`). The investigation argued
the cache + write-queue interaction is sound under our single-MCP-server
deployment; this test exercises it empirically.

Workflow:
  1. Plan a fresh task (or reuse a slug if --task is given).
  2. Spawn N threads, each running `pm executing` against the task.
  3. Collect exit codes; expect exactly ONE 0, the rest 6 or 8.
  4. Verify the latest TaskStatus in hashharness has exactly one
     "working" event whose text_sha256 matches the winning agent's
     return.
  5. Reclaim the task at the end so the queue is left clean.

Usage:
  stress_claim_race.py [--queue Q] [--agents N] [--keep] [--task SHA]

Exit codes:
  0  exactly one agent won — protocol holds
  1  more than one agent reported exit 0 — race-safety BROKEN
  2  zero agents reported exit 0 — liveness broken
  3  the task's latest TaskStatus doesn't match the winner
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

import store

HERE = Path(__file__).parent
PM = str(HERE / "pm")


def run_pm(*args: str) -> tuple[int, str, str]:
    p = subprocess.run([PM, *args], capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def make_target_task(queue: str) -> str:
    """Plan a single throwaway task and return its sha256."""
    slug = f"stress-{secrets.token_hex(4)}"
    rc, stdout, stderr = run_pm(
        "plan", "--queue", queue,
        "--title", f"stress race {slug}",
        "--text", "stress test for parallel claim contention",
        "--slug", slug,
    )
    if rc != 0:
        sys.stderr.write(f"plan failed: {stderr}\n")
        sys.exit(2)
    payload = json.loads(stdout)
    return payload["task"]["text_sha256"]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--queue", default="default")
    p.add_argument("--agents", type=int, default=8)
    p.add_argument("--keep", action="store_true",
                   help="don't reclaim the task after the test")
    p.add_argument("--task",
                   help="use an existing task SHA instead of planning a new one")
    args = p.parse_args()

    target = args.task or make_target_task(args.queue)
    print(f"target task: {target[:24]}…")

    # Spawn N threads. Each runs `pm executing` and reports its outcome.
    results: list[tuple[int, str, str]] = []
    lock = threading.Lock()
    barrier = threading.Barrier(args.agents)

    def worker(idx: int) -> None:
        agent = f"stress-{idx}-{os.getpid()}"
        # Synchronize starts so all threads hit the recheck window together.
        barrier.wait(timeout=10)
        rc, out, err = run_pm("executing", "--task", target,
                               "--note", f"stress agent {agent}")
        with lock:
            results.append((rc, out, err))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(args.agents)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.time() - t0

    # Tally
    winners = [r for r in results if r[0] == 0]
    pre_refusals = sum(1 for r in results if r[0] == 6)
    race_losses = sum(1 for r in results if r[0] == 8)
    other = sum(1 for r in results if r[0] not in (0, 6, 8))

    print(f"agents={args.agents}  elapsed={elapsed:.2f}s")
    print(f"  winners (exit 0): {len(winners)}")
    print(f"  pre-claim refusals (exit 6): {pre_refusals}")
    print(f"  race losses (exit 8): {race_losses}")
    print(f"  other: {other}")

    # Cross-check against the chain
    latest = store.latest_status(target)
    if latest is None:
        sys.stderr.write("no TaskStatus — something went very wrong\n")
        return 3
    print(f"  latest tip: {latest['text_sha256'][:24]}…  status={store.status_value(latest)}")

    if not args.keep:
        store.reclaim(target, reason="stress test cleanup", reclaimer="stress")
        print(f"  reclaimed.")

    # Verdicts
    if len(winners) == 0:
        print("FAIL: zero winners — liveness broken")
        return 2
    if len(winners) > 1:
        print("FAIL: multiple winners — race-safety BROKEN")
        return 1
    winner_status = json.loads(winners[0][1])
    if winner_status.get("text_sha256") != latest["text_sha256"]:
        print("FAIL: chain tip doesn't match the winner's append")
        return 3

    print(f"PASS: exactly one winner; chain tip matches.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Stress test: spawn N parallel `pm pull` workers (each with a distinct
sticky context) against a queue holding M sticky parent tasks, and
verify every worker that has work to do actually claims something —
no silent starvation.

This is the runtime regression guard for the per-invocation skip-set
fix in pull.py. Without that fix, pull's retry loop re-picked the same
first-by-(context_priority, created_at) candidate every iteration. When
two workers raced on the same parent, the loser would re-pick the same
parent six times in a row, exhaust --max-retries, and silently
``return 0`` with empty stdout — the worker shell saw empty TASK and
quit "queue empty" while a sibling parent sat idle.

Workflow:
  1. Plan M sticky parents in a fresh queue.
  2. Spawn N threads, each running `pm pull --queue Q` with its own
     PM_CONTEXT_ID.
  3. Collect successful claims (TASK printed) and empty exits.
  4. Expect: exactly min(N, M) workers got a task, the rest got
     none. No worker should exit silently while runnable tasks remain
     and that worker is sticky-eligible for them.
  5. Optionally verify on the chain that each claimed parent's working
     TaskStatus carries the claimer's context_id.

Usage:
  stress_pull_starve.py [--queue Q] [--workers N] [--parents M]
                        [--trials T] [--keep]

Exit codes:
  0  every trial succeeded — protocol holds
  1  at least one trial saw starvation (a worker exited empty while a
     sticky-eligible parent remained `new`)
  2  no trial completed (orchestrator failure)
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import store

HERE = Path(__file__).parent
PM = str(HERE / "pm")


def run_pull(queue: str, ctx: str) -> tuple[int, str, str]:
    env = dict(os.environ)
    env["PM_CONTEXT_ID"] = ctx
    p = subprocess.run(
        [PM, "pull", "--queue", queue],
        capture_output=True, text=True, env=env,
    )
    return p.returncode, p.stdout, p.stderr


def parse_pull(stdout: str) -> dict[str, str]:
    """Parse the shell-eval-able stdout into a dict."""
    out: dict[str, str] = {}
    for line in stdout.splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        # strip the single-quote shell quoting
        if v.startswith("'") and v.endswith("'"):
            v = v[1:-1].replace("'\\''", "'")
        out[k] = v
    return out


def plan_parent(queue: str, slug: str) -> str:
    """Plan a sticky parent. Returns the task's text_sha256."""
    p = subprocess.run(
        [PM, "plan", "--queue", queue, "--slug", slug,
         "--title", slug.upper(), "--text", f"parent {slug}", "--sticky"],
        capture_output=True, text=True,
    )
    if p.returncode != 0:
        raise RuntimeError(f"plan failed for {slug}: {p.stderr}")
    # plan.py prints `{"task": {...}, "status": {...}}`; parse out the sha
    data = json.loads(p.stdout)
    return data["task"]["text_sha256"]


def run_trial(queue_prefix: str, n_workers: int, n_parents: int,
              trial_idx: int) -> tuple[bool, str]:
    """Run one trial. Returns (passed, summary)."""
    queue = f"{queue_prefix}-{trial_idx}-{secrets.token_hex(3)}"
    parent_shas: list[str] = []
    for i in range(n_parents):
        parent_shas.append(plan_parent(queue, f"p{i+1}"))

    contexts = [str(uuid.uuid4()) for _ in range(n_workers)]
    results: list[tuple[int, dict[str, str], str]] = [None] * n_workers  # type: ignore

    def worker(idx: int) -> None:
        rc, out, err = run_pull(queue, contexts[idx])
        results[idx] = (rc, parse_pull(out), err)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    expected = min(n_workers, n_parents)
    claims = [r for r in results if r and r[1].get("TASK")]
    actual = len(claims)
    claimed_shas = sorted(r[1]["TASK"] for r in claims)
    distinct_shas = len(set(claimed_shas))

    summary_lines = [
        f"trial #{trial_idx} queue={queue}: workers={n_workers} parents={n_parents} "
        f"expected_claims={expected} actual={actual} distinct={distinct_shas}",
    ]
    for i, r in enumerate(results):
        if r is None:
            summary_lines.append(f"  worker[{i}]: NO RESULT")
            continue
        rc, parsed, err = r
        slug = parsed.get("SLUG", "")
        retries_lost = parsed.get("RETRIES_LOST", "")
        summary_lines.append(
            f"  worker[{i}] ctx={contexts[i][:8]} rc={rc} "
            f"task={(parsed.get('TASK') or '<empty>')[:12]} slug={slug} "
            f"retries_lost={retries_lost or '-'}"
        )
        if err.strip():
            for line in err.strip().splitlines():
                summary_lines.append(f"    [stderr] {line}")

    passed = (actual == expected) and (distinct_shas == actual)
    return passed, "\n".join(summary_lines)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--queue", default="stress-pull-starve",
                   help="prefix for trial queue names")
    p.add_argument("--workers", type=int, default=2,
                   help="parallel workers per trial (each gets a distinct context)")
    p.add_argument("--parents", type=int, default=2,
                   help="sticky parent tasks planned per trial")
    p.add_argument("--trials", type=int, default=10,
                   help="how many independent trials to run")
    p.add_argument("--keep", action="store_true",
                   help="leave queues populated after the run (no cleanup)")
    args = p.parse_args()

    if args.workers < 1 or args.parents < 1:
        sys.stderr.write("workers and parents must be >= 1\n")
        return 2

    fails = 0
    for i in range(1, args.trials + 1):
        try:
            passed, summary = run_trial(
                args.queue, args.workers, args.parents, i,
            )
        except Exception as exc:
            sys.stderr.write(f"trial {i} ORCHESTRATOR FAIL: {exc}\n")
            return 2
        prefix = "PASS" if passed else "FAIL"
        print(f"[{prefix}] {summary}")
        if not passed:
            fails += 1

    print(f"\n{args.trials - fails}/{args.trials} trials passed")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

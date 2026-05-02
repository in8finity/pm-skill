#!/usr/bin/env python3
"""Cancel a task — supervisor / planner override.

Anyone can cancel a non-terminal task. Cancellation writes a synthetic
TaskReport (carrying the reason) and a TaskStatus(rejected,
cancelled=true) that links proof → that report. From a chain perspective
the task is rejected; the ``cancelled`` attribute distinguishes a
deliberate cancel from a worker-reported rejection.

This is the runtime counterpart of the ``cancel`` transition in
``system-models/planning.als`` (verified by 3 cancel-safety
assertions: CancelledIsRejectedTerminal, CancelOnlyOnNonTerminal,
CancelledHasProof).

Usage:
  cancel.py --task SHA [--reason TEXT] [--cancelled-by ID] [--cascade]

Exit codes:
  0 cancelled (and any cascade)
  6 task already absorbing (done / rejected / superseded) — refuse
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys

import store


def default_canceller() -> str:
    return os.environ.get("PM_AGENT_ID") or f"manual@{socket.gethostname()}"


def cancel_one(task_sha: str, *, reason: str, cancelled_by: str) -> dict | None:
    cur = store.status_value(store.latest_status(task_sha))
    # Refuse on any absorbing status: done/rejected (terminal-absorbing
    # per planning.als) and superseded (absorbing per planning_replan.als
    # R4 SupersededIsAbsorbing — cancelling on top of a superseded task
    # would falsify the absorption property and pollute the audit chain
    # with a redundant `rejected` after the new clone has already
    # replaced it; cancel the successor instead).
    if cur in ("done", "rejected", "superseded"):
        sys.stderr.write(
            f"refusing: task {task_sha[:12]} status is '{cur}' (absorbing)\n"
        )
        return None
    return store.cancel_task(task_sha, reason=reason, cancelled_by=cancelled_by)


def cascade(task_sha: str, *, reason: str, cancelled_by: str, queue: str,
            visited: set[str]) -> list[dict]:
    """DFS-cancel any undone subtasks. Returns list of cancel results."""
    out: list[dict] = []
    if task_sha in visited:
        return out
    visited.add(task_sha)
    children = store.find_undone_subtasks(task_sha, queue)
    for child in children:
        child_sha = child["text_sha256"]
        slug = (child.get("attributes") or {}).get("slug", "?")
        result = cancel_one(child_sha,
                             reason=f"parent cancelled: {reason}",
                             cancelled_by=cancelled_by)
        if result is not None:
            out.append({"task": child_sha, "slug": slug, **result})
            # Recurse into grandchildren
            out.extend(cascade(child_sha, reason=reason,
                                cancelled_by=cancelled_by, queue=queue,
                                visited=visited))
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--task", required=True)
    p.add_argument("--reason", default="cancelled by supervisor")
    p.add_argument("--cancelled-by", default=default_canceller())
    p.add_argument("--cascade", action="store_true",
                   help="recursively cancel undone subtasks (parentTask reverse-links)")
    args = p.parse_args()

    primary = cancel_one(args.task, reason=args.reason,
                         cancelled_by=args.cancelled_by)
    if primary is None:
        return 6

    out = {"task": args.task, "primary": primary, "cascade": []}
    if args.cascade:
        # Find the queue from the cancelled task itself
        task = store.get_task(args.task) or {}
        queue = (task.get("attributes") or {}).get("queue", "default")
        # `visited` starts empty — it's a cycle-breaker for the recursive
        # walk, NOT a "skip the root" marker. cascade() adds task_sha
        # itself on entry, then iterates children.
        out["cascade"] = cascade(args.task,
                                  reason=args.reason,
                                  cancelled_by=args.cancelled_by,
                                  queue=queue,
                                  visited=set())
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

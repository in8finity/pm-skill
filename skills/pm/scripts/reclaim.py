#!/usr/bin/env python3
"""Force-reclaim a task — append TaskStatus(new, reclaimed=true).

For supervisor / human override of a stuck task. Refuses if the task is
not currently in `working` (terminal tasks are absorbing; new tasks are
already reclaimable).

With ``--cascade``, also reclaims every undone descendant (parentTask
reverse-link DFS) that is currently in `working`. Use this when a sticky
chain's session died and you need to release the whole subtree so a
fresh agent can pick it up; the dead agent's PM_CONTEXT_ID is stripped
from each task in the subtree.

Usage:
  reclaim.py --task SHA [--reason TEXT] [--reclaimer ID] [--cascade]

Exit codes:
  0  reclaim succeeded (and any cascade)
  6  task not in `working`
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys

import store


def default_reclaimer() -> str:
    return os.environ.get("PM_AGENT_ID") or f"manual@{socket.gethostname()}"


def reclaim_one(task_sha: str, *, reason: str, reclaimer: str) -> dict | None:
    cur = store.status_value(store.latest_status(task_sha))
    if cur != "working":
        sys.stderr.write(
            f"refusing: task {task_sha[:12]} status is '{cur}', expected 'working'\n"
        )
        return None
    return store.reclaim(task_sha, reason=reason, reclaimer=reclaimer)


def cascade(task_sha: str, *, reason: str, reclaimer: str, queue: str,
            visited: set[str]) -> list[dict]:
    """DFS through parentTask reverse-links, reclaim every undone child
    currently in ``working``. ``visited`` starts empty (cycle-breaker only,
    not entry-skip)."""
    out: list[dict] = []
    if task_sha in visited:
        return out
    visited.add(task_sha)
    children = store.find_undone_subtasks(task_sha, queue)
    for child in children:
        child_sha = child["text_sha256"]
        cur = store.status_value(store.latest_status(child_sha))
        if cur != "working":
            # Child is `new` (not yet claimed) or terminal — skip.
            # Recursing into a `new` child finds nothing useful; recursing
            # into a terminal one is irrelevant.
            continue
        slug = (child.get("attributes") or {}).get("slug", "?")
        result = store.reclaim(
            child_sha,
            reason=f"parent reclaimed: {reason}",
            reclaimer=reclaimer,
        )
        out.append({"task": child_sha, "slug": slug, **{
            k: v for k, v in result.items()
            if k in ("text_sha256", "created_at")
        }})
        out.extend(cascade(child_sha, reason=reason, reclaimer=reclaimer,
                            queue=queue, visited=visited))
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--task", required=True)
    p.add_argument("--reason", default="manual reclaim")
    p.add_argument("--reclaimer", default=default_reclaimer())
    p.add_argument("--cascade", action="store_true",
                   help="also reclaim every undone working descendant "
                        "(parentTask reverse-links)")
    p.add_argument("--context-id", default=None,
                   help="accepted for CLI symmetry with other pm verbs; "
                        "reclaim is a supervisor override and does not "
                        "consult the caller's sticky binding, so this "
                        "flag is intentionally a no-op. Lets callers pass "
                        "--context-id uniformly to every pm subcommand "
                        "without special-casing reclaim.")
    args = p.parse_args()

    primary = reclaim_one(args.task,
                          reason=args.reason,
                          reclaimer=args.reclaimer)
    if primary is None:
        return 6

    out = {"task": args.task, "primary": primary, "cascade": []}
    if args.cascade:
        task = store.get_task(args.task) or {}
        queue = (task.get("attributes") or {}).get("queue", "default")
        out["cascade"] = cascade(
            args.task,
            reason=args.reason,
            reclaimer=args.reclaimer,
            queue=queue,
            visited=set(),
        )
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

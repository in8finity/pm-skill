#!/usr/bin/env python3
"""Return the next runnable task as JSON, or ``null`` if none.

A task is runnable when:
  - its current status is ``new``
  - every task in ``links.dependsOn`` has current status ``done``
  - every direct child (task whose ``parentTask`` link points at this
    one) has reached a terminal status (``done`` / ``rejected`` /
    ``superseded``). Pending children block the parent so a wrapper
    step (e.g. one expanded into a subskill via ``--depth ≥1``) only
    becomes runnable after its expansion completes — preserving the
    "parent rolls up its children" semantic.
  - its ``attributes.workdir`` matches the caller's workdir (or the task
    has no workdir attribute, i.e. legacy / cross-cutting)

The caller's workdir is ``$PM_WORKDIR`` if set, else ``realpath(cwd)``.
Workdir filtering scopes the queue to a workspace: a worker started in
``~/projects/A`` only sees tasks planned from ``~/projects/A``.

Tasks are returned in created_at order (oldest first).

Usage:
  next.py [--queue Q]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import store


def caller_workdir() -> str:
    return os.path.realpath(os.environ.get("PM_WORKDIR") or os.getcwd())


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--queue", default="default")
    p.add_argument("--context-id", default=None,
                   help="sticky context id (overrides $PM_CONTEXT_ID). When "
                        "set, pm next pre-filters tasks the caller couldn't "
                        "legally claim — skipping sticky tasks bound to a "
                        "different context. Mirrors the executing/report/"
                        "finished/heartbeat flag for CLI symmetry.")
    args = p.parse_args()

    here = caller_workdir()
    agent_context = args.context_id or os.environ.get("PM_CONTEXT_ID") or None

    tasks = store.list_tasks(args.queue)

    # Link values are record_sha256 (hashharness link contract); we key
    # status lookups off text_sha256, so build a one-shot translation.
    record_to_text = {t["record_sha256"]: t["text_sha256"] for t in tasks}

    # O(open) candidate discovery: ask the backend for only those tasks
    # whose CURRENT status is `new`. With the tip-attribute index in
    # hashharness, this scales with runnable work, not with total queue
    # history — so a queue full of terminal `done`/`rejected`/`superseded`
    # tasks no longer drags every call. Statuses for the deps and parents
    # of those candidates (which may be in any state) are then resolved
    # with one targeted bulk call below.
    new_shas = store.open_task_shas(args.queue, status="new", tasks=tasks)
    status_cache: dict[str, str | None] = {sha: "new" for sha in new_shas}

    need_status: set[str] = set()
    for t in tasks:
        if t["text_sha256"] not in new_shas:
            continue
        for d in (t.get("links") or {}).get("dependsOn") or []:
            dt = record_to_text.get(d)
            if dt and dt not in status_cache:
                need_status.add(dt)
        p = (t.get("links") or {}).get("parentTask")
        if p:
            pt = record_to_text.get(p)
            if pt and pt not in status_cache:
                need_status.add(pt)
    if need_status:
        status_cache.update(store.bulk_status_values(list(need_status)))

    ctx_cache: dict[str, set[str]] = {}

    def status_of(sha: str) -> str | None:
        if sha not in status_cache:
            status_cache[sha] = store.status_value(store.latest_status(sha))
        return status_cache[sha]

    def required_ctx(sha: str) -> set[str]:
        """Cached collect_required_contexts — same per-call scope as
        status_cache. Bounds the cost of context-affinity sorting at
        O(N) total instead of O(N²) per pm next call."""
        if sha not in ctx_cache:
            ctx_cache[sha] = store.collect_required_contexts(sha)
        return ctx_cache[sha]

    def context_priority(t: dict) -> int:
        """0 if this caller has affinity to t (own context_id matches,
        or inherited via the sticky-ancestor chain); 1 otherwise. Used
        as the primary sort key so a worker drains its own subtree
        before competing for unrelated older tasks. No-op when the
        caller has no PM_CONTEXT_ID — every task gets priority 1 and
        the sort collapses to pure created_at FIFO. See
        proposal-pm-context-preference (planning-shared/next.py)."""
        if agent_context is None:
            return 1
        sha = t["text_sha256"]
        own = store.task_context_id(sha)
        if own == agent_context:
            return 0
        return 0 if agent_context in required_ctx(sha) else 1

    # Only `new` tasks can ever be runnable; everything else is filtered
    # out of the loop entirely so we don't pay even one fallback status
    # round trip for terminal/working tasks.
    tasks = [t for t in tasks if t["text_sha256"] in new_shas]
    tasks.sort(key=lambda t: (context_priority(t), t.get("created_at", "")))

    def dep_done(d_record: str) -> bool:
        d_text = record_to_text.get(d_record)
        if d_text is None:
            # Cross-queue dep or dangling reference — treat as not-done.
            return False
        return status_of(d_text) == "done"

    # Lazy cache for cross-queue parent lookups inside parent_claimed.
    # Populated only when a child's parent_record is not in the per-
    # queue listing — typical pm next call doesn't touch it.
    xq_parent_status_cache: dict[str, str | None] = {}

    def parent_claimed(t: dict) -> bool:
        """A child is runnable only after its parent's lifecycle has begun.
        Parent-claim gate enforces the convention "parent owns the
        subtree's lifecycle / binds the context": children can't start
        until somebody has claimed the parent. A parent in `working` /
        `done` / `rejected` / `superseded` satisfies the gate (the
        lifecycle has begun). The gate is **universal across queues** —
        if the parent isn't in the current queue's listing, we look
        it up globally rather than treating it as "no parent." See
        planning_parent_gate.als#ChildBlockedUntilParentClaimed and
        #CrossQueueChildBlockedUntilParentClaimed."""
        parent_record = (t.get("links") or {}).get("parentTask")
        if not parent_record:
            return True  # no parent — top-level task
        parent_text = record_to_text.get(parent_record)
        if parent_text is not None:
            return status_of(parent_text) != "new"
        # Cross-queue parent: fall back to a global record-sha lookup
        # so the gate fires uniformly. Cached per `pm next` invocation
        # to keep the worst-case cost bounded.
        if parent_record not in xq_parent_status_cache:
            xq_parent_status_cache[parent_record] = \
                store.task_status_by_record_sha(parent_record)
        cached = xq_parent_status_cache[parent_record]
        if cached is None:
            return True  # dangling parent link — no Task exists at all
        return cached != "new"

    workdir_skipped = 0
    for t in tasks:
        sha = t["text_sha256"]
        bound = store.task_workdir(t)
        if bound is not None and bound != here:
            workdir_skipped += 1
            continue
        if status_of(sha) != "new":
            continue
        deps = (t.get("links") or {}).get("dependsOn") or []
        if not all(dep_done(d) for d in deps):
            continue
        if not parent_claimed(t):
            continue
        # Sticky pre-filter: skip a task we couldn't legally claim. Same
        # check executing.py runs at claim-time; doing it here means we
        # don't surface a task the worker would just bounce on with
        # exit 10. Cheap because non-sticky tasks short-circuit.
        try:
            store.check_sticky_eligibility(sha, agent_context)
        except (store.StickyContextMismatch, store.StickyContextConflict):
            continue
        print(json.dumps(t, indent=2))
        return 0

    # Diagnostic: if we returned null but the queue had tasks scoped to
    # a different workdir than the caller's, surface that on stderr so
    # the worker doesn't think the queue is empty when it's just
    # invisible. Fixes the "first worker thought queue was empty" foot-
    # gun where PM_WORKDIR isn't set and tasks all have a workdir.
    if workdir_skipped:
        sys.stderr.write(
            f"pm next: filtered {workdir_skipped} task(s) by workdir "
            f"mismatch (caller workdir={here!r}); set PM_WORKDIR=... or "
            f"run from the planner's cwd to see them\n"
        )
    print("null")
    return 0


if __name__ == "__main__":
    sys.exit(main())

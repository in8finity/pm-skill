#!/usr/bin/env python3
"""Enqueue a new Task. Outputs the created task as JSON.

Usage:
  plan.py --title T --text BODY [--queue Q] [--slug S]
          [--parent TASK_SHA] [--depends-on SHA[,SHA...]]

If --parent is set, the new task is recorded as a subtask: it links to the
parent's current TaskStatus via ``spawnedAt`` (the decision point).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

import store


def resolve_workdir(parent_task: dict | None) -> str | None:
    """Pick the workdir to bind this task to.

    Priority: explicit ``PM_WORKDIR`` env > parent task's workdir (so
    subtasks share the parent's binding even if the worker chdir'd into
    a subdirectory before spawning) > realpath of current cwd.

    ``PM_WORKDIR=`` (set but empty) is the escape hatch: returns None,
    producing a task with no workdir attribute that any worker sees.
    """
    env = os.environ.get("PM_WORKDIR")
    if env is not None:
        return os.path.realpath(env) if env else None
    if parent_task is not None:
        inherited = store.task_workdir(parent_task)
        if inherited:
            return inherited
    return os.path.realpath(os.getcwd())


def slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s or "task"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--title", required=True)
    p.add_argument("--text", required=True)
    p.add_argument("--queue", default="default")
    p.add_argument("--slug")
    p.add_argument("--parent", help="parent task sha256")
    p.add_argument("--depends-on", default="")
    p.add_argument("--verifier", default="",
                   help="absolute path of a script that validates the task "
                        "result; pm finished invokes it with env PM_TASK / "
                        "PM_REPORT_SHA / PM_QUEUE / PM_SLUG and exit 0 = pass")
    p.add_argument("--sticky", action="store_true",
                   help="mark task as sticky-context: the first agent to "
                        "claim it owns it (via PM_CONTEXT_ID) until reclaim. "
                        "Subtasks via --parent inherit sticky automatically.")
    args = p.parse_args()

    slug = args.slug or slugify(args.title)
    existing = store.find_task_by_slug(args.queue, slug)
    if existing:
        # Self-heal: if a previous run created the Task but crashed before
        # appending the genesis status, finish the job idempotently instead
        # of erroring out.
        if store.latest_status(existing["text_sha256"]) is None:
            status = store.append_status(
                existing["text_sha256"], "new", note=f"enqueued: {args.title}",
            )
            print(json.dumps({"task": existing, "status": status, "healed": True}, indent=2))
            return 0
        sys.stderr.write(f"slug '{slug}' already exists in queue '{args.queue}'\n")
        return 4

    spawned_at = None
    if args.parent:
        parent_status = store.latest_status(args.parent)
        if parent_status is None:
            sys.stderr.write(f"parent {args.parent} has no status — cannot spawn subtask\n")
            return 5
        spawned_at = parent_status["text_sha256"]

    deps = [d.strip() for d in args.depends_on.split(",") if d.strip()]

    # Dep validation — see system-models/planning.als plan[] precondition.
    # The runtime gate that keeps the formal NoCycle property derivable from
    # the transition system rather than carried as a static input assumption.
    if deps:
        own_sha = store.sha256_text(store.task_identity_text(args.queue, slug))
        if own_sha in deps:
            sys.stderr.write(
                f"refusing: dependsOn includes this task's own sha "
                f"({own_sha[:12]}) — self-loop is unrunnable\n"
            )
            return 11
        for d in deps:
            target = store.get_task(d)
            if target is None:
                sys.stderr.write(
                    f"refusing: dep {d[:12]} does not resolve to an "
                    f"existing Task — task would be forever blocked\n"
                )
                return 11
            cur = store.status_value(store.latest_status(d))
            if cur in ("rejected", "superseded"):
                sys.stderr.write(
                    f"refusing: dep {d[:12]} latest status is '{cur}' — "
                    f"can never become 'done', task would be forever blocked\n"
                )
                return 11

    # Fetch parent once for both sticky inheritance and workdir resolution.
    parent_task = store.get_task(args.parent) if args.parent else None

    # Sticky inheritance: subtask of a sticky parent is sticky too.
    sticky = bool(args.sticky)
    if parent_task and not sticky and store.task_is_sticky(parent_task):
        sticky = True

    workdir = resolve_workdir(parent_task)

    try:
        task = store.create_task(
            queue=args.queue,
            title=args.title,
            text=args.text,
            slug=slug,
            parent_task_sha=args.parent,
            spawned_at_status_sha=spawned_at,
            depends_on=deps,
            verifier=args.verifier or None,
            sticky=sticky,
            workdir=workdir,
        )
    except store.SlugTaken:
        # The pre-check (find_task_by_slug) said the slug was free, but a
        # concurrent plan() committed first and our content-addressed
        # create_task collided on text_sha256. The slug is taken — refuse.
        sys.stderr.write(
            f"slug '{slug}' was claimed concurrently in queue '{args.queue}' — try again\n"
        )
        return 4
    # Genesis status: new
    status = store.append_status(task["text_sha256"], "new", note=f"enqueued: {args.title}")
    print(json.dumps({"task": task, "status": status}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

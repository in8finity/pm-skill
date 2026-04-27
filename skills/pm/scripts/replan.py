#!/usr/bin/env python3
"""Replan a task — restart it (and optionally its dependency-chain ancestors).

Use when a task was interrupted / rejected / completed-but-wrong and needs
to be redone, possibly with adjustments. Two operating modes:

  - **In-place reset** (no ``--text``/``--verifier``): append
    ``TaskStatus(new, replanned=true)`` to the target. The Task record
    itself is untouched (immutable), the chain just resets to ``new``
    so a worker can pick it up again.

  - **Body adjustment** (``--text`` or ``--verifier`` given): create a
    NEW Task with the adjusted body in the same queue, slug suffixed
    with ``-r<N>``, ``attributes.replan_of`` linking back to the
    original. Append ``TaskStatus(superseded)`` to the original (a new
    terminal status that excludes it from the queue forever).

By default ``replan`` walks the target's ``links.dependsOn`` ancestors
recursively and resets every ancestor that is currently ``done`` or
``rejected`` back to ``new`` (in-place — ancestor adjustments are not
supported here; replan each ancestor separately if you need to edit
them). Use ``--no-cascade-up`` to limit the operation to the target.

Note: replan never rewrites the dependsOn graph. Ancestors keep their
original sha (in-place reset doesn't change identity); the new target
task (if adjusted) keeps the SAME ``dependsOn`` list as the original
since the ancestor shas haven't changed — they've just gone back to
``new`` and will be redone before the new target becomes runnable.

Usage:
  replan.py --task SHA [--text "new body"] [--verifier "..."]
            [--no-cascade-up] [--note "..."]

Exit codes:
  0  replan succeeded
  6  target task is in a non-replannable status (already superseded)
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import store


REPLAN_NEW_NOTE = "replanned"


def reset_in_place(task_sha: str, note: str) -> dict[str, Any]:
    """Append TaskStatus(new, replanned=true). Skips if already in `new`
    or `working` (no-op replan would be confusing)."""
    cur = store.status_value(store.latest_status(task_sha))
    if cur == "superseded":
        raise RuntimeError(f"task {task_sha[:12]} is superseded; cannot replan")
    if cur in ("new", "working"):
        return {"task": task_sha, "skipped": True, "current": cur}
    return store.append_status(
        task_sha, "new",
        note=note or REPLAN_NEW_NOTE,
        extra_attrs={"replanned": True},
    )


def supersede_and_clone(
    orig_task: dict[str, Any],
    *,
    new_text: str | None,
    new_verifier: str | None,
    note: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Create a new Task with adjusted body, mark the original superseded.

    Returns ``(new_task, superseded_status)``.
    """
    orig_sha = orig_task["text_sha256"]
    attrs = orig_task.get("attributes") or {}
    queue = attrs.get("queue") or "default"
    base_slug = attrs.get("slug") or "task"

    # Find a free replan slug in the same queue.
    suffix = 1
    while True:
        candidate = f"{base_slug}-r{suffix}"
        if store.find_task_by_slug(queue, candidate) is None:
            break
        suffix += 1

    body = new_text if new_text is not None else attrs.get("body", "")
    verifier = new_verifier if new_verifier is not None else attrs.get("verifier")
    sticky = bool(attrs.get("sticky"))
    workdir = attrs.get("workdir")

    deps = (orig_task.get("links") or {}).get("dependsOn") or []
    parent = (orig_task.get("links") or {}).get("parentTask")
    spawned_at = (orig_task.get("links") or {}).get("spawnedAt")

    new_task = store.create_task(
        queue=queue,
        title=orig_task.get("title", "") or "",
        text=body,
        slug=candidate,
        parent_task_sha=parent,
        spawned_at_status_sha=spawned_at,
        depends_on=deps,
        verifier=verifier,
        sticky=sticky,
        workdir=workdir,
    )
    # Genesis status for the new task.
    store.append_status(
        new_task["text_sha256"], "new",
        note=f"replan_of {orig_sha[:12]}",
        extra_attrs={"replan_of": orig_sha},
    )

    # Mark the original superseded, pointing at the new task.
    superseded = store.append_status(
        orig_sha, "superseded",
        note=note or f"superseded by {new_task['text_sha256'][:12]}",
        extra_attrs={"superseded_by": new_task["text_sha256"]},
    )
    return new_task, superseded


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--task", required=True)
    p.add_argument("--text", default=None,
                   help="adjusted body for the target (creates a new Task)")
    p.add_argument("--verifier", default=None,
                   help="adjusted verifier for the target (creates a new Task)")
    p.add_argument("--no-cascade-up", action="store_true",
                   help="limit replan to the target only; don't reset ancestors")
    p.add_argument("--note", default="")
    args = p.parse_args()

    orig = store.get_task(args.task)
    if orig is None:
        sys.stderr.write(f"task {args.task[:12]} not found\n")
        return 6
    cur = store.status_value(store.latest_status(args.task))
    if cur == "superseded":
        sys.stderr.write(
            f"refusing: task {args.task[:12]} is already superseded\n"
        )
        return 6

    out: dict[str, Any] = {"target": args.task, "ancestors": [], "target_result": None}

    # Cascade up first so by the time the (possibly new) target task
    # becomes runnable, its dependencies are already reset.
    if not args.no_cascade_up:
        for anc_sha in store.find_dependency_ancestors(args.task):
            anc_cur = store.status_value(store.latest_status(anc_sha))
            if anc_cur not in ("done", "rejected"):
                # Skip ancestors that are still pending / in-flight; they're
                # already going to be (re-)done.
                out["ancestors"].append({
                    "task": anc_sha, "skipped": True, "current": anc_cur,
                })
                continue
            res = reset_in_place(anc_sha, args.note)
            out["ancestors"].append({"task": anc_sha, **{
                k: v for k, v in res.items()
                if k in ("text_sha256", "created_at", "skipped", "current")
            }})

    has_edit = args.text is not None or args.verifier is not None
    if has_edit:
        new_task, superseded = supersede_and_clone(
            orig,
            new_text=args.text,
            new_verifier=args.verifier,
            note=args.note,
        )
        out["target_result"] = {
            "mode": "supersede_and_clone",
            "original": args.task,
            "new_task": new_task["text_sha256"],
            "new_slug": (new_task.get("attributes") or {}).get("slug"),
            "superseded_status": superseded["text_sha256"],
        }
    else:
        res = reset_in_place(args.task, args.note)
        out["target_result"] = {"mode": "reset_in_place", **{
            k: v for k, v in res.items()
            if k in ("text_sha256", "created_at", "skipped", "current")
        }}

    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

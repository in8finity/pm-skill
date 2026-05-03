#!/usr/bin/env python3
"""Bulk-enqueue tasks from a JSON file.

Input is a JSON array of task specs:
    [
      {
        "slug":   "stable-id",
        "title":  "human label",
        "text":   "full body",
        "parent":     "<task-sha>",     // optional
        "depends_on": ["sha", ...],     // optional
        "verifier":   "skill:foo",      // optional
        "sticky":     true,             // optional (auto if parent is sticky)
        "workdir":    "/abs/path"       // optional (else $PM_WORKDIR or parent's)
      },
      ...
    ]

Behavior is idempotent per slug:
- new slug                    -> create Task + genesis TaskStatus(new)
- slug exists, no status      -> append genesis TaskStatus(new) (heal)
- slug exists, has status     -> skip

Usage:
  bulk_plan.py [--queue Q] --input path/to/specs.json
  bulk_plan.py [--queue Q] --input -        # read JSON from stdin

Stops on the first hard create_task failure (returns exit 1). All other
outcomes (create, heal, skip) are reported per-line on stdout as TSV:
    <text_sha256>\t<slug>\t<outcome:created|healed|skipped>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import store
from plan import resolve_workdir


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--queue", default="default")
    p.add_argument("--input", required=True, help="path to JSON specs (or - for stdin)")
    args = p.parse_args()

    raw = sys.stdin.read() if args.input == "-" else Path(args.input).read_text()
    specs = json.loads(raw)
    if not isinstance(specs, list):
        sys.stderr.write("input must be a JSON array of task specs\n")
        return 2

    spawned_at_cache: dict[str, str] = {}

    def spawned_at(parent_sha: str) -> str:
        if parent_sha in spawned_at_cache:
            return spawned_at_cache[parent_sha]
        s = store.latest_status(parent_sha)
        if s is None:
            sys.stderr.write(f"parent {parent_sha[:16]} has no status\n")
            sys.exit(5)
        spawned_at_cache[parent_sha] = s["text_sha256"]
        return spawned_at_cache[parent_sha]

    created = healed = skipped = 0
    for spec in specs:
        slug = spec["slug"]
        title = spec["title"]
        text = spec["text"]
        parent = spec.get("parent")
        deps = spec.get("depends_on") or []
        verifier = spec.get("verifier") or None
        sticky = bool(spec.get("sticky"))
        workdir_override = spec.get("workdir")

        # Inherit sticky + workdir from parent (matches plan.py behavior).
        parent_task = store.get_task(parent) if parent else None
        if parent_task and not sticky and store.task_is_sticky(parent_task):
            sticky = True
        if workdir_override:
            workdir = workdir_override
        else:
            workdir = resolve_workdir(parent_task)

        existing = store.find_task_by_slug(args.queue, slug)
        if existing:
            sha = existing["text_sha256"]
            if store.latest_status(sha) is None:
                store.append_status(sha, "new", note=f"enqueued: {title}")
                healed += 1
                outcome = "healed"
            else:
                skipped += 1
                outcome = "skipped"
            print(f"{sha}\t{slug}\t{outcome}")
            continue

        try:
            task = store.create_task(
                queue=args.queue,
                title=title,
                text=text,
                slug=slug,
                parent_task_sha=parent,
                spawned_at_status_sha=spawned_at(parent) if parent else None,
                depends_on=deps,
                verifier=verifier,
                sticky=sticky,
                workdir=workdir,
            )
            sha = task["text_sha256"]
            store.append_status(sha, "new", note=f"enqueued: {title}")
            created += 1
            print(f"{sha}\t{slug}\tcreated")
        except Exception as exc:  # hard failure: stop
            sys.stderr.write(f"FAIL slug={slug}: {exc}\n")
            sys.stderr.write(f"created={created} healed={healed} skipped={skipped}\n")
            return 1

    sys.stderr.write(f"--done-- created={created} healed={healed} skipped={skipped}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

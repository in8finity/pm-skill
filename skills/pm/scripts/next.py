#!/usr/bin/env python3
"""Return the next runnable task as JSON, or ``null`` if none.

A task is runnable when:
  - its current status is ``new``
  - every task in ``links.dependsOn`` has current status ``done``
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
    args = p.parse_args()

    here = caller_workdir()

    tasks = store.list_tasks(args.queue)
    tasks.sort(key=lambda t: t.get("created_at", ""))

    status_cache: dict[str, str | None] = {}

    def status_of(sha: str) -> str | None:
        if sha not in status_cache:
            status_cache[sha] = store.status_value(store.latest_status(sha))
        return status_cache[sha]

    for t in tasks:
        sha = t["text_sha256"]
        bound = store.task_workdir(t)
        if bound is not None and bound != here:
            continue
        if status_of(sha) != "new":
            continue
        deps = (t.get("links") or {}).get("dependsOn") or []
        if any(status_of(d) != "done" for d in deps):
            continue
        print(json.dumps(t, indent=2))
        return 0

    print("null")
    return 0


if __name__ == "__main__":
    sys.exit(main())

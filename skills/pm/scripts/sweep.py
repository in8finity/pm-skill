#!/usr/bin/env python3
"""Scan a queue for zombie tasks (working but stale) and reclaim them.

A task is considered stale when its last activity (latest TaskStatus,
TaskReport, or TaskHeartbeat by ``created_at``) is older than ``--ttl``
seconds. Reclaim appends a TaskStatus(new, reclaimed=true) so next.py
returns the task to the pool.

This is the runtime counterpart of the Reclaim transition in
system-models/planning_lease.als.

Usage:
  sweep.py [--queue Q] [--ttl SECONDS] [--reclaimer ID] [--dry-run]

Exit codes:
  0 — sweep complete (with or without reclaims)

Output: JSON summary { "scanned", "stale", "reclaimed": [...] }.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from datetime import datetime, timezone

import store


def parse_iso(ts: str) -> datetime:
    # Accepts ...Z or ...+00:00
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts)


def default_reclaimer() -> str:
    return os.environ.get("PM_AGENT_ID") or f"sweeper@{socket.gethostname()}"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--queue", default="default")
    p.add_argument("--ttl", type=int, default=300,
                   help="seconds since last activity before a task is stale (default 300)")
    p.add_argument("--reclaimer", default=default_reclaimer(),
                   help="identifier recorded in attributes.reclaimer")
    p.add_argument("--dry-run", action="store_true",
                   help="report what would be reclaimed without writing")
    args = p.parse_args()

    now = datetime.now(timezone.utc)
    tasks = store.list_tasks(args.queue)

    scanned = 0
    stale: list[dict[str, str]] = []
    reclaimed: list[dict[str, str]] = []

    for t in tasks:
        scanned += 1
        sha = t["text_sha256"]
        current = store.latest_status(sha)
        if not current or store.status_value(current) != "working":
            continue

        last_iso = store.last_activity_at(sha)
        if not last_iso:
            continue
        age = (now - parse_iso(last_iso)).total_seconds()
        if age <= args.ttl:
            continue

        slug = (t.get("attributes") or {}).get("slug", "?")
        entry = {
            "task": sha,
            "slug": slug,
            "last_activity": last_iso,
            "age_seconds": int(age),
        }
        stale.append(entry)
        if args.dry_run:
            continue
        result = store.reclaim(
            sha,
            reason=f"no activity for {int(age)}s (ttl={args.ttl})",
            reclaimer=args.reclaimer,
        )
        reclaimed.append({**entry, "reclaim_status_sha": result["text_sha256"]})

    print(json.dumps(
        {
            "scanned": scanned,
            "stale": len(stale),
            "reclaimed": reclaimed if not args.dry_run else stale,
            "dry_run": args.dry_run,
            "ttl": args.ttl,
        },
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())

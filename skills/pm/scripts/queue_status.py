#!/usr/bin/env python3
"""Print counts by latest status for a planning queue.

Usage:
  queue_status.py [--queue Q] [--json]

Default output (text, columns aligned):
    queue=default tasks=136
      new       125
      working     1
      done       10
      rejected    0
      orphan      0       (Tasks with no TaskStatus — run heal_orphans.py)

With --json: emits {"queue": "...", "total": N, "counts": {...},
"orphans": [{"slug","sha"}, ...]}.
"""
from __future__ import annotations

import argparse
import json
import sys

import store


BUCKETS = ("new", "working", "done", "rejected")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--queue", default="default")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    counts = {b: 0 for b in BUCKETS}
    counts["orphan"] = 0
    orphan_list: list[dict[str, str]] = []
    other: dict[str, int] = {}

    tasks = store.list_tasks(args.queue)
    # One bulk tip lookup instead of one (two-round-trip) latest_status
    # per task — the hot path that made `pm status` O(N) on a big store.
    status_map = store.bulk_status_values([t["text_sha256"] for t in tasks])
    for t in tasks:
        sha = t["text_sha256"]
        slug = (t.get("attributes") or {}).get("slug") or t.get("title", "")
        v = status_map.get(sha)
        if v is None:
            counts["orphan"] += 1
            orphan_list.append({"slug": slug, "sha": sha})
            continue
        if v in counts:
            counts[v] += 1
        else:
            other[v] = other.get(v, 0) + 1

    if args.json:
        out = {
            "queue": args.queue,
            "total": len(tasks),
            "counts": {**counts, **other},
            "orphans": orphan_list,
        }
        print(json.dumps(out, indent=2))
        return 0

    print(f"queue={args.queue} tasks={len(tasks)}")
    for b in BUCKETS:
        print(f"  {b:<9} {counts[b]:>4}")
    print(f"  {'orphan':<9} {counts['orphan']:>4}"
          f"{'   (run heal_orphans.py)' if counts['orphan'] else ''}")
    for k, v in sorted(other.items()):
        print(f"  {k:<9} {v:>4}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

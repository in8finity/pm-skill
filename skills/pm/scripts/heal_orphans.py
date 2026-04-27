#!/usr/bin/env python3
"""Find Tasks in a queue that have no TaskStatus and append a genesis ``new``.

Plan operations are not atomic across (create_task, append_status); a crash
between the two leaves an orphan Task. This script is the recovery tool.

Usage:
  heal_orphans.py [--queue Q] [--dry-run]

Output (TSV per healed/found task on stdout):
    <text_sha256>\t<slug>\t<orphan|healed>
"""
from __future__ import annotations

import argparse
import sys

import store


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--queue", default="default")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    orphans = []
    for t in store.list_tasks(args.queue):
        if store.latest_status(t["text_sha256"]) is None:
            orphans.append(t)

    for t in orphans:
        sha = t["text_sha256"]
        slug = (t.get("attributes") or {}).get("slug") or t.get("title", "")
        if args.dry_run:
            print(f"{sha}\t{slug}\torphan")
            continue
        store.append_status(sha, "new", note=f"enqueued: {t.get('title','')}")
        print(f"{sha}\t{slug}\thealed")

    sys.stderr.write(
        f"queue={args.queue} orphans={len(orphans)} "
        f"{'(dry-run)' if args.dry_run else 'healed'}\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

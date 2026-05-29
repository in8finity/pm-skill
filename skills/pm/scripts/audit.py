#!/usr/bin/env python3
"""pm audit — cryptographic integrity attestation of a planning queue.

What this proves
================
For every record in scope (the queue's Task records plus each per-task
TaskStatus / TaskReport / TaskHeartbeat chain), the hashharness backend
re-checks:

  - the record's content hash and chain links are intact,
  - the record validates against its bound schema,
  - the chain ordering and `chain_predecessor` CAS history is consistent.

Unlike ``verify_chain`` (which followed a single named root's reachable
set), ``verify_work_package`` checks **every** record in each work
package — so it catches an orphan TaskStatus, a tampered payload, or a
``done`` slipped in without its report.

Use it as a supervisor sweep ("before I trust this batch of agent
output, prove the audit trail is intact") or a periodic integrity check.

Usage
-----
  pm audit                       # whole instance — every planning:* wp
  pm audit --queue Q             # one queue (queue wp + its task chains)
  pm audit --json                # JSON payload instead of human summary
  pm audit --verbose             # ask backend for full per-item errors,
                                 #   not just counts

Exit codes
----------
  0 — every work package verified clean
  1 — at least one work package reported errors
  2 — backend unreachable / RPC error (propagated from mcp_client)
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import mcp_client
import store


def collect_queue_wps(queue: str) -> list[str]:
    """Queue wp plus every per-task wp in that queue. One ``list_tasks``
    call; per-task wps are derived deterministically from the task sha."""
    wps = [store.queue_wp(queue)]
    for t in store.list_tasks(queue):
        wps.append(store.task_wp(t["text_sha256"]))
    return wps


def collect_instance_wps() -> list[str]:
    """Every ``planning:*`` work package the backend knows about."""
    res = mcp_client.tool("list_work_packages", {"prefix": "planning:"})
    if isinstance(res, dict):
        wps = res.get("work_package_ids") or res.get("work_packages") or []
    elif isinstance(res, list):
        wps = res
    else:
        wps = []
    return [wp for wp in wps if isinstance(wp, str)]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--queue", default=None,
                   help="scope the audit to a single queue (queue wp + its "
                        "per-task chains). Omit to audit every "
                        "planning:* work package the backend knows.")
    p.add_argument("--json", action="store_true",
                   help="emit the verify_work_package payload verbatim "
                        "instead of a human-readable summary.")
    p.add_argument("--verbose", action="store_true",
                   help="ask the backend to surface per-item errors, not "
                        "just error counts (passes summary=False).")
    p.add_argument("--chunk", type=int, default=50,
                   help="how many work packages to verify per round trip. "
                        "Audit is sequential server-side, so a whole-"
                        "instance call on a seasoned store can blow the "
                        "30s socket timeout in one shot. Chunking keeps "
                        "each call responsive at the cost of one round "
                        "trip per chunk. Default 50.")
    args = p.parse_args(argv)

    if args.queue:
        wps = collect_queue_wps(args.queue)
        scope_label = f"queue={args.queue}"
    else:
        wps = collect_instance_wps()
        scope_label = "instance (all planning:* work packages)"

    if not wps:
        sys.stderr.write(f"pm audit: no work packages found for scope {scope_label}\n")
        return 0

    # Chunked verify so a whole-instance audit on a large store doesn't
    # blow the socket timeout in a single batch.
    merged_results: dict[str, Any] = {}
    merged_ok = True
    for i in range(0, len(wps), args.chunk):
        batch = wps[i:i + args.chunk]
        part = mcp_client.tool(
            "verify_work_package",
            {"work_package_ids": batch, "summary": not args.verbose},
        ) or {}
        if not part.get("ok"):
            merged_ok = False
        merged_results.update(part.get("results") or {})
    res: dict[str, Any] = {
        "ok": merged_ok,
        "checked_work_packages": len(merged_results),
        "results": merged_results,
    }

    if args.json:
        print(json.dumps(res, indent=2))
        return 0 if res.get("ok") else 1

    ok = bool(res.get("ok"))
    checked = res.get("checked_work_packages", len(wps))
    results = res.get("results") or {}

    failed = [
        (wp_id, r) for wp_id, r in results.items() if not r.get("ok")
    ]
    total_items = sum(int(r.get("checked_items") or 0) for r in results.values())

    print(f"pm audit: {scope_label}")
    print(f"  work_packages_checked = {checked}")
    print(f"  records_checked       = {total_items}")
    print(f"  result                = {'OK' if ok else 'FAIL'}")

    if failed:
        print(f"  failed work packages ({len(failed)}):")
        for wp_id, r in failed:
            errs = r.get("errors_count", "?")
            checked_items = r.get("checked_items", "?")
            print(f"    - {wp_id}  errors={errs}  checked={checked_items}")
            if args.verbose:
                for err in (r.get("errors") or [])[:5]:
                    print(f"        {err}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

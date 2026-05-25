#!/usr/bin/env python3
"""Extract the `## Affected files` block from a task's TaskReport(s).

Convention used by script-path verifiers (`pm finished` runs an
external check). The worker's report declares which files the gate
should apply to via a structured block:

    ## Affected files

    sources/walton/latash-2010/ideas-and-claims/movement-control.yaml
    sources/walton/latash-2010/ideas-and-claims/synergies.yaml

The block may use a bulleted list (``- path``) or bare lines. Blank
lines and comment lines (``# foo``) inside the block are ignored.
The block ends at the next ``## `` heading or EOF.

Usage:
  report_files.py --task SHA [--report SHA] [--all-reports]
                  [--json | --null]

Resolution (mutually exclusive):
  --report SHA   — extract from this specific report
  --task SHA     — extract from the LATEST report on the task (default,
                   most common pattern for verifier scripts that run
                   right after `pm report`)
  --all-reports  — extract from every report on the task, deduplicated,
                   in order. Use when an iterative worker emits files
                   across multiple reports.

Output forms:
  default        — one path per line
  --null         — NUL-separated (``xargs -0``-friendly)
  --json         — JSON array on a single line

Exit codes:
  0  one or more files printed (or empty list on stdout)
  2  usage error
  6  task not found
  7  no TaskReport on the task (or named --report not found)

Worked example for a verifier script (script-path form):

    #!/usr/bin/env bash
    # /path/to/check-rationale-banlist.sh — passed to `pm plan --verifier`
    set -e
    files=$(skills/pm/scripts/pm report-files --task "$PM_TASK" \\
            --report "$PM_REPORT_SHA")
    rc=0
    while IFS= read -r f; do
        [[ -z "$f" ]] && continue
        it-tools/scripts/check-rationale-banlist.sh "$f" || rc=1
    done <<< "$files"
    exit $rc

`pm finished` runs this with PM_TASK / PM_REPORT_SHA / PM_QUEUE /
PM_SLUG / PM_VERIFIER in env and (task_sha, report_sha) as positional
args. Exit 0 from the script → done; non-zero → exit 9 from
``pm finished``, task stays ``working``.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

import store


AFFECTED_HEADING = re.compile(r"^\s*##\s+Affected files\s*$", re.IGNORECASE)
ANY_HEADING = re.compile(r"^\s*##\s+")
BULLET = re.compile(r"^\s*-\s+")
COMMENT_OR_BLANK = re.compile(r"^\s*(?:#.*)?$")


def extract_paths(body: str) -> list[str]:
    """Parse the body of one TaskReport; return the list of paths in
    its ``## Affected files`` block (empty list if the block is
    missing). Stops at the next ``## `` heading or EOF."""
    out: list[str] = []
    in_block = False
    for line in body.splitlines():
        if AFFECTED_HEADING.match(line):
            in_block = True
            continue
        if not in_block:
            continue
        if ANY_HEADING.match(line) and not AFFECTED_HEADING.match(line):
            # Next section — block ends here.
            break
        if COMMENT_OR_BLANK.match(line):
            continue
        cleaned = BULLET.sub("", line).strip()
        if cleaned:
            out.append(cleaned)
    return out


def emit(paths: list[str], *, as_json: bool, null: bool) -> None:
    if as_json:
        print(json.dumps(paths))
        return
    sep = "\0" if null else "\n"
    if paths:
        sys.stdout.write(sep.join(paths))
        if not null:
            sys.stdout.write("\n")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--task", help="task text_sha256")
    p.add_argument("--report",
                   help="report text_sha256 (default: latest report on --task)")
    p.add_argument("--all-reports", action="store_true",
                   help="union of every report on the task, deduplicated, "
                        "in order of report appearance")
    fmt = p.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true",
                     help="emit JSON array")
    fmt.add_argument("--null", action="store_true",
                     help="NUL-separate paths (xargs -0 friendly)")
    args = p.parse_args()

    if not args.task and not args.report:
        sys.stderr.write("usage: provide --task or --report\n")
        return 2

    if args.report:
        # Direct fetch by text_sha256. We use the existing helper rather
        # than walking the chain.
        from mcp_client import tool  # local import to keep top-level small
        rec = tool("get_item_by_hash", {"text_sha256": args.report})
        if not isinstance(rec, dict) or rec.get("type") != "TaskReport":
            sys.stderr.write(f"no TaskReport with text_sha256={args.report[:12]}\n")
            return 7
        emit(extract_paths(rec.get("text") or ""),
             as_json=args.json, null=args.null)
        return 0

    # --task: validate the task exists.
    if store.get_task(args.task) is None:
        sys.stderr.write(f"task {args.task[:12]} not found\n")
        return 6

    reports = store.list_reports(args.task) if hasattr(store, "list_reports") \
        else _list_reports_fallback(args.task)
    if not reports:
        sys.stderr.write(f"no TaskReport on task {args.task[:12]}\n")
        return 7

    if args.all_reports:
        seen: set[str] = set()
        merged: list[str] = []
        for r in reports:
            for path in extract_paths(r.get("text") or ""):
                if path not in seen:
                    seen.add(path)
                    merged.append(path)
        emit(merged, as_json=args.json, null=args.null)
        return 0

    latest = reports[-1]
    emit(extract_paths(latest.get("text") or ""),
         as_json=args.json, null=args.null)
    return 0


def _list_reports_fallback(task_sha: str) -> list[dict]:
    """Fetch every TaskReport on the task ordered oldest→newest.
    Mirrors show.py's `_chain`; kept local so we don't reach into
    private helpers there."""
    import mcp_client
    res = mcp_client.tool("get_work_package", {
        "work_package_id": store.task_wp(task_sha),
        "type": "TaskReport",
    })
    if isinstance(res, dict):
        items = res.get("items") or []
    elif isinstance(res, list):
        items = res
    else:
        items = []
    items.sort(key=lambda it: it.get("created_at") or "")
    return items


if __name__ == "__main__":
    sys.exit(main())

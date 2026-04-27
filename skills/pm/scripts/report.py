#!/usr/bin/env python3
"""Append a TaskReport (proof of work) to a task. Reports are chained.

Usage:
  report.py --task SHA --title T (--text BODY | --text-file PATH)

Exit codes:
  0  — report appended
  2  — usage error
  10 — sticky-context refusal: task is bound to a different PM_CONTEXT_ID
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import store


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--task", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--text")
    p.add_argument("--text-file")
    args = p.parse_args()

    if args.text and args.text_file:
        sys.stderr.write("provide --text or --text-file, not both\n")
        return 2
    text = args.text if args.text is not None else (
        Path(args.text_file).read_text() if args.text_file else None
    )
    if not text:
        sys.stderr.write("missing report body\n")
        return 2

    bound = store.status_context_id(store.latest_status(args.task))
    if bound:
        agent_context = os.environ.get("PM_CONTEXT_ID") or None
        if agent_context != bound:
            sys.stderr.write(
                f"refusing: task {args.task[:12]} is sticky-bound to "
                f"context {bound[:12]}; PM_CONTEXT_ID is "
                f"'{(agent_context or '<unset>')[:12]}'\n"
            )
            return 10

    report = store.append_report(args.task, args.title, text)
    if not isinstance(report, dict) or report.get("type") != "TaskReport":
        # mcp_client.tool returns the error message as a string when the
        # backend rejects (legacy `_unwrap_content` behavior); surface
        # it with a clear non-zero exit instead of silently succeeding.
        sys.stderr.write(
            f"refusing: append_report failed: {report!r}\n"
        )
        return 8
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

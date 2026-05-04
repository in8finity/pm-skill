#!/usr/bin/env python3
"""Assemble a task body for one step extracted from another skill's
SKILL.md, so a worker reading the body in isolation has everything
needed to execute the step — no need to round-trip through the source
SKILL.md to learn the contract.

This closes the "Substep EIAC-S7." anti-pattern: the orchestrator (an
LLM driving pm-{auto,assisted,guided}-skill-execution) doesn't compose
the body itself; it calls this helper, which builds a structured body
from the extractor's JSON output + the verbatim source lines. No LLM
shortcut is possible — the body is generated, not improvised.

Usage:
  build_task_body.py --steps STEPS_JSON \\
                     --step <n> \\
                     --prompt "<original problem statement>" \\
                     --mode <auto|assisted|guided> \\
                     [--workdir <path>]

  STEPS_JSON is the file produced by `pm extract-steps` with
  validation enabled (so each step has line_number + body_lines).
  --step matches the step's `n` field (e.g. "9", "9.formal-debugger.1").

Output (stdout): the task body string. Pipe into a bulk-plan JSON
construction:

  pm extract-steps formal-modeling --max-depth 0 > /tmp/steps.json
  body=$(pm build-task-body --steps /tmp/steps.json --step 9 \\
         --prompt "verify the auth state machine" --mode auto \\
         --workdir /Users/me/projects/auth)
  jq -nc --arg b "$body" '[{slug:"step-9-run-loop",title:"Step 9",text:$b}]' \\
    | pm bulk-plan --queue Q --input -

Body shape (deterministic — workers learn to scan it):
  Driving skill: <name>
  Driving skill path: <abs path to SKILL.md>
  Step number: <n>
  Step title: <title>
  Skill anchor: <verbatim anchor line from SKILL.md>
  Reference: <skill_path>:<line_number>
  Step lines: <start>-<end>      (inclusive 1-indexed)
  Subskills invoked: <comma-separated, or "(none)">
  Mode: <auto|assisted|guided>
  Workdir: <path or "(inherit)">
  Prompt: <orchestrator's problem statement>

  ----- Step spec (verbatim from SKILL.md lines <start>-<end>) -----
  <verbatim source text>
  -----------------------------------------------------------------

Exit codes:
  0  body printed to stdout
  2  usage error (missing inputs, JSON parse failure)
  6  step <n> not found in STEPS_JSON
  7  step found but lacks line_number/body_lines (extract with validation)
  8  source SKILL.md not readable
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def find_step(steps: list[dict], step_id: str) -> dict | None:
    """Walk the (possibly nested) step tree and return the first step
    whose `n` matches step_id."""
    for s in steps:
        if s.get("n") == step_id:
            return s
        for nested in (s.get("nested") or []):
            sub = find_step(nested.get("steps") or [], step_id)
            if sub is not None:
                return sub
    return None


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--steps", required=True,
                   help="path to JSON produced by `pm extract-steps`")
    p.add_argument("--step", required=True,
                   help="value of the step's `n` field, e.g. '9' or '9.formal-debugger.1'")
    p.add_argument("--prompt", required=True,
                   help="original problem statement / objective the skill is being applied to")
    p.add_argument("--mode", default="auto",
                   choices=("auto", "assisted", "guided"),
                   help="execution mode the worker should use (default: auto)")
    p.add_argument("--workdir", default="",
                   help="absolute workdir for the task (default: inherit from caller)")
    args = p.parse_args()

    try:
        with open(args.steps) as fh:
            data = json.load(fh)
    except Exception as exc:
        sys.stderr.write(f"can't read --steps file {args.steps!r}: {exc}\n")
        return 2

    skill_name = data.get("skill") or "?"
    skill_path = data.get("skill_path") or ""
    if not skill_path:
        sys.stderr.write("STEPS_JSON missing skill_path; can't read source\n")
        return 2

    step = find_step(data.get("steps") or [], args.step)
    if step is None:
        sys.stderr.write(f"step {args.step!r} not found in {args.steps}\n")
        return 6

    line = step.get("line_number")
    body_lines = step.get("body_lines")
    if not line or not body_lines:
        sys.stderr.write(
            f"step {args.step!r} has no line_number/body_lines — re-extract "
            f"with validation enabled (the default for `pm extract-steps`)\n"
        )
        return 7

    try:
        src_lines = Path(skill_path).read_text().splitlines()
    except Exception as exc:
        sys.stderr.write(f"can't read source SKILL.md {skill_path!r}: {exc}\n")
        return 8

    start, end = body_lines
    # Clamp defensively in case the source changed between extract and build.
    start = max(1, min(start, len(src_lines)))
    end   = max(start, min(end, len(src_lines)))
    spec_text = "\n".join(src_lines[start-1:end])

    subskills = step.get("subskills_invoked") or []
    subs_field = ", ".join(subskills) if subskills else "(none)"

    workdir_field = args.workdir or "(inherit)"

    body = (
        f"Driving skill: {skill_name}\n"
        f"Driving skill path: {skill_path}\n"
        f"Step number: {step.get('n')}\n"
        f"Step title: {step.get('title') or '?'}\n"
        f"Skill anchor: {step.get('anchor') or '(none)'}\n"
        f"Reference: {skill_path}:{line}\n"
        f"Step lines: {start}-{end}\n"
        f"Subskills invoked: {subs_field}\n"
        f"Mode: {args.mode}\n"
        f"Workdir: {workdir_field}\n"
        f"Prompt: {args.prompt}\n"
        f"\n"
        f"----- Step spec (verbatim from SKILL.md lines {start}-{end}) -----\n"
        f"{spec_text}\n"
        f"-----------------------------------------------------------------\n"
    )

    sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())

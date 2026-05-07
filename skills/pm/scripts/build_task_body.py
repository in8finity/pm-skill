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

Two body shapes are supported:

- `--mode {auto|assisted|guided}` (default): per-step worker body.
  Requires `--step <n>`; emits the verbatim spec for that step.

- `--mode parent`: lightweight grouping/contexting parent body.
  Does NOT carry work instructions. Lists the children planned under
  this parent and embeds the `Role: parent` marker that
  `pm bulk-plan` lints for. Use this when planning a parent task
  that wraps a per-step expansion (see `skills/pm/plan/SKILL.md`
  "Parents are grouping nodes"). `--step` is ignored.

Usage:
  build_task_body.py --steps STEPS_JSON \\
                     --step <n> \\
                     --prompt "<original problem statement>" \\
                     --mode <auto|assisted|guided> \\
                     [--workdir <path>]

  build_task_body.py --steps STEPS_JSON \\
                     --mode parent \\
                     --prompt "<original problem statement>" \\
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
    p.add_argument("--step", default=None,
                   help="value of the step's `n` field, e.g. '9' or '9.formal-debugger.1'. "
                        "Required for --mode {auto,assisted,guided}; ignored for --mode parent.")
    p.add_argument("--prompt", required=True,
                   help="original problem statement / objective the skill is being applied to")
    p.add_argument("--mode", default="auto",
                   choices=("auto", "assisted", "guided", "parent"),
                   help="execution mode the worker should use (default: auto). "
                        "Use 'parent' to emit a lightweight grouping-node body "
                        "(no work instructions; lists children) — required by "
                        "the `pm bulk-plan` parent-body lint.")
    p.add_argument("--workdir", default="",
                   help="absolute workdir for the task (default: inherit from caller)")
    p.add_argument("--child-slug-prefix", default="step-",
                   help="prefix used to derive each child's slug from its step `n` "
                        "field when --mode=parent. Default 'step-' produces "
                        "slugs like 'step-1', 'step-2'. Set to '' to use the raw "
                        "step n.")
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

    workdir_field = args.workdir or "(inherit)"

    if args.mode == "parent":
        top_steps = data.get("steps") or []
        if not top_steps:
            sys.stderr.write("STEPS_JSON has no top-level steps; can't list children\n")
            return 6
        prefix = args.child_slug_prefix
        children_lines = []
        for s in top_steps:
            n = s.get("n") or "?"
            title = s.get("title") or "?"
            slug = f"{prefix}{n}"
            children_lines.append(f"  - {slug}: {title}")
        children_block = "\n".join(children_lines)

        body = (
            f"Driving skill: {skill_name}\n"
            f"Driving skill path: {skill_path}\n"
            f"Role: parent\n"
            f"Mode: parent\n"
            f"Workdir: {workdir_field}\n"
            f"Prompt: {args.prompt}\n"
            f"\n"
            f"This task is a GROUPING / CONTEXTING node — do NOT execute the\n"
            f"skill in this body. Per `skills/pm/plan/SKILL.md` \"Parents are\n"
            f"grouping nodes\", the actual work lives in the children listed\n"
            f"below. As the worker that claimed this parent:\n"
            f"\n"
            f"  - Hold the lifecycle / sticky-context lease.\n"
            f"  - Do NOT replicate the children's work here. Each child runs\n"
            f"    in its own claim and produces its own TaskReport.\n"
            f"  - Run `pm finished` on this parent only after every child\n"
            f"    reaches {{done, rejected, superseded}} (`pm finished`\n"
            f"    refuses with exit 14 otherwise).\n"
            f"  - If a queue-level rollup is needed, it should already be\n"
            f"    planned as a final child that depends on every sibling —\n"
            f"    not produced here.\n"
            f"\n"
            f"Children planned under this parent:\n"
            f"{children_block}\n"
        )
        sys.stdout.write(body)
        return 0

    if not args.step:
        sys.stderr.write("--step is required for --mode={auto,assisted,guided}\n")
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

    # Modes that allow iteration / mid-step subtasks get a mechanical
    # footer listing the queue actions available to the worker — so the
    # body itself teaches the contract instead of relying on the worker
    # having read the orchestrator's SKILL.md. Auto mode is excluded:
    # it rejects gates rather than iterating, so the toolkit doesn't
    # apply.
    if args.mode in ("guided", "assisted"):
        body += (
            f"\n"
            f"----- Mid-step iteration toolkit (queue actions you may take) -----\n"
            f"You are claiming this task as a worker. If the step's prescribed\n"
            f"dialogue produces a sub-question, fails a gate, or otherwise\n"
            f"requires iteration, prefer queue actions over inline improvisation.\n"
            f"All commands take `--task <sha>`; THIS task's sha is the one you\n"
            f"just claimed via `pm executing` / `pm pull`.\n"
            f"\n"
            f"  Open a sub-question subtask (defer/branch from this step):\n"
            f"    pm plan --queue <queue> --parent <this-task-sha> \\\n"
            f"            --slug <stable-id> --title \"...\" --text \"...\"\n"
            f"    # Then `pm executing --task <subtask-sha>` to claim it.\n"
            f"    # Resume the parent step after the subtask reaches done.\n"
            f"\n"
            f"  Checkpoint partial progress (without finishing):\n"
            f"    pm report --task <this-task-sha> --title \"...\" --text \"...\"\n"
            f"\n"
            f"  Retry from the start of this step (gate failed, iteration needed):\n"
            f"    pm replan --task <this-task-sha>   # appends fresh `new` status\n"
            f"\n"
            f"  Abandon an exploratory subtask (parent stays open):\n"
            f"    pm cancel --task <subtask-sha> --reason \"...\"\n"
            f"\n"
            f"  Reject this step (precondition won't be supplied; do not skip silently):\n"
            f"    pm finished --task <this-task-sha> --rejected\n"
            f"    # Always pair with a `pm report` explaining why first.\n"
            f"\n"
            f"  Heartbeat (long-running step holding a sticky lease):\n"
            f"    pm heartbeat --task <this-task-sha>\n"
            f"\n"
            f"Constraints:\n"
            f"  - Subtasks inherit sticky+workdir from this parent automatically.\n"
            f"  - This step cannot `pm finished` until every subtask you opened\n"
            f"    is in {{done, rejected, superseded}} (exit 14 otherwise).\n"
            f"  - Record every iteration / subtask in the final TaskReport so the\n"
            f"    queue stays auditable.\n"
            f"-------------------------------------------------------------------\n"
        )

    sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())

---
name: planning-plan
description: >
  Enqueue a new task on the planning board backed by hashharness. Creates an
  immutable Task record plus an initial TaskStatus(new). Supports subtasks
  (link to parent + parent's status at decision point) and dependency links
  to other tasks. Use when the user says "plan", "queue", "enqueue", "add
  task", or "create subtask".
---

# planning:plan — enqueue a task

## Inputs

- `--title` — short human label (required)
- `--text` — full task description (required)
- `--queue` — board name; default `default`
- `--slug` — stable id within the queue; auto-derived from title if omitted
- `--parent <task-sha>` — when set, makes this a subtask. The new Task links
  `parentTask` → parent and `spawnedAt` → the parent's *current* TaskStatus
  (the decision point at which the subtask was initiated).
- `--depends-on sha[,sha...]` — dependency tasks. The task is not eligible
  for `next` until each dependency reaches status `done`.
- `--verifier <spec>` — optional gate that `pm finished` runs before
  allowing the done transition. Forms:
  - **`skill:<skill-name>`** — *self-attestation* (default for
    skill-based checks). The worker is contractually required to
    apply the named skill against their own work and embed a
    `## Verifier Attestation` block in the TaskReport (with
    `verifier:` matching this string verbatim, `verdict: PASS|FAIL`,
    and `evidence:`). `pm finished` parses the block and gates on
    the verdict — it does NOT spawn a separate LLM. Use this when
    the criterion is encapsulated as a skill the worker can run.
  - **`prompt:<criterion>`** — *self-attestation* with a free-form
    criterion. Same attestation contract as `skill:` above; the
    worker must reason about the criterion and record their verdict
    in the report's attestation block.
  - **`verify-skill:<skill-name>`** / **`verify-prompt:<criterion>`** —
    opt-in: `pm finished` spawns `claude -p` as an *independent*
    subprocess that re-judges the task + report. Higher cost; useful
    when self-attestation isn't trusted enough. Requires the
    `claude` CLI on PATH (otherwise exit 127, task stays in
    `working`). The LLM must terminate with `VERDICT: PASS` or
    `VERDICT: FAIL: <reason>`.
  - **`<absolute path>`** (or shell-prefixed command, e.g.
    `env FOO=bar /path/to/check.sh`) — spawn a subprocess. The
    script receives env `PM_TASK`, `PM_REPORT_SHA`, `PM_QUEUE`,
    `PM_SLUG`, `PM_VERIFIER` and positional `<task-sha>
    <report-sha>`. Exit 0 = pass, non-zero = fail.

  Failure (any form) exits `pm finished` with code 9, leaving the
  task in `working`. `--rejected` and `--skip-verifier` bypass.
  Closes the "hollow proof-of-work" gap — see
  `system-models/reports/planning-blind-spots.md` #3.

## Procedure

1. Ensure the schema is registered (run once per data dir):
   `../scripts/pm setup`
2. Create the task and genesis status:
   `../scripts/pm plan --title "..." --text "..." [--queue Q] [--parent SHA] [--depends-on SHA,SHA]`
3. Output is `{ "task": ..., "status": ... }` — record `task.text_sha256`;
   it is the identifier used by the other planning skills.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Task created (or self-healed by appending genesis status to an
existing slug-key Task) |
| 4 | Slug already taken in this queue (either pre-check found it, or a
concurrent `plan` claimed it first) |
| 5 | `--parent` was provided but parent has no status yet |

## Slug uniqueness (structurally enforced)

Two parallel `plan` invocations with the same `(queue, slug)` cannot both
succeed. `Task.text` is set to the canonical key `task:<queue>/<slug>`,
so `text_sha256` is determined solely by the slug. hashharness rejects
duplicate `text_sha256`, raising `SlugTaken`; `plan.py` catches it and
exits 4. The user's free-form description lives in `attributes.body`.

This is the structural gate that `system-models/planning_plan_race.als`'s
`UniqueSlugInQueue` assertion verifies.

## Notes

- Every task is automatically scoped to the planner's workdir:
  `task.attributes.workdir = realpath(getcwd())` (or `$PM_WORKDIR` if
  set). Subtasks inherit the parent's workdir. `pm next` filters out
  tasks whose workdir doesn't match the worker's, so a worker started
  in `~/projects/A` only sees tasks planned from that workspace. To
  plan a task that any worker should be able to claim regardless of
  cwd, set `PM_WORKDIR=` to an empty string before running `pm plan`
  (the resulting task will have no `workdir` attribute and matches
  every workspace, like legacy pre-feature tasks).
- Task records use `work_package_id = planning:<queue>`.
- TaskStatus and TaskReport records use `work_package_id = planning:task:<task-sha>`
  so `find_tip` returns the latest status / report per task in O(1).
- The genesis status is `new`. To begin work, use `planning:executing`.

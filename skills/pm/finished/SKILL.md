---
name: planning-finished
description: >
  Mark a task as done (or rejected) on the planning board. Appends a
  terminal TaskStatus that links proof -> latest TaskReport on the task.
  Refuses if no TaskReport exists, enforcing "task to be finished requires
  proof of work/report". If the Task declares a verifier at plan time,
  this script runs it before allowing `done`. Use when an agent has
  submitted its final report and wants to close the task; pass
  --rejected to close as failed instead.
---

# planning:finished — close a task with proof

## Procedure

`../scripts/pm finished --task <task-sha> [--rejected] [--note "..."]`

- Reads the latest TaskReport for the task. Refuses with exit code 7 if
  none exists.
- If the Task has `attributes.verifier` and the terminal state is
  `done`, runs that verifier against the latest report before appending
  the terminal status. Non-zero verifier exit refuses the transition and
  leaves the task in `working` (exit code 9). `--rejected` bypasses this
  gate because rejection does not claim successful work.
- Appends a TaskStatus with:
  - `attributes.status = "done"` (or `"rejected"` with `--rejected`)
  - `links.task = <task-sha>`
  - `links.prevStatus = <latest-status-sha>`
  - `links.proof = <latest-report-sha>`  ← proof of work
- Allowed previous statuses: `working` or `new`. Refuses on `done` /
  `rejected` (terminal).

## Why proof is mandatory

A `done` status without an attached TaskReport is meaningless on an
append-only board: there is no way to audit *what* was done. This script
makes the proof link a hard precondition.

## Verifier gate

When a task was planned with `--verifier <spec>`, "proof exists" is
not sufficient for `done`: the verifier must also pass against the
latest report. Three forms are supported:

- **`skill:NAME`** / **`prompt:CRITERION`** — *self-attestation*
  (default). The worker is expected to apply the skill / prompt and
  embed a `## Verifier Attestation` block in the TaskReport with:

  ```
  ## Verifier Attestation

  verifier: <verbatim copy of task.attributes.verifier>
  verdict: PASS         # or: FAIL: <one short reason>
  evidence:
    <free-form, multi-line OK; runs until next `## ` or EOF>
  ```

  `pm finished` parses this block and requires a verbatim verifier
  match plus `verdict: PASS`. Missing block, mismatched verifier, or
  non-PASS verdict → exit 9, task stays in `working`.

- **`verify-skill:NAME`** / **`verify-prompt:CRITERION`** — opt-in:
  `pm finished` spawns `claude -p` as an independent subprocess that
  judges the report. Use when self-attestation isn't trusted enough.
  Requires `claude` CLI on PATH.

- **`<shell command>`** — script-path verifier; spawned with
  `PM_TASK`, `PM_REPORT_SHA`, `PM_QUEUE`, `PM_SLUG`, `PM_VERIFIER` env
  and positional `<task-sha> <report-sha>`. Exit 0 = pass.

`--rejected` and `--skip-verifier` bypass the gate.

Verifier metadata (`verifier`, `verifier_exit`, truncated
`verifier_summary`) is recorded on the terminal TaskStatus's
attributes for audit.

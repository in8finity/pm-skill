---
name: pm-next
description: >
  Return the next runnable planning task as JSON, or "null" if the queue is
  empty / blocked. A task is runnable when its current TaskStatus is "new",
  every task in its dependsOn list has status "done", and every direct child
  (via parentTask) has reached a terminal status (done/rejected/superseded).
  Use when the user says "next task", "what's next", "pull next", or before
  spawning a worker.
---

# pm:next — pull the next runnable task

## Procedure

`../scripts/pm next [--queue Q]`

- Prints the full Task JSON of the oldest runnable task, or the literal
  string `null` if none.
- Does **not** mutate state — call `pm:executing` to claim the task.

## Selection rules

1. Iterate Tasks in the queue ordered by `created_at` ascending.
2. Skip tasks whose `attributes.workdir` is set and does not equal the
   caller's workdir (`$PM_WORKDIR` if set, else `realpath(cwd)`). This
   scopes the queue to the workspace the planner was in. Tasks with no
   `workdir` attribute (legacy / pre-feature) remain visible everywhere.
3. Skip tasks whose latest TaskStatus is not `new` (i.e. anything already
   claimed, done, or rejected).
4. Skip tasks where any `links.dependsOn` target's latest status is not
   `done`. This is the dependency gate.
5. Skip tasks that have at least one direct child (task whose
   `parentTask` link points back at this one) whose latest status is
   not in `{done, rejected, superseded}`. This is the
   **parent-rolls-up-children gate** — a wrapper task expanded into a
   subskill (via `pm-{auto,assisted,guided}-skill-execution --depth ≥1`)
   stays unrunnable until its expansion completes, so the wrapper's
   rollup report is written *after* the children's outcomes are
   known. Verified by `system-models/planning_parent_gate.als` (6
   assertions, scope 6) and tests G33/G34/G35.
6. Return the first survivor.

## Workdir binding

`pm plan` records `os.path.realpath(os.getcwd())` (or `$PM_WORKDIR`)
into `task.attributes.workdir` at plan time. Subtasks inherit the
parent's workdir. The filter above ensures a worker started in
`~/projects/A` only pulls tasks planned from `~/projects/A` — even if
multiple workspaces share one hashharness backend. Override the
caller's workdir with `PM_WORKDIR=/some/path pm next` when a worker is
running in a sandbox dir but should pull tasks scoped to a different
project.

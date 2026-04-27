---
name: planning-executing
description: >
  Claim a task for execution. Appends a TaskStatus(working) chained to the
  previous status of that task. Refuses if the task is not currently in
  state "new" (prevents two workers claiming the same task). Use when an
  agent picks up a task returned by planning:next.
---

# planning:executing — mark a task as working

## Procedure

`../scripts/pm executing --task <task-sha> [--agent ID]`

- Reads the latest TaskStatus for `<task-sha>`. Refuses with exit code 6
  unless that status is `new` (**pre-claim** check).
- Appends a new TaskStatus with `attributes.status = "working"`,
  `attributes.agent = <agent-id>`, `links.task = <task-sha>`,
  `links.prevStatus = <previous-status-sha>`.
- The claim TaskStatus uses deterministic text
  `claim:<task>/<prev-status>`. Two claimants racing off the same
  previous tip therefore collide on `text_sha256`; hashharness accepts
  exactly one create and the loser exits with code 8 (**claim race
  lost**) — caller MUST drop the task.
- On success (exit 0), prints the created TaskStatus as JSON.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Claim won; this agent owns the task |
| 6 | Pre-claim refusal: task was not in state `new` |
| 8 | Claim race lost: another agent claimed off the same prev-tip first |

## Concurrency note

hashharness items are immutable and identified by `sha256(text)`. The
claim path intentionally does **not** use a random nonce: the text is
fully determined by `(task, prevStatus)`. Two parallel claimants aiming
at the same predecessor therefore produce the same `text_sha256`, and
hashharness rejects the second create.

This race-safety is **structurally enforced** by the store plus script
(exit 8). It matches the formal model's `commitClaim` / `abortClaim`
split — see `system-models/planning.als` and the `NoDoubleCommit`
assertion.

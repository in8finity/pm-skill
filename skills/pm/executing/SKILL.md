---
name: pm-executing
description: >
  Claim a task for execution. Appends a TaskStatus(working) chained to the
  previous status of that task. Refuses if the task is not currently in
  state "new" (prevents two workers claiming the same task). Use when an
  agent picks up a task returned by pm:next.
---

# pm:executing — mark a task as working

## Procedure

`../scripts/pm executing --task <task-sha> [--agent ID]`

- Reads the latest TaskStatus for `<task-sha>`. Refuses with exit code 6
  unless that status is `new` (**pre-claim** check).
- Appends a new TaskStatus with `attributes.status = "working"`,
  `attributes.agent = <agent-id>`, `links.task = <task-sha>`,
  `links.prevStatus = <previous-status-sha>`.
- `prevStatus` is declared `chain_predecessor` in the schema, so
  hashharness compare-and-swaps the TaskStatus head for this task on
  every append: two claimants racing off the same previous tip both
  submit the same `prevStatus`, exactly one append wins, the other is
  rejected with 'head moved' which the script surfaces as exit 8
  (**claim race lost**) — caller MUST drop the task.
- On success (exit 0), prints the created TaskStatus as JSON.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Claim won; this agent owns the task |
| 6 | Pre-claim refusal: task was not in state `new` |
| 8 | Claim race lost: another agent claimed off the same prev-tip first |

## Concurrency note

The TaskStatus type declares `prevStatus` as a `chain_predecessor` link
in the planning schema. On every `create_item`, hashharness atomically
checks that `prevStatus` equals the current head record_sha256 for this
task's TaskStatus chain, advances the head if so, and rejects with
'head moved' otherwise. Two parallel claimants observing the same tip
therefore both submit the same `prevStatus`; exactly one append wins.

This race-safety is **structurally enforced** by the store plus script
(exit 8). It matches the formal model's `commitClaim` / `abortClaim`
split — see `system-models/planning.als` and the `NoDoubleCommit`
assertion.

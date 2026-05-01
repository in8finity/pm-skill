# planning skill set

A queue/board for agent work, persisted on hashharness as an append-only
chain of immutable items.

## Skills

| Skill              | Purpose                                                       |
|--------------------|---------------------------------------------------------------|
| `pm:plan`     | Enqueue a Task (+ genesis TaskStatus=`new`)                  |
| `pm:next`     | Return the next runnable Task, or `null`                     |
| `pm:executing`| Claim a Task → TaskStatus=`working`                          |
| `pm:report`   | Append TaskReport (proof of work; chained per task)          |
| `pm:finished` | Close a Task → TaskStatus=`done` (or `rejected`) with proof  |
| `pm:cancel`   | Supervisor cancel → synthetic proof + TaskStatus=`rejected`  |
| `pm:execute`  | Spawn N parallel agents that drain the queue                 |

## Storage model (hashharness types)

```
Task         attributes: { slug, queue }
             links:      parentTask, spawnedAt -> TaskStatus, dependsOn[]
TaskStatus   attributes: { status: new|working|done|rejected }
             links:      task, prevStatus, proof -> TaskReport
TaskReport   links:      task, prevReport
```

Three chains exist per task:
1. **Status chain** — `prevStatus` links from each TaskStatus to the prior one.
2. **Report chain** — `prevReport` links between TaskReports.
3. **Subtask chain** — a subtask's `parentTask` plus `spawnedAt` (a pointer to
   the parent's status at the moment the subtask was decided).

`work_package_id`:
- Tasks in queue Q live in `planning:Q`.
- Status / report records for task T live in `planning:task:<T-sha256>` so
  `find_tip` returns the latest status/report per task.

## Setup

The skills assume the hashharness MCP server is reachable over HTTP.

```bash
HASHHARNESS_MCP_TRANSPORT=http \
HASHHARNESS_DATA_DIR=./data \
python -m hashharness.mcp_server
```

Then register the planning types (idempotent — merges with existing schema):

```bash
python3 scripts/setup_schema.py
```

The scripts read `HASHHARNESS_MCP_URL` (default `http://127.0.0.1:38417/mcp`).

## Conventions

- `proof` link on a `done`/`rejected` TaskStatus is **mandatory** — finished.py
  refuses if there is no TaskReport on the task.
- `pm:cancel` preserves the same audit rule by synthesizing a
  TaskReport for the cancel reason, then linking `proof` from the
  cancelling `rejected` status.
- A subtask must be created with `--parent`; plan.py will read the parent's
  current TaskStatus and link `spawnedAt` to it.
- Dependencies are enforced by `next.py`: a task is only returned when every
  `dependsOn` target is `done`.

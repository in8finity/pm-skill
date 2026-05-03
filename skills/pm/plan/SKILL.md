---
name: pm-plan
description: >
  Enqueue a new task on the planning board backed by hashharness. Creates an
  immutable Task record plus an initial TaskStatus(new). Supports subtasks
  (link to parent + parent's status at decision point) and dependency links
  to other tasks. Use when the user says "plan", "queue", "enqueue", "add
  task", or "create subtask".
---

# pm:plan — enqueue a task

## Inputs

- `--title` — short human label (required)
- `--text` — full task description (required)
- `--queue` — board name; default `default`
- `--slug` — stable id within the queue; auto-derived from title if omitted
- `--parent <task-sha>` — when set, makes this a subtask. The new Task links
  `parentTask` → parent and `spawnedAt` → the parent's *current* TaskStatus
  (the decision point at which the subtask was initiated).
- `--depends-on sha[,sha...]` — dependency tasks. The task is not eligible
  for `next` until each dependency reaches status `done`. Validated at
  enqueue time; the following are refused with **exit 11** rather than
  silently creating an unrunnable task:
  - **Self-loop**: a sha equal to this task's own prospective
    `sha256("task:<queue>/<slug>")`. Such a task could never satisfy
    its own dep gate.
  - **Non-existent target**: a sha that doesn't resolve to a stored
    Task. The task would be permanently blocked because `next` cannot
    read a missing dep's status.
  - **Forever-blocked target**: a dep whose latest status is `rejected`
    or `superseded`. These statuses never become `done`, so the new
    task would block on the queue forever. (Use `pm replan` to revive
    a rejected dep first, or omit the dep.)
- `--sticky` — bind the task's TaskStatus chain to whichever agent
  context first claims it. Subsequent claims, reports, heartbeats,
  and finishes against this task — and any sticky descendants in its
  parent / dependency chain — must come from the same
  `$PM_CONTEXT_ID` (or `--context-id` flag); mismatches refuse with
  exit 10 (`StickyContextMismatch`). Subtasks via `--parent` inherit
  sticky automatically. See "Clustering work by agent" below for
  when and how to use this.
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

### Enqueueing many tasks at once — prefer `pm bulk-plan`

When you are about to enqueue more than ~3 tasks in a row (e.g. one
task per step of a skill, one task per chunk of a dataset), use
`pm bulk-plan` instead of looping `pm plan`. One invocation, one
permission prompt, one transactional summary line — and the canonical
allowlist target so subsequent runs don't prompt at all.

```bash
cat > /tmp/plan.json <<'JSON'
[
  {"slug":"step-1","title":"Step 1","text":"...", "verifier":"skill:foo","sticky":true},
  {"slug":"step-2","title":"Step 2","text":"...", "depends_on":["<sha-of-step-1>"]},
  {"slug":"step-3","title":"Step 3","text":"...", "depends_on":["<sha-of-step-2>"]}
]
JSON
pm bulk-plan --queue Q --input /tmp/plan.json
```

Per-spec fields: `slug`, `title`, `text` (required); `parent`,
`depends_on`, `verifier`, `sticky`, `workdir` (optional, same
semantics as the `pm plan` flags). Sticky and workdir auto-inherit
from the parent if not given. Output is one TSV line per task
(`<sha>\t<slug>\t<created|healed|skipped>`) so the chain references
needed for subsequent specs are easy to splice. Idempotent per
`(queue, slug)` — re-running with the same input is safe.

**Anti-pattern**: do **not** generate a one-off shell script that
loops `pm plan` (`for spec in ...; do pm plan ...; done > /tmp/x.sh;
bash /tmp/x.sh`). That triggers a permission prompt for the generated
script (Claude Code can't allowlist arbitrary one-shot scripts), and
loses the per-slug idempotency that `pm bulk-plan` provides.

## Clustering work by agent

When several tasks share an expensive resource (a set of files, a
compiled artifact, a large in-context dataset, an open browser
session), you usually want **one agent to do them all** rather than
have N agents each pay the load cost. The planning board makes this
explicit through three patterns; pick by the shape of the sharing.

### Pattern A — sticky cluster with a parent (recommended for shared reads)

Plan a sticky **cluster parent** whose body lists the shared
resource (file paths, chunk ids, repo URL, model name, etc.), then
plan each item as a `--parent` subtask:

```bash
PARENT=$(pm plan --queue Q --title "Cluster N: chunks 88-91,98" \
                 --text "Shared chunks: 88, 89, 90, 91, 98" \
                 --sticky | jq -r .task.text_sha256)

pm plan --queue Q --title "Item A" --text "..." --parent "$PARENT"
pm plan --queue Q --title "Item B" --text "..." --parent "$PARENT"
pm plan --queue Q --title "Item C" --text "..." --parent "$PARENT"
```

Because the parent is sticky and subtasks **inherit sticky from the
parent automatically**, the first worker to claim the parent (with
its own `PM_CONTEXT_ID`) binds the whole cluster to that context;
any other worker's claim on a sibling refuses with exit 10. The
bound worker reads the shared resource once into its own context
and drains the cluster from there. Other workers naturally route to
*other* clusters via `pm next`.

### Pattern B — sticky chain via dependsOn (sequential reuse)

When tasks share resources AND must run in order, use sticky tasks
linked by `--depends-on`. Sticky propagates across the dep chain
the same way it propagates across `parent`:

```bash
A=$(pm plan ... --sticky | jq -r .task.text_sha256)
B=$(pm plan ... --sticky --depends-on $A | jq -r .task.text_sha256)
C=$(pm plan ... --sticky --depends-on $B | jq -r .task.text_sha256)
```

The first agent to claim A binds the chain; B and C will only be
claimable by the same context once their deps complete.

### Pattern C — non-sticky singletons

When a task has no expensive shared read with any other task, leave
`--sticky` off. It rides the standard `pm next` pull path — any
free agent can claim it. Mixing singletons with clusters in the
same queue is fine; the dispatch happens per-task.

### Matching a context_id to a cluster you've already identified

If you've **already done** the cluster analysis (e.g., from a
similarity computation or a chunk-overlap matrix) and want to dispatch
work with deterministic agent-to-cluster mapping rather than
first-come-first-bind:

1. Mint one context-id per cluster up front:
   `CLUSTER_5_CTX=$(pm context-id)`
2. Have the worker assigned to that cluster pass it explicitly via
   the new `--context-id` flag (or set `PM_CONTEXT_ID` in the
   subprocess env). Use `--context-id` rather than the env var to
   avoid scope drift across nested subshells:
   ```bash
   pm executing --task "$PARENT" --context-id "$CLUSTER_5_CTX"
   pm executing --task "$SUB_A"  --context-id "$CLUSTER_5_CTX"
   pm report     --task "$SUB_A" --context-id "$CLUSTER_5_CTX" --title r --text x
   pm finished   --task "$SUB_A" --context-id "$CLUSTER_5_CTX"
   ```
3. The first claim binds; all subsequent claims with the same
   context-id succeed; any other agent's claims refuse with exit 10.

This **maps your cluster identity onto the planning board's context
identity 1:1**, so the dashboard's `ctx:` tag corresponds directly
to your cluster number — useful for debugging and reporting.

### Anti-pattern: minting a fresh context-id per claim

Do NOT call `pm context-id` inside the per-task loop:

```bash
# WRONG — produces N distinct ctx values for one agent's work
for task in $tasks; do
  CTX=$(pm context-id)         # fresh UUID each iteration
  pm executing --task $task --context-id $CTX
done
```

Each claim ends up in its own one-task "cluster". Sticky still
works structurally (no double-claims), but the dashboard's `ctx:`
tags become uninformative and you lose the ability to reason about
"which agent did which cluster". Mint one context-id per worker (or
per cluster, per Pattern C) and reuse it.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Task created (or self-healed by appending genesis status to an
existing slug-key Task) |
| 4 | Slug already taken in this queue (either pre-check found it, or a
concurrent `plan` claimed it first) |
| 5 | `--parent` was provided but parent has no status yet |
| 11 | Invalid graph at enqueue time. Sub-cases:
  - **`--depends-on` self-loop** (sha equals this task's prospective sha)
  - **`--depends-on` non-existent target** (sha doesn't resolve to a stored Task)
  - **`--depends-on` forever-blocked target** (target's latest status is `rejected` / `superseded`)
  - **`--parent` self-parent** (sha equals this task's prospective sha)
  - **`--parent` cycle** (parent chain transitively contains this task's sha)

  Verified by `system-models/planning_parent_gate.als#{NoSelfParent, NoCycle}`
  for the parent cases, and `planning.als` `plan[t]` precondition for the dep cases. |

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
- The genesis status is `new`. To begin work, use `pm:executing`.

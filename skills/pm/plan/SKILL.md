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
    opt-in: `pm finished` spawns Codex or Claude Code as an
    *independent* subprocess that re-judges the task + report. Higher
    cost; useful when self-attestation isn't trusted enough. Requires
    either `codex` or `claude` on PATH (otherwise exit 127, task stays
    in `working`; `PM_LLM_CLI` can force the choice). The LLM must
    terminate with `VERDICT: PASS` or `VERDICT: FAIL: <reason>`.
  - **`<absolute path>`** (or shell-prefixed command, e.g.
    `env FOO=bar /path/to/check.sh`) — spawn a subprocess. The
    script receives env `PM_TASK`, `PM_REPORT_SHA`, `PM_QUEUE`,
    `PM_SLUG`, `PM_VERIFIER` and positional `<task-sha>
    <report-sha>`. Exit 0 = pass, non-zero = fail.

  Failure (any form) exits `pm finished` with code 9, leaving the
  task in `working`. `--rejected` and `--skip-verifier` bypass.
  Closes the "hollow proof-of-work" gap — see
  `system-models/reports/planning-blind-spots.md` #3.

  #### Self-attestation caveats

  The `skill:` and `prompt:` forms **trust the worker's report**.
  `pm finished` parses the attestation block, gates on the verdict,
  and does NOT re-run anything. This is fast and structurally clean
  for criteria that genuinely require LLM judgement (prose quality,
  citation grounding, semantic correctness) — re-judging on a
  separate context is wasteful when the worker who did the work
  already has the evidence at hand.

  But it has a serious failure mode: **if the worker's "check"
  silently fails or returns a misleading result, `pm finished`
  accepts the lie.** Observed cause in practice: a sub-agent's bash
  allowlist permits relative paths (`Bash(it-tools/scripts/*.sh *)`)
  but not absolute paths; the worker invokes the gate via an
  absolute path; the sandbox refuses; the worker's exit-code
  handling mis-classifies the denial as success; the attestation
  block records `verdict: PASS` while the gate was never actually
  green. See
  `feedback/verifier-attestation-reliability-2026-05-25.md` for the
  source incident.

  **Rule of thumb.** For *mechanical* gates — validators, schema
  checks, banlist sweeps, anything where the criterion is a
  deterministic script — prefer the **script-path** verifier form
  (`<absolute path>`). `pm finished` actually executes the script,
  so worker mis-attestation is structurally impossible. Reserve
  `skill:`/`prompt:` for criteria that genuinely need LLM
  judgement. When `skill:`/`prompt:` is the right tool but trust
  matters, escalate to `verify-skill:`/`verify-prompt:` for an
  independent re-judge subprocess.

  #### Script-path verifiers: the `## Affected files` convention

  Mechanical gates usually apply to one or more files the worker
  touched. The verifier needs to know which. The convention is:
  **the worker's TaskReport carries a `## Affected files` block,
  and the verifier script reads it via `pm report-files`.**

  Report shape (bulleted or bare; comments and blank lines OK):

  ```markdown
  ## Summary

  Cleaned the rationale prose for `movement-control.yaml`.

  ## Affected files

  - sources/walton/latash-2010/ideas-and-claims/movement-control.yaml
  - sources/walton/latash-2010/ideas-and-claims/synergies.yaml
  # paths are repo-root-relative

  ## Verifier Attestation

  verifier: /Users/me/it-tools/scripts/check-rationale-banlist.sh
  verdict: PASS
  evidence: pm report-files | xargs -I{} check-rationale-banlist.sh {}
  ```

  Verifier script (passed to `pm plan --verifier`):

  ```bash
  #!/usr/bin/env bash
  # check-rationale-banlist.sh — script-path verifier for `pm finished`
  set -e
  files=$(skills/pm/scripts/pm report-files --task "$PM_TASK" \
          --report "$PM_REPORT_SHA")
  rc=0
  while IFS= read -r f; do
    [[ -z "$f" ]] && continue
    it-tools/scripts/banlist-impl.sh "$f" || rc=1
  done <<< "$files"
  exit $rc
  ```

  `pm finished` runs this with `PM_TASK` / `PM_REPORT_SHA` /
  `PM_QUEUE` / `PM_SLUG` / `PM_VERIFIER` in env. The verifier reads
  the report, fans the gate out over each affected file, returns
  exit 0 only if every file passes. The worker cannot fabricate
  this — there's no attestation block; the actual gate runs.

  **When the worker iterates** (multiple reports on the same task
  before `pm finished`), the verifier can either:
    - read the **latest** report (default of `pm report-files
      --task SHA` — typical), OR
    - union across **all** reports with
      `pm report-files --task SHA --all-reports`. Use when files
      accumulated across rounds and you want to re-gate every one.

  Output forms of `pm report-files`:
    - default — one path per line
    - `--null` — NUL-separated, `xargs -0`-friendly
    - `--json` — JSON array

## Procedure

1. Ensure the schema is registered (run once per data dir):
   `../scripts/pm setup`
2. Create the task and genesis status:
   `../scripts/pm plan --title "..." --text "..." [--queue Q] [--parent SHA] [--depends-on SHA,SHA]`
3. Output is `{ "task": ..., "status": ... }` — record `task.text_sha256`;
   it is the identifier used by the other planning skills.

### Closing a queue with a finalizer

A queue is "done" when every task in it is settled. To make that
provable as a single signal, append a **finalizer task** that depends
on every other task in the queue. When the finalizer reaches `done`,
you have a one-shot artifact saying "the queue completed in full" —
useful for downstream gating, audit, or just as an unambiguous human
checkpoint.

`pm bulk-plan --finalize-slug <slug>` does this for you:

```bash
pm bulk-plan --queue Q --finalize-slug queue-rollup --input plan.json
# auto-appends: {slug: "queue-rollup", depends_on_slugs: [<every other slug>]}
```

The finalizer's body is a generic "read each prior task's report and
summarise" prompt; override with `--finalize-text "..."` if your queue
needs a specific rollup. The slug must not collide with any spec
slug (`exit 8` if it does).

A queue can have at most one finalizer that aggregates **everything**.
If you bulk-plan in multiple batches, only the first batch's finalizer
covers the whole queue; later batches' finalizers cover only their own
specs. For incrementally-grown queues, plan the finalizer last and
manually edit its `depends_on_slugs` to add new tasks.

### Parents are grouping nodes — put work in children

The queue convention is that **a parent task is a structural node, not
a work node**. Its purpose is two things and only two things:

- **Grouping**: a stable parentTask handle that ties a subtree together
  for cancel/reclaim cascades, dashboard rollup, and rollup auditing.
- **Contexting**: when sticky, a parent's claim is the binding event
  for the chain — every sticky descendant must share that context.

A parent's task body should be empty/trivial. **Don't put summarize-
the-children logic in the parent's body**; put it in a final child
that depends on every sibling:

```
P                                ← grouping/contexting parent (empty body)
├── S1                           ← work child #1
├── S2 depends_on=[S1]           ← work child #2
├── S3 depends_on=[S2]           ← work child #3
└── rollup depends_on=[S1,S2,S3] ← summary child; reads the others' reports
```

Why: a parent that "summarizes children" creates a temporal coupling
the queue can't help with — workers can claim it but can't do its work
until the children finish. The rollup-as-final-child pattern is what
the dep gate is for; it lets the queue order things naturally.

The runtime gate matches this convention: parents are claimable as
soon as their deps are done (don't wait for children); they cannot
**finish** until every child is in {done, rejected, superseded}
(`pm finished` exit 14 otherwise). See
`system-models/planning_parent_gate.als#ParentNotFinishedWhilePendingChild`.

**Generating a conformant parent body.** Don't compose parent bodies
by hand — call:

```bash
pm build-task-body --steps STEPS_JSON --mode parent \
                   --prompt "<original problem statement>" \
                   [--workdir <abs path>]
```

The helper emits a fixed lightweight body that lists the children
(top-level steps from STEPS_JSON), embeds the `Role: parent` marker
line, and carries the worker-facing instruction "do NOT replicate the
children's work here". This is the marker `pm bulk-plan` lints for.

**Lint enforcement** (`pm bulk-plan`): any spec referenced as
`parent_slug` by some other spec in the same batch must contain a
`Role: parent` line in its body. Bulk-plan refuses with **exit 12**
otherwise, listing the offending parent slugs. Bypass with
`--allow-heavy-parent` for legacy / exceptional cases (and accept
that you've taken the convention off the table for those parents).

### Inserting work mid-execution — plan it as your own subtasks

A task that, *while running*, discovers it needs more work done **before
a downstream sibling** can run should plan that work as **its own
subtasks** (`--parent <self>`), not as free tasks. You do **not** need
to retro-edit the downstream task's `dependsOn` (and there's no verb to
do so) — subtask nesting plus two existing gates enforce the ordering
for free:

- **rollup-at-finish**: the running task cannot reach `done` until every
  descendant it just spawned is settled (`pm finished` exit 14).
- **dependsOn**: a downstream task that depends on the running one stays
  blocked until it is `done` — which now transitively requires the whole
  freshly-planned subtree.

```
T1 ─▶ T2 ─▶ T3            (dependsOn chain, planned up front)
        │
        └─ while T2 is `working`, it plans:
             T2a (--parent T2) ── T2a.1 / T2a.2 / T2a.3   (--parent T2a)
             T2b (--parent T2)
```

T3 (`dependsOn=[T2]`) cannot run until T2 is `done`; T2 cannot finish
until T2a, T2b — and T2a's three leaves — are all settled. So the new
work lands strictly **between T2 and T3** without touching T3's deps.

Mechanics worth knowing when you drive this by hand (workers normally
don't hit these because they only ever act on what `pm next`/`pm pull`
hand them):
- **Claim ≠ runnable.** `pm executing` claims any `new` task without
  checking deps or the parent gate — those gates live in `pm next` /
  `pm pull`. Take what `pm next` gives you; don't claim out of order or
  you bypass the ordering this pattern relies on.
- **A spawned parent holds open.** Once you claim a subtree parent
  (e.g. T2a), its own children become runnable, but finishing the parent
  is refused (exit 14) until those children settle — finish the parent
  *last*, after its leaves roll up.
- **Don't re-claim the spawner.** The task doing the spawning (T2) is
  already `working`; close it with `pm report` + `pm finished` only —
  a second `pm executing` returns exit 6 (`status is 'working'`).

Golden flow `G93` (`tests/integration/test_golden.py`) exercises this
end to end.

### Cross-queue parent/child: cascade-pause discipline

The default and recommended pattern is **same-queue** parent+children:
the runtime `children_settled` gate (`pm finished` exit 14) catches
"close parent while children still pending" automatically, and the
dashboard rollup reads naturally.

**Before reaching for a separate queue, check the shape.** A common
reason to want a separate queue is "the parent is doing real work
and *also* needs to spawn children below it." That's the heavy-parent
anti-pattern — see the "Parents are grouping nodes" section above.
Three things collide when a parent does work AND has children:

  1. The parent's claim is held by one worker for the entire span;
     its lease has to stay alive through the children's drain (the
     parent can't `pm finished` until `children_settled` is true).
  2. If the parent worker is busy with long work and stops
     heartbeating, sweep reclaims it — the original worker's
     eventual chain writes fail with `ClaimLost`, and a successor
     picks up a parent whose "real work" is happening elsewhere.
  3. If the parent body is "summarize the children," it cannot run
     until children finish, but the parent is already `working` —
     the worker has to busy-wait or release the claim.

The fix is to **extract a grouping parent** (empty body, `Role:
parent`) and demote the original work to a sibling child. The
rollup goes in another child with `depends_on=[all the siblings]`.
This is what `pm bulk-plan` lints for (exit 12 without `Role:
parent`); `pm build-task-body --mode parent` generates the
conformant empty body. Only after the shape is right does the
question of "same queue or separate queue" actually matter.

When you genuinely need a parent on queue A with children on
**a different queue B** (e.g. children are enrichment work that wants
its own scheduling / sticky binding / worker pool separate from the
parent's queue), the runtime now protects you: `children_settled`
(`store.py:623`) scans **all queues** via a single `find_items`
call and filters on `links.parentTask`. `pm finished` on the parent
will refuse with exit 14 while any cross-queue child is still
`new`/`working`. `cancel --cascade` and `reclaim --cascade` walk the
same cross-queue reverse-link, so a single cascade call against the
parent reaches children on every queue.

**Worker-side discipline still recommended:**

Even though the gate now enforces, a worker that knows it spawned
cross-queue children should still announce them explicitly and
checkpoint progress — successor workers have no out-of-band signal
about which subqueue to look at if the parent is reclaimed mid-flight.

1. **Plan-time hint.** The parent's body should explicitly name
   the subqueue it spawned and the slugs it spawned there. This is
   the only durable signal a successor worker has — there's no
   reverse-lookup MCP primitive that says "show me every queue
   that has a child of this parent." Include in the parent body:

   ```
   Subqueue: enrich-walton-corpus
   Subqueue-children: enrich-idea-001, enrich-idea-002, ...
   ```

2. **Detect "children haven't settled yet"** from the parent worker
   before calling `pm finished`. Use `pm list` against the subqueue:

   ```bash
   pending=$(skills/pm/scripts/pm list --queue enrich-walton-corpus \
             --state new --json | jq length)
   working=$(skills/pm/scripts/pm list --queue enrich-walton-corpus \
             --state working --json | jq length)
   if (( pending + working > 0 )); then
     # don't finish; pause instead (step 3)
     ...
   fi
   ```

3. **Pause without finishing.** Leave the parent in `working` and
   attach a `pm report` documenting progress + what's still owed:

   ```
   printf '## Progress\n\nDrained 9/13 children on queue enrich-walton-corpus.\n\nOutstanding:\n- enrich-idea-010 (new)\n- enrich-idea-011 (new)\n- enrich-idea-012 (working — agent worker-c1de44…)\n- enrich-idea-013 (new)\n\nResume strategy: a successor worker should re-enter this parent (already working), re-check the subqueue, and either drain remaining or pause again.\n' \
     | skills/pm/scripts/pm report --task "$PARENT_SHA" \
                                   --title "subqueue still draining" \
                                   --text -
   ```

   Heartbeating on the parent keeps the claim alive; if the worker is
   stopping (e.g. rate-limit window), do **not** heartbeat — let
   `pm sweep` reclaim the parent so a fresh worker can pick it up.

4. **Successor handoff.** A worker that claims the parent via
   `pm pull` (after sweep/reclaim) reads the latest `pm report` on
   the parent to find the named subqueue and outstanding child
   slugs. Re-runs step 2. Closes the parent (`pm finished`) only
   when the subqueue lists 0 `new` + 0 `working` children.

5. **What the runtime now enforces (vs. what's still on the worker):**

   - `pm finished` on a parent with cross-queue children that are
     still `new`/`working` → exit 14 (gate is cross-queue as of the
     reverse-link-via-`find_items` refactor in `store.py`).
   - `pm cancel --cascade <parent>` → cascades to children on every
     queue, not just the parent's.
   - `pm reclaim --cascade <parent>` → same; cross-queue.
   - **Still on the worker:** announcing the subqueue + child slugs
     in the parent body (no reverse-lookup primitive for a successor
     to discover which queue children live on); checkpointing
     progress between drain attempts; choosing whether to heartbeat
     the parent through long subqueue drains or release it for sweep.

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
| 12 | (`pm bulk-plan` only) Parent-body lint failed: a spec is a
parent of another spec in the batch but its body lacks the
`Role: parent` marker line. Generate the body with
`pm build-task-body --mode parent`, or pass
`--allow-heavy-parent` to bulk-plan to bypass. |
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

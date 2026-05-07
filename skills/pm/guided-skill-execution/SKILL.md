---
name: pm-guided-skill-execution
description: >
  Drive another skill step-by-step through the planning queue, with the user
  in the loop at every gate. Extracts the target skill's steps from its
  SKILL.md, plans one task per step (chained by dependsOn), and executes them
  one at a time — pausing after each step to surface decisions, accept
  user-supplied subtask requests, and confirm before moving on. Use when the
  user says "guided run", "step through skill X", "drive skill X with
  user-in-the-loop", or asks for an execution that respects a skill's
  prescribed dialogue gates.
---

# pm:guided-skill-execution — drive a skill task-by-task with user dialogue

## When to use this vs assisted vs auto

| | Guided | Assisted (`pm-assisted-skill-execution`) | Auto (`pm-auto-skill-execution`) |
|---|---|---|---|
| Each step | Pause, present, wait for user input | Default-pick routine gates; pause at critical | Pick documented default, log, continue |
| Critical gate / no default | Pause + ask | **Pause + ask, then continue** | Reject the task |
| Mandatory step missing precondition | Pause + ask | **Pause + ask: skip / supply / reject** | Reject |
| New subtasks | Add when the user requests, or when a step's dialogue gate produces a sub-decision | Skill-prescribed OR mid-dialogue user request | Only when the skill itself prescribes one |
| Best for | Novel problems, high-stakes proofs, anything with reconciliation/sign-off gates | Mostly-routine runs that may need 1–3 user inputs | Routine runs of well-understood skills |

## Inputs

- `--skill <name>` — target skill to execute (e.g. `formal-modeling`, `formal-debugger`).
- `--prompt <text>` — the problem statement / objective the skill is being applied to.
- `--queue <name>` — optional override; default is `skill-exec:<skill>:<UTC-timestamp>`.
- `--workdir <path>` — optional override; default is `cwd`.
- `--depth <N>` — how many levels of nested-skill recursion to expand
  as subtasks. Default `0` (flat: each step is one task, even if the
  step says "invoke skill X"). Use `1` to expand one level: step
  `9: Run loop` invoking `formal-debugger` becomes parent task `9`
  plus one subtask per debugger step (`9.formal-debugger.1`,
  `9.formal-debugger.2`, …). `2` expands two levels deep. Higher
  values multiply queue size aggressively (depth-2 over a 10-step
  skill invoking 7-step subskills ≈ 70 tasks); use `1` as the common
  case and `2` only for cluster-style runs you want fully tracked.

## Procedure

### Phase 0 — Step extraction and pre-run table

1. Run the depth-aware extractor:
   ```bash
   skills/pm/scripts/pm extract-steps <skill> --max-depth <N>
   ```
   where `<N>` is the `--depth` input (default `0`). Output is JSON:
   `{skill, skill_path, strategy, max_depth, steps: [...]}`.
   At depth 0, each step is `{n, title, anchor, subskills_invoked, verified}`.
   At depth ≥1, a step that invokes another skill carries a `nested`
   array of `{skill, steps: [...], strategy}` entries; nested step ids
   are dotted (e.g. `9.formal-debugger.1`) so the call chain is
   unambiguous when many subskills are present.

   For depth 0, `extract_steps.py <skill>` (the regex-only fallback)
   is also acceptable when `claude` isn't on PATH.

2. **Print the pre-run table to the user** before any task is enqueued:
   ```
   Skill: <skill>          (path: <skill_path>)
   Prompt: <prompt>
   Queue: <queue>
   Extraction strategy: <strategy>          Subtask depth: <N>

   Steps → planned tasks:
     Step <n>      <title>                  → slug: step-<n>-<kebab(title)>
       └─ subskill <X> (expanded, <K> nested steps):
          Step <n.X.1>  <title>             → slug: step-<n>-<X>-1-<kebab>
          Step <n.X.2>  <title>             → slug: step-<n>-<X>-2-<kebab>
     ...
   ```
   Indent nested rows under their parent so the user can see at a glance
   which subskills got expanded (and at what depth).

   If the strategy is `top-level-sections` or returned `<2` steps, also tell
   the user: *"The target skill has no obvious step structure; I'm using
   top-level section headings as steps. Please confirm or supply your own
   step list."*

   If any step has `verified: false` (LLM extractor returned an anchor
   the source SKILL.md doesn't contain verbatim), flag it explicitly and
   ask the user to confirm or correct that row before enqueueing.

3. **Wait for user confirmation.** Acceptable replies: "ok", "go", "yes",
   any explicit affirmative, or a corrected step list. Silence is not
   confirmation — re-prompt.

### Phase 1 — Plan all steps as chained tasks

**Always use `pm bulk-plan`** for the initial Phase-1 enqueue — one
permission prompt instead of N, and the canonical allowlist target.
Do **not** generate a one-off `bash /tmp/upload-*.sh` loop of
`pm plan` calls; that triggers a fresh prompt for the generated
script every run.

Build a JSON file once, then upload:

```bash
cat > /tmp/plan.json <<'JSON'
[
  {"slug":"step-1-clarify-prompt", "title":"Step 1: ...", "text":"<body>"},
  {"slug":"step-2-identify-entities", "title":"Step 2: ...", "text":"<body>",
   "depends_on":["<sha-of-step-1>"]},
  ...
]
JSON
skills/pm/scripts/pm bulk-plan --queue <queue> --input /tmp/plan.json
```

Use `parent_slug` and `depends_on_slugs` so the whole tree uploads
in **one** bulk-plan call — bulk-plan resolves slug references in
batch order, so you never need to know shas in advance:

```json
[
  {"slug":"step-1", "title":"Step 1", "text":"<body>"},
  {"slug":"step-2", "title":"Step 2", "text":"<body>",
   "depends_on_slugs":["step-1"]}
]
```

**With `--depth ≥1`**, pass `--chain-siblings` to bulk-plan and walk
the extractor's nested tree:

```bash
pm bulk-plan --queue <queue> --chain-siblings --input /tmp/plan.json
```

`--chain-siblings` auto-adds `depends_on` between consecutive specs
sharing the same `parent_slug` (in array order). Nested substeps run
sequentially in array order — required for skill expansion to honour
the prescribed step order.

**Parent task convention** (see `skills/pm/plan/SKILL.md` "Parents are
grouping nodes"): under `--depth ≥1`, the top-level `steps[i]` parent
is a **grouping/contexting node**, not a work node. Its body must be
lightweight: build it with `pm build-task-body --mode parent` (NOT
the per-step `--mode guided` form). The helper emits a body containing
the `Role: parent` marker that `pm bulk-plan` lints for — heavy parent
bodies are refused with exit 12. All actual work lives in children. If
the step needs a rollup/summary, add it as a **final child** that
depends on every sibling — the rollup belongs in a child task, not in
the parent. The parent's `pm finished` is gated: it cannot close until
every child is in {done, rejected, superseded} (exit 14 otherwise).

```bash
parent_body=$(pm build-task-body \
  --steps /tmp/steps.json --mode parent \
  --prompt "<original problem statement>" \
  --workdir <workdir>)
```

The parent IS claimable as soon as its deps are done — that's the
sticky-context binding event. A worker can claim the parent first,
hold the lifecycle / context lease, then process children inheriting
the binding, then close the parent after children settle.

Tree shape per spec:
- The top-level `steps[i]` becomes a parent task (no `parent_slug`,
  but `depends_on_slugs: [<last-child-of-prior-expansion>]` so the
  chain doesn't advance before the previous subskill finishes).
- Each nested entry under `steps[i].nested[j].steps[k]` becomes a
  child task with `parent_slug: "step-<i>"`. With `--chain-siblings`
  you do NOT need to add `depends_on_slugs` between siblings — the
  flag does it for you in array order. (You can still set it
  explicitly to encode a non-array DAG.)
- For a step that needs a summary of its subskill expansion, append
  one extra child after the nested-step list with
  `depends_on_slugs:["<last-nested-step-slug>"]` and a body that
  reads the children's reports.
- Children inherit `sticky` and `workdir` from the parent
  automatically (bulk-plan does this), so a sticky parent makes the
  whole subskill expansion sticky to the same agent context.

Bulk-plan stays idempotent per `(queue, slug)`, so re-running the
same JSON is safe — failed mid-upload runs can simply be retried.
A bad slug-reference (typo, or a child placed before its parent in
the array) exits 7 with a specific error before any partial damage.

For one-off mid-step subtasks (Phase 2, item 4 below), `pm plan` is
fine — the prompt cost is paid once per subtask, not per chain.

**Build each task body via `pm build-task-body`** — do NOT compose
the body yourself. The helper reads the extractor's JSON, splices the
verbatim step spec from the source SKILL.md (with explicit
`Reference: <skill_path>:<line>` and `Step lines: <start>-<end>`
fields), and writes a fully-formed body to stdout. This eliminates
the "Substep EIAC-S7." placeholder anti-pattern by construction —
no LLM-composed body ever exists.

```bash
# 1. Extract once, write to a temp file the build-task-body calls reference.
pm extract-steps <skill> --max-depth <N> > /tmp/steps.json

# 2. Per step, ask the helper for a body string.
body=$(pm build-task-body \
  --steps /tmp/steps.json \
  --step <n>            \
  --prompt "<original problem statement>" \
  --mode guided         \
  --workdir <workdir>)

# 3. Splice into a bulk-plan spec.
jq -nc --arg b "$body" '{slug:"step-<n>-<kebab>", title:"Step <n>: <title>", text:$b}' \
  >> /tmp/plan.jsonl
```

The helper guarantees the body contains: driving skill name + path,
step number + title, anchor, **`Reference: <path>:<line>`** (so a
worker can `Read(file_path=<path>, offset=<line>, limit=<N>)` directly
to the prescribed step), `Step lines: <start>-<end>`, subskills
invoked, mode, workdir, prompt, and the verbatim step spec from the
SKILL.md between the anchor line and the next step's anchor.

Workers learn this body shape once and scan it deterministically;
they don't have to round-trip through the source SKILL.md to discover
the contract. The Reference field is there for when they want more
context than the splice covers.

**If you must hand-roll a body** (e.g. an ad-hoc mid-step subtask
that wasn't in the extractor output), include at minimum: a `Step
spec:` paragraph with the verbatim or tight-paraphrased contract,
`Reference: <skill_path>:<line>` if applicable, `Mode:`, `Workdir:`,
and `Prompt:`. Slug-only bodies (`"Substep EIAC-S7."`) are refused
by skilled reviewers and force every worker to re-derive the
contract from the source — defeating the queue's audit value and
burning context.

Record each task's `text_sha256`. The `--depends-on` flag links each step
to the previous one — the queue is a strict chain, not parallel work.

### Phase 2 — Execute step by step

For each task in order:

1. `pm next --queue <queue>` returns the next runnable step.
2. `pm executing --task <sha> --agent guided` claims it.
3. **Read the matched section of the SKILL.md** — locate the step by its
   anchor and read the full prescribed dialogue. Apply it verbatim:
   - If the step says "ask the user X", ask X. Wait for a real reply.
   - If the step has a "block until …" precondition, enforce it. Don't
     paper over a missing precondition by inferring an answer.
   - If the step prescribes a confirmation table or boundary review,
     produce the table and wait for sign-off.
4. **Mid-step subtasks.** If the dialogue produces a sub-decision the user
   wants to defer or branch on (e.g., "first explore option A, then come
   back"), open a subtask:
   ```bash
   skills/pm/scripts/pm plan --queue <queue> \
     --parent <current-step-sha> --slug <stable-id> \
     --title "..." --text "..."
   ```
   Then claim and execute the subtask before resuming the parent step.
5. After the step's prescribed work is done, write the report:
   ```bash
   skills/pm/scripts/pm report --task <sha> \
     --title "Step <n>: <title> — <one-line outcome>" --text-file <path>
   ```
   The report MUST record:
   - The user-decision question(s) asked, and the verbatim reply received.
   - The artefacts produced (file paths, hashes, etc).
   - Any deviations from the skill's prescribed step (and why).
6. Mark done:
   ```bash
   skills/pm/scripts/pm finished --task <sha>
   ```
7. Briefly summarise the step outcome to the user before pulling the next
   one. Pause for the user's go-ahead — guided mode treats the boundary
   between steps as an implicit gate even when the skill itself doesn't.

### Phase 3 — Post-run summary

After the last step is `done`, present:
- Queue name and total tasks (including any subtasks added mid-run).
- For each step: matched skill section, outcome, deviations from prescribed.
- A concrete next-step pointer (e.g., "the formal-modeling skill expects
  a reconciliation report at `./system-models/reports/<domain>-reconciliation.md`
  — that was Step 10 and was completed at <path>").

## Notes

- This skill orchestrates; it does NOT do the work itself. Each step's
  actual execution is Claude reading the matched skill section and
  applying it. The planning queue is the audit trail.
- If a skill's step says "use the X subagent", spawn the subagent for
  that step's work — but the subagent's report must come back through
  `pm report` so the chain stays auditable.
- If a step legitimately cannot be done (missing dependency, user
  declines a precondition), reject it: `pm finished --task <sha> --rejected`.
  Do NOT silently skip — every step in the pre-run table must terminate.
- The pre-run table is the contract with the user. If the run produces
  more tasks (subtasks, replans), the post-run summary must show them
  alongside the originals so the user can see how the plan evolved.
- **Replanning a step inside a `--depth ≥1` expansion.** Two cases:
  - **Following the convention** (parent = grouping; rollup = final
    child that depends on every sibling): `pm replan --task <child>
    --cascade-down` is enough. The rollup-child is a dep-descendant
    of the replanned child, so cascade-down catches it; the rollup
    re-runs once the new child finishes. The grouping parent is
    still finishable — it has nothing of its own to redo.
  - **Legacy: rollup work in the parent body.** If the parent body
    itself does the summary, its `Done` status is now stale. Use
    `pm replan --task <child> --no-cascade --cascade-down-parents`
    to also reset the rollup ancestor(s) via the parentTask chain,
    so the parent re-derives its summary after the child redoes.
  See `pm-replan/SKILL.md` for the full mode matrix; the
  cross-feature soundness is verified in
  `system-models/planning_replan_with_parent_gate.als` (P6 / P7).

## Failure modes worth knowing

- **Extractor returns 0 or 1 steps.** The skill has no machine-readable
  step structure. Stop and ask the user for an explicit step list.
- **A step's prescribed dialogue is ambiguous.** Don't guess — quote the
  ambiguous text to the user and ask which interpretation to apply.
- **The user goes silent at a gate.** Do not infer agreement. Ask once
  more, then if still no reply, mark the queue paused and report status.
- **Sub-agent dies on first `pm next` with "permission denied".** This
  is almost always one of two allowlist shape mismatches — see
  `pm-execute/SKILL.md` ("Permission allowlist gotchas"). Short
  version: `Bash(pm next *)` doesn't match bare `pm next` (no args),
  and `export X=Y; pm ...` chains don't match `Bash(pm ...)`. For
  sticky work, always pass `--context-id <ctx>` inline; never tell
  the worker to `export PM_CONTEXT_ID` themselves.

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

## When to use this vs auto

| | Guided | Auto (`pm-auto-skill-execution`) |
|---|---|---|
| Each step | Pause, present, wait for user input | Pick the documented default, log the choice, continue |
| New subtasks | Add when the user requests, or when a step's dialogue gate produces a sub-decision | Add only when the skill itself prescribes one |
| Best for | Novel problems, high-stakes proofs, anything with reconciliation/sign-off gates | Routine runs of well-understood skills |

## Inputs

- `--skill <name>` — target skill to execute (e.g. `formal-modeling`, `formal-debugger`).
- `--prompt <text>` — the problem statement / objective the skill is being applied to.
- `--queue <name>` — optional override; default is `skill-exec:<skill>:<UTC-timestamp>`.
- `--workdir <path>` — optional override; default is `cwd`.

## Procedure

### Phase 0 — Step extraction and pre-run table

1. Run the extractor:
   ```bash
   python3 ~/.claude/skills/pm-skill-shared/extract_steps.py <skill>
   ```
   Output is JSON: `{skill, skill_path, strategy, steps: [{n, title, anchor}, ...]}`.

2. **Print the pre-run table to the user** before any task is enqueued:
   ```
   Skill: <skill>          (path: <skill_path>)
   Prompt: <prompt>
   Queue: <queue>
   Extraction strategy: <strategy>

   Steps → planned tasks:
     Step <n>  <title>          → slug: step-<n>-<kebab(title)>
     ...
   ```
   If the strategy is `top-level-sections` or returned `<2` steps, also tell
   the user: *"The target skill has no obvious step structure; I'm using
   top-level section headings as steps. Please confirm or supply your own
   step list."*

3. **Wait for user confirmation.** Acceptable replies: "ok", "go", "yes",
   any explicit affirmative, or a corrected step list. Silence is not
   confirmation — re-prompt.

### Phase 1 — Plan all steps as chained tasks

For each step `(n, title)` in order:

```bash
~/.claude/skills/planning-shared/pm plan \
  --queue <queue> \
  --slug step-<n>-<kebab(title)> \
  --title "Step <n>: <title>" \
  --text "<full task body — see below>" \
  [--depends-on <prev-step-sha>]
```

Task body MUST contain:
- `Driving skill: <skill>` and `Step number: <n>` and `Step title: <title>`.
- `Prompt: <prompt>` (the original problem statement).
- `Skill anchor:` quoting the matched line from the SKILL.md (the `anchor`
  field from the extractor) so a worker can grep back to the source.
- `Mode: guided` (so the worker knows to pause for user input at gates).
- `Workdir: <workdir>`.
- `Reference: <skill_path>` so the worker can read the full skill text.

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
   ~/.claude/skills/planning-shared/pm plan --queue <queue> \
     --parent <current-step-sha> --slug <stable-id> \
     --title "..." --text "..."
   ```
   Then claim and execute the subtask before resuming the parent step.
5. After the step's prescribed work is done, write the report:
   ```bash
   ~/.claude/skills/planning-shared/pm report --task <sha> \
     --title "Step <n>: <title> — <one-line outcome>" --text-file <path>
   ```
   The report MUST record:
   - The user-decision question(s) asked, and the verbatim reply received.
   - The artefacts produced (file paths, hashes, etc).
   - Any deviations from the skill's prescribed step (and why).
6. Mark done:
   ```bash
   ~/.claude/skills/planning-shared/pm finished --task <sha>
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

## Failure modes worth knowing

- **Extractor returns 0 or 1 steps.** The skill has no machine-readable
  step structure. Stop and ask the user for an explicit step list.
- **A step's prescribed dialogue is ambiguous.** Don't guess — quote the
  ambiguous text to the user and ask which interpretation to apply.
- **The user goes silent at a gate.** Do not infer agreement. Ask once
  more, then if still no reply, mark the queue paused and report status.

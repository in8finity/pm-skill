---
name: pm-auto-skill-execution
description: >
  Drive another skill end-to-end through the planning queue without user
  dialogue. Extracts the target skill's steps from its SKILL.md, plans one
  task per step (chained by dependsOn), and executes them sequentially —
  picking the documented default for every choice the skill would normally
  ask the user about, recording the choice + reasoning in each task report.
  Use when the user says "auto-run skill X", "execute skill X without
  prompts", or wants a hands-off application of a well-understood skill.
---

# pm:auto-skill-execution — drive a skill task-by-task without dialogue

## When to use this vs assisted vs guided

| | Auto | Assisted (`pm-assisted-skill-execution`) | Guided (`pm-guided-skill-execution`) |
|---|---|---|---|
| Each step | Pick default, record it, continue | Default-pick at routine gates; pause at critical | Pause, present, wait for user |
| Critical gate / no default | **Reject the task** | **Pause + ask, then continue** | Pause, present, wait |
| Mandatory step missing precondition | **Reject** | **Pause + ask: skip / supply / reject** | Pause + ask |
| New subtasks | Only when the skill itself prescribes them | Skill-prescribed OR mid-dialogue user request | Whenever the user requests one |
| Best for | Routine runs, batch processing, well-understood skills | Mostly-routine runs that may need 1–3 user inputs | Novel problems, sign-off gates |

The contract: stay **as close to the skill's prescribed flow as possible**.
Auto means "no dialogue", not "free improvisation". Every deviation from
the skill must be logged.

## Inputs

- `--skill <name>` — target skill to execute.
- `--prompt <text>` — the problem statement / objective.
- `--queue <name>` — optional override; default is `skill-exec:<skill>:<UTC-timestamp>`.
- `--workdir <path>` — optional override; default is `cwd`.
- `--depth <N>` — nested-skill expansion depth, same semantics as
  `pm-guided-skill-execution`'s `--depth`. Default `0` (flat). At
  depth ≥1, subskill steps become real child tasks under the parent
  step and execute via the same auto-pick rules; the post-run
  summary lists nested expansions alongside the originals.

## Procedure

### Phase 0 — Step extraction and pre-run table (same as guided)

1. Run the extractor:
   ```bash
   skills/pm/scripts/pm extract-steps <skill>
   ```

2. **Print the pre-run table to the user**:
   ```
   Skill: <skill>          (path: <skill_path>)
   Prompt: <prompt>
   Queue: <queue>
   Mode: auto
   Extraction strategy: <strategy>

   Steps → planned tasks:
     Step <n>  <title>          → slug: step-<n>-<kebab(title)>
     ...
   ```

3. **One confirmation.** Auto mode asks the user exactly once: "Proceed
   with auto execution? Reply 'go' to start, or supply corrections."
   This is the only dialogue gate in auto mode. After "go", everything
   else is internal-default. Silence is not "go" — re-prompt once, then
   abort.

### Phase 1 — Plan all steps as chained tasks

Same as guided's Phase 1, except the task body's `Mode:` field is
`auto` (so the worker knows to default-pick at gates instead of
asking).

### Phase 2 — Execute step by step (no dialogue)

For each task in order:

1. `pm next --queue <queue>` returns the next runnable step.
2. `pm executing --task <sha> --agent auto` claims it.
3. **Read the matched section of the SKILL.md.** For every dialogue
   gate the step prescribes:
   - **If the skill names a default** ("if no answer, use X", "default to
     Y", "static is the default"), use it.
   - **If the skill enumerates options without a default**, pick the option
     whose preconditions match the prompt. Document the precondition match
     in the task report.
   - **If neither default nor matching precondition exists**, pick the
     option that minimises scope (the smallest, most reversible choice)
     and flag it in the report as `Auto-default-by-minimal-scope`.
   - **NEVER skip a gate silently.** Every gate the skill prescribes must
     produce a recorded decision in the task report — the recorded
     decision IS the audit trail of "did auto stay close to the skill?".
4. **Subtasks.** Create only when the skill itself prescribes one
   (e.g., reconciliation step that says "for each source, do …"). Do not
   create exploratory subtasks — that's guided's job.
5. **MANDATORY steps.** Some skills mark a step as mandatory (e.g.,
   formal-modeling Step 10 reconciliation: "do not iterate until …").
   Auto mode MUST execute these — picking a default does not mean
   skipping. If a mandatory step's preconditions cannot be satisfied
   (e.g., reconciliation requires source files that do not exist),
   reject the task with the reason; do not silently complete.
6. After the step's prescribed work is done, write the report:
   ```bash
   skills/pm/scripts/pm report --task <sha> \
     --title "Step <n>: <title> — <one-line outcome>" --text-file <path>
   ```
   Report MUST contain a section `## Auto-decisions made` listing each
   gate the skill prescribed, the option picked, and the rule used to
   pick it (`skill-default | precondition-match | minimal-scope`).
7. Mark done: `pm finished --task <sha>`.

### Phase 3 — Post-run summary

After the last step is `done`, present:
- Queue name and total tasks.
- For each step: matched skill section, outcome, decisions made and rule
  used (so the user can audit "did auto stay close to the skill?").
- An explicit list of any **MANDATORY steps that were rejected** with
  the missing preconditions, and any deviations from the skill's
  prescribed flow.

## Notes

- Auto mode's promise is "I will follow the skill closely and tell you
  every choice I made for you." It is NOT "I will figure out what's best
  regardless of the skill." If the skill is wrong for the prompt, that
  is a problem to surface in the post-run summary, not to fix mid-run.
- Auto-decisions are picked deterministically from the same prompt
  (skill-default first, then precondition-match, then minimal-scope).
  Re-running the same skill on the same prompt should produce the same
  decisions modulo solver non-determinism.
- If a step's "default" is ambiguous because the skill doesn't name one,
  the report's `Auto-decisions made` section MUST explicitly say
  `Skill names no default; picked X by minimal-scope`. This is the
  user's signal that the skill text could use clarification.

## Failure modes worth knowing

- **Skill demands user input no documented default exists for.** Reject
  the task with the reason; do not invent an answer. The user can re-run
  the same step in guided mode to supply the missing input.
- **Mandatory step preconditions missing.** Reject; do not silently
  complete. Auto mode preserving the audit trail matters more than
  green-status optics.
- **Extractor returns 0 or 1 steps.** Same as guided — abort and report.
  Auto mode does not invent step structure.

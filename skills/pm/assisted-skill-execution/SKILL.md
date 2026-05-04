---
name: pm-assisted-skill-execution
description: >
  Drive another skill through the planning queue with a "default + escalate"
  policy: routine gates pick the documented default and continue silently
  (like auto), but gates the skill marks critical or where no default exists
  pause and ask the user (like guided) — and never auto-reject a step just
  because a decision is required. Use when you want hands-off batch progress
  on the easy parts but real human input on the hard ones, especially for
  partially-understood skills or runs where some inputs only the user knows.
---

# pm:assisted-skill-execution — drive a skill with default + escalate

## When to use this vs auto vs guided

| Mode | Routine gate | Critical / no-default gate | Mandatory step with missing precondition |
|---|---|---|---|
| **auto** | pick default, log, continue | reject the task (`--rejected`) | reject |
| **guided** | pause + ask | pause + ask | pause + ask |
| **assisted** *(this skill)* | pick default, log, continue | **pause + ask, then continue** | **pause + ask: skip, supply, or reject** |

The contract: **don't bother the user about routine choices, but don't fail
silently or burn a `--rejected` when a real decision is needed.** Critical
gates and missing preconditions become questions, not refusals. Routine
defaults flow through without dialogue.

## When this is the right choice

- **You roughly trust the skill's defaults** but suspect there are 1–3 spots
  where the run will need your input.
- **You don't want to babysit every step** but also don't want to discover a
  whole tree of `rejected` tasks at the end.
- **A skill is partially-understood** for the current prompt — auto would
  reject the unclear bits; you want to *answer* them once and continue.
- **A run spans hours** and you want to be pinged only when the run is
  actually blocked on you, not on every step boundary.

## Inputs

- `--skill <name>` — target skill to execute.
- `--prompt <text>` — the problem statement / objective.
- `--queue <name>` — optional override; default is `skill-exec:<skill>:<UTC-timestamp>`.
- `--workdir <path>` — optional override; default is `cwd`.
- `--depth <N>` — nested-skill expansion depth, same semantics as
  `pm-guided-skill-execution`'s `--depth`. Default `0` (flat). At
  depth ≥1, subskill steps become real child tasks under the parent
  step. The default-pick + escalate policy applies to nested steps
  exactly as it does to top-level ones.
- `--always-ask <pattern>` — optional repeatable; substring patterns that
  force the assisted mode to escalate any gate whose skill section text
  matches. Useful for "ask me about anything that says 'scope'".
- `--never-ask` — optional; treats every gate as routine. Equivalent to
  `--ask-only` mode but for cases where the skill *thinks* something is
  critical and you've decided up front it's safe. Use with care; document
  in the post-run summary.

## What counts as critical (heuristics)

A gate is **critical** (escalate to user) when ANY of the following match the
skill section for the step:

1. Imperative dialogue verbs: *"ask the user"*, *"confirm with the user"*,
   *"sign off"*, *"present to the user and wait"*, *"do not proceed without"*.
2. Block-until preconditions: *"block until X is true"*, *"halt unless …"*,
   *"MUST … before proceeding"*.
3. Reconciliation / boundary / scope decisions where the skill enumerates
   options without a clear default (per the auto-mode `Skill names no
   default; picked X by minimal-scope` rule — assisted *asks* instead of
   minimal-scoping).
4. The user passed `--always-ask <pattern>` and the section text matches.
5. The step is mandatory (e.g., formal-modeling Step 10 reconciliation) AND
   its preconditions are not satisfied (sources missing, etc.) — auto would
   reject; assisted asks "skip / supply / reject?".

A gate is **routine** (default-pick) when:

- The skill names an explicit default ("if no answer, use X", "default to Y").
- The skill enumerates options and the precondition match is unambiguous.
- None of the critical heuristics above apply.

## Procedure

### Phase 0 — Step extraction and pre-run table

Identical to auto / guided: run `skills/pm/scripts/pm extract-steps <skill>`, print the pre-run table, get one
confirmation from the user. The table now also lists, for each step, the
**predicted gate kind**: `routine` (auto-default), `critical` (will pause),
`mandatory` (will pause if preconditions fail). The user can override with
`--always-ask`.

```
Skill: formal-modeling   (path: …)
Prompt: <prompt>
Queue: skill-exec:formal-modeling:2026-05-03T…
Mode: assisted
Extraction strategy: numbered-sections

Steps → planned tasks → gate kind:
  Step 0  Clarify the prompt        → slug: step-0-… → critical
  Step 1  Identify entities         → slug: step-1-… → routine
  Step 2  Identify states           → slug: step-2-… → routine
  …
  Step 9  Run → Interpret → Re-run  → slug: step-9-… → routine (mandatory)
  Step 10 Reconcile against source  → slug: step-10-… → critical (mandatory)
  Step 10b Enforcement audit        → slug: step-10b-… → routine

Proceed? Reply 'go' to start, supply corrections, or pass --always-ask
to escalate additional gates.
```

### Phase 1 — Plan all steps as chained tasks

Same as guided's Phase 1. The task body's `Mode:` field is `assisted`. Each
task body also carries a `Predicted gate kind: routine|critical|mandatory`
hint so the worker knows whether to default-pick or escalate at execute
time.

### Phase 2 — Execute step by step

For each task in order:

1. `pm next --queue <queue>` returns the next runnable step.
2. `pm executing --task <sha> --agent assisted` claims it.
3. **Read the matched section of the SKILL.md.** Decide gate kind LIVE
   (don't blindly trust the pre-run prediction — section text might have
   sub-gates):

   - **Routine gate** → behave like auto:
     - Pick the documented default; if multiple options match, use
       precondition match; if neither, use minimal-scope.
     - Record the choice + rule in the report under `## Auto-decisions made`.
     - Continue without dialogue.

   - **Critical gate** → behave like guided:
     - Pause. Print to the user:
       ```
       Step <n>: <title>
       Skill section says: "<verbatim quote of the relevant line(s)>"
       Options I can see:
         A) <option> — default if you say nothing
         B) <option>
         C) <option>
       Why I'm asking: <one of {imperative dialogue, block-until,
                                no-default ambiguity, --always-ask match}>
       Reply with the letter, or supply your own answer.
       ```
     - Wait for user reply. **Default-after-timeout is allowed** if the
       skill prescribes one; otherwise re-prompt once, then mark the
       queue paused (don't reject — pausing keeps the chain resumable).
     - Record the question + verbatim reply in the report under
       `## User-decisions asked`.

   - **Mandatory step with missing preconditions** → never auto-reject:
     - Pause. Print:
       ```
       Step <n>: <title> is mandatory but precondition X is not met:
         <what's missing>
       Options:
         skip — mark this step done with a "precondition waived" note
                (only safe if you're sure the downstream steps don't need it)
         supply — supply the missing input now (file path, value, etc.)
         reject — close the step as rejected and let downstream steps fail
                  on their dep gate (matches auto behavior)
       ```
     - Default to `reject` if the user goes silent twice.
     - Record outcome in the report.

4. **Subtasks**: create only when the skill prescribes one OR the user
   supplies one mid-dialogue (intermediate between auto's "skill-only" and
   guided's "user-anytime"). Document the trigger in the parent's report.

5. After the step's prescribed work is done, write the report:
   ```bash
   skills/pm/scripts/pm report --task <sha> \
     --title "Step <n>: <title> — <one-line outcome>" --text-file <path>
   ```
   The report MUST contain:
   - `## Auto-decisions made` — every routine gate's pick + rule.
   - `## User-decisions asked` — every critical-gate question + verbatim
     reply, OR a single line `(none — all gates routine)`.
   - `## Mandatory-precondition outcomes` — for any mandatory step, the
     precondition state and the user's choice (`skip`/`supply`/`reject`),
     OR `(no mandatory steps in this task)`.

6. Mark done: `pm finished --task <sha>`.

### Phase 3 — Post-run summary

After the last step is `done`, present:

- Queue name and total tasks (including any subtasks added mid-run).
- For each step: matched skill section, gate kind realised at execute
  time (may differ from prediction), outcome.
- **Aggregate question count**: "Asked the user X times across N steps; Y
  questions matched `--always-ask`, Z were skill-prescribed critical, W were
  mandatory-precondition escalations."
- Any deviations from the skill's prescribed flow, listed explicitly.
- Pointer to the artefacts the skill expects (reports, models, etc.).

## Notes

- **Assisted is the right default for human-in-the-loop runs that aren't
  novel.** Auto is for batch / cron-like runs; guided is for novel work
  where every step deserves attention. Assisted is the everyday mode.
- **Re-runnability**: assisted runs are reproducible if and only if the
  user gives the same answers to the same critical-gate questions. Auto
  decisions are deterministic; the dialogue answers aren't. The report's
  `## User-decisions asked` section is the canonical record of "what the
  user said this run."
- **Pause vs reject**: a paused queue is RESUMABLE — `pm next` will return
  the same task and the run can pick up where it left off. A rejected step
  is TERMINAL — downstream steps will fail their dep gate. Assisted prefers
  pausing to rejecting; that's the central design choice that distinguishes
  it from auto.
- **--never-ask** turns assisted into auto-with-paused-fail-mode: every
  critical gate degrades to default-pick, but mandatory-precondition
  failures still pause (instead of auto's reject). Use when you trust the
  skill's defaults but want a chance to intervene if a step physically
  can't run.

## Failure modes worth knowing

- **User goes silent at a critical gate.** Re-prompt once, then mark the
  queue paused (NOT rejected). The user can reply later via the dashboard
  or by re-running the assisted skill — the chain is intact.
- **Skill text is ambiguous about whether a gate is critical.** Default to
  **escalate** (treat as critical). Better one extra question than a wrong
  silent default. Document the ambiguity in the post-run summary.
- **Mandatory step missing preconditions AND user picks `skip`.** Record
  loudly (`## Mandatory-precondition outcomes` with `OUTCOME: skip
  (precondition-waived)`); some downstream steps will likely fail.
  Don't second-guess the user, but make the waiver auditable.
- **Extractor returns 0 or 1 steps.** Same as auto / guided — abort and
  report. Assisted does not invent step structure.

## How to invoke

```bash
# Standard run — assisted picks defaults, asks at critical gates.
pm-assisted-skill-execution \
  --skill formal-modeling \
  --prompt "Verify the order-fulfilment state machine"

# Add custom escalation: ask me whenever a section talks about scope.
pm-assisted-skill-execution \
  --skill formal-modeling \
  --prompt "..." \
  --always-ask "scope" --always-ask "boundary"

# Trust skill defaults completely; only intervene on physical blockers.
pm-assisted-skill-execution \
  --skill formal-modeling \
  --prompt "..." \
  --never-ask
```

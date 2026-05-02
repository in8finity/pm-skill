---
name: pm-extract-steps
description: >
  Extract the major workflow steps from another skill's SKILL.md, with
  bash-side validation that every extracted step's anchor is verbatim
  in the source (catches LLM hallucination), and optional recursion
  into nested skill invocations (step X of skill A invoking skill B
  yields nested steps named `X.B.S1`, `X.B.S2`, ...). Two modes —
  llm (semantic, default when claude is on PATH) and regex (pattern-
  matching fallback). Used internally by pm-auto-skill-execution,
  pm-assisted-skill-execution, pm-guided-skill-execution; also
  callable standalone for inspecting a skill's structure.
---

# pm:extract-steps — semantic step extraction with validation

## When to use

- **Inspecting a skill's structure** — "what are the major steps in
  skill X?" without reading the whole SKILL.md by hand.
- **Driving auto/assisted/guided execution** — those skills call this
  one to produce the pre-run table (Phase 0).
- **Building dashboards or run reports** — the JSON output is stable
  and includes every step's `verified` flag, so you can flag steps
  that drifted between extractor and source.
- **Following nested skill invocations** — when a skill says "use
  skill X for this step", `--max-depth N` recurses N levels deep and
  splices skill X's steps under the parent step's `nested` key.

## Procedure

`../scripts/pm extract-steps <skill-name> [--mode llm|regex|auto] [--max-depth N] [--no-validate]`

Or by absolute path: `pm extract-steps --path /abs/path/to/SKILL.md ...`

Default mode is `auto` — picks `llm` if `claude` is on PATH, else
falls back to `regex`. Default max-depth is 2 (parent + 2 nested
levels). Validation is on by default; `--no-validate` skips the
grep-back check (faster, but loses the `verified` flag).

## Output JSON shape

```json
{
  "skill": "formal-modeling",
  "skill_path": "/Users/.../formal-modeling/SKILL.md",
  "strategy": "llm-extraction",
  "max_depth": 2,
  "steps": [
    {
      "n": "0",
      "title": "Clarify the prompt",
      "anchor": "0. **Clarify the prompt** — Assess prompt quality.",
      "subskills_invoked": [],
      "verified": true
    },
    {
      "n": "9",
      "title": "Run → Interpret → Re-run loop",
      "anchor": "9. **Run → Interpret → Re-run loop** ...",
      "subskills_invoked": ["formal-debugger"],
      "verified": true,
      "nested": [
        {
          "skill": "formal-debugger",
          "strategy": "llm-extraction",
          "steps": [
            {"n": "9.formal-debugger.1", "title": "Frame the symptom", ...},
            ...
          ]
        }
      ]
    }
  ]
}
```

Each step carries:
- `n` — opaque step identifier (string; preserved from source)
- `title` — short human-readable name
- `anchor` — verbatim substring of source for grep-back
- `subskills_invoked` — list of OTHER skills this step references
- `verified` — true iff `anchor` appears as a substring of source
- `nested` — present iff this step had subskills_invoked AND recursion
  was allowed (depth > 0)

## Two modes

### llm mode (default when `claude` is on PATH)

Calls `claude -p` with a structured prompt that asks for the major
workflow steps, the anchor line for each, and any subskills invoked.
The LLM does semantic extraction — it understands "## Phase 0",
"### Step 1", "1. Foo", "If the user says X, run skill Y" all as the
same shape, where regex-only would treat them as different.

The LLM is instructed to OMIT a step rather than fabricate an anchor
it can't find verbatim. The bash validation step then verifies every
returned anchor by `grep -F`-ing it against the source — anything
that doesn't match is marked `verified: false` (rather than dropped,
so the caller can decide whether to trust the LLM).

### regex mode (always available)

Delegates to the pre-existing `extract_steps.py` extractor, which
tries three strategies in order: `step-headings` (matches `^# Step
N`), `numbered-in-section` (numbered items inside a section like
"Procedure" / "Workflow"), `top-level-sections` (excluding
boilerplate). First strategy that yields ≥2 steps wins. No
validation needed since the extractor reads the source directly.

Use `--mode regex` to force regex extraction (faster, no LLM call,
deterministic). The default `auto` picks regex when `claude` isn't
available.

## Validation: anchor-must-be-verbatim

After llm extraction, every step's `anchor` field is checked against
the source SKILL.md by string-substring. The post-validation step
adds a `verified: bool` to each step.

This catches the common LLM failure mode where the model invents a
plausible-looking step heading that doesn't actually appear in the
source. The downstream consumer (auto/assisted/guided) can then
choose to skip unverified steps or surface them for user
confirmation.

`--no-validate` skips this — useful for quick inspection where you
trust the LLM and don't need the bash overhead.

## Recursion into nested skills

When `--max-depth N > 0` and the LLM identifies subskills invoked by
a step, the script recursively calls itself with `--max-depth (N-1)`
on each subskill. Returned nested steps are re-keyed with the parent
step's `n` as a prefix:

```
Parent step "9" invokes skill "formal-debugger" → nested steps
become "9.formal-debugger.1", "9.formal-debugger.2", ...
```

This makes the JSON output shape unambiguous even with deep nesting:
the dotted path describes the call chain.

Recursion failures (network, timeout, missing skill) are captured
per-subskill as `{skill: "...", error: "..."}` rather than aborting
the whole extraction.

## Inputs

- `<skill-name>` — name of the skill to extract from. Resolved to a
  SKILL.md path via the same search order extract_steps.py uses
  (`~/.claude/skills/<name>`, plugin marketplace, plugin cache).
- `--path <abs>` — absolute path to a SKILL.md (overrides name).
- `--mode <auto|llm|regex>` — extraction strategy. Default `auto`.
- `--max-depth <N>` — how many levels of nested-skill recursion
  to follow. Default 2. `--max-depth 0` disables recursion.
- `--no-validate` — skip the bash-side anchor verification step.

## Failure modes worth knowing

- **`claude -p` unavailable.** Auto mode silently falls back to
  regex. Explicit `--mode llm` will exit with the failure message.
- **LLM returns non-JSON despite the prompt.** The script tries a
  best-effort code-fence strip, then falls back to regex extraction
  rather than failing.
- **LLM hallucinated anchors.** Validation marks them `verified:
  false`; the consumer decides what to do. Use `--no-validate` only
  if you've already vetted the LLM's output.
- **Recursion timeout (default 120s per subskill).** That subskill's
  entry becomes `{skill: "...", error: "subprocess.TimeoutExpired"}`;
  the outer extraction continues.
- **Circular skill invocations.** Not currently detected — depth
  limiting is the only stop. Use `--max-depth 1` or 2 for unfamiliar
  skill graphs.

## How auto/assisted/guided use this

They call `pm extract-steps <skill> --max-depth 0` (or 1 if you want
nested cluster mode) at Phase 0 to build the pre-run table. Each
returned step becomes one task in the queue, slug-keyed by
`step-<n>-<kebab(title)>`. Steps with `verified: false` are flagged
in the pre-run table for the user to confirm before queueing.

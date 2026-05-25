# system-models/

Formal models (`*.als`, `*.dfy`) plus the reconciliation reports under `reports/`.

## enforcement.yaml — verified property → evidence map

`enforcement.yaml` maps each verified property to the evidence that
enforces it: model assertions, code call sites, exit codes, skill-text
phrases, and golden tests. Two checkers consume the same YAML and
both pass clean:

    # Local checker (no extra deps, short golden IDs supported)
    python3 system-models/check_enforcement.py

    # Bundled formal-modeling checker (closure_gates + --check-coverage)
    bash $FM/scripts/verify.sh --check-enforcement \
        system-models/enforcement.yaml --project-root . [--check-coverage]
    # where $FM = ~/.claude/plugins/cache/morozov-claude-plugin/formal-methods/<ver>/skills/formal-modeling

Both exit 0 if every entry passes, non-zero with a per-property red/green
table if anything has drifted. Wire either into CI to catch the prose-
claims-something-the-code-no-longer-has class of bug. To add a new
property: append an entry to the YAML; first run will tell you what's
missing.

**Schema compatibility.** The YAML is a flat list of entries (the
bundled checker auto-coerces this to `{properties: [...]}`); paths
are repo-root-relative. Test names use **full Python function names**
(`g56_parent_finish_blocked_while_children_pending`) so both checkers
resolve them. The local checker also accepts short golden IDs
(`G56`) for backward compatibility.

**`--check-coverage`** (bundled checker only) flags any model
`check Foo` that has no enforcement entry — catches "the model
proves X but nobody wrote down how X is enforced." False positives:
the bundled `ALS_CHECK_RE` regex doesn't strip Alloy comments, so
a comment line beginning with `check <word>` will be flagged
incorrectly. Avoid by rephrasing such comments.

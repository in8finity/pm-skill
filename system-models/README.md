# system-models/

Formal models (`*.als`, `*.dfy`) plus the reconciliation reports under `reports/`.

## enforcement.yaml — verified property → evidence map

`enforcement.yaml` maps each verified property to the evidence that
enforces it: model assertions, code call sites, exit codes, skill-text
phrases, and golden tests. Run from repo root:

    python3 system-models/scripts/check_enforcement.py \
        system-models/enforcement.yaml \
        --project-root . \
        [--check-coverage]

Exit 0 if every entry passes, non-zero with a per-property red/green
table if anything has drifted. Wire into CI to catch the prose-claims-
something-the-code-no-longer-has class of bug. To add a new property:
append an entry to the YAML; first run will tell you what's missing.

`--check-coverage` flags any model `check Foo` that has no
enforcement entry — catches "the model proves X but nobody wrote
down how X is enforced." Known false positive: the `ALS_CHECK_RE`
regex doesn't strip Alloy comments, so a comment line beginning
with `check <word>` will be flagged incorrectly. Avoid by
rephrasing such comments.

### Why vendored

`system-models/scripts/check_enforcement.py` is **vendored** from the
formal-modeling plugin (path documented in the file's docstring), so
CI and external contributors can run the audit without installing
the plugin. Dependencies: PyYAML (required), bashlex (optional —
only for `closure_gates` with `language: bash`). To re-sync against
an upstream update, overwrite the vendored file from the plugin's
path and re-run the audit; the script's docstring carries the source
path.

### Schema

YAML is a flat list of property entries (the checker auto-coerces
this to `{properties: [...]}`); paths are repo-root-relative when
invoked with `--project-root .`. Test names use **full Python
function names** (`g56_parent_finish_blocked_while_children_pending`).
See `references/enforcement-map.reference` in the plugin for the
authoritative schema; the relevant fields summarised:

* `models[].asserts` — `assert <name>` and `check <name>` must both
  appear in the cited `.als` file.
* `code_gates[].must_call` — Python AST-checked; dotted names work.
* `code_gates[].must_match` — regex search on the file.
* `code_gates[].must_exit` — `return N`, `sys.exit(N)`, `exit(N)`,
  or `raise SystemExit(N)`.
* `skill_texts[].must_mention` — case-insensitive substring list.
* `closure_gates` — AST-walk every call site of a protected
  primitive; demands a gate in the same enclosing function. Catches
  NEW call sites the file-list never named.
* `tests` — searched as `def [test_]<name>\b` across files matching
  `tests_glob` (default `tests/**/test_*.py`).

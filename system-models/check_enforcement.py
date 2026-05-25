#!/usr/bin/env python3
"""Enforcement-map checker.

Loads system-models/enforcement.yaml and verifies that every claimed
gate (model assertion / code call / skill prose / golden test) is
actually present in the repo. Exits 0 if every entry passes, non-zero
if any gate is missing.

This is the prose-drift-prevention tool proposed in
system-models/reports/proposal-enforcement-map-for-formal-modeling-skill.md.
The reconciliation report should be a *render* of this artifact, not a
hand-written narrative — that closes the class of bug where prose
claims an enforcement that the code no longer has.

Usage:
  python3 system-models/check_enforcement.py [--enforcement PATH]

Per-gate checks:
  models[i].asserts        — `assert <name>` substring grep on the .als
  code_gates[i].must_call  — AST-parse the .py; assert the named
                              function appears in any Call node (handles
                              direct calls, attribute calls, etc.)
  code_gates[i].must_exit  — grep for `return <N>` or `sys.exit(<N>)`
  skill_texts[i].must_mention — grep each phrase as case-insensitive
                              substring
  tests[i]                 — grep `def <test>_` in
                              tests/integration/test_golden.py
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

import yaml


# ---- per-gate primitives ---------------------------------------------

def grep(path: Path, pattern: str, *, regex: bool = False) -> bool:
    try:
        text = path.read_text()
    except FileNotFoundError:
        return False
    if regex:
        return re.search(pattern, text) is not None
    return pattern in text


def file_calls_function(py_path: Path, fn_name: str) -> bool:
    """True if the .py file contains any Call whose callable references
    the given function name.

    Handles three call shapes:
      foo(...)            → Name(id=foo)
      mod.foo(...)        → Attribute(attr=foo)
      mod.sub.foo(...)    → Attribute(attr=foo) (recursively)
    """
    try:
        src = py_path.read_text()
    except FileNotFoundError:
        return False
    try:
        tree = ast.parse(src, filename=str(py_path))
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == fn_name:
            return True
        if isinstance(func, ast.Attribute) and func.attr == fn_name:
            return True
    return False


def file_has_exit(py_path: Path, code: int) -> bool:
    """True if the .py file contains `return <code>` or `sys.exit(<code>)`."""
    try:
        text = py_path.read_text()
    except FileNotFoundError:
        return False
    patterns = [
        rf"\breturn\s+{code}\b",
        rf"\bsys\.exit\(\s*{code}\s*\)",
        rf"\bexit\(\s*{code}\s*\)",
    ]
    return any(re.search(p, text) for p in patterns)


def model_has_assert(als_path: Path, assert_name: str) -> bool:
    return grep(als_path, rf"\bassert\s+{re.escape(assert_name)}\b", regex=True)


def test_exists(tests_path: Path, test_name: str) -> bool:
    """True if a Python `def` matching ``test_name`` appears in the
    goldens file. Accepts two forms:

      * Short ID (e.g. ``G80``, ``G46s``) — matches ``def g80...`` /
        ``def g46...``. Sub-case suffix letters (G46s) are treated as
        markers, not part of the canonical name.
      * Full function name (e.g. ``g80_pull_writes_...``) — matches
        ``def g80_pull_writes_...`` or ``def test_g80_pull_writes_...``.
        This is the form the bundled formal-modeling
        ``check_enforcement.py`` consumes, so YAMLs that use full
        names work in BOTH checkers."""
    # Short-ID form (legacy).
    m = re.match(r"^[Gg](\d+)([a-z]*)$", test_name)
    if m:
        n = m.group(1)
        pat = rf"\bdef\s+g{n}[a-z_][a-z0-9_]*\s*\("
        if grep(tests_path, pat, regex=True):
            return True
    # Full-name form (matches the bundled checker's behavior).
    n = re.escape(test_name)
    pat = rf"def\s+(?:test_)?{n}\s*\("
    return grep(tests_path, pat, regex=True)


# ---- per-entry check --------------------------------------------------

def check_entry(entry: dict, repo_root: Path) -> dict:
    failures: list[str] = []

    # models
    for m in entry.get("models") or []:
        path = repo_root / m["file"]
        for name in m.get("asserts") or []:
            if not model_has_assert(path, name):
                failures.append(f"model {m['file']}: missing `assert {name}`")

    # code_gates
    # Each gate may carry any subset of:
    #   must_call:  <fn name>            — AST-checked Call presence
    #   must_match: <regex>              — re.search on file contents
    #   must_exit:  <int>                — return N / sys.exit(N) presence
    # An entry passes only when ALL the present checks for its gate pass.
    for g in entry.get("code_gates") or []:
        path = repo_root / g["file"]
        if not path.exists():
            failures.append(f"code_gate file missing: {g['file']}")
            continue
        if "must_call" in g:
            fn = g["must_call"]
            if not file_calls_function(path, fn):
                failures.append(f"code_gate {g['file']}: no call to `{fn}`")
        if "must_match" in g:
            pat = g["must_match"]
            if not grep(path, pat, regex=True):
                failures.append(f"code_gate {g['file']}: pattern not found: {pat!r}")
        if "must_exit" in g:
            code = g["must_exit"]
            if not file_has_exit(path, code):
                failures.append(f"code_gate {g['file']}: no `return {code}` / `sys.exit({code})`")

    # skill_texts
    for s in entry.get("skill_texts") or []:
        path = repo_root / s["file"]
        if not path.exists():
            failures.append(f"skill_text file missing: {s['file']}")
            continue
        try:
            body = path.read_text().lower()
        except OSError as exc:
            failures.append(f"skill_text {s['file']}: cannot read ({exc})")
            continue
        for phrase in s.get("must_mention") or []:
            if phrase.lower() not in body:
                failures.append(f"skill_text {s['file']}: missing phrase {phrase!r}")

    # tests
    tests_path = repo_root / "tests" / "integration" / "test_golden.py"
    for t in entry.get("tests") or []:
        if not test_exists(tests_path, t):
            failures.append(f"test {t}: not found in {tests_path.relative_to(repo_root)}")

    return {"id": entry["id"], "failures": failures, "ok": not failures}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--enforcement",
                   default="system-models/enforcement.yaml",
                   help="path to enforcement.yaml (default: system-models/enforcement.yaml)")
    p.add_argument("--repo-root", default=".",
                   help="repo root for resolving relative paths (default: cwd)")
    args = p.parse_args()

    repo_root = Path(args.repo_root).resolve()
    enf_path = (repo_root / args.enforcement).resolve()
    try:
        entries = yaml.safe_load(enf_path.read_text())
    except FileNotFoundError:
        sys.stderr.write(f"enforcement file not found: {enf_path}\n")
        return 2
    if not isinstance(entries, list):
        sys.stderr.write(f"enforcement file must be a YAML list of entries\n")
        return 2

    results = [check_entry(e, repo_root) for e in entries]

    # Render
    width = max((len(r["id"]) for r in results), default=10)
    print(f"\n{'property'.ljust(width)}  status   failures")
    print("-" * (width + 30))
    fail_total = 0
    for r in results:
        status = "✓ OK" if r["ok"] else "✗ FAIL"
        print(f"{r['id'].ljust(width)}  {status}   {len(r['failures'])}")
        for f in r["failures"]:
            print(f"  · {f}")
        fail_total += len(r["failures"])

    print()
    print(f"Total: {sum(1 for r in results if r['ok'])}/{len(results)} entries OK; "
          f"{fail_total} failure(s)")
    return 0 if fail_total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Mark a task as ``done`` or ``rejected``. Requires a TaskReport on the task.

If the Task carries ``attributes.verifier`` (set at plan time), the verifier
runs before the done transition is allowed. Three forms are supported:

  - ``skill:NAME`` / ``prompt:CRITERION`` — self-attestation (default).
    The worker applies the skill / prompt themselves and embeds a
    ``## Verifier Attestation`` block in the TaskReport with fields
    ``verifier:`` (must match the task verifier verbatim),
    ``verdict: PASS|FAIL[: reason]``, and ``evidence:`` (free-form).
  - ``verify-skill:NAME`` / ``verify-prompt:CRITERION`` — opt-in:
    re-run as an independent ``claude -p`` subprocess that judges the
    report. Higher cost; useful when self-attestation isn't trusted.
  - <shell command> — spawn the script with PM_* env + positional shas.

Verifier exit 0 = pass, non-zero = fail. The done TaskStatus records
``verifier``, ``verifier_exit``, and a truncated ``verifier_summary`` so
the audit chain documents WHO checked the work and what they observed.

Usage:
  finished.py --task SHA [--rejected] [--note "..."] [--verifier-timeout SECONDS]
              [--skip-verifier] [--verifier-input -|FILE]

Exit codes:
  0  task closed
  6  task not in working/new
  7  no TaskReport on the task
  9  verifier failed (--rejected bypasses this; --skip-verifier overrides)
  10 sticky-context refusal: task is bound to a different PM_CONTEXT_ID
  13 --skip-verifier without --note "<reason>" of >= 10 chars
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from typing import Any

import store


SUMMARY_BUDGET = 4096   # truncate verifier output to this many chars
DEFAULT_TIMEOUT = 60


def truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    half = (n - 8) // 2
    return s[:half] + "\n…[trim]…\n" + s[-half:]


VERDICT_PREFIX = "VERDICT:"
LLM_DEFAULT_TIMEOUT = 120

ATTESTATION_HEADING = "## Verifier Attestation"


def parse_attestation(report_text: str) -> dict[str, str] | None:
    """Find a ``## Verifier Attestation`` block in a TaskReport body.

    Expected shape::

        ## Verifier Attestation

        verifier: <verbatim verifier string>
        verdict: PASS|FAIL[: reason]
        evidence:
          <free-form, may be multi-line; runs until next ``## `` or EOF>

    Returns dict with keys ``verifier``, ``verdict``, ``evidence`` on
    success, or ``None`` if the heading is absent. Missing fields are
    returned as empty strings so the caller can produce a clear error.
    """
    lines = report_text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip() == ATTESTATION_HEADING:
            start = i + 1
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start, len(lines)):
        if lines[j].startswith("## ") and lines[j].strip() != ATTESTATION_HEADING:
            end = j
            break
    block = lines[start:end]

    out = {"verifier": "", "verdict": "", "evidence": ""}
    i = 0
    while i < len(block):
        line = block[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue
        lower = stripped.lower()
        if lower.startswith("verifier:"):
            out["verifier"] = stripped.split(":", 1)[1].strip()
            i += 1
        elif lower.startswith("verdict:"):
            out["verdict"] = stripped.split(":", 1)[1].strip()
            i += 1
        elif lower.startswith("evidence:"):
            inline = stripped.split(":", 1)[1].strip()
            collected: list[str] = [inline] if inline else []
            i += 1
            while i < len(block):
                nxt = block[i]
                nxt_stripped = nxt.strip()
                if nxt_stripped.lower().startswith(
                    ("verifier:", "verdict:", "evidence:")
                ):
                    break
                collected.append(nxt)
                i += 1
            out["evidence"] = "\n".join(collected).strip()
        else:
            i += 1
    return out


def fetch_llm_context(task_sha: str, report_sha: str) -> dict[str, str]:
    """Pull task and report contents for inclusion in an LLM prompt."""
    import mcp_client
    task = store.get_task(task_sha) or {}
    report = mcp_client.tool("get_item_by_hash", {"text_sha256": report_sha}) or {}
    if not isinstance(report, dict):
        report = {}
    attrs = task.get("attributes") or {}
    return {
        "task_title": task.get("title", ""),
        "task_body": attrs.get("body") or task.get("text", ""),
        "task_slug": attrs.get("slug", ""),
        "report_title": report.get("title", ""),
        "report_body": report.get("text", ""),
    }


def parse_verdict(stdout: str) -> tuple[int, str]:
    """Find the last ``VERDICT:`` line. PASS → exit 0, FAIL → exit 1.
    Missing verdict → exit 2 (treated as failure)."""
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith(VERDICT_PREFIX):
            verdict_body = line[len(VERDICT_PREFIX):].strip()
            if verdict_body.upper().startswith("PASS"):
                return 0, verdict_body
            return 1, verdict_body
    return 2, "no VERDICT line found in LLM output"


def build_prompt_verifier_prompt(criterion: str, ctx: dict[str, str]) -> str:
    return f"""You are a verifier for an immutable task-management chain.

Decide whether the worker's report demonstrates that the criterion is met.

=== TASK: {ctx['task_title']} (slug: {ctx['task_slug']}) ===
{ctx['task_body']}

=== WORKER'S REPORT: {ctx['report_title']} ===
{ctx['report_body']}

=== CRITERION ===
{criterion}

=== INSTRUCTIONS ===
Be strict. If the report doesn't contain enough evidence to satisfy the criterion, fail.
You may include reasoning in your response, but the LAST line MUST be EXACTLY one of:
  VERDICT: PASS
  VERDICT: FAIL: <one short sentence with the reason>
"""


def build_skill_verifier_prompt(skill_name: str, ctx: dict[str, str]) -> str:
    return f"""You are a verifier for an immutable task-management chain. Apply the verification logic of the '{skill_name}' skill to the work below.

If '{skill_name}' is available as a skill, invoke it. Otherwise, evaluate the work against what '{skill_name}' would check based on its name.

=== TASK: {ctx['task_title']} (slug: {ctx['task_slug']}) ===
{ctx['task_body']}

=== WORKER'S REPORT: {ctx['report_title']} ===
{ctx['report_body']}

=== INSTRUCTIONS ===
Be strict. If the report doesn't satisfy what '{skill_name}' would require, fail.
You may include reasoning, but the LAST line MUST be EXACTLY one of:
  VERDICT: PASS
  VERDICT: FAIL: <one short sentence with the reason>
"""


def run_llm_verifier(prompt: str, *, verifier_str: str, timeout: int) -> dict[str, Any]:
    """Spawn the `claude` CLI with a wrapped prompt, parse the VERDICT line."""
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return {
            "verifier": verifier_str,
            "verifier_exit": 127,
            "verifier_summary": (
                "`claude` CLI not found on PATH. The skill: and prompt: "
                "verifier variants spawn Claude Code as a subprocess; "
                "install Claude Code or use a script-path verifier instead."
            ),
            "verifier_timeout": False,
        }
    except subprocess.TimeoutExpired:
        return {
            "verifier": verifier_str,
            "verifier_exit": 124,
            "verifier_summary": f"TIMEOUT after {timeout}s (LLM verifier)",
            "verifier_timeout": True,
        }
    stdout = proc.stdout.decode("utf-8", errors="replace")
    stderr = proc.stderr.decode("utf-8", errors="replace")
    exit_code, verdict_text = parse_verdict(stdout)
    summary = (
        f"verdict: {verdict_text}\n--llm stdout--\n{stdout}"
        + (f"\n--llm stderr--\n{stderr}" if stderr.strip() else "")
    )
    return {
        "verifier": verifier_str,
        "verifier_exit": exit_code,
        "verifier_summary": truncate(summary, SUMMARY_BUDGET),
        "verifier_timeout": False,
    }


def verify_attestation(
    *, verifier: str, report_text: str
) -> dict[str, Any]:
    """Default check for ``skill:`` / ``prompt:`` verifiers.

    The worker is expected to apply the skill / prompt themselves and
    embed a ``## Verifier Attestation`` block in the TaskReport. We
    require:
      - the block exists,
      - ``verifier:`` matches the task's verifier verbatim,
      - ``verdict: PASS``.

    Anything else fails with exit 1.
    """
    block = parse_attestation(report_text)
    if block is None:
        return {
            "verifier": verifier,
            "verifier_exit": 1,
            "verifier_summary": (
                "missing '## Verifier Attestation' block in TaskReport. "
                f"task declares verifier='{verifier}'; the worker must "
                "apply it and embed an attestation block (verifier:, "
                "verdict: PASS|FAIL[: reason], evidence:) in the report."
            ),
            "verifier_timeout": False,
        }
    declared = block.get("verifier", "")
    verdict = block.get("verdict", "")
    evidence = block.get("evidence", "")
    if declared != verifier:
        return {
            "verifier": verifier,
            "verifier_exit": 1,
            "verifier_summary": truncate(
                f"attestation verifier mismatch: task='{verifier}' "
                f"attestation='{declared}'. Must match verbatim.",
                SUMMARY_BUDGET,
            ),
            "verifier_timeout": False,
        }
    if not verdict.upper().startswith("PASS"):
        return {
            "verifier": verifier,
            "verifier_exit": 1,
            "verifier_summary": truncate(
                f"attestation verdict not PASS: '{verdict}'\n"
                f"--evidence--\n{evidence}",
                SUMMARY_BUDGET,
            ),
            "verifier_timeout": False,
        }
    return {
        "verifier": verifier,
        "verifier_exit": 0,
        "verifier_summary": truncate(
            f"attestation PASS\n--evidence--\n{evidence}",
            SUMMARY_BUDGET,
        ),
        "verifier_timeout": False,
    }


def run_verifier(
    *,
    verifier: str,
    task_sha: str,
    report_sha: str,
    report_text: str,
    queue: str,
    slug: str,
    timeout: int,
    stdin_data: bytes | None = None,
) -> dict[str, Any]:
    """Dispatch on verifier prefix:

      ``skill:NAME`` / ``prompt:CRITERION`` — default: parse the
                       worker's ``## Verifier Attestation`` block in
                       the TaskReport. The worker is responsible for
                       applying the skill / prompt; this gate just
                       enforces that they did and recorded the verdict.
      ``verify-skill:NAME`` / ``verify-prompt:CRITERION`` — opt-in:
                       spawn ``claude -p`` as a separate subprocess to
                       independently re-check the task + report. Useful
                       when self-attestation is not trusted enough.
      anything else  — treat as a shell command path; pass task / report
                       sha as positional args plus PM_* env.
    """
    if verifier.startswith("skill:") or verifier.startswith("prompt:"):
        return verify_attestation(verifier=verifier, report_text=report_text)

    if verifier.startswith("verify-skill:"):
        skill_name = verifier[len("verify-skill:"):].strip()
        ctx = fetch_llm_context(task_sha, report_sha)
        prompt = build_skill_verifier_prompt(skill_name, ctx)
        return run_llm_verifier(prompt, verifier_str=verifier,
                                timeout=max(timeout, LLM_DEFAULT_TIMEOUT))
    if verifier.startswith("verify-prompt:"):
        criterion = verifier[len("verify-prompt:"):].strip()
        ctx = fetch_llm_context(task_sha, report_sha)
        prompt = build_prompt_verifier_prompt(criterion, ctx)
        return run_llm_verifier(prompt, verifier_str=verifier,
                                timeout=max(timeout, LLM_DEFAULT_TIMEOUT))

    # Shell command path (existing behavior).
    env = dict(os.environ)
    env.update({
        "PM_TASK": task_sha,
        "PM_REPORT_SHA": report_sha,
        "PM_QUEUE": queue,
        "PM_SLUG": slug,
        "PM_VERIFIER": verifier,
    })
    argv = shlex.split(verifier) + [task_sha, report_sha]
    try:
        proc = subprocess.run(
            argv,
            env=env,
            capture_output=True,
            timeout=timeout,
            input=stdin_data,
        )
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        summary = (stdout + ("\n--stderr--\n" + stderr if stderr else "")).strip()
        return {
            "verifier": verifier,
            "verifier_exit": proc.returncode,
            "verifier_summary": truncate(summary, SUMMARY_BUDGET),
            "verifier_timeout": False,
        }
    except subprocess.TimeoutExpired:
        return {
            "verifier": verifier,
            "verifier_exit": 124,
            "verifier_summary": f"TIMEOUT after {timeout}s",
            "verifier_timeout": True,
        }
    except FileNotFoundError as exc:
        return {
            "verifier": verifier,
            "verifier_exit": 127,
            "verifier_summary": f"verifier not found: {exc}",
            "verifier_timeout": False,
        }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--task", required=True)
    p.add_argument("--rejected", action="store_true",
                   help="mark as 'rejected' instead of 'done' (skips verifier)")
    p.add_argument("--note", default="")
    p.add_argument("--verifier-timeout", type=int, default=DEFAULT_TIMEOUT)
    p.add_argument("--skip-verifier", action="store_true",
                   help="DANGER: skip the verifier even if the task declares one. "
                        "Requires --note \"<reason>\" of at least 10 chars; "
                        "the audit chain records verifier_exit=-1 + the note "
                        "so a future auditor can see WHY this bypass fired.")
    p.add_argument("--context-id", default=None,
                   help="sticky context id (overrides $PM_CONTEXT_ID)")
    args = p.parse_args()

    # --skip-verifier requires a non-trivial --note explaining the bypass.
    # Records of this form are auditable (verifier_exit=-1 plus the note
    # text on the closing TaskStatus); without the note the audit chain
    # can show WHO bypassed but not WHY.
    if args.skip_verifier:
        note = (args.note or "").strip()
        if len(note) < 10:
            sys.stderr.write(
                "refusing: --skip-verifier requires --note \"<reason>\" of "
                "at least 10 chars explaining the bypass. The audit chain "
                "records verifier_exit=-1 but a future auditor needs to "
                "know WHY this specific task was bypassed.\n"
                "  Example: --note \"verifier flake; manually re-checked tests pass\"\n"
            )
            return 13

    latest_st = store.latest_status(args.task)
    current = store.status_value(latest_st)
    if current not in ("working", "new"):
        sys.stderr.write(f"refusing: task {args.task[:12]} status is '{current}'\n")
        return 6

    # Full sticky-chain check (matches executing.py). The previous
    # version only inspected the task's own latest context_id, which
    # missed two cases: (a) a sticky parent's binding wasn't enforced
    # for non-sticky children, and (b) after a reclaim clears the own
    # binding, ANY agent could close the task. Use the same gate
    # executing.py uses so all four state-mutating subcommands
    # (executing/heartbeat/report/finished) fail identically.
    agent_context = args.context_id or os.environ.get("PM_CONTEXT_ID") or None
    try:
        store.check_sticky_eligibility(args.task, agent_context)
    except (store.StickyContextMismatch, store.StickyContextConflict) as e:
        sys.stderr.write(f"refusing: {e}\n")
        return 10

    report = store.latest_report(args.task)
    if report is None:
        sys.stderr.write(
            "refusing: no TaskReport found — submit a report first via report.py\n"
        )
        return 7

    task = store.get_task(args.task) or {}
    task_attrs = task.get("attributes") or {}
    verifier = (task_attrs.get("verifier") or "").strip()

    extra_attrs: dict[str, Any] = {}
    final_status = "rejected" if args.rejected else "done"

    if verifier and final_status == "done" and not args.skip_verifier:
        sys.stderr.write(f"running verifier: {verifier}\n")
        result = run_verifier(
            verifier=verifier,
            task_sha=args.task,
            report_sha=report["text_sha256"],
            report_text=report.get("text", ""),
            queue=task_attrs.get("queue", ""),
            slug=task_attrs.get("slug", ""),
            timeout=args.verifier_timeout,
        )
        extra_attrs.update(result)
        if result["verifier_exit"] != 0:
            sys.stderr.write(
                f"verifier failed (exit {result['verifier_exit']}); not finishing.\n"
                f"summary: {result['verifier_summary'][:512]}\n"
            )
            # Record the failed attempt as an attribute on a `working` ghost?
            # No — we don't append on failure; the task stays working. The
            # verdict is in stderr / exit code. Caller can submit a fresh
            # report and retry.
            return 9
    elif verifier and args.skip_verifier:
        extra_attrs["verifier"] = verifier
        extra_attrs["verifier_exit"] = -1
        extra_attrs["verifier_summary"] = "SKIPPED via --skip-verifier"

    out = store.append_status(
        args.task,
        final_status,
        note=args.note or f"finished as {final_status}",
        proof_report_sha=report["record_sha256"],
        extra_attrs=extra_attrs or None,
    )
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

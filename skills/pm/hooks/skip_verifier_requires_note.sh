#!/usr/bin/env bash
# PreToolUse hook — refuse `pm finished --skip-verifier` unless the
# caller also passes a non-trivial `--note "..."` justifying the bypass.
#
# Why: --skip-verifier is the documented escape hatch for declared
# verifiers, and `pm finished` records `verifier_exit = -1` on the
# closing status so a downstream auditor can grep for bypasses. But
# the recorded note defaults to empty — the audit chain shows WHO
# bypassed, not WHY. This hook makes the why mandatory at the agent
# boundary.
#
# Triggered by:
#   {
#     "hooks": {
#       "PreToolUse": [
#         {
#           "matcher": "Bash",
#           "hooks": [{
#             "type": "command",
#             "command": "$CLAUDE_PROJECT_DIR/skills/pm/hooks/skip_verifier_requires_note.sh"
#           }]
#         }
#       ]
#     }
#   }
#
# Override: PM_HOOK_ALLOW_BARE_SKIP=1 disables the rule.

set -u

if [[ "${PM_HOOK_ALLOW_BARE_SKIP:-}" == "1" ]]; then
  exit 0
fi

if ! command -v python3 >/dev/null 2>&1; then
  # Fail open without python3 — too brittle to parse robustly in pure bash.
  exit 0
fi

# Delegate the parsing to python3 (handles UTF-8, quotes, prefixes).
# Stage stdin to a temp file BEFORE the heredoc — `python3 - <<'PY' … PY`
# would consume the heredoc as python's stdin, dropping the hook payload.
# (Same trap that bit extract_skill_steps.sh weeks ago.)
payload_file=$(mktemp -t pm-hook-payload-XXXX)
trap 'rm -f "$payload_file"' EXIT
cat > "$payload_file"

PAYLOAD_PATH="$payload_file" python3 <<'PY'
import json
import re
import shlex
import sys


def is_pm_finished_invocation(cmd: str) -> bool:
    """True iff `cmd` actually invokes `pm finished` (as opposed to
    just mentioning the string in echo / grep / a comment / a test).

    Splits on common command-chaining separators (|, &&, ||, ;) and
    asks each segment: stripping leading env-var assignments and
    sudo/timeout/etc., is the leading argv `pm` and the next argv
    `finished`?

    A bare leading word that resolves to a path ending in `pm` (e.g.
    `./pm`, `/abs/path/pm`, `~/.claude/skills/.../pm`) also counts.
    """
    segments = re.split(r'\s*(?:\||\|\||&&|;)\s*', cmd)
    for seg in segments:
        try:
            argv = shlex.split(seg, posix=True)
        except ValueError:
            continue
        # Drop leading env-var assignments (FOO=bar BAZ=qux ...).
        while argv and re.fullmatch(r"[A-Z_][A-Z0-9_]*=.*", argv[0]):
            argv = argv[1:]
        # Drop leading sudo/timeout/nohup wrappers.
        while argv and argv[0] in ("sudo", "timeout", "nohup", "env"):
            argv = argv[1:] if argv[0] != "env" else _drop_env(argv)
            if argv and argv[0] in ("-n",) and len(argv) > 1:
                argv = argv[1:]
        if not argv:
            continue
        # First token must be `pm` (or a path ending in /pm).
        head = argv[0]
        head_basename = head.rsplit("/", 1)[-1]
        if head_basename != "pm":
            continue
        # Second token must be `finished`.
        if len(argv) >= 2 and argv[1] == "finished":
            return True
    return False


def _drop_env(argv):
    # `env` wrapper takes optional flags then KEY=val ... cmd args.
    out = argv[1:]
    while out and re.fullmatch(r"[A-Z_][A-Z0-9_]*=.*", out[0]):
        out = out[1:]
    return out


def extract_note(argv: list[str]) -> str:
    """Return the value of --note from argv, or '' if not present."""
    for i, tok in enumerate(argv):
        if tok == "--note" and i + 1 < len(argv):
            return argv[i + 1]
        if tok.startswith("--note="):
            return tok[len("--note="):]
    return ""


def has_skip_verifier(argv: list[str]) -> bool:
    return "--skip-verifier" in argv


def emit_deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))


def main() -> int:
    import os
    try:
        with open(os.environ["PAYLOAD_PATH"]) as fh:
            payload = json.load(fh)
    except Exception:
        return 0

    cmd = ((payload.get("tool_input") or {}).get("command")) or ""
    if not cmd:
        return 0

    # Cheap pre-filter: if the literal phrase isn't anywhere in the
    # command, no need to do the expensive parse.
    if "pm finished" not in cmd and "/pm finished" not in cmd:
        return 0

    if not is_pm_finished_invocation(cmd):
        return 0

    # Find the segment that's actually `pm finished ...` and inspect it.
    # We know there's at least one such segment from is_pm_finished_invocation.
    segments = re.split(r'\s*(?:\||\|\||&&|;)\s*', cmd)
    for seg in segments:
        try:
            argv = shlex.split(seg, posix=True)
        except ValueError:
            continue
        while argv and re.fullmatch(r"[A-Z_][A-Z0-9_]*=.*", argv[0]):
            argv = argv[1:]
        while argv and argv[0] in ("sudo", "timeout", "nohup"):
            argv = argv[1:]
        if not argv:
            continue
        if argv[0].rsplit("/", 1)[-1] != "pm" or len(argv) < 2 or argv[1] != "finished":
            continue
        if not has_skip_verifier(argv):
            continue  # this `pm finished` doesn't use --skip-verifier; skip
        note = extract_note(argv).strip()
        if len(note) >= 10:
            return 0  # justified bypass — allow
        emit_deny(
            "pm finished --skip-verifier requires --note \"<reason>\" of "
            "at least 10 chars explaining the bypass. The audit chain "
            "records the verifier_exit=-1 marker but a future auditor "
            "needs to know WHY this specific task was bypassed. Add e.g.: "
            "--note \"verifier flake; manually re-checked tests pass\". "
            "Set PM_HOOK_ALLOW_BARE_SKIP=1 to disable this hook."
        )
        return 0

    return 0


sys.exit(main())
PY

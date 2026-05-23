#!/usr/bin/env python3
"""pm limits — read Claude Code rate-limit state and decide spawn/wait/stop.

Source of truth is the JSON cache at $CLAUDE_CONFIG_DIR/rate-limits.json
(default ~/.claude/rate-limits.json), written by this skill's capture
hook: skills/pm/hooks/statusline_capture.sh. That hook is what the user
wires into .claude/settings.json's statusLine.command — it snapshots
the harness-fed input JSON on every render, then delegates display
back to the user's prior statusline if one is configured. The cache
contains the same .rate_limits.five_hour / .seven_day fields the
statusline reads, so this script and the status bar always agree.

Exit codes (so callers can branch without parsing JSON):

  0 ok       — under all caps; safe to spawn workers
  1 unknown  — cache missing, stale, or malformed; caller decides
  2 stop     — seven-day usage breaches the reserve; stop spawning
  3 wait     — five-hour usage at/above max; sleep until reset

With --json, the decision payload is printed regardless of exit code.
"""
import argparse
import json
import sys
import time
from pathlib import Path

import os

CACHE = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude")) / "rate-limits.json"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--reserve-weekly",
        type=float,
        default=20.0,
        help="Stop spawning when seven-day usage exceeds (100 - RESERVE)%%. "
             "Default 20 (i.e. cap at 80%% weekly usage).",
    )
    ap.add_argument(
        "--max-five-hour",
        type=float,
        default=95.0,
        help="Wait when five-hour usage is at/above this percent. Default 95.",
    )
    ap.add_argument(
        "--max-stale-sec",
        type=int,
        default=600,
        help="Treat cache as unknown if its mtime is older than this. Default 600.",
    )
    ap.add_argument(
        "--cache-path",
        default=str(CACHE),
        help=f"Override the cache file location (default: {CACHE}).",
    )
    ap.add_argument("--json", action="store_true", help="Always print the decision JSON to stdout.")
    args = ap.parse_args()

    cache_path = Path(args.cache_path)
    emit = (lambda d: print(json.dumps(d, indent=2))) if args.json else (lambda d: None)

    if not cache_path.exists():
        emit({"status": "unknown", "reason": f"cache missing: {cache_path}"})
        return 1

    age = time.time() - cache_path.stat().st_mtime
    if age > args.max_stale_sec:
        emit({"status": "unknown", "reason": f"cache stale: {int(age)}s old", "cache_age_sec": int(age)})
        return 1

    try:
        data = json.loads(cache_path.read_text())
    except Exception as exc:  # noqa: BLE001
        emit({"status": "unknown", "reason": f"cache parse error: {exc}"})
        return 1

    rl = data.get("rate_limits") or {}
    five = rl.get("five_hour") or {}
    seven = rl.get("seven_day") or {}

    five_pct = _as_float(five.get("used_percentage"))
    seven_pct = _as_float(seven.get("used_percentage"))
    five_reset = _as_int(five.get("resets_at"))
    seven_reset = _as_int(seven.get("resets_at"))

    decision = {
        "five_hour_pct": five_pct,
        "five_hour_reset_at": five_reset,
        "seven_day_pct": seven_pct,
        "seven_day_reset_at": seven_reset,
        "weekly_budget_left_pct": round(100.0 - seven_pct, 2),
        "cache_age_sec": int(age),
        "reserve_weekly": args.reserve_weekly,
        "max_five_hour": args.max_five_hour,
    }

    weekly_cap = 100.0 - args.reserve_weekly
    if seven_pct >= weekly_cap:
        decision["status"] = "stop"
        decision["reason"] = (
            f"seven-day usage {seven_pct:.1f}% >= cap {weekly_cap:.1f}% "
            f"(reserve {args.reserve_weekly:.1f}%)"
        )
        decision["wait_seconds"] = _delta_until(seven_reset)
        emit(decision)
        return 2

    if five_pct >= args.max_five_hour:
        wait = _delta_until(five_reset)
        if wait is None or wait <= 0:
            wait = 1800
        decision["status"] = "wait"
        decision["reason"] = f"five-hour usage {five_pct:.1f}% >= {args.max_five_hour:.1f}%"
        decision["wait_seconds"] = max(60, wait + 30)
        emit(decision)
        return 3

    decision["status"] = "ok"
    decision["wait_seconds"] = 0
    emit(decision)
    return 0


def _as_float(val) -> float:
    try:
        return float(val) if val is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _as_int(val):
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _delta_until(reset_at):
    if not reset_at:
        return None
    return max(0, int(reset_at - time.time()))


if __name__ == "__main__":
    sys.exit(main())

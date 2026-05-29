#!/usr/bin/env python3
"""pm limits — read Claude/Codex rate-limit state and decide spawn/wait/stop.

Claude source:
  $CLAUDE_CONFIG_DIR/pm-rate-limits.json (default ~/.claude/pm-rate-limits.json),
  written by skills/pm/hooks/statusline_capture.sh.

Codex source:
  the freshest token-count event in the current thread's
  ~/.codex/sessions/*.jsonl file, falling back to the latest session log.

Exit codes (so callers can branch without parsing JSON):

  0 ok       — under all caps; safe to spawn workers
  1 unknown  — source missing or malformed; budget is undeterminable
  2 stop     — seven-day usage breaches the reserve; stop spawning
  3 wait     — five-hour usage at/above max; sleep until reset
  4 stale    — source found and parsed, but older than --max-stale-sec;
               the budget signal is UNENFORCEABLE, not a stop. The
               last-known decision is exposed as `fresh_status` and the
               raw numbers + `source_age_seconds` are included so the
               caller can judge for itself. Distinct from `unknown`
               (no data at all) and from `stop` (real over-budget) on
               purpose: a stale read silently treated as "stop" is a
               footgun that has caused false work-stoppages.

With --json, the decision payload is printed regardless of exit code.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Snapshot:
    source: str
    path: Path
    age_sec: int
    five_pct: float
    five_reset: int | None
    seven_pct: float
    seven_reset: int | None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--reserve-weekly",
        type=float,
        default=20.0,
        help="Headroom (in percentage points) to leave UNTOUCHED on the "
             "seven-day bucket. Stop spawning when seven-day usage exceeds "
             "(100 - RESERVE)%%. NOTE the inversion: --reserve-weekly 30 "
             "means 'stop at 70%% consumed', not 'use 30%%'. Default 20 "
             "(cap at 80%% weekly usage).",
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
        help="Override the rate-limit source file. Claude uses a JSON cache; "
             "Codex uses a session JSONL file.",
    )
    ap.add_argument("--json", action="store_true", help="Always print the decision JSON to stdout.")
    args = ap.parse_args(argv)

    emit = (lambda d: print(json.dumps(d, indent=2))) if args.json else (lambda d: None)
    # Snapshots are loaded regardless of age — staleness is a separate
    # axis from loadability, decided here, not buried in the loader.
    snapshot, reason = load_snapshot(args.cache_path)
    if snapshot is None:
        emit({"status": "unknown", "reason": reason, "source_age_seconds": None})
        return 1

    decision = {
        "source": snapshot.source,
        "source_path": str(snapshot.path),
        "five_hour_pct": snapshot.five_pct,
        "five_hour_reset_at": snapshot.five_reset,
        "seven_day_pct": snapshot.seven_pct,
        "seven_day_reset_at": snapshot.seven_reset,
        "weekly_budget_left_pct": round(100.0 - snapshot.seven_pct, 2),
        "cache_age_sec": snapshot.age_sec,        # kept for back-compat
        "source_age_seconds": snapshot.age_sec,
        "max_stale_sec": args.max_stale_sec,
        "reserve_weekly": args.reserve_weekly,
        "max_five_hour": args.max_five_hour,
    }

    budget_status, budget_reason, wait_seconds = _classify(snapshot, args)
    decision["wait_seconds"] = wait_seconds if wait_seconds is not None else 0
    if budget_reason:
        decision["reason"] = budget_reason

    # A source that exists and parses but is older than the staleness
    # threshold yields an UNENFORCEABLE signal — not a stop. Surface the
    # would-be decision as `fresh_status` so the caller can still see
    # "last-known weekly was 1%, clearly safe" rather than collapsing to
    # an ambiguous "unknown" that a worker might (wrongly) read as stop.
    if snapshot.age_sec > args.max_stale_sec:
        decision["status"] = "stale"
        decision["stale"] = True
        decision["fresh_status"] = budget_status
        decision["reason"] = (
            f"source is {snapshot.age_sec}s old (> {args.max_stale_sec}s threshold); "
            f"budget signal unenforceable. Last-known decision would be "
            f"'{budget_status}'" + (f": {budget_reason}" if budget_reason else "") + "."
        )
        emit(decision)
        return 4

    decision["stale"] = False
    decision["status"] = budget_status
    emit(decision)
    return {"ok": 0, "stop": 2, "wait": 3}[budget_status]


def _classify(snapshot: Snapshot, args) -> tuple[str, str | None, int | None]:
    """Pure budget decision on a snapshot, independent of staleness.
    Returns (status, reason, wait_seconds)."""
    weekly_cap = 100.0 - args.reserve_weekly
    if snapshot.seven_pct >= weekly_cap:
        return (
            "stop",
            f"seven-day usage {snapshot.seven_pct:.1f}% >= cap {weekly_cap:.1f}% "
            f"(reserve {args.reserve_weekly:.1f}%)",
            _delta_until(snapshot.seven_reset),
        )
    if snapshot.five_pct >= args.max_five_hour:
        wait = _delta_until(snapshot.five_reset)
        if wait is None or wait <= 0:
            wait = 1800
        return (
            "wait",
            f"five-hour usage {snapshot.five_pct:.1f}% >= {args.max_five_hour:.1f}%",
            max(60, wait + 30),
        )
    return "ok", None, 0


def load_snapshot(cache_path: str | None = None) -> tuple[Snapshot | None, str]:
    """Return the FRESHEST parseable snapshot across all candidate
    sources. Staleness no longer disqualifies a source here — that
    judgement is made by the caller against --max-stale-sec — but when
    several sources exist we still prefer the youngest, so a fresh
    lower-priority source wins over a stale higher-priority one."""
    reasons: list[str] = []
    best: Snapshot | None = None
    for source, path in iter_candidate_paths(cache_path):
        snapshot, reason = load_snapshot_from_path(path, source)
        if snapshot is None:
            reasons.append(reason)
            continue
        if best is None or snapshot.age_sec < best.age_sec:
            best = snapshot
    if best is not None:
        return best, ""
    if not reasons:
        return None, "no Claude or Codex rate-limit source found"
    return None, "; ".join(reasons)


def iter_candidate_paths(cache_path: str | None):
    if cache_path:
        yield "explicit", Path(cache_path)
        return

    seen: set[Path] = set()

    for path in _codex_thread_paths():
        if path not in seen:
            seen.add(path)
            yield "codex", path

    for claude in claude_cache_candidates():
        if claude not in seen:
            seen.add(claude)
            yield "claude", claude

    for path in _latest_codex_session_paths(limit=5):
        if path not in seen:
            seen.add(path)
            yield "codex", path


def default_claude_cache() -> Path:
    return _claude_cache_dir() / "pm-rate-limits.json"


def claude_cache_candidates() -> list[Path]:
    cache_dir = _claude_cache_dir()
    return [
        cache_dir / "pm-rate-limits.json",
        cache_dir / "rate-limits.json",
    ]


def _claude_cache_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))


def codex_sessions_dir() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")) / "sessions"


def _codex_thread_paths() -> list[Path]:
    thread_id = os.environ.get("CODEX_THREAD_ID")
    if not thread_id:
        return []
    root = codex_sessions_dir()
    if not root.exists():
        return []
    return sorted(root.rglob(f"*{thread_id}*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)


def _latest_codex_session_paths(limit: int) -> list[Path]:
    root = codex_sessions_dir()
    if not root.exists():
        return []
    paths = sorted(root.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return paths[:limit]


def load_snapshot_from_path(path: Path, hinted_source: str) -> tuple[Snapshot | None, str]:
    if not path.exists():
        return None, f"{hinted_source} source missing: {path}"

    age = int(time.time() - path.stat().st_mtime)
    try:
        data = _read_source_payload(path)
        snap = _snapshot_from_payload(data, path, age, hinted_source)
    except Exception as exc:  # noqa: BLE001
        return None, f"{hinted_source} source parse error: {path} ({exc})"
    return snap, ""


def _read_source_payload(path: Path) -> dict:
    if path.suffix == ".jsonl":
        return _read_latest_jsonl_event(path)
    return json.loads(path.read_text())


def _read_latest_jsonl_event(path: Path) -> dict:
    for line in _tail_lines(path):
        if "\"rate_limits\"" not in line:
            continue
        data = json.loads(line)
        if _has_supported_rate_limits(data):
            return data
    raise ValueError("no supported rate-limit event found")


def _tail_lines(path: Path, chunk_size: int = 65536):
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        buf = b""
        pos = size
        while pos > 0:
            read_size = min(chunk_size, pos)
            pos -= read_size
            fh.seek(pos)
            buf = fh.read(read_size) + buf
            lines = buf.splitlines()
            if pos > 0:
                buf = lines[0]
                lines = lines[1:]
            else:
                buf = b""
            for line in reversed(lines):
                if line:
                    yield line.decode("utf-8")
        if buf:
            yield buf.decode("utf-8")


def _has_supported_rate_limits(data: dict) -> bool:
    try:
        _normalize_rate_limits(data)
    except ValueError:
        return False
    return True


def _snapshot_from_payload(data: dict, path: Path, age_sec: int, hinted_source: str) -> Snapshot:
    source, five_pct, five_reset, seven_pct, seven_reset = _normalize_rate_limits(data)
    if hinted_source == "explicit":
        hinted_source = source
    return Snapshot(
        source=hinted_source,
        path=path,
        age_sec=age_sec,
        five_pct=five_pct,
        five_reset=five_reset,
        seven_pct=seven_pct,
        seven_reset=seven_reset,
    )


def _normalize_rate_limits(data: dict) -> tuple[str, float, int | None, float, int | None]:
    if not isinstance(data, dict):
        raise ValueError("top-level JSON must be an object")

    rate_limits = data.get("rate_limits")
    if isinstance(rate_limits, dict) and ("five_hour" in rate_limits or "seven_day" in rate_limits):
        five = rate_limits.get("five_hour") or {}
        seven = rate_limits.get("seven_day") or {}
        return (
            "claude",
            _as_float(five.get("used_percentage")),
            _as_int(five.get("resets_at")),
            _as_float(seven.get("used_percentage")),
            _as_int(seven.get("resets_at")),
        )

    payload = data.get("payload")
    if isinstance(payload, dict):
        rate_limits = payload.get("rate_limits")
    if isinstance(rate_limits, dict) and ("primary" in rate_limits or "secondary" in rate_limits):
        return _normalize_codex_rate_limits(rate_limits)

    raise ValueError("unsupported rate-limit payload")


def _normalize_codex_rate_limits(rate_limits: dict) -> tuple[str, float, int | None, float, int | None]:
    primary = rate_limits.get("primary") or {}
    secondary = rate_limits.get("secondary") or {}
    entries = [primary, secondary]

    five = _pick_window(entries, 300) or primary
    seven = _pick_window(entries, 10080) or secondary

    return (
        "codex",
        _as_float(five.get("used_percent")),
        _as_int(five.get("resets_at")),
        _as_float(seven.get("used_percent")),
        _as_int(seven.get("resets_at")),
    )


def _pick_window(entries: list[dict], minutes: int) -> dict | None:
    for entry in entries:
        if _as_int(entry.get("window_minutes")) == minutes:
            return entry
    return None


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

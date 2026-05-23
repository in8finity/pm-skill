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
  1 unknown  — source missing, stale, or malformed; caller decides
  2 stop     — seven-day usage breaches the reserve; stop spawning
  3 wait     — five-hour usage at/above max; sleep until reset

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
        help="Override the rate-limit source file. Claude uses a JSON cache; "
             "Codex uses a session JSONL file.",
    )
    ap.add_argument("--json", action="store_true", help="Always print the decision JSON to stdout.")
    args = ap.parse_args(argv)

    emit = (lambda d: print(json.dumps(d, indent=2))) if args.json else (lambda d: None)
    snapshot, reason = load_snapshot(args.max_stale_sec, args.cache_path)
    if snapshot is None:
        emit({"status": "unknown", "reason": reason})
        return 1

    decision = {
        "source": snapshot.source,
        "source_path": str(snapshot.path),
        "five_hour_pct": snapshot.five_pct,
        "five_hour_reset_at": snapshot.five_reset,
        "seven_day_pct": snapshot.seven_pct,
        "seven_day_reset_at": snapshot.seven_reset,
        "weekly_budget_left_pct": round(100.0 - snapshot.seven_pct, 2),
        "cache_age_sec": snapshot.age_sec,
        "reserve_weekly": args.reserve_weekly,
        "max_five_hour": args.max_five_hour,
    }

    weekly_cap = 100.0 - args.reserve_weekly
    if snapshot.seven_pct >= weekly_cap:
        decision["status"] = "stop"
        decision["reason"] = (
            f"seven-day usage {snapshot.seven_pct:.1f}% >= cap {weekly_cap:.1f}% "
            f"(reserve {args.reserve_weekly:.1f}%)"
        )
        decision["wait_seconds"] = _delta_until(snapshot.seven_reset)
        emit(decision)
        return 2

    if snapshot.five_pct >= args.max_five_hour:
        wait = _delta_until(snapshot.five_reset)
        if wait is None or wait <= 0:
            wait = 1800
        decision["status"] = "wait"
        decision["reason"] = f"five-hour usage {snapshot.five_pct:.1f}% >= {args.max_five_hour:.1f}%"
        decision["wait_seconds"] = max(60, wait + 30)
        emit(decision)
        return 3

    decision["status"] = "ok"
    decision["wait_seconds"] = 0
    emit(decision)
    return 0


def load_snapshot(max_stale_sec: int, cache_path: str | None = None) -> tuple[Snapshot | None, str]:
    reasons: list[str] = []
    for source, path in iter_candidate_paths(cache_path):
        snapshot, reason = load_snapshot_from_path(path, max_stale_sec, source)
        if snapshot is not None:
            return snapshot, reason
        reasons.append(reason)
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


def load_snapshot_from_path(path: Path, max_stale_sec: int, hinted_source: str) -> tuple[Snapshot | None, str]:
    if not path.exists():
        return None, f"{hinted_source} source missing: {path}"

    age = int(time.time() - path.stat().st_mtime)
    if age > max_stale_sec:
        return None, f"{hinted_source} source stale: {path} ({age}s old)"

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

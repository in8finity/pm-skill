#!/usr/bin/env python3
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "skills" / "pm" / "scripts"))
import limits  # noqa: E402


class LimitsTest(unittest.TestCase):
    def run_main(self, *args: str) -> tuple[int, dict]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = limits.main(list(args))
        out = buf.getvalue().strip()
        return rc, json.loads(out)

    def test_claude_cache_path_is_supported(self) -> None:
        now = int(time.time())
        with tempfile.TemporaryDirectory() as td:
            cache_path = Path(td) / "pm-rate-limits.json"
            cache_path.write_text(json.dumps({
                "rate_limits": {
                    "five_hour": {"used_percentage": 96.0, "resets_at": now + 120},
                    "seven_day": {"used_percentage": 40.0, "resets_at": now + 3600},
                }
            }))

            rc, data = self.run_main("--json", "--cache-path", str(cache_path))

        self.assertEqual(rc, 3)
        self.assertEqual(data["status"], "wait")
        self.assertEqual(data["source"], "claude")
        self.assertEqual(data["five_hour_pct"], 96.0)
        self.assertGreaterEqual(data["wait_seconds"], 60)

    def test_codex_jsonl_cache_path_is_supported(self) -> None:
        now = int(time.time())
        with tempfile.TemporaryDirectory() as td:
            session_path = Path(td) / "session.jsonl"
            event = {
                "timestamp": "2026-05-24T01:21:16.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "rate_limits": {
                        "primary": {"used_percent": 10.0, "window_minutes": 300, "resets_at": now + 120},
                        "secondary": {"used_percent": 85.0, "window_minutes": 10080, "resets_at": now + 3600},
                    },
                },
            }
            session_path.write_text(json.dumps(event) + "\n")

            rc, data = self.run_main("--json", "--cache-path", str(session_path))

        self.assertEqual(rc, 2)
        self.assertEqual(data["status"], "stop")
        self.assertEqual(data["source"], "codex")
        self.assertEqual(data["seven_day_pct"], 85.0)

    def test_stale_source_is_not_a_stop(self) -> None:
        # Weekly ~1%, five-hour ~9% — clearly under budget. But the file
        # is older than the staleness threshold, so the verdict must be
        # `stale` (exit 4), NOT collapsed to `unknown` or read as `stop`.
        now = int(time.time())
        with tempfile.TemporaryDirectory() as td:
            cache_path = Path(td) / "pm-rate-limits.json"
            cache_path.write_text(json.dumps({
                "rate_limits": {
                    "five_hour": {"used_percentage": 9.0, "resets_at": now + 120},
                    "seven_day": {"used_percentage": 1.0, "resets_at": now + 3600},
                }
            }))
            old = now - 5000
            os.utime(cache_path, (old, old))

            rc, data = self.run_main(
                "--json", "--cache-path", str(cache_path), "--max-stale-sec", "600"
            )

        self.assertEqual(rc, 4)
        self.assertEqual(data["status"], "stale")
        self.assertTrue(data["stale"])
        self.assertEqual(data["fresh_status"], "ok")
        self.assertGreaterEqual(data["source_age_seconds"], 600)

    def test_missing_source_is_unknown_with_null_age(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "does-not-exist.json"
            rc, data = self.run_main("--json", "--cache-path", str(missing))
        self.assertEqual(rc, 1)
        self.assertEqual(data["status"], "unknown")
        self.assertIsNone(data["source_age_seconds"])

    def test_fresh_source_reports_not_stale(self) -> None:
        now = int(time.time())
        with tempfile.TemporaryDirectory() as td:
            cache_path = Path(td) / "pm-rate-limits.json"
            cache_path.write_text(json.dumps({
                "rate_limits": {
                    "five_hour": {"used_percentage": 9.0, "resets_at": now + 120},
                    "seven_day": {"used_percentage": 1.0, "resets_at": now + 3600},
                }
            }))
            rc, data = self.run_main("--json", "--cache-path", str(cache_path))
        self.assertEqual(rc, 0)
        self.assertEqual(data["status"], "ok")
        self.assertFalse(data["stale"])
        self.assertIn("source_age_seconds", data)

    def test_auto_detects_current_codex_thread(self) -> None:
        now = int(time.time())
        thread_id = "019e56b6-b84d-71a2-927a-71fb598040ba"
        with tempfile.TemporaryDirectory() as home:
            session_dir = Path(home) / ".codex" / "sessions" / "2026" / "05" / "24"
            session_dir.mkdir(parents=True)
            session_path = session_dir / f"rollout-2026-05-24T01-21-16-{thread_id}.jsonl"
            session_path.write_text(json.dumps({
                "timestamp": "2026-05-24T01:21:16.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "rate_limits": {
                        "primary": {"used_percent": 12.0, "window_minutes": 300, "resets_at": now + 120},
                        "secondary": {"used_percent": 21.0, "window_minutes": 10080, "resets_at": now + 3600},
                    },
                },
            }) + "\n")

            with mock.patch.dict(os.environ, {"HOME": home, "CODEX_THREAD_ID": thread_id}, clear=False):
                rc, data = self.run_main("--json")

        self.assertEqual(rc, 0)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["source"], "codex")
        self.assertEqual(data["source_path"], str(session_path))


if __name__ == "__main__":
    unittest.main()

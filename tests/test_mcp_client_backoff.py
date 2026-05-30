#!/usr/bin/env python3
"""Unit tests for mcp_client's HTTP 503 / Retry-After backoff.

hashharness >= v0.5.0 returns 503 + Retry-After when its inflight semaphore
is exhausted. The client must retry instead of treating it as a fatal
"server unreachable" error (which is what the pre-fix code did, because
HTTPError is a subclass of URLError).
"""
from __future__ import annotations

import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "skills" / "pm" / "scripts"))
import mcp_client  # noqa: E402


def _http_error(code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return urllib.error.HTTPError(
        url="http://mock/mcp",
        code=code,
        msg="overload",
        hdrs=headers,  # type: ignore[arg-type]
        fp=None,
    )


def _ok_response(payload: dict) -> mock.MagicMock:
    resp = mock.MagicMock()
    resp.read.return_value = json.dumps(payload).encode("utf-8")
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


class McpClientBackoffTest(unittest.TestCase):
    def setUp(self) -> None:
        # Deterministic jitter (0.75 + 0.5*random.random()) → 1.0 when random=0.5.
        self._random_patch = mock.patch("mcp_client.random.random", return_value=0.5)
        self._random_patch.start()
        self._sleep_calls: list[float] = []
        self._sleep_patch = mock.patch(
            "mcp_client.time.sleep", side_effect=self._sleep_calls.append
        )
        self._sleep_patch.start()
        self.addCleanup(self._random_patch.stop)
        self.addCleanup(self._sleep_patch.stop)

    def test_503_then_200_retries_and_returns_body(self) -> None:
        with mock.patch(
            "mcp_client.urllib.request.urlopen",
            side_effect=[
                _http_error(503, retry_after="2"),
                _ok_response({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}),
            ],
        ) as urlopen:
            result = mcp_client.call("ping")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(urlopen.call_count, 2)
        # Honoured Retry-After=2 with jitter factor 1.0.
        self.assertEqual(self._sleep_calls, [2.0])

    def test_502_504_are_also_retried(self) -> None:
        with mock.patch(
            "mcp_client.urllib.request.urlopen",
            side_effect=[
                _http_error(502),
                _http_error(504),
                _ok_response({"jsonrpc": "2.0", "id": 1, "result": "v"}),
            ],
        ) as urlopen:
            result = mcp_client.call("ping")
        self.assertEqual(result, "v")
        self.assertEqual(urlopen.call_count, 3)
        # Two sleeps (one per retry), both default 1s with jitter factor 1.0.
        self.assertEqual(self._sleep_calls, [1.0, 1.0])

    def test_missing_retry_after_defaults_to_one_second(self) -> None:
        with mock.patch(
            "mcp_client.urllib.request.urlopen",
            side_effect=[
                _http_error(503),  # no Retry-After header
                _ok_response({"jsonrpc": "2.0", "id": 1, "result": None}),
            ],
        ):
            mcp_client.call("ping")
        self.assertEqual(self._sleep_calls, [1.0])

    def test_unparseable_retry_after_falls_back_to_one_second(self) -> None:
        with mock.patch(
            "mcp_client.urllib.request.urlopen",
            side_effect=[
                _http_error(503, retry_after="not-a-number"),
                _ok_response({"jsonrpc": "2.0", "id": 1, "result": None}),
            ],
        ):
            mcp_client.call("ping")
        self.assertEqual(self._sleep_calls, [1.0])

    def test_retry_after_is_bounded(self) -> None:
        # 999s gets clamped to the 30s ceiling.
        with mock.patch(
            "mcp_client.urllib.request.urlopen",
            side_effect=[
                _http_error(503, retry_after="999"),
                _ok_response({"jsonrpc": "2.0", "id": 1, "result": None}),
            ],
        ):
            mcp_client.call("ping")
        self.assertEqual(self._sleep_calls, [30.0])
        self._sleep_calls.clear()
        # 0s gets clamped up to the 0.1s floor (no busy-loop on a buggy server).
        with mock.patch(
            "mcp_client.urllib.request.urlopen",
            side_effect=[
                _http_error(503, retry_after="0"),
                _ok_response({"jsonrpc": "2.0", "id": 1, "result": None}),
            ],
        ):
            mcp_client.call("ping")
        self.assertEqual(self._sleep_calls, [0.1])

    def test_exhausted_retries_exits_3(self) -> None:
        with mock.patch.dict("mcp_client.os.environ", {"PM_MCP_RETRY_MAX": "3"}), \
             mock.patch(
                 "mcp_client.urllib.request.urlopen",
                 side_effect=[_http_error(503, retry_after="1")] * 3,
             ), \
             mock.patch("mcp_client.sys.stderr", new=io.StringIO()) as err:
            with self.assertRaises(SystemExit) as ctx:
                mcp_client.call("ping")
        self.assertEqual(ctx.exception.code, 3)
        self.assertIn("HTTP 503", err.getvalue())
        # Two sleeps (between attempts 1→2 and 2→3); the third failure exits.
        self.assertEqual(len(self._sleep_calls), 2)

    def test_non_retryable_http_error_exits_3_immediately(self) -> None:
        with mock.patch(
            "mcp_client.urllib.request.urlopen",
            side_effect=[_http_error(400)],
        ), mock.patch("mcp_client.sys.stderr", new=io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                mcp_client.call("ping")
        self.assertEqual(ctx.exception.code, 3)
        self.assertEqual(self._sleep_calls, [])

    def test_urlerror_still_exits_2(self) -> None:
        with mock.patch(
            "mcp_client.urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ), mock.patch("mcp_client.sys.stderr", new=io.StringIO()) as err:
            with self.assertRaises(SystemExit) as ctx:
                mcp_client.call("ping")
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("unreachable", err.getvalue())

    def test_call_safe_returns_error_not_exit(self) -> None:
        # An MCP-protocol error (in the JSON body) is still surfaced via the
        # ``(None, error)`` tuple — retry logic doesn't change that contract.
        with mock.patch(
            "mcp_client.urllib.request.urlopen",
            side_effect=[
                _ok_response(
                    {"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "x"}}
                ),
            ],
        ):
            result, err = mcp_client.call_safe("ping")
        self.assertIsNone(result)
        self.assertEqual(err, {"code": -1, "message": "x"})

    def test_call_safe_also_retries_503(self) -> None:
        with mock.patch(
            "mcp_client.urllib.request.urlopen",
            side_effect=[
                _http_error(503, retry_after="1"),
                _ok_response({"jsonrpc": "2.0", "id": 1, "result": {"ok": 1}}),
            ],
        ):
            result, err = mcp_client.call_safe("ping")
        self.assertIsNone(err)
        self.assertEqual(result, {"ok": 1})
        self.assertEqual(self._sleep_calls, [1.0])


if __name__ == "__main__":
    unittest.main()

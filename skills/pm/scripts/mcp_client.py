#!/usr/bin/env python3
"""Minimal JSON-RPC HTTP client for hashharness MCP server.

Connects to ``HASHHARNESS_MCP_URL`` (default ``http://127.0.0.1:38417/mcp``).
Requires the hashharness MCP server to be running with HTTP transport:

    HASHHARNESS_MCP_TRANSPORT=http hashharness-mcp  # or python -m hashharness.mcp_server
"""
from __future__ import annotations

import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from typing import Any

DEFAULT_URL = "http://127.0.0.1:38417/mcp"
_NEXT_ID = 0

# hashharness >= v0.5.0 returns HTTP 503 + Retry-After when its inflight
# semaphore is exhausted; transient 502/504 are treated the same way.
_RETRYABLE_HTTP_STATUSES = (502, 503, 504)


def _next_id() -> int:
    global _NEXT_ID
    _NEXT_ID += 1
    return _NEXT_ID


def _retry_delay(retry_after: str | None) -> float:
    """Parse a Retry-After header value (integer seconds) and bound it.

    Falls back to 1s when missing or unparseable. ±25% jitter is added by
    the caller so concurrent workers don't re-collide on the next slot.
    """
    try:
        delay = float(retry_after) if retry_after is not None else 1.0
    except ValueError:
        delay = 1.0
    return min(max(delay, 0.1), 30.0)


def _post_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """POST a JSON-RPC payload, honouring HTTP 503 + Retry-After backpressure.

    Retries up to ``PM_MCP_RETRY_MAX`` (default 5) on 502/503/504, sleeping
    Retry-After seconds with ±25% jitter so N workers don't synchronise
    their retries. URLError (truly unreachable) and non-retriable HTTP
    errors exit the process immediately, preserving the prior contract.
    """
    url = os.environ.get("HASHHARNESS_MCP_URL", DEFAULT_URL)
    body = json.dumps(payload).encode("utf-8")
    max_attempts = max(1, int(os.environ.get("PM_MCP_RETRY_MAX", "5")))

    for attempt in range(max_attempts):
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # HTTPError must be handled before URLError (subclass relation).
            if exc.code in _RETRYABLE_HTTP_STATUSES and attempt + 1 < max_attempts:
                delay = _retry_delay(exc.headers.get("Retry-After"))
                time.sleep(delay * (0.75 + 0.5 * random.random()))
                continue
            sys.stderr.write(
                f"hashharness MCP HTTP {exc.code} {exc.reason} "
                f"after {attempt + 1} attempt(s)\n"
            )
            sys.exit(3)
        except urllib.error.URLError as exc:
            sys.stderr.write(
                f"hashharness MCP unreachable at {url}: {exc}\n"
                "Start it with: HASHHARNESS_MCP_TRANSPORT=http python -m hashharness.mcp_server\n"
            )
            sys.exit(2)

    # Defensive — the loop above should have returned or exited.
    sys.stderr.write(
        f"hashharness MCP retries exhausted after {max_attempts} attempts\n"
    )
    sys.exit(3)


def call(method: str, params: dict[str, Any] | None = None) -> Any:
    data = _post_payload(
        {
            "jsonrpc": "2.0",
            "id": _next_id(),
            "method": method,
            "params": params or {},
        }
    )
    if "error" in data:
        sys.stderr.write(f"MCP error: {json.dumps(data['error'])}\n")
        sys.exit(3)
    return data.get("result")


def tool(name: str, arguments: dict[str, Any]) -> Any:
    """Invoke an MCP tool and return the parsed first content payload.

    Exits the process with code 3 on MCP error.
    """
    result = call("tools/call", {"name": name, "arguments": arguments})
    return _unwrap_content(result)


def call_safe(method: str, params: dict[str, Any] | None = None) -> tuple[Any, dict[str, Any] | None]:
    """Like ``call`` but returns ``(result, error)`` instead of exiting on MCP error."""
    data = _post_payload(
        {
            "jsonrpc": "2.0",
            "id": _next_id(),
            "method": method,
            "params": params or {},
        }
    )
    if "error" in data:
        return None, data["error"]
    return data.get("result"), None


def tool_safe(name: str, arguments: dict[str, Any]) -> tuple[Any, dict[str, Any] | None]:
    """Like ``tool`` but returns ``(payload, error)`` instead of exiting on MCP error.

    MCP returns tool errors via ``result.isError = True`` with the error
    message in ``result.content[0].text``. We surface those as the error
    return so callers can distinguish success from failure.
    """
    result, err = call_safe("tools/call", {"name": name, "arguments": arguments})
    if err is not None:
        return None, err
    if isinstance(result, dict) and result.get("isError"):
        return None, {"message": _extract_text(result), "isError": True}
    return _unwrap_content(result), None


def _extract_text(result: Any) -> str:
    content = (result or {}).get("content") or []
    if content:
        text = content[0].get("text")
        if text is not None:
            return str(text)
    return str(result)


def _unwrap_content(result: Any) -> Any:
    """Extract the parsed payload from a tool result.

    Tool-level errors (``isError: True``) are surfaced *only* through
    ``tool_safe``. The legacy ``tool()`` path returns the error text as a
    plain string, preserving the long-standing convention that callers
    using ``find_tip`` on an empty chain see the "No items found" message
    as a string and treat it as ``None`` in their normalization (see
    ``store._normalize_tip``).
    """
    content = (result or {}).get("content") or []
    if not content:
        return None
    first = content[0]
    text = first.get("text")
    if text is None:
        return first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text

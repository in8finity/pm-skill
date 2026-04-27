#!/usr/bin/env python3
"""Minimal JSON-RPC HTTP client for hashharness MCP server.

Connects to ``HASHHARNESS_MCP_URL`` (default ``http://127.0.0.1:38417/mcp``).
Requires the hashharness MCP server to be running with HTTP transport:

    HASHHARNESS_MCP_TRANSPORT=http hashharness-mcp  # or python -m hashharness.mcp_server
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

DEFAULT_URL = "http://127.0.0.1:38417/mcp"
_NEXT_ID = 0


def _next_id() -> int:
    global _NEXT_ID
    _NEXT_ID += 1
    return _NEXT_ID


def call(method: str, params: dict[str, Any] | None = None) -> Any:
    url = os.environ.get("HASHHARNESS_MCP_URL", DEFAULT_URL)
    payload = {
        "jsonrpc": "2.0",
        "id": _next_id(),
        "method": method,
        "params": params or {},
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        sys.stderr.write(
            f"hashharness MCP unreachable at {url}: {exc}\n"
            "Start it with: HASHHARNESS_MCP_TRANSPORT=http python -m hashharness.mcp_server\n"
        )
        sys.exit(2)
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
    url = os.environ.get("HASHHARNESS_MCP_URL", DEFAULT_URL)
    payload = {
        "jsonrpc": "2.0",
        "id": _next_id(),
        "method": method,
        "params": params or {},
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        sys.stderr.write(
            f"hashharness MCP unreachable at {url}: {exc}\n"
            "Start it with: HASHHARNESS_MCP_TRANSPORT=http python -m hashharness.mcp_server\n"
        )
        sys.exit(2)
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

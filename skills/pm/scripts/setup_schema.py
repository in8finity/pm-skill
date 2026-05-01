#!/usr/bin/env python3
"""Merge planning types into the active hashharness schema (idempotent).

Schema is append-only and hash-chained. We pass ``expected_prev`` so a
concurrent ``set_schema`` from another agent is rejected with
'schema head moved' instead of silently forking.
"""
from __future__ import annotations

import json
from pathlib import Path

import mcp_client

FRAGMENT = Path(__file__).with_name("schema_fragment.json")


def current_head_sha() -> str | None:
    """Return record_sha256 of the current schema head, or None if genesis."""
    history = mcp_client.tool("get_schema_history", {}) or {}
    versions = history.get("versions") if isinstance(history, dict) else None
    if not versions:
        return None
    return versions[-1].get("record_sha256")


def main() -> None:
    fragment = json.loads(FRAGMENT.read_text())
    current = mcp_client.tool("get_schema", {}) or {}
    types = dict(current.get("types") or {})
    changed = False
    for name, defn in fragment.items():
        if types.get(name) != defn:
            types[name] = defn
            changed = True
    if not changed:
        print(json.dumps({"ok": True, "changed": False}))
        return
    expected_prev = current_head_sha()
    result = mcp_client.tool(
        "set_schema",
        {"schema": {"types": types}, "expected_prev": expected_prev},
    )
    print(json.dumps({"ok": True, "changed": True, "schema": result}, indent=2))


if __name__ == "__main__":
    main()

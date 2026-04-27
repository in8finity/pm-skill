#!/usr/bin/env python3
"""Merge planning types into the active hashharness schema (idempotent)."""
from __future__ import annotations

import json
from pathlib import Path

import mcp_client

FRAGMENT = Path(__file__).with_name("schema_fragment.json")


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
    result = mcp_client.tool("set_schema", {"schema": {"types": types}})
    print(json.dumps({"ok": True, "changed": True, "schema": result}, indent=2))


if __name__ == "__main__":
    main()

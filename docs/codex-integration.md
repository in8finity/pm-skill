# Codex Integration

`pm` works under Codex without Claude's hook framework, but the
substitute mechanisms are different.

## Mapping from Claude hooks

| Claude hook | Purpose | Codex substitute | Gap |
|---|---|---|---|
| `SessionStart` | Load `HASHHARNESS_MCP_URL`, expose `pm`, initialize session env | Launch Codex through `skills/pm/scripts/codex_pm.sh` | None, if you use the wrapper |
| `PreToolUse` on `mcp__hashharness__create_item` / `set_schema` | Refuse direct planning writes that bypass `pm` | Keep direct hashharness write tools behind approval in Codex config, and prefer `pm` commands over raw MCP calls | Codex has approval/policy primitives, but no public payload-aware tool hook equivalent here |
| `Stop` | Refuse turn-end while this agent still owns `working` tasks | Run `pm owned-check` or `pm owned --strict` before ending the session | Manual check, not automatic turn interception |

## Recommended Codex workflow

### 1. Launch Codex through the wrapper

```bash
skills/pm/scripts/codex_pm.sh
```

What it does:

- sources the first matching env file from:
  `HASHHARNESS_ENV`, `~/.hashharness/env`, `~/.codex/hashharness/env`,
  `~/.claude/hashharness/env`, `<repo>/.hashharness/env`
- prepends `skills/pm/scripts` to `PATH`, so `pm` is available
- mints `PM_CONTEXT_ID` once if one is not already set
- on first use, warns if `~/.codex/config.toml` still auto-approves
  raw `hashharness` write tools

Sanity check:

```bash
skills/pm/scripts/codex_pm.sh --check
```

### 2. Keep direct hashharness writes behind approval

In Codex, the closest guard to Claude's `PreToolUse` hook is MCP-tool
approval policy in `~/.codex/config.toml`.

Suggested minimal shape:

```toml
[mcp_servers.hashharness]
url = "http://127.0.0.1:38417/mcp"

[mcp_servers.hashharness.tools.find_items]
approval_mode = "approve"

[mcp_servers.hashharness.tools.get_work_package]
approval_mode = "approve"

[mcp_servers.hashharness.tools.verify_chain]
approval_mode = "approve"

# Do not auto-approve raw planning writes for day-to-day pm use:
# Remove these blocks if they exist in your config.
#
# [mcp_servers.hashharness.tools.create_item]
# approval_mode = "approve"
#
# [mcp_servers.hashharness.tools.set_schema]
# approval_mode = "approve"
```

If you want `pm` to remain the only normal write path for planning
records:

- do not blanket-auto-approve direct `hashharness` write tools for day
  to day work
- prefer `pm plan`, `pm report`, `pm finished`, `pm cancel`, etc.
- reserve raw `create_item` / `set_schema` for storage debugging or
  schema maintenance

The policy surface is per-tool approval, not a content-aware deny rule,
so Codex cannot cleanly say "allow `create_item` for other item types
but deny it for `Task` / `TaskStatus` / `TaskReport` / `TaskHeartbeat`"
the way the Claude hook can.

### 3. Check for dangling owned tasks before you stop

Run:

```bash
pm owned-check
```

If it exits `1`, this session still owns one or more tasks in
`working`. Close them with `pm finished` or `pm cancel` before ending
the session.

The narrow wrapper is:

```bash
skills/pm/scripts/pm_owned_check.sh
```

If you prefer to use the underlying command directly:

```bash
pm owned --strict
```

Without `--strict`, the command is just a status view:

```bash
pm owned
pm owned --json
pm owned --queue default
```

### 4. Rate-limit awareness works without Claude hooks

`pm limits` can read Codex's own rate-limit telemetry directly from the
current session log in `~/.codex/sessions/*.jsonl`.

- If `CODEX_THREAD_ID` is set, it prefers that thread's session file.
- Otherwise it falls back to the freshest Codex session log.
- `pm limits --cache-path /path/to/session.jsonl` forces a specific
  Codex session file.

This means `pm execute --running` can make the same `ok` / `wait` /
`stop` decisions under Codex without installing Claude's statusline
capture hook.

## Minimal rules

This repo now ships a narrow rule file at `.codex/rules/pm.rules` that
allows only:

- `skills/pm/scripts/pm_owned_check.sh`
- `./skills/pm/scripts/pm_owned_check.sh`

If your Codex build loads project-local `.codex/rules/*.rules`, that is
enough to allow the pre-stop check without broadly allowlisting shell
usage.

## Notes

- `pm owned` uses the same agent-identity convention as the worker
  scripts: `PM_AGENT_ID`, else `worker-<PM_CONTEXT_ID[:12]>`.
- The wrapper intentionally sets `PM_CONTEXT_ID` once so all `pm`
  commands in that Codex session share the same sticky-context identity.
- This is a close approximation of the Claude hooks, not a perfect
  replacement. The biggest missing piece is automatic refusal of direct
  MCP writes based on the payload type.

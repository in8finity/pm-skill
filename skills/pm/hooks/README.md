# hashharness-pm — Claude Code hooks

Optional Claude Code hooks that close gaps the storage protocol can't see at the agent boundary. The protocol enforces what gets *written* to the chain; these hooks catch what an agent is *about to do* before it tries to write something off-protocol or stop a turn with state still open.

## When to install

Install if you run agents under `pm` and want:

- **Auto-bootstrap of `HASHHARNESS_MCP_URL`** at session start so workers don't die on a missing env var.
- **Refusal of direct hashharness MCP writes** that would bypass `pm`'s protocol gates (the threat-model bypass the README documents).
- **Forced justification** when an agent uses `pm finished --skip-verifier` (the audit chain records `verifier_exit = -1`, but doesn't demand a reason).
- **Refusal of turn-end** when the agent left a task in `working` without closing or cancelling it (catches "I'm done!" said in chat without a `pm finished` call).

The hooks are advisory in the sense that you can turn them off per-environment with the documented overrides. They're mandatory in the Claude Code sense — when active, they refuse the action with a structured reason the agent reads and reacts to.

## When to use a hook vs adjusting `pm` itself

These three hooks exist because the trigger isn't a `pm` command call — there's no place inside `pm` to put the rule.

- `session_start.sh` fires on Claude Code session start, before any agent code runs.
- `no_direct_mcp_writes.sh` fires on direct MCP calls (`mcp__hashharness__create_item`) — those bypass `pm` by design, so a `pm`-side check would never see them.
- `stop_with_open_task.py` fires on Claude Code turn-end — there is no `pm stop`.

**A rule that fits inside a `pm` subcommand belongs in `pm`, not in a hook.** For example, `pm finished --skip-verifier` requiring a `--note "<reason>"` is enforced directly in `finished.py` (exit 13) — no hook needed. Reasons code wins for in-`pm` rules:

- single source of truth — the rule lives where the gate is
- works regardless of agent platform / hook framework / settings.json wiring
- discoverable via `--help`
- testable as part of the integration suite without setting up hooks
- not fail-open: if you didn't pass the note, exit 13 always

Hooks are the right tool when the trigger is *outside* `pm`. The three below are.

## The three hooks

| File | Event | Effect |
|---|---|---|
| `session_start.sh` | `SessionStart` | Sources `~/.hashharness/env` (or fallbacks), mirrors the env into `$CLAUDE_ENV_FILE` for subsequent hooks, surfaces a notice. Never blocks. |
| `no_direct_mcp_writes.sh` | `PreToolUse` on `mcp__hashharness__create_item` / `mcp__hashharness__set_schema` | Refuses direct writes for the four planning types (`Task`, `TaskStatus`, `TaskReport`, `TaskHeartbeat`). User-defined item types pass through. |
| `stop_with_open_task.py` | `Stop` | Refuses turn-end if the current agent identity (`$PM_AGENT_ID`, else `worker-<PM_CONTEXT_ID[:12]>`, else `hostname-pid`) holds any task in `working`. |

## Wiring

Add to `.claude/settings.json` at the project root:

```json
{
  "hooks": {
    "SessionStart": [
      {"hooks": [{
        "type": "command",
        "command": "$CLAUDE_PROJECT_DIR/skills/pm/hooks/session_start.sh",
        "timeout": 5
      }]}
    ],
    "PreToolUse": [
      {
        "matcher": "mcp__hashharness__create_item|mcp__hashharness__set_schema",
        "hooks": [{
          "type": "command",
          "command": "$CLAUDE_PROJECT_DIR/skills/pm/hooks/no_direct_mcp_writes.sh",
          "timeout": 5
        }]
      },
    ],
    "Stop": [
      {"hooks": [{
        "type": "command",
        "command": "$CLAUDE_PROJECT_DIR/skills/pm/hooks/stop_with_open_task.py",
        "timeout": 15
      }]}
    ]
  }
}
```

Adjust `$CLAUDE_PROJECT_DIR/skills/pm/hooks/...` to match where you've vendored the hook scripts. If you've installed hashharness-pm under `~/.claude/skills/`, swap the prefix accordingly.

## Per-hook overrides

| Override env var | Effect |
|---|---|
| `PM_HOOK_ALLOW_DIRECT=1` | Disables `no_direct_mcp_writes.sh`. Use for hashharness-pm developers debugging storage. |
| `PM_HOOK_ALLOW_OPEN_TASKS=1` | Disables `stop_with_open_task.py`. Lets the agent stop with working claims open (sweep will eventually reclaim). |
| `PM_HOOK_QUEUES=q1,q2,*` | Restricts the Stop hook's scan to specific queues. Default scans everything via `find_items` (cheap on a small backend, slower on a large one). |
| `HASHHARNESS_ENV=/path/env` | Override the SessionStart hook's env-file lookup. |

The override mechanism is the documented escape hatch — set the var in the worker's environment when you legitimately need to bypass.

## What these hooks don't do

- They don't replace `pm`'s protocol gates. The storage refuses malformed writes regardless of whether the hooks fire. Hooks catch honest mistakes earlier with better DX.
- They don't enforce anything multi-agent. Each Claude Code session has its own hook execution; coordination across agents is the protocol's job (chain_predecessor, sticky-context, parent gate).
- They don't ship as a Claude Code plugin (yet). The hooks are vendored as files under this skill's `hooks/` directory; users wire them up by editing `.claude/settings.json`. A future plugin packaging would let an enterprise admin install them as managed hooks that workers can't disable.

## Failure mode

All four hooks are **fail-open**: a missing dependency (no jq, no python3, no MCP env, importable pm scripts not found) causes the hook to silently exit 0, letting the action proceed. This matches Claude Code's hook design and avoids hooks-block-agent-by-accident regressions, at the cost of "hook silently disabled" being possible. Test the hooks in your environment before relying on them.

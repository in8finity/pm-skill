#!/usr/bin/env bash
# PreToolUse hook — refuse direct hashharness MCP writes that would
# bypass the `pm` CLI for the four planning types (Task, TaskStatus,
# TaskReport, TaskHeartbeat).
#
# Why: every protocol guarantee — claim race, proof-of-work, verifier,
# sticky context, parent gate — lives in the `pm` scripts and the
# storage-layer chain_predecessor. A direct mcp__hashharness__create_item
# bypasses ALL of that. The README's threat-model section calls this out
# as the known weakness for cooperative-agent use; this hook closes it
# at the agent boundary.
#
# Configured via:
#   {
#     "hooks": {
#       "PreToolUse": [
#         {
#           "matcher": "mcp__hashharness__create_item|mcp__hashharness__set_schema",
#           "hooks": [{"type":"command","command":"$CLAUDE_PROJECT_DIR/skills/pm/hooks/no_direct_mcp_writes.sh"}]
#         }
#       ]
#     }
#   }
#
# Override: set PM_HOOK_ALLOW_DIRECT=1 in the environment to disable the
# block (intended for hashharness-pm developers who legitimately need to
# poke storage during debugging).

set -u

if [[ "${PM_HOOK_ALLOW_DIRECT:-}" == "1" ]]; then
  exit 0
fi

# Read the tool call payload from stdin.
input=$(cat)

if ! command -v jq >/dev/null 2>&1; then
  # Without jq we can't safely inspect — fail open.
  exit 0
fi

tool_name=$(jq -r '.tool_name // ""' <<<"$input")
item_type=$(jq -r '.tool_input.type // ""' <<<"$input")

# set_schema always blocked (the planning schema should only be installed
# via `pm setup`, never edited ad-hoc by a worker).
if [[ "$tool_name" == "mcp__hashharness__set_schema" ]]; then
  jq -nc '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: "Direct schema writes are reserved for `pm setup`. Run that instead. Set PM_HOOK_ALLOW_DIRECT=1 to override."
    }
  }'
  exit 0
fi

# create_item blocked only for the four planning types.
case "$item_type" in
  Task|TaskStatus|TaskReport|TaskHeartbeat)
    jq -nc --arg t "$item_type" '{
      hookSpecificOutput: {
        hookEventName: "PreToolUse",
        permissionDecision: "deny",
        permissionDecisionReason: ("Direct MCP write of " + $t + " bypasses the pm protocol gates (claim race, proof-of-work, sticky-context, verifier). Use `pm plan / executing / report / finished / heartbeat` instead. Set PM_HOOK_ALLOW_DIRECT=1 to override.")
      }
    }'
    exit 0
    ;;
esac

# Other item types (e.g. user-defined data records the agent legitimately
# wants to store) pass through.
exit 0

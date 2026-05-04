#!/usr/bin/env bash
# SessionStart hook — bootstrap hashharness-pm env for this session.
#
# Looks for ~/.hashharness/env (the file pm-install writes), and:
#   1. Writes its KEY=VALUE pairs into $CLAUDE_ENV_FILE so subsequent
#      hooks see them.
#   2. Prints a notice to stdout that becomes additionalContext to the
#      model — the model then knows the env is wired and can proceed
#      to call `pm` directly without troubleshooting "is the MCP up?"
#
# When ~/.hashharness/env is missing, prints a hint pointing at
# `pm install`, but never blocks (SessionStart can't block anyway).
#
# Configured via:
#   {
#     "hooks": {
#       "SessionStart": [
#         {"hooks": [{"type":"command","command":"$CLAUDE_PROJECT_DIR/skills/pm/hooks/session_start.sh"}]}
#       ]
#     }
#   }

set -u

# Make the `pm` wrapper reachable as a bare command in THIS (orchestrator)
# session. Empirically, sub-agents spawned via the Agent tool do NOT
# inherit $CLAUDE_ENV_FILE — they get a fresh env from the user profile.
# So this PATH munge helps the orchestrator only. For sub-agents, the
# project allowlist must permit the relative form
# `skills/pm/scripts/pm <verb> *` and worker prompts should invoke pm
# via that relative path (workers' cwd is the project root). See
# `skills/pm/execute/SKILL.md` "Permission allowlist gotchas" for the
# full story.
if [[ -n "${CLAUDE_PROJECT_DIR:-}" && -d "$CLAUDE_PROJECT_DIR/skills/pm/scripts" \
      && -n "${CLAUDE_ENV_FILE:-}" && -w "$(dirname "$CLAUDE_ENV_FILE")" ]]; then
  echo "PATH=$CLAUDE_PROJECT_DIR/skills/pm/scripts:$PATH" >> "$CLAUDE_ENV_FILE"
fi

# Candidate env file locations, in priority order. First match wins.
ENV_CANDIDATES=(
  "${HASHHARNESS_ENV:-}"
  "$HOME/.hashharness/env"
  "$HOME/.claude/hashharness/env"
)
[[ -n "${CLAUDE_PROJECT_DIR:-}" ]] && ENV_CANDIDATES+=("$CLAUDE_PROJECT_DIR/.hashharness/env")

env_file=""
for cand in "${ENV_CANDIDATES[@]}"; do
  [[ -n "$cand" && -f "$cand" ]] || continue
  env_file="$cand"
  break
done

if [[ -z "$env_file" ]]; then
  cat <<EOF
hashharness-pm: no \`env\` file found at ~/.hashharness/env or fallbacks.
If you're using \`pm\` in this session, run:
    skills/pm/scripts/pm install --to-home --yes
or set HASHHARNESS_MCP_URL by hand. Otherwise this notice is harmless.
EOF
  exit 0
fi

# Mirror the env file's KEY=VALUE lines into $CLAUDE_ENV_FILE so they
# persist across subsequent hook executions in the same session.
if [[ -n "${CLAUDE_ENV_FILE:-}" && -w "$(dirname "$CLAUDE_ENV_FILE")" ]]; then
  # Strip `export ` prefix (the env file uses it for shell-source compat).
  sed -E 's/^[[:space:]]*export[[:space:]]+//' "$env_file" >> "$CLAUDE_ENV_FILE"
fi

# Surface the URL so the model sees we're configured.
mcp_url=$(grep -E '^[[:space:]]*(export[[:space:]]+)?HASHHARNESS_MCP_URL=' "$env_file" \
          | head -1 \
          | sed -E 's/^[[:space:]]*(export[[:space:]]+)?HASHHARNESS_MCP_URL=//; s/^"//; s/"$//')

cat <<EOF
hashharness-pm: env loaded from $env_file
  HASHHARNESS_MCP_URL=${mcp_url:-<unset>}
  pm CLI ready — see \`skills/pm/scripts/pm --help\` for verbs.
EOF

exit 0

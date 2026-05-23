#!/bin/sh
# statusline_capture.sh — self-contained statusline hook for pm:execute
# --running mode. Captures the harness-provided JSON (which carries
# .rate_limits.five_hour / .seven_day fields) to a cache file so
# `pm limits` can read the same numbers the status bar sees, then
# delegates rendering to the user's prior statusline command if one is
# configured via $PM_UPSTREAM_STATUSLINE.
#
# Why this exists: Claude Code only feeds rate_limits state to the
# statusline hook's stdin. There is no other local file or env var that
# exposes those percentages, so any tool that wants to reason about
# them (the `--running` controller in pm:execute) needs a statusline
# hook to snapshot it.
#
# Install (single statusline, no prior one):
#   {
#     "statusLine": {
#       "type": "command",
#       "command": "$CLAUDE_PROJECT_DIR/skills/pm/hooks/statusline_capture.sh"
#     }
#   }
#
# Install (chain with existing statusline — capture + delegate render):
#   {
#     "statusLine": {
#       "type": "command",
#       "command": "PM_UPSTREAM_STATUSLINE=/path/to/your/statusline.sh \
#                   $CLAUDE_PROJECT_DIR/skills/pm/hooks/statusline_capture.sh"
#     }
#   }
#
# With no upstream, prints a minimal one-line render so the user still
# gets a status bar.

set -u

input=$(cat)

# Atomic cache write — readers must never see a torn file.
cache_dir=${CLAUDE_CONFIG_DIR:-$HOME/.claude}
cache_path="$cache_dir/rate-limits.json"
tmp="$cache_dir/.rate-limits.json.tmp.$$"
if printf '%s' "$input" > "$tmp" 2>/dev/null; then
  mv "$tmp" "$cache_path" 2>/dev/null || rm -f "$tmp"
fi

# Delegate rendering.
if [ -n "${PM_UPSTREAM_STATUSLINE:-}" ] && [ -x "${PM_UPSTREAM_STATUSLINE}" ]; then
  printf '%s' "$input" | "$PM_UPSTREAM_STATUSLINE"
  exit $?
fi

# Minimal fallback render so the user still sees a useful bar.
if command -v jq >/dev/null 2>&1; then
  dir=$(printf '%s' "$input" | jq -r '.workspace.current_dir // .cwd // ""' | xargs -I{} basename {} 2>/dev/null)
  model=$(printf '%s' "$input" | jq -r '.model.display_name // ""')
  q5=$(printf '%s' "$input" | jq -r '.rate_limits.five_hour.used_percentage // empty')
  q7=$(printf '%s' "$input" | jq -r '.rate_limits.seven_day.used_percentage // empty')
  line="${dir:-?}"
  [ -n "$model" ] && line="$line  $model"
  [ -n "$q5" ] && line="$line  5h:$(printf '%.0f' "$q5")%"
  [ -n "$q7" ] && line="$line  7d:$(printf '%.0f' "$q7")%"
  printf '%s\n' "$line"
else
  printf 'pm:execute statusline capture active\n'
fi

#!/usr/bin/env bash
# smoke_test.sh — minimal end-to-end pm sanity check against a live backend.
#
# Usage:
#   smoke_test.sh
#   smoke_test.sh --queue-prefix my-prefix
#
# Requires:
#   - HASHHARNESS_MCP_URL points at a running hashharness MCP server
#   - the planning schema is already registered (`pm setup`)

set -euo pipefail

queue_prefix="pm-smoke"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --queue-prefix)
      queue_prefix="$2"
      shift 2
      ;;
    -h|--help)
      sed -n '2,11p' "$0" | sed 's/^# //; s/^#//'
      exit 0
      ;;
    *)
      echo "unknown arg: $1" >&2
      exit 64
      ;;
  esac
done

SOURCE="${BASH_SOURCE[0]}"
while [ -L "$SOURCE" ]; do
  DIR="$(cd -P "$(dirname "$SOURCE")" >/dev/null 2>&1 && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  [[ $SOURCE != /* ]] && SOURCE="$DIR/$SOURCE"
done
SCRIPTS_DIR="$(cd -P "$(dirname "$SOURCE")" >/dev/null 2>&1 && pwd)"
PM="$SCRIPTS_DIR/pm"

if [[ -z "${HASHHARNESS_MCP_URL:-}" ]]; then
  echo "HASHHARNESS_MCP_URL is unset; source your hashharness env file first" >&2
  exit 2
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required for smoke-test JSON parsing" >&2
  exit 2
fi

queue="${queue_prefix}-$(date +%s)-$$"
title="pm smoke test"
body="verify the queue can plan, claim, report, and finish"
report_title="pm smoke test report"
report_body="smoke test passed"

echo "→ queue: $queue"
plan_json="$("$PM" plan --queue "$queue" --title "$title" --text "$body")"
task_sha="$(
  printf '%s' "$plan_json" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["task"]["text_sha256"])'
)"

next_json="$("$PM" next --queue "$queue")"
if [[ "$next_json" == "null" ]]; then
  echo "smoke test failed: pm next returned null for queue $queue" >&2
  exit 1
fi
next_sha="$(
  printf '%s' "$next_json" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["text_sha256"])'
)"

if [[ "$task_sha" != "$next_sha" ]]; then
  echo "smoke test mismatch: planned task $task_sha but next returned $next_sha" >&2
  exit 1
fi

"$PM" executing --task "$task_sha" >/dev/null
"$PM" report --task "$task_sha" --title "$report_title" --text "$report_body" >/dev/null
"$PM" finished --task "$task_sha" >/dev/null

detail_json="$("$PM" show --task "$task_sha" --json)"
status="$(
  printf '%s' "$detail_json" \
    | python3 -c 'import json,sys; data=json.load(sys.stdin); print((data["statuses"][-1]["attributes"] or {}).get("status",""))'
)"

if [[ "$status" != "done" ]]; then
  echo "smoke test failed: final status is '$status', expected 'done'" >&2
  exit 1
fi

cat <<EOF
✓ pm smoke test passed
  queue: $queue
  task:  $task_sha
EOF

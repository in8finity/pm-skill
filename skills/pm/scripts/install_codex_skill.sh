#!/usr/bin/env bash
# install_codex_skill.sh — vendor the full hashharness-pm skill bundle
# into Codex's skill directory and generate flat pm-* alias skills.
#
# Usage:
#   install_codex_skill.sh
#   install_codex_skill.sh --check
#   install_codex_skill.sh --root /path/to/.codex/skills
#   install_codex_skill.sh --no-aliases
#
# Default target:
#   ${CODEX_HOME:-$HOME/.codex}/skills/pm
#
# Why this exists:
#   - Codex's generic GitHub skill installer expects one top-level SKILL.md
#   - hashharness-pm is a bundle with shared relative paths
#   - vendoring the whole tree preserves those relative paths
#   - flat pm-* aliases cover Codex builds that do not discover nested skills

set -euo pipefail

SOURCE="${BASH_SOURCE[0]}"
while [ -L "$SOURCE" ]; do
  DIR="$(cd -P "$(dirname "$SOURCE")" >/dev/null 2>&1 && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  [[ $SOURCE != /* ]] && SOURCE="$DIR/$SOURCE"
done
SCRIPTS_DIR="$(cd -P "$(dirname "$SOURCE")" >/dev/null 2>&1 && pwd)"
BUNDLE_SOURCE="$(cd -P "$SCRIPTS_DIR/.." >/dev/null 2>&1 && pwd)"

CODEX_HOME_DIR="${CODEX_HOME:-$HOME/.codex}"
TARGET_ROOT="${CODEX_HOME_DIR}/skills"
GENERATE_ALIASES="yes"
CHECK_ONLY="no"
BUNDLE_NAME="pm"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --root)
      TARGET_ROOT="$2"
      shift 2
      ;;
    --no-aliases)
      GENERATE_ALIASES="no"
      shift
      ;;
    --check)
      CHECK_ONLY="yes"
      shift
      ;;
    --yes|-y)
      shift
      ;;
    -h|--help)
      sed -n '2,20p' "$0" | sed 's/^# //; s/^#//'
      exit 0
      ;;
    *)
      echo "unknown arg: $1" >&2
      exit 64
      ;;
  esac
done

BUNDLE_TARGET="${TARGET_ROOT}/${BUNDLE_NAME}"

collect_skill_dirs() {
  find "$BUNDLE_SOURCE" -mindepth 2 -maxdepth 2 -name SKILL.md -print \
    | sort \
    | while read -r skill_file; do
        dirname "$skill_file"
      done
}

alias_name_for_dir() {
  local skill_dir="$1"
  local base
  base="$(basename "$skill_dir")"
  printf 'pm-%s\n' "$base"
}

is_installed() {
  [[ -f "$BUNDLE_TARGET/SKILL.md" ]] \
    && [[ -f "$BUNDLE_TARGET/scripts/pm" ]] \
    && [[ -d "$BUNDLE_TARGET/skill-shared" ]]
}

alias_count() {
  local count=0
  while read -r skill_dir; do
    [[ -n "$skill_dir" ]] || continue
    count=$((count + 1))
  done < <(collect_skill_dirs)
  printf '%s\n' "$count"
}

if [[ "$CHECK_ONLY" == "yes" ]]; then
  if is_installed; then
    echo "✓ pm skill bundle installed at $BUNDLE_TARGET"
    if [[ "$GENERATE_ALIASES" == "yes" ]]; then
      echo "  aliases expected: $(alias_count)"
    fi
    exit 0
  fi
  echo "✗ pm skill bundle not installed at $BUNDLE_TARGET"
  exit 1
fi

mkdir -p "$TARGET_ROOT"
TMP_ROOT="$(mktemp -d "${TARGET_ROOT}/.pm-install.XXXXXX")"
trap 'rm -rf "$TMP_ROOT"' EXIT

mkdir -p "$TMP_ROOT/$BUNDLE_NAME"
cp -R "$BUNDLE_SOURCE"/. "$TMP_ROOT/$BUNDLE_NAME"
find "$TMP_ROOT/$BUNDLE_NAME" -type d -name '__pycache__' -prune -exec rm -rf {} +

while read -r skill_dir; do
  [[ -n "$skill_dir" ]] || continue
  alias_name="$(alias_name_for_dir "$skill_dir")"
  if [[ "$GENERATE_ALIASES" == "yes" ]]; then
    alias_dir="$TMP_ROOT/$alias_name"
    subskill="$(basename "$skill_dir")"
    mkdir -p "$alias_dir"
    cat > "$alias_dir/SKILL.md" <<EOF
---
name: ${alias_name}
description: >
  Alias to the \`pm/${subskill}\` subskill from the vendored hashharness-pm
  bundle. Use this when Codex does not discover nested subskills under
  \`pm/\` automatically.
---

# ${alias_name}

This is a thin alias for the bundled workflow at \`../pm/${subskill}/SKILL.md\`.
Read that file and follow it as the authoritative instructions.

Use the shared helpers under \`../pm/scripts/\`, \`../pm/hooks/\`, and
\`../pm/skill-shared/\`; do not copy them into this alias directory.
EOF
  fi
done < <(collect_skill_dirs)

rm -rf "$BUNDLE_TARGET"
while read -r skill_dir; do
  [[ -n "$skill_dir" ]] || continue
  rm -rf "$TARGET_ROOT/$(alias_name_for_dir "$skill_dir")"
done < <(collect_skill_dirs)

mv "$TMP_ROOT/$BUNDLE_NAME" "$BUNDLE_TARGET"
if [[ "$GENERATE_ALIASES" == "yes" ]]; then
  find "$TMP_ROOT" -mindepth 1 -maxdepth 1 -type d ! -name "$BUNDLE_NAME" -print \
    | while read -r alias_dir; do
        mv "$alias_dir" "$TARGET_ROOT/$(basename "$alias_dir")"
      done
fi

cat <<EOF
✓ pm skill bundle installed
  bundle:      $BUNDLE_TARGET
  aliases:     $(if [[ "$GENERATE_ALIASES" == "yes" ]]; then alias_count; else echo 0; fi)
  codex root:  $TARGET_ROOT

Next steps:

  1. restart Codex so it rescans $TARGET_ROOT

  2. install the hashharness backend:
       $BUNDLE_TARGET/scripts/pm install --to-home --yes

  3. start the MCP server:
       ~/.hashharness/launch.sh &

  4. register the planning schema:
       source ~/.hashharness/env
       $BUNDLE_TARGET/scripts/pm setup

  5. run the smoke test:
       source ~/.hashharness/env
       $BUNDLE_TARGET/scripts/pm smoke-test

Optional:

  Launch Codex through the wrapper so PATH, PM_CONTEXT_ID, and the
  first available hashharness env file are loaded automatically:

       $BUNDLE_TARGET/scripts/codex_pm.sh
EOF

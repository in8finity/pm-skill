#!/usr/bin/env bash
# install_hashharness.sh — install hashharness into an isolated venv and
# wire it up so `pm setup` and the worker loop can talk to it.
#
# Idempotent: if hashharness is already importable in the chosen venv,
# the script reports its location and exits 0 without re-installing.
#
# Default install location is ~/.hashharness/. The script also accepts
# --to-claude (~/.claude/hashharness/) or --to-project (./.hashharness/
# under the current repo). Pass --where <path> for a custom location.
#
# Usage:
#   install_hashharness.sh                 # interactive: asks where
#   install_hashharness.sh --to-home       # ~/.hashharness/
#   install_hashharness.sh --to-claude     # ~/.claude/hashharness/
#   install_hashharness.sh --to-project    # ./.hashharness/
#   install_hashharness.sh --where <PATH>  # explicit
#   install_hashharness.sh --port 38417    # override HTTP port
#   install_hashharness.sh --yes           # don't prompt; use defaults
#   install_hashharness.sh --check         # just check, don't install
#
# What it produces under <install-root>/:
#   venv/                      # python virtualenv with hashharness installed
#   data/                      # HASHHARNESS_DATA_DIR
#   launch.sh                  # ready-to-run launcher script
#   env                        # source-able env vars for HASHHARNESS_MCP_URL etc
#
# After install, start the MCP server with:
#   <install-root>/launch.sh &
# and (once per data dir) register the planning schema with:
#   pm setup
#
# Requires: python3 (3.10+), git, curl (for the optional self-test).

set -euo pipefail

HASHHARNESS_REPO="${HASHHARNESS_REPO:-https://github.com/in8finity/hashharness.git}"
DEFAULT_PORT="${HASHHARNESS_HTTP_PORT:-38417}"

# ---- arg parsing ---------------------------------------------------------

mode=""
explicit_where=""
auto_yes="no"
check_only="no"
port="$DEFAULT_PORT"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --to-home)    mode="home";    shift ;;
    --to-claude)  mode="claude";  shift ;;
    --to-project) mode="project"; shift ;;
    --where)      mode="explicit"; explicit_where="$2"; shift 2 ;;
    --port)       port="$2";      shift 2 ;;
    --yes|-y)     auto_yes="yes"; shift ;;
    --check)      check_only="yes"; shift ;;
    -h|--help)
      sed -n '2,30p' "$0" | sed 's/^# //; s/^#//'
      exit 0
      ;;
    *)
      echo "unknown arg: $1" >&2
      exit 64
      ;;
  esac
done

# ---- prerequisites -------------------------------------------------------

need() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "missing prerequisite: $1" >&2
    echo "install it first, then re-run this script." >&2
    exit 2
  fi
}
need python3
need git

PY_VER="$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'; then
  echo "python3 >= 3.10 required (found $PY_VER)" >&2
  exit 2
fi

# ---- pick install location -----------------------------------------------

resolve_install_root() {
  case "$mode" in
    home)     echo "$HOME/.hashharness" ;;
    claude)   echo "$HOME/.claude/hashharness" ;;
    project)
      # Find git repo root, falling back to cwd.
      local root
      root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
      echo "$root/.hashharness"
      ;;
    explicit) echo "$explicit_where" ;;
    *)
      # Interactive default: home, unless --yes is set.
      if [[ "$auto_yes" == "yes" ]]; then
        echo "$HOME/.hashharness"
      else
        printf "Where should hashharness be installed?\n  1) ~/.hashharness            (recommended; per-user)\n  2) ~/.claude/hashharness     (alongside Claude Code skills)\n  3) %s/.hashharness  (per-project)\n  4) custom path\nChoice [1]: " "$(pwd)" >&2
        read -r choice
        choice="${choice:-1}"
        case "$choice" in
          1) echo "$HOME/.hashharness" ;;
          2) echo "$HOME/.claude/hashharness" ;;
          3) echo "$(pwd)/.hashharness" ;;
          4)
            printf "absolute path: " >&2
            read -r custom
            echo "$custom"
            ;;
          *) echo "invalid choice" >&2; exit 64 ;;
        esac
      fi
      ;;
  esac
}

INSTALL_ROOT="$(resolve_install_root)"
VENV="$INSTALL_ROOT/venv"
DATA_DIR="$INSTALL_ROOT/data"
LAUNCHER="$INSTALL_ROOT/launch.sh"
ENV_FILE="$INSTALL_ROOT/env"

echo "→ install root: $INSTALL_ROOT"
echo "→ HTTP port:    $port"
[[ "$check_only" == "yes" ]] && echo "→ check-only mode (no install)" >&2

# ---- check existing install ----------------------------------------------

is_installed() {
  if [[ -x "$VENV/bin/python3" ]] \
     && "$VENV/bin/python3" -c 'import hashharness' >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

if is_installed; then
  installed_ver="$("$VENV/bin/python3" -c 'import hashharness; print(getattr(hashharness, "__version__", "unknown"))' 2>/dev/null || echo unknown)"
  echo "✓ hashharness already installed at $VENV (version: $installed_ver)"
  echo "  data dir: $DATA_DIR"
  echo "  launcher: $LAUNCHER"
  echo
  echo "  to start:   $LAUNCHER &"
  echo "  to stop:    pkill -f hashharness.mcp_server"
  echo "  env:        source $ENV_FILE"
  exit 0
fi

if [[ "$check_only" == "yes" ]]; then
  echo "✗ hashharness NOT installed at $VENV"
  exit 1
fi

# ---- install -------------------------------------------------------------

echo "→ creating $INSTALL_ROOT/{venv,data}"
mkdir -p "$VENV" "$DATA_DIR"

if [[ ! -f "$VENV/bin/python3" ]]; then
  echo "→ creating venv"
  python3 -m venv "$VENV"
fi

# Always upgrade pip in the venv first; old pips have edge cases on macOS.
"$VENV/bin/python3" -m pip install --quiet --upgrade pip wheel setuptools

# Install hashharness from git. If a local checkout exists at
# $HASHHARNESS_LOCAL, prefer that (developer mode).
if [[ -n "${HASHHARNESS_LOCAL:-}" && -f "$HASHHARNESS_LOCAL/pyproject.toml" ]]; then
  echo "→ installing hashharness (editable) from $HASHHARNESS_LOCAL"
  "$VENV/bin/python3" -m pip install --quiet -e "$HASHHARNESS_LOCAL"
else
  echo "→ installing hashharness from $HASHHARNESS_REPO"
  "$VENV/bin/python3" -m pip install --quiet "git+$HASHHARNESS_REPO"
fi

# Verify install.
if ! "$VENV/bin/python3" -c 'import hashharness' >/dev/null 2>&1; then
  echo "✗ install completed but 'import hashharness' fails" >&2
  echo "  check $VENV/bin/pip list for what landed" >&2
  exit 1
fi

# ---- launcher + env file -------------------------------------------------

cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
# Auto-generated by install_hashharness.sh — start the MCP server in HTTP mode.
exec env \\
  HASHHARNESS_MCP_TRANSPORT=http \\
  HASHHARNESS_HTTP_PORT="$port" \\
  HASHHARNESS_DATA_DIR="$DATA_DIR" \\
  "$VENV/bin/python3" -m hashharness.mcp_server "\$@"
EOF
chmod +x "$LAUNCHER"

cat > "$ENV_FILE" <<EOF
# Auto-generated by install_hashharness.sh — source to get the right
# environment for talking to this hashharness instance.
export HASHHARNESS_MCP_URL="http://127.0.0.1:$port/mcp"
export HASHHARNESS_DATA_DIR="$DATA_DIR"
# Path to the launcher (start with: \$HASHHARNESS_LAUNCH &)
export HASHHARNESS_LAUNCH="$LAUNCHER"
EOF

# ---- summary -------------------------------------------------------------

installed_ver="$("$VENV/bin/python3" -c 'import hashharness; print(getattr(hashharness, "__version__", "unknown"))' 2>/dev/null || echo unknown)"
cat <<EOF

✓ hashharness installed (version: $installed_ver)
  install root:  $INSTALL_ROOT
  venv:          $VENV
  data dir:      $DATA_DIR
  HTTP port:     $port
  launcher:      $LAUNCHER
  env file:      $ENV_FILE

Next steps:

  1. start the MCP server:
       $LAUNCHER &

  2. (one-shot, per data dir) register the planning schema:
       source $ENV_FILE
       pm setup

  3. (optional) add to your shell rc so the env is loaded automatically:
       echo "source $ENV_FILE" >> ~/.zshrc      # or ~/.bashrc

The pm CLI honours \$HASHHARNESS_MCP_URL; once the server is running
and the env is sourced, all pm commands route to this instance.
EOF

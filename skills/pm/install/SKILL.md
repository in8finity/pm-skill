---
name: pm-install
description: >
  Install the hashharness MCP backend that pm uses for its append-only
  storage. Bootstraps an isolated Python venv, installs hashharness from
  git, generates a launcher script and a source-able env file. Three
  install locations: ~/.hashharness/ (per-user, recommended),
  ~/.claude/hashharness/ (alongside Claude Code skills), or
  ./.hashharness/ (per-project at git root). Idempotent — re-running
  reports the existing install and exits 0. Use when a fresh
  workstation needs hashharness, when "pm setup" fails because no MCP
  server is reachable, or when you want a sandboxed second instance
  for testing.
---

# pm:install — bootstrap the hashharness MCP backend

## When to use

- **Fresh workstation** — pm needs hashharness running and reachable
  via `$HASHHARNESS_MCP_URL`. This skill is the easy path from "git
  clone of hashharness-pm" to "first `pm plan` works".
- **`pm setup` fails with `MCP unreachable`** — usually because nothing
  is running at `http://127.0.0.1:38417/mcp`. Run this skill first,
  then `pm setup`.
- **You want a second isolated instance** — point a project at its own
  hashharness data dir without touching the user-wide one. Use
  `--to-project` for `<repo>/.hashharness/`.
- **You're migrating between machines** — install on the new box,
  copy the data dir contents over, restart.

## Procedure

`../scripts/pm install [--to-home | --to-claude | --to-project | --where PATH] [--port N] [--yes] [--check]`

Or, equivalently:

`../scripts/install_hashharness.sh ...same args...`

Without flags it's interactive — picks one of three locations from a
menu. With `--yes` it skips prompts and uses defaults (which means
`~/.hashharness/`, port 38417). With `--check` it reports whether
hashharness is already installed at the chosen location and exits
non-zero if not, without changing anything.

## Three install locations

| Flag | Path | When |
|---|---|---|
| `--to-home` | `~/.hashharness/` | **Default**. One backend per user, shared across all projects. |
| `--to-claude` | `~/.claude/hashharness/` | If you keep all Claude Code state in one place. |
| `--to-project` | `<git-root>/.hashharness/` (or `cwd` if not in a repo) | When a project needs its own data isolated from the user-wide instance. |
| `--where <path>` | explicit | For non-standard layouts (e.g. on a network volume, in `/opt`, etc.). |

## What gets created

Under `<install-root>/`:

```
venv/         Python virtualenv with hashharness pip-installed
              (from $HASHHARNESS_REPO, default
              github.com/in8finity/hashharness; or from
              $HASHHARNESS_LOCAL=/path for editable dev mode)
data/         HASHHARNESS_DATA_DIR — hash-chained items live here
launch.sh     starts the MCP server in HTTP mode with the right env
              baked in (port, data dir, transport)
env           source-able exports: HASHHARNESS_MCP_URL,
              HASHHARNESS_DATA_DIR, HASHHARNESS_LAUNCH
```

The launcher and env file are regenerated on each install, so a
re-install with `--port 12345` updates them in place. The venv and
data dir are preserved.

## Idempotency

Re-running on an existing install:

```
✓ hashharness already installed at /Users/.../venv (version: 0.x.y)
  data dir: ...
  launcher: ...
  to start:   /path/launch.sh &
  to stop:    pkill -f hashharness.mcp_server
  env:        source /path/env
```

Exits 0 without modifying anything. Safe to run from automation.

## After install: the three follow-up commands

```bash
# 1. start the MCP server (background)
~/.hashharness/launch.sh &

# 2. source the env so pm and other tools find HASHHARNESS_MCP_URL
source ~/.hashharness/env

# 3. (one-shot per data dir) register the planning schema
pm setup
```

After that, every other pm command works.

## Inputs

- `--to-home` / `--to-claude` / `--to-project` / `--where <path>` —
  pick install location (interactive menu if none given).
- `--port <N>` — HTTP port for the MCP server (default 38417).
- `--yes` / `-y` — non-interactive; use defaults for unset choices.
- `--check` — report whether hashharness is installed at the chosen
  location; do NOT install. Exits 0 if installed, 1 if not.

## Prerequisites

- `python3` ≥ 3.10
- `git`
- network access to clone hashharness from GitHub (unless you set
  `HASHHARNESS_LOCAL=/path/to/local/hashharness/checkout` for
  editable dev-mode install)

The script checks both before doing anything destructive and exits 2
with a clear message if either is missing.

## Failure modes worth knowing

- **`pip install` fails mid-way.** The venv is left in a partially-
  installed state. Re-run the install script — `pip` will pick up
  where it left off. If it keeps failing, delete the `venv/`
  subdirectory and re-run.
- **Port already in use.** The launcher will fail to start with an
  EADDRINUSE error from Python. Pick a different port:
  `pm install --port 38418` (this rewrites the launcher and env in
  place).
- **`import hashharness` works but `pm setup` fails.** The MCP server
  isn't running. `pm install` only installs; you still need
  `~/.hashharness/launch.sh &` to start it.
- **Multiple installs on one box.** Each gets its own port + data
  dir. Source the right `env` file before running pm commands so
  `$HASHHARNESS_MCP_URL` points at the intended instance.

## Notes

- The installer writes nothing outside `<install-root>/` and the venv
  it creates inside it. Removing the install is `rm -rf
  <install-root>/`.
- The launcher uses `exec env` so it inherits your shell's process
  group; `Ctrl+C` in the foreground shell will stop it cleanly.
  Background-launched (`launch.sh &`), use
  `pkill -f hashharness.mcp_server` to stop.
- For developers iterating on hashharness itself, set
  `HASHHARNESS_LOCAL=/path/to/local/checkout` before running the
  installer — it'll do an editable install (`pip install -e`) so your
  edits land without re-installing.

---
name: pm
description: >
  Planning-board skill bundle for coordinating parallel coding agents with
  hashharness-backed append-only task, status, and report chains. Use this
  as the entry point to the bundled pm subskills under this directory, such
  as `plan`, `next`, `executing`, `report`, `finished`, `execute`,
  `replan`, `cancel`, `reclaim`, `sweep`, `dashboard`, `install`,
  `extract-steps`, and the `*-skill-execution` variants.
---

# pm

This bundle vendors the `hashharness-pm` skill set under one top-level
directory so its shared `scripts/`, `hooks/`, and `skill-shared/`
helpers stay on the relative paths expected by the bundled SKILL files.

Use the subskills in this directory for concrete workflows:

- `plan`
- `next`
- `executing`
- `report`
- `finished`
- `execute`
- `cancel`
- `replan`
- `reclaim`
- `sweep`
- `dashboard`
- `install`
- `extract-steps`
- `auto-skill-execution`
- `assisted-skill-execution`
- `guided-skill-execution`

If you are installing this bundle into Codex from a git checkout, run
`scripts/install_codex_skill.sh` from this directory or from the repo's
`skills/pm/scripts/` path.

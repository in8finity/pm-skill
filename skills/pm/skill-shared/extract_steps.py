#!/usr/bin/env python3
r"""Extract a list of step titles from a skill's SKILL.md.

Locates the SKILL.md by name (search order: ~/.claude/skills/<name>,
~/.claude/plugins/marketplaces/*/skills/<name>, ~/.claude/plugins/cache/*/skills/<name>,
or any path matching */skills/<name>/SKILL.md). Then tries three extraction
strategies in order; the first that yields >=2 steps wins.

Strategies:
  1. step-headings       lines matching ^#{1,6}\s*Step\s*\d+
  2. numbered-in-section numbered items inside the first section whose
                         heading contains one of: Guided, Steps, Procedure,
                         Workflow, Process
  3. top-level-sections  ^## headings, excluding boilerplate (Inputs, Notes,
                         Examples, References, Bundled files, etc.)

Output: JSON with {skill, skill_path, strategy, steps: [{n, title, anchor}]}
where anchor is the raw heading/line text useful for grep-back.

Usage:
  extract_steps.py <skill-name>
  extract_steps.py --path /abs/path/to/SKILL.md
"""
import argparse
import json
import os
import re
import sys
from glob import glob
from pathlib import Path

HOME = Path.home()
BOILERPLATE_HEADINGS = {
    "inputs", "notes", "examples", "reference files", "references",
    "bundled files", "exit codes", "output", "outputs", "see also",
    "scripts", "running models", "model output location",
    "report output location", "when not to use", "tone and style",
    "file & directory tasks", "system", "doing tasks",
    "workflow integration", "modeling conventions",
    "common patterns from production models", "modeling styles: when to use what",
    "writing a model: two modes",
}


def find_skill_md(name: str) -> Path:
    candidates = [
        HOME / ".claude" / "skills" / name / "SKILL.md",
    ]
    candidates += [Path(p) for p in glob(
        str(HOME / ".claude" / "plugins" / "marketplaces" / "*" / "skills" / name / "SKILL.md"))]
    candidates += [Path(p) for p in glob(
        str(HOME / ".claude" / "plugins" / "cache" / "*" / "*" / "*" / "skills" / name / "SKILL.md"))]
    candidates += [Path(p) for p in glob(
        str(HOME / ".claude" / "plugins" / "cache" / "*" / "skills" / name / "SKILL.md"))]
    candidates += [Path(p) for p in glob(
        str(HOME / ".claude" / "plugins" / "cache" / "*" / "*" / "skills" / name / "SKILL.md"))]
    for p in candidates:
        if p.exists():
            return p
    sys.exit(f"error: SKILL.md for '{name}' not found in standard locations")


STEP_HEADING_RE = re.compile(r"^(#{1,6})\s*Step\s+(\d+)\b\.?\s*(.*?)\s*$", re.I)
SECTION_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
NUMBERED_RE = re.compile(r"^\s*(\d+)\.\s+(?:\*\*([^*]+)\*\*\s*[—-]?\s*)?(.*?)\s*$")


def strip_md(s: str) -> str:
    s = re.sub(r"\*\*(.*?)\*\*", r"\1", s)
    s = re.sub(r"\*(.*?)\*", r"\1", s)
    s = re.sub(r"`([^`]+)`", r"\1", s)
    s = s.rstrip(" :—–-")
    return s.strip()


def extract_step_headings(lines):
    out = []
    for ln in lines:
        m = STEP_HEADING_RE.match(ln)
        if m:
            n = int(m.group(2))
            title = strip_md(m.group(3)) or f"Step {n}"
            out.append({"n": n, "title": title, "anchor": ln.rstrip()})
    return out


SECTION_KEYWORDS = ("guided", "steps", "procedure", "workflow", "process",
                    "step by step")


def extract_numbered_in_section(lines):
    in_target_section = False
    section_level = None
    out = []
    for ln in lines:
        m = SECTION_RE.match(ln)
        if m:
            level = len(m.group(1))
            heading = strip_md(m.group(2)).lower()
            if in_target_section and level <= section_level:
                break
            if any(kw in heading for kw in SECTION_KEYWORDS):
                in_target_section = True
                section_level = level
                continue
        if in_target_section:
            nm = NUMBERED_RE.match(ln)
            if nm:
                n = int(nm.group(1))
                bold = nm.group(2)
                rest = nm.group(3)
                title = strip_md(bold) if bold else strip_md(rest.split(".", 1)[0])
                title = title or f"Step {n}"
                # avoid sub-list false-positives by requiring start at column 0
                if not ln.startswith(" "):
                    out.append({"n": n, "title": title, "anchor": ln.rstrip()})
    return out


def extract_top_sections(lines):
    out = []
    n = 0
    for ln in lines:
        m = SECTION_RE.match(ln)
        if m and len(m.group(1)) == 2:
            heading = strip_md(m.group(2))
            if heading.lower() in BOILERPLATE_HEADINGS:
                continue
            n += 1
            out.append({"n": n, "title": heading, "anchor": ln.rstrip()})
    return out


STRATEGIES = [
    ("step-headings", extract_step_headings),
    ("numbered-in-section", extract_numbered_in_section),
    ("top-level-sections", extract_top_sections),
]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name", nargs="?", help="skill name (e.g. formal-modeling)")
    ap.add_argument("--path", help="absolute path to SKILL.md (overrides name)")
    args = ap.parse_args()
    if not args.path and not args.name:
        ap.error("provide skill name or --path")

    path = Path(args.path) if args.path else find_skill_md(args.name)
    text = path.read_text()
    lines = text.splitlines()

    chosen = None
    for label, fn in STRATEGIES:
        steps = fn(lines)
        if len(steps) >= 2:
            chosen = (label, steps)
            break
    if not chosen:
        chosen = ("none", [])

    label, steps = chosen
    out = {
        "skill": args.name or path.parent.name,
        "skill_path": str(path),
        "strategy": label,
        "steps": steps,
    }
    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()

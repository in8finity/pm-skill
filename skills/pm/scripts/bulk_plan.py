#!/usr/bin/env python3
"""Bulk-enqueue tasks from a JSON file.

Input is a JSON array of task specs:
    [
      {
        "slug":   "stable-id",
        "title":  "human label",
        "text":   "full body",
        "parent":          "<task-sha>",       // optional
        "parent_slug":     "earlier-slug",     // optional alt to parent
        "depends_on":      ["sha", ...],       // optional
        "depends_on_slugs":["earlier-slug",...],// optional alt to depends_on
        "verifier":        "skill:foo",        // optional
        "sticky":          true,               // optional (auto from parent)
        "workdir":         "/abs/path"         // optional (else env / parent's)
      },
      ...
    ]

`parent_slug` and `depends_on_slugs` resolve to text_sha256s of tasks
referenced by slug in the same queue — either created earlier in this
same batch, or already-existing tasks. This makes single-batch nested
trees workable without computing shas in advance: declare parents
before children in the array, use slug references throughout, and
bulk-plan resolves them in order.

Behavior is idempotent per slug:
- new slug                    -> create Task + genesis TaskStatus(new)
- slug exists, no status      -> append genesis TaskStatus(new) (heal)
- slug exists, has status     -> skip

Usage:
  bulk_plan.py [--queue Q] --input path/to/specs.json
  bulk_plan.py [--queue Q] --input -                 # read JSON from stdin
  bulk_plan.py [--queue Q] --input ... --chain-siblings
      # auto-add depends_on between consecutive specs sharing the same
      # parent_slug (sequential children). Use for nested-skill expansion.

Stops on the first hard create_task failure (returns exit 1). All other
outcomes (create, heal, skip) are reported per-line on stdout as TSV:
    <text_sha256>\t<slug>\t<outcome:created|healed|skipped>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import store
from plan import resolve_workdir


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--queue", default="default")
    p.add_argument("--input", required=True, help="path to JSON specs (or - for stdin)")
    p.add_argument(
        "--chain-siblings", action="store_true",
        help="auto-chain consecutive specs sharing the same parent_slug "
             "via depends_on (in array order). Use this for skill-expansion "
             "runs where nested steps must execute sequentially under their "
             "parent. Top-level specs (no parent_slug) are not affected — "
             "chain those explicitly with depends_on_slugs.",
    )
    p.add_argument(
        "--finalize-slug", default=None, metavar="SLUG",
        help="auto-append a finalizer task at the end of the spec with "
             "depends_on_slugs covering every other slug in the batch. "
             "The finalizer is the queue-level rollup — when it reaches "
             "`done` the queue is provably finished. Use one finalizer per "
             "queue (multiple --finalize-slug invocations across batches "
             "create independent finalizers, which won't aggregate).",
    )
    p.add_argument(
        "--finalize-title", default="Queue rollup",
        help="title for the auto-appended finalizer task "
             "(only used when --finalize-slug is set)",
    )
    p.add_argument(
        "--allow-heavy-parent", action="store_true",
        help="bypass the parent-body lint. By default, a spec referenced as "
             "`parent_slug` by another spec in the batch must contain a "
             "`Role: parent` line in its body (the marker emitted by "
             "`pm build-task-body --mode parent`). The lint enforces the "
             "convention that parent tasks are lightweight grouping/contexting "
             "nodes, not work nodes — see `skills/pm/plan/SKILL.md` "
             "\"Parents are grouping nodes\". Use this flag for legacy or "
             "exceptional cases where you've reviewed and accepted a heavy "
             "parent body.",
    )
    p.add_argument(
        "--finalize-text",
        default="Queue-level rollup. This task depends on every other "
                "task in the batch and reaches `done` only after all of "
                "them settle. Use as the audit-of-record that the queue "
                "completed in full. Body: read each prior task's report "
                "and assemble a one-paragraph summary (or skip the read "
                "if the queue's outputs are otherwise self-evident).",
        help="text body for the auto-appended finalizer task "
             "(only used when --finalize-slug is set)",
    )
    args = p.parse_args()

    raw = sys.stdin.read() if args.input == "-" else Path(args.input).read_text()
    specs = json.loads(raw)
    if not isinstance(specs, list):
        sys.stderr.write("input must be a JSON array of task specs\n")
        return 2

    # Auto-append the finalizer first, BEFORE chain-siblings, so the
    # finalizer's depends_on covers everything below.
    if args.finalize_slug:
        if any(s["slug"] == args.finalize_slug for s in specs):
            sys.stderr.write(
                f"--finalize-slug '{args.finalize_slug}' collides with a "
                f"spec slug; pick another name or remove the explicit spec\n"
            )
            return 8
        specs.append({
            "slug": args.finalize_slug,
            "title": args.finalize_title,
            "text": args.finalize_text,
            "depends_on_slugs": [s["slug"] for s in specs],
        })

    # Parent-body lint: any spec referenced as `parent_slug` by some
    # other spec in this batch is a parent in the parentTask sense.
    # Per `skills/pm/plan/SKILL.md` "Parents are grouping nodes", a
    # parent's body must be lightweight — not a work directive. Refuse
    # parent specs whose body lacks the `Role: parent` marker (emitted
    # by `pm build-task-body --mode parent`). Bypass with
    # --allow-heavy-parent.
    if not args.allow_heavy_parent:
        parent_slugs = {s["parent_slug"] for s in specs if s.get("parent_slug")}
        slug_to_spec = {s["slug"]: s for s in specs}
        offenders = []
        for ps in parent_slugs:
            spec = slug_to_spec.get(ps)
            if spec is None:
                # parent_slug references a pre-existing task in the queue,
                # not a spec in this batch — out of lint scope.
                continue
            body = spec.get("text") or ""
            if not any(line.strip() == "Role: parent" for line in body.splitlines()):
                offenders.append(ps)
        if offenders:
            sys.stderr.write(
                "parent-body lint failed: the following specs are parents "
                "(referenced as `parent_slug` by other specs) but their body "
                "lacks the `Role: parent` marker line:\n"
            )
            for slug in offenders:
                sys.stderr.write(f"  - {slug}\n")
            sys.stderr.write(
                "\nParent tasks must be lightweight grouping/contexting nodes — "
                "see `skills/pm/plan/SKILL.md` \"Parents are grouping nodes\". "
                "Use `pm build-task-body --mode parent --steps STEPS_JSON --prompt P` "
                "to generate a conformant body, or pass --allow-heavy-parent to "
                "bypass this check for legacy/exceptional cases.\n"
            )
            return 12

    # Auto-chain siblings: walk specs in order, remember the last slug
    # seen per parent_slug, and inject it into the next sibling's
    # depends_on_slugs (if not already present). Mutates the spec in
    # place so the rest of the loop processes the augmented version.
    if args.chain_siblings:
        last_sibling: dict[str, str] = {}
        for spec in specs:
            ps = spec.get("parent_slug")
            if not ps:
                continue
            prior = last_sibling.get(ps)
            if prior:
                deps = list(spec.get("depends_on_slugs") or [])
                if prior not in deps:
                    deps.append(prior)
                    spec["depends_on_slugs"] = deps
            last_sibling[ps] = spec["slug"]

    spawned_at_cache: dict[str, str] = {}

    def spawned_at(parent_sha: str) -> str:
        if parent_sha in spawned_at_cache:
            return spawned_at_cache[parent_sha]
        s = store.latest_status(parent_sha)
        if s is None:
            sys.stderr.write(f"parent {parent_sha[:16]} has no status\n")
            sys.exit(5)
        spawned_at_cache[parent_sha] = s["text_sha256"]
        return spawned_at_cache[parent_sha]

    # slug → text_sha256 for tasks created earlier in this batch (or
    # discovered via find_task_by_slug for slugs that pre-exist).
    slug_to_sha: dict[str, str] = {}

    def resolve_slug(slug_ref: str) -> str:
        if slug_ref in slug_to_sha:
            return slug_to_sha[slug_ref]
        existing = store.find_task_by_slug(args.queue, slug_ref)
        if existing:
            slug_to_sha[slug_ref] = existing["text_sha256"]
            return slug_to_sha[slug_ref]
        sys.stderr.write(
            f"slug-reference '{slug_ref}' could not be resolved — must be "
            f"created earlier in this batch or already exist in queue '{args.queue}'\n"
        )
        sys.exit(7)

    created = healed = skipped = 0
    for spec in specs:
        slug = spec["slug"]
        title = spec["title"]
        text = spec["text"]
        parent = spec.get("parent")
        if not parent and spec.get("parent_slug"):
            parent = resolve_slug(spec["parent_slug"])

        # NoSelfParent + NoCycle on parentTask graph (mirrors plan.py).
        # parent_slug resolution naturally catches self-reference (the
        # spec being processed isn't yet in slug_to_sha and find_task_by
        # _slug fails before this loop adds it), but the sha-form
        # `parent` field can refer to anything — guard it explicitly.
        if parent:
            own_sha = store.sha256_text(store.task_identity_text(args.queue, slug))
            if parent == own_sha:
                sys.stderr.write(
                    f"refusing slug={slug}: parent is this task's own sha "
                    f"({own_sha[:12]}) — self-parent invalid\n"
                )
                sys.exit(11)
            if own_sha in store.find_parent_chain_ancestors(parent):
                sys.stderr.write(
                    f"refusing slug={slug}: parent chain transitively "
                    f"contains this task's sha ({own_sha[:12]}) — would "
                    f"create a parentTask cycle\n"
                )
                sys.exit(11)

        deps = list(spec.get("depends_on") or [])
        for dep_slug in (spec.get("depends_on_slugs") or []):
            deps.append(resolve_slug(dep_slug))
        verifier = spec.get("verifier") or None
        sticky = bool(spec.get("sticky"))
        workdir_override = spec.get("workdir")

        # Inherit sticky + workdir from parent (matches plan.py behavior).
        parent_task = store.get_task(parent) if parent else None
        if parent_task and not sticky and store.task_is_sticky(parent_task):
            sticky = True
        if workdir_override:
            workdir = workdir_override
        else:
            workdir = resolve_workdir(parent_task)

        existing = store.find_task_by_slug(args.queue, slug)
        if existing:
            sha = existing["text_sha256"]
            if store.latest_status(sha) is None:
                store.append_status(sha, "new", note=f"enqueued: {title}")
                healed += 1
                outcome = "healed"
            else:
                skipped += 1
                outcome = "skipped"
            print(f"{sha}\t{slug}\t{outcome}")
            slug_to_sha[slug] = sha
            continue

        try:
            task = store.create_task(
                queue=args.queue,
                title=title,
                text=text,
                slug=slug,
                parent_task_sha=parent,
                spawned_at_status_sha=spawned_at(parent) if parent else None,
                depends_on=deps,
                verifier=verifier,
                sticky=sticky,
                workdir=workdir,
            )
            sha = task["text_sha256"]
            store.append_status(sha, "new", note=f"enqueued: {title}")
            created += 1
            print(f"{sha}\t{slug}\tcreated")
            slug_to_sha[slug] = sha
        except Exception as exc:  # hard failure: stop
            sys.stderr.write(f"FAIL slug={slug}: {exc}\n")
            sys.stderr.write(f"created={created} healed={healed} skipped={skipped}\n")
            return 1

    sys.stderr.write(f"--done-- created={created} healed={healed} skipped={skipped}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

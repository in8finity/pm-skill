#!/usr/bin/env python3
"""Live integration tests for the planning-* skill set golden flows.

Requires a running hashharness MCP server at HASHHARNESS_MCP_URL
(default http://127.0.0.1:38417/mcp).

Each golden flow:
- uses a unique random-suffixed queue name so it doesn't collide with
  live data or other concurrent test runs;
- executes real `pm` subprocess commands and asserts the chain shape
  via the same store helpers production uses;
- prints PASS/FAIL per flow and exits non-zero on any failure.

Usage:
  python3 tests/integration/test_golden.py
  python3 tests/integration/test_golden.py --only G1 G3
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PM = str(REPO / "skills" / "pm" / "scripts" / "pm")
sys.path.insert(0, str(REPO / "skills" / "pm" / "scripts"))
import store  # noqa: E402


def fresh_queue(prefix: str) -> str:
    return f"itest-{prefix}-{secrets.token_hex(3)}"


def pm(*args: str, env_extra: dict[str, str] | None = None,
       cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run([PM, *args], capture_output=True, text=True,
                          env=env, cwd=cwd)
    if check and proc.returncode != 0:
        raise AssertionError(
            f"pm {' '.join(args)} failed (exit {proc.returncode})\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
    return proc


def assert_eq(actual, expected, label: str) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


# ---------------------------------------------------------------------------
# Golden flows
# ---------------------------------------------------------------------------

def g1_fresh_plan_execute_finish() -> None:
    """G1: plan → claim → report → finish (in-place reset path).

    Skips the verifier gate (no `--verifier` set) so we exercise the
    minimal happy path: 4 commands, terminal `done` status with proof.
    """
    q = fresh_queue("g1")
    out = pm("plan", "--queue", q, "--title", "g1 task",
             "--text", "compute the answer to life",
             env_extra={"PM_WORKDIR": ""})
    plan = json.loads(out.stdout)
    task_sha = plan["task"]["text_sha256"]

    # Claim
    pm("executing", "--task", task_sha)
    assert_eq(store.status_value(store.latest_status(task_sha)),
              "working", "G1 status after claim")

    # Report
    pm("report", "--task", task_sha, "--title", "g1 done", "--text", "42")
    report = store.latest_report(task_sha)
    assert report is not None, "G1 expected a TaskReport on chain"

    # Finish
    pm("finished", "--task", task_sha)
    assert_eq(store.status_value(store.latest_status(task_sha)),
              "done", "G1 final status")
    final = store.latest_status(task_sha)
    proof = (final.get("links") or {}).get("proof")
    # Link values are record_sha256 per hashharness contract.
    assert_eq(proof, report["record_sha256"], "G1 proof link points at report")


def g2_chained_deps_pull_order() -> None:
    """G2: A → B with `dependsOn`. `pm next` returns A first, B blocked
    until A is `done`."""
    q = fresh_queue("g2")
    a = json.loads(pm("plan", "--queue", q, "--title", "A",
                      "--text", "stage one",
                      env_extra={"PM_WORKDIR": ""}).stdout)["task"]["text_sha256"]
    b = json.loads(pm("plan", "--queue", q, "--title", "B",
                      "--text", "stage two", "--depends-on", a,
                      env_extra={"PM_WORKDIR": ""}).stdout)["task"]["text_sha256"]

    nxt = pm("next", "--queue", q, env_extra={"PM_WORKDIR": ""}).stdout.strip()
    assert nxt != "null", "G2 first next() should not be null"
    assert json.loads(nxt)["text_sha256"] == a, "G2 first next() must be A"

    # Drive A through to `done`.
    pm("executing", "--task", a)
    pm("report", "--task", a, "--title", "A done", "--text", "ok")
    pm("finished", "--task", a)

    nxt2 = pm("next", "--queue", q, env_extra={"PM_WORKDIR": ""}).stdout.strip()
    assert nxt2 != "null", "G2 second next() should not be null after A done"
    assert json.loads(nxt2)["text_sha256"] == b, "G2 second next() must be B"


def g3_verifier_attestation_happy_path() -> None:
    """G3: verifier=skill:simplify with a properly-formed attestation
    block in the report → `pm finished` exits 0 and verifier metadata is
    recorded on the done-status."""
    q = fresh_queue("g3")
    plan = json.loads(pm(
        "plan", "--queue", q, "--title", "g3 attested",
        "--text", "demonstrate attestation",
        "--verifier", "skill:simplify",
        env_extra={"PM_WORKDIR": ""},
    ).stdout)
    task_sha = plan["task"]["text_sha256"]
    pm("executing", "--task", task_sha)

    body = (
        "I did the work.\n\n"
        "## Verifier Attestation\n"
        "verifier: skill:simplify\n"
        "verdict: PASS\n"
        "evidence:\n"
        "  Reviewed the changes for code reuse, quality, efficiency.\n"
        "  All checks satisfied.\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(body)
        path = f.name
    try:
        pm("report", "--task", task_sha, "--title",
           "g3 attested report", "--text-file", path)
    finally:
        os.unlink(path)

    pm("finished", "--task", task_sha)
    final = store.latest_status(task_sha)
    assert_eq(store.status_value(final), "done", "G3 final status")
    attrs = final.get("attributes") or {}
    assert_eq(attrs.get("verifier"), "skill:simplify", "G3 verifier attr")
    assert_eq(attrs.get("verifier_exit"), 0, "G3 verifier_exit attr")
    assert "PASS" in (attrs.get("verifier_summary") or ""), \
        "G3 verifier_summary should mention PASS"


def g3b_verifier_attestation_missing_block_blocks_finish() -> None:
    """G3b: same setup but report omits the attestation block → finish
    exits 9, task stays in `working`."""
    q = fresh_queue("g3b")
    plan = json.loads(pm(
        "plan", "--queue", q, "--title", "g3b unattested",
        "--text", "no attestation",
        "--verifier", "skill:simplify",
        env_extra={"PM_WORKDIR": ""},
    ).stdout)
    task_sha = plan["task"]["text_sha256"]
    pm("executing", "--task", task_sha)
    pm("report", "--task", task_sha, "--title",
       "g3b sloppy report", "--text", "did stuff")

    proc = pm("finished", "--task", task_sha, check=False)
    assert_eq(proc.returncode, 9, "G3b expected exit 9 on missing attestation")
    assert_eq(store.status_value(store.latest_status(task_sha)),
              "working", "G3b task should remain in working")


def g4_subtask_inherits_parent_workdir() -> None:
    """G4: planner in /tmp/<X> plans parent; worker spawns child with
    --parent. Child inherits parent's workdir and parentTask link.
    Sticky should also inherit if parent is sticky."""
    q = fresh_queue("g4")
    with tempfile.TemporaryDirectory(prefix="pm-itest-g4-") as td:
        parent = json.loads(pm(
            "plan", "--queue", q, "--title", "g4 parent",
            "--text", "spawns a child", "--sticky",
            cwd=td,
        ).stdout)["task"]
        parent_sha = parent["text_sha256"]
        parent_workdir = (parent.get("attributes") or {}).get("workdir")
        assert parent_workdir == os.path.realpath(td), \
            f"G4 parent workdir should be {os.path.realpath(td)}, got {parent_workdir}"

        # Child planned from a *subdirectory* of the parent's workdir;
        # inheritance should pin it to the parent's workdir, not cwd.
        sub = os.path.join(td, "subdir")
        os.makedirs(sub)
        # Parent must have at least a status before --parent is accepted.
        # plan.py appended the genesis status, so we're good. Now claim
        # the parent so a working-status exists for spawnedAt. Parent is
        # sticky → claim requires a PM_CONTEXT_ID; reuse it for the
        # subtask plan so the binding flows through.
        ctx = pm("context-id").stdout.strip()
        sticky_env = {"PM_CONTEXT_ID": ctx}
        pm("executing", "--task", parent_sha, env_extra=sticky_env)

        child = json.loads(pm(
            "plan", "--queue", q, "--title", "g4 child",
            "--text", "spawned from working parent",
            "--parent", parent_sha,
            cwd=sub, env_extra=sticky_env,
        ).stdout)["task"]
        child_attrs = child.get("attributes") or {}
        child_links = child.get("links") or {}
        assert_eq(child_attrs.get("workdir"), parent_workdir,
                  "G4 child workdir must inherit parent's, not cwd")
        assert_eq(child_attrs.get("sticky"), True,
                  "G4 child should inherit sticky from parent")
        # Link values are record_sha256 per hashharness contract.
        parent_record_sha = store.get_task(parent_sha)["record_sha256"]
        assert_eq(child_links.get("parentTask"), parent_record_sha,
                  "G4 child.parentTask must link to parent")
        assert child_links.get("spawnedAt"), \
            "G4 child.spawnedAt must point at parent's working status"


def g5_workdir_isolation() -> None:
    """G5: planner in dir A → worker in dir B sees `null`; worker in
    dir A sees the task."""
    q = fresh_queue("g5")
    with tempfile.TemporaryDirectory(prefix="pm-itest-g5a-") as a, \
         tempfile.TemporaryDirectory(prefix="pm-itest-g5b-") as b:
        plan = json.loads(pm("plan", "--queue", q, "--title", "g5",
                             "--text", "scoped to A", cwd=a).stdout)
        task_sha = plan["task"]["text_sha256"]
        bound = (plan["task"].get("attributes") or {}).get("workdir")
        assert_eq(bound, os.path.realpath(a), "G5 plan-time workdir capture")

        # Worker in dir B: queue looks empty.
        nxt_b = pm("next", "--queue", q, cwd=b).stdout.strip()
        assert_eq(nxt_b, "null",
                  "G5 worker in unrelated dir must see null queue")

        # Worker in dir A: gets the task.
        nxt_a = pm("next", "--queue", q, cwd=a).stdout.strip()
        assert nxt_a != "null", "G5 worker in plan dir must see the task"
        assert_eq(json.loads(nxt_a)["text_sha256"], task_sha,
                  "G5 worker in plan dir must get the right task sha")


def g6_claim_race() -> None:
    """G6: two parallel `pm executing` against one `new` task → exactly
    one wins (exit 0), the other loses (exit 8). Hashharness's native
    `chain_predecessor` head-move check on `prevStatus` is the safety
    guarantee."""
    q = fresh_queue("g6")
    plan = json.loads(pm("plan", "--queue", q, "--title", "race",
                         "--text", "claim me",
                         env_extra={"PM_WORKDIR": ""}).stdout)
    sha = plan["task"]["text_sha256"]
    procs = [
        subprocess.Popen([PM, "executing", "--task", sha,
                          "--agent", f"a{i}"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         env={**os.environ, "PM_WORKDIR": ""})
        for i in range(2)
    ]
    rcs = sorted(p.wait() for p in procs)
    assert rcs == [0, 8], f"G6 expected one winner+one loser; got {rcs}"
    assert_eq(store.status_value(store.latest_status(sha)), "working",
              "G6 final status must be working")


def g7_replan_modes() -> None:
    """G7: in one chain exercise replan's four code paths — in-place
    reset, --no-cascade-up, default cascade-up, supersede+clone."""
    q = fresh_queue("g7")
    a = json.loads(pm("plan", "--queue", q, "--title", "A", "--text", "stage 1",
                      env_extra={"PM_WORKDIR": ""}
                      ).stdout)["task"]["text_sha256"]
    b = json.loads(pm("plan", "--queue", q, "--title", "B", "--text", "stage 2",
                      "--depends-on", a,
                      env_extra={"PM_WORKDIR": ""}
                      ).stdout)["task"]["text_sha256"]

    # Drive A to done.
    pm("executing", "--task", a)
    pm("report", "--task", a, "--title", "ok", "--text", "A done")
    pm("finished", "--task", a)

    # Drive B to rejected.
    pm("executing", "--task", b)
    pm("report", "--task", b, "--title", "fail", "--text", "borked")
    pm("finished", "--task", b, "--rejected")

    # (a) --no-cascade-up: only B reset, A stays done.
    pm("replan", "--task", b, "--no-cascade-up")
    assert_eq(store.status_value(store.latest_status(b)), "new",
              "G7a B reset to new")
    assert_eq(store.status_value(store.latest_status(a)), "done",
              "G7a A untouched by --no-cascade-up")

    # Set up cascade-up: re-reject B.
    pm("executing", "--task", b)
    pm("report", "--task", b, "--title", "fail2", "--text", "borked2")
    pm("finished", "--task", b, "--rejected")

    # (b) Default cascade-up: B AND A both reset.
    pm("replan", "--task", b)
    assert_eq(store.status_value(store.latest_status(b)), "new",
              "G7b B reset")
    assert_eq(store.status_value(store.latest_status(a)), "new",
              "G7b A also reset by default cascade-up")

    # Drive A done again, then supersede+clone B with edits.
    pm("executing", "--task", a)
    pm("report", "--task", a, "--title", "ok2", "--text", "A done2")
    pm("finished", "--task", a)

    out = json.loads(pm(
        "replan", "--task", b, "--text", "rewritten body",
        "--verifier", "skill:simplify", "--no-cascade-up",
    ).stdout)
    new_b = out["target_result"]["new_task"]
    assert new_b != b, "G7c clone must have new sha"
    new_b_obj = store.get_task(new_b)
    new_attrs = new_b_obj.get("attributes") or {}
    assert_eq(new_attrs.get("body"), "rewritten body", "G7c new body")
    assert_eq(new_attrs.get("verifier"), "skill:simplify",
              "G7c new verifier")
    assert (new_attrs.get("slug") or "").endswith("-r1"), \
        f"G7c slug should end with -r1, got {new_attrs.get('slug')}"
    assert_eq(store.status_value(store.latest_status(b)), "superseded",
              "G7c original B superseded")


def g8_sticky_context_binding() -> None:
    """G8: sticky task claimed with PM_CONTEXT_ID=A binds the chain to
    A. Report or finished from PM_CONTEXT_ID=B exit 10."""
    q = fresh_queue("g8")
    sha = json.loads(pm(
        "plan", "--queue", q, "--title", "sticky",
        "--text", "bound to context", "--sticky",
        env_extra={"PM_WORKDIR": ""},
    ).stdout)["task"]["text_sha256"]
    ctx_a = pm("context-id").stdout.strip()
    ctx_b = pm("context-id").stdout.strip()
    assert ctx_a != ctx_b, "G8 generated context IDs should differ"

    pm("executing", "--task", sha,
       env_extra={"PM_CONTEXT_ID": ctx_a, "PM_WORKDIR": ""})
    bound = store.status_context_id(store.latest_status(sha))
    assert_eq(bound, ctx_a, "G8 claim records context_id on status")

    p = pm("report", "--task", sha, "--title", "wrong", "--text", "x",
           env_extra={"PM_CONTEXT_ID": ctx_b, "PM_WORKDIR": ""},
           check=False)
    assert_eq(p.returncode, 10,
              f"G8 report from wrong context must exit 10; got {p.returncode}")

    pm("report", "--task", sha, "--title", "right", "--text", "y",
       env_extra={"PM_CONTEXT_ID": ctx_a, "PM_WORKDIR": ""})

    p = pm("finished", "--task", sha,
           env_extra={"PM_CONTEXT_ID": ctx_b, "PM_WORKDIR": ""},
           check=False)
    assert_eq(p.returncode, 10,
              f"G8 finished from wrong context must exit 10; got {p.returncode}")

    pm("finished", "--task", sha,
       env_extra={"PM_CONTEXT_ID": ctx_a, "PM_WORKDIR": ""})
    assert_eq(store.status_value(store.latest_status(sha)), "done",
              "G8 finished from right context succeeds")


def g9_slug_race() -> None:
    """G9: two parallel `pm plan` with the same (queue, slug) → one
    exit 0, the other exit 4. Content-addressed slug uniqueness."""
    q = fresh_queue("g9")
    procs = [
        subprocess.Popen(
            [PM, "plan", "--queue", q, "--slug", "racer",
             "--title", f"r{i}", "--text", f"body {i}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, "PM_WORKDIR": ""})
        for i in range(2)
    ]
    rcs = sorted(p.wait() for p in procs)
    assert rcs == [0, 4], \
        f"G9 expected one winner+one slug-taken loser; got {rcs}"


def g10_shell_path_verifier_failure() -> None:
    """G10: a shell-script verifier that exits 1 blocks `pm finished`
    with exit 9; verifier_summary captures the script's stderr."""
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
        f.write("#!/bin/bash\necho 'evidence too thin' >&2\nexit 1\n")
        verifier_path = f.name
    os.chmod(verifier_path, 0o755)
    try:
        q = fresh_queue("g10")
        sha = json.loads(pm(
            "plan", "--queue", q, "--title", "g10",
            "--text", "shell verifier", "--verifier", verifier_path,
            env_extra={"PM_WORKDIR": ""},
        ).stdout)["task"]["text_sha256"]
        pm("executing", "--task", sha)
        pm("report", "--task", sha, "--title", "r", "--text", "weak")
        p = pm("finished", "--task", sha, check=False)
        assert_eq(p.returncode, 9,
                  f"G10 expected exit 9 from script verifier; got {p.returncode}")
        assert_eq(store.status_value(store.latest_status(sha)), "working",
                  "G10 task stays in working after verifier failure")
    finally:
        os.unlink(verifier_path)


def g11_cancel_cascade() -> None:
    """G11: parent + 2 working children via parentTask. `pm cancel
    --cascade` rejects all three; each gets a synthetic TaskReport
    carrying the reason."""
    q = fresh_queue("g11")
    parent = json.loads(pm(
        "plan", "--queue", q, "--title", "P", "--text", "parent",
        env_extra={"PM_WORKDIR": ""},
    ).stdout)["task"]["text_sha256"]
    pm("executing", "--task", parent)
    children: list[str] = []
    for i in (1, 2):
        c = json.loads(pm(
            "plan", "--queue", q, "--title", f"C{i}",
            "--text", f"child {i}", "--parent", parent,
            env_extra={"PM_WORKDIR": ""},
        ).stdout)["task"]["text_sha256"]
        pm("executing", "--task", c)
        children.append(c)

    pm("cancel", "--task", parent, "--cascade",
       "--reason", "G11 supervisor kill")

    for t in [parent] + children:
        assert_eq(store.status_value(store.latest_status(t)), "rejected",
                  f"G11 task {t[:8]} must be rejected after cascade")
        assert store.latest_report(t) is not None, \
            f"G11 task {t[:8]} must have a synthetic cancel report"


def g12_reclaim_cascade_sticky() -> None:
    """G12: sticky chain stuck in `working`. `pm reclaim --cascade`
    walks parentTask reverse-links, resets every undone descendant to
    `new`, and marks each status with `reclaimed=true`."""
    q = fresh_queue("g12")
    ctx = pm("context-id").stdout.strip()
    sticky_env = {"PM_CONTEXT_ID": ctx, "PM_WORKDIR": ""}
    parent = json.loads(pm(
        "plan", "--queue", q, "--title", "P",
        "--text", "parent", "--sticky", env_extra=sticky_env,
    ).stdout)["task"]["text_sha256"]
    pm("executing", "--task", parent, env_extra=sticky_env)
    child = json.loads(pm(
        "plan", "--queue", q, "--title", "C",
        "--text", "child", "--parent", parent, env_extra=sticky_env,
    ).stdout)["task"]["text_sha256"]
    pm("executing", "--task", child, env_extra=sticky_env)

    pm("reclaim", "--task", parent, "--cascade",
       "--reason", "agent died")

    for t in (parent, child):
        latest = store.latest_status(t)
        assert_eq(store.status_value(latest), "new",
                  f"G12 task {t[:8]} must be reclaimed to new")
        assert (latest.get("attributes") or {}).get("reclaimed"), \
            f"G12 task {t[:8]} latest status must carry reclaimed=true"


def g13_heartbeat_sweep() -> None:
    """G13: claim a task, age its tip past TTL, run `pm sweep --ttl 1`,
    expect the task back to `new` (zombie reclaim).

    Adds ~2s wall-clock; matches the documented zombie-recovery flow."""
    q = fresh_queue("g13")
    sha = json.loads(pm(
        "plan", "--queue", q, "--title", "g13",
        "--text", "zombie", env_extra={"PM_WORKDIR": ""},
    ).stdout)["task"]["text_sha256"]
    pm("executing", "--task", sha)
    assert_eq(store.status_value(store.latest_status(sha)), "working",
              "G13 task claimed before sleep")

    time.sleep(2)

    pm("sweep", "--queue", q, "--ttl", "1")
    final = store.latest_status(sha)
    assert_eq(store.status_value(final), "new",
              "G13 sweep must reset the zombie back to new")
    assert (final.get("attributes") or {}).get("reclaimed"), \
        "G13 reclaim status must carry reclaimed=true"


def g14_dep_gate_rejected_blocks() -> None:
    """G14: `pm next` skips a task whose dep is `rejected` — same as a
    non-`done` dep. The user must replan or cancel B explicitly to
    unblock the chain."""
    q = fresh_queue("g14")
    a = json.loads(pm("plan", "--queue", q, "--title", "A", "--text", "x",
                      env_extra={"PM_WORKDIR": ""}
                      ).stdout)["task"]["text_sha256"]
    b = json.loads(pm("plan", "--queue", q, "--title", "B", "--text", "y",
                      "--depends-on", a,
                      env_extra={"PM_WORKDIR": ""}
                      ).stdout)["task"]["text_sha256"]
    pm("executing", "--task", a)
    pm("report", "--task", a, "--title", "fail", "--text", "no")
    pm("finished", "--task", a, "--rejected")

    nxt = pm("next", "--queue", q,
             env_extra={"PM_WORKDIR": ""}).stdout.strip()
    assert_eq(nxt, "null",
              f"G14 expected null (B blocked by rejected A); got {nxt[:120]}")
    # B is still `new`; the dep gate, not the status, blocks it.
    assert_eq(store.status_value(store.latest_status(b)), "new",
              "G14 B itself stays in new")


def g15_spawned_at_links_current_status() -> None:
    """G15: subtask's `links.spawnedAt` must point at the parent's
    current TaskStatus sha at the moment of plan — i.e. the working-
    status appended by `pm executing`, not the genesis `new` status."""
    q = fresh_queue("g15")
    parent = json.loads(pm(
        "plan", "--queue", q, "--title", "P", "--text", "parent",
        env_extra={"PM_WORKDIR": ""},
    ).stdout)["task"]["text_sha256"]
    pm("executing", "--task", parent)
    # Link values are record_sha256 per hashharness contract.
    parent_working_record_sha = store.latest_status(parent)["record_sha256"]

    child = json.loads(pm(
        "plan", "--queue", q, "--title", "C", "--text", "child",
        "--parent", parent, env_extra={"PM_WORKDIR": ""},
    ).stdout)["task"]
    spawned = (child.get("links") or {}).get("spawnedAt")
    assert_eq(spawned, parent_working_record_sha,
              "G15 spawnedAt must point at parent's current working status")


def g16_finished_without_report() -> None:
    """G16: claim a task, then `pm finished` with no TaskReport on the
    chain → exit 7, task stays `working`. Closes the proof-gate
    coverage gap (G3b only covers exit 9, where a report exists)."""
    q = fresh_queue("g16")
    sha = json.loads(pm("plan", "--queue", q, "--title", "g16",
                        "--text", "no report",
                        env_extra={"PM_WORKDIR": ""}).stdout
                     )["task"]["text_sha256"]
    pm("executing", "--task", sha)
    p = pm("finished", "--task", sha, check=False)
    assert_eq(p.returncode, 7,
              f"G16 expected exit 7 with no report; got {p.returncode}")
    assert_eq(store.status_value(store.latest_status(sha)), "working",
              "G16 task must remain in working after refused finish")


def g17_cancel_terminal_refused() -> None:
    """G17: drive a task to `done`, then `pm cancel` it → exit 6, task
    stays `done`. Closes the cancellation-gate refusal coverage gap
    (G11 only covers the cascade-success path)."""
    q = fresh_queue("g17")
    sha = json.loads(pm("plan", "--queue", q, "--title", "g17",
                        "--text", "cancel-after-done",
                        env_extra={"PM_WORKDIR": ""}).stdout
                     )["task"]["text_sha256"]
    pm("executing", "--task", sha)
    pm("report", "--task", sha, "--title", "g17 done", "--text", "ok")
    pm("finished", "--task", sha)
    assert_eq(store.status_value(store.latest_status(sha)), "done",
              "G17 setup: task must be done before cancel attempt")
    p = pm("cancel", "--task", sha,
           "--reason", "G17 should be refused", check=False)
    assert_eq(p.returncode, 6,
              f"G17 expected exit 6 cancelling terminal task; got {p.returncode}")
    assert_eq(store.status_value(store.latest_status(sha)), "done",
              "G17 task must remain done after refused cancel")


def g18_self_loop_dep_refused() -> None:
    """G18: `pm plan --depends-on <own-prospective-sha>` exits 11 (self-loop
    is unrunnable; would deadlock the queue). Closes the dep-validation
    cycle gap formalized as plan[t]'s `t not in t.deps` precondition."""
    import hashlib
    q = fresh_queue("g18")
    slug = "self-loop"
    own_text = f"task:{q}/{slug}"
    own_sha = hashlib.sha256(own_text.encode()).hexdigest()
    p = pm("plan", "--queue", q, "--slug", slug, "--title", "g18",
           "--text", "self-dep", "--depends-on", own_sha,
           env_extra={"PM_WORKDIR": ""}, check=False)
    assert_eq(p.returncode, 11,
              f"G18 expected exit 11 on self-loop dep; got {p.returncode}")
    # Task must not have been created.
    assert store.find_task_by_slug(q, slug) is None, \
        "G18 task must not exist after self-loop refusal"


def g20_heartbeat_wins_reclaim_race() -> None:
    """G20: simulate the TTL-window race — sweep snapshots the heartbeat
    tip BEFORE the worker heartbeats, then attempts reclaim. With the
    preempt-heartbeat protocol, `chain_predecessor` on `prevHeartbeat`
    rejects the preempt → WorkerStillAlive → task stays `working`.
    Closes the heartbeat-vs-reclaim race."""
    q = fresh_queue("g20")
    sha = json.loads(pm("plan", "--queue", q, "--title", "g20",
                        "--text", "race victim",
                        env_extra={"PM_WORKDIR": ""}).stdout
                     )["task"]["text_sha256"]
    pm("executing", "--task", sha)

    # Sweeper would have observed the heartbeat tip BEFORE the worker
    # heartbeated — capture that snapshot now.
    prev_hb = store.latest_heartbeat(sha)
    prev_hb_sha = prev_hb["record_sha256"] if prev_hb else None

    # Worker heartbeats — extending the chain past the sweeper's snapshot.
    pm("heartbeat", "--task", sha)

    # Sweeper attempts reclaim with the now-stale snapshot.
    try:
        store.reclaim(
            sha,
            reason="g20 stale-snapshot reclaim attempt",
            reclaimer="g20-sweeper",
            preempt_heartbeat=True,
            preempt_prev_heartbeat_sha=prev_hb_sha,
        )
        raise AssertionError(
            "G20 reclaim should have raised WorkerStillAlive"
        )
    except store.WorkerStillAlive:
        pass

    # Task must remain working — worker not evicted.
    assert_eq(store.status_value(store.latest_status(sha)), "working",
              "G20 task must remain working after raced reclaim is refused")


def g21_sweep_wins_with_no_concurrent_heartbeat() -> None:
    """G21: counterpart to G20 — when no heartbeat races, the preempt
    commits and the reclaim status follows. Ensures the preempt mechanism
    isn't a regression on the dead-worker recovery path."""
    q = fresh_queue("g21")
    sha = json.loads(pm("plan", "--queue", q, "--title", "g21",
                        "--text", "sweep wins",
                        env_extra={"PM_WORKDIR": ""}).stdout
                     )["task"]["text_sha256"]
    pm("executing", "--task", sha)

    prev_hb = store.latest_heartbeat(sha)
    prev_hb_sha = prev_hb["record_sha256"] if prev_hb else None

    # No worker heartbeat in between — sweeper preempt should commit.
    result = store.reclaim(
        sha,
        reason="g21 dead-worker recovery",
        reclaimer="g21-sweeper",
        preempt_heartbeat=True,
        preempt_prev_heartbeat_sha=prev_hb_sha,
    )
    assert_eq(store.status_value(store.latest_status(sha)), "new",
              "G21 task must be reclaimed to new")
    assert (result.get("attributes") or {}).get("reclaimed"), \
        "G21 reclaim status must carry reclaimed=true"


def g19_nonexistent_dep_refused() -> None:
    """G19: `pm plan --depends-on <bogus-sha>` exits 11 (forever-blocked
    deps refused at plan time, not silently created)."""
    q = fresh_queue("g19")
    bogus = "0" * 64
    p = pm("plan", "--queue", q, "--slug", "bogus-dep", "--title", "g19",
           "--text", "depends on nothing", "--depends-on", bogus,
           env_extra={"PM_WORKDIR": ""}, check=False)
    assert_eq(p.returncode, 11,
              f"G19 expected exit 11 on non-existent dep; got {p.returncode}")
    assert store.find_task_by_slug(q, "bogus-dep") is None, \
        "G19 task must not exist after bad-dep refusal"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

ALL_FLOWS = {
    "G1": g1_fresh_plan_execute_finish,
    "G2": g2_chained_deps_pull_order,
    "G3": g3_verifier_attestation_happy_path,
    "G3b": g3b_verifier_attestation_missing_block_blocks_finish,
    "G4": g4_subtask_inherits_parent_workdir,
    "G5": g5_workdir_isolation,
    "G6": g6_claim_race,
    "G7": g7_replan_modes,
    "G8": g8_sticky_context_binding,
    "G9": g9_slug_race,
    "G10": g10_shell_path_verifier_failure,
    "G11": g11_cancel_cascade,
    "G12": g12_reclaim_cascade_sticky,
    "G13": g13_heartbeat_sweep,
    "G14": g14_dep_gate_rejected_blocks,
    "G15": g15_spawned_at_links_current_status,
    "G16": g16_finished_without_report,
    "G17": g17_cancel_terminal_refused,
    "G18": g18_self_loop_dep_refused,
    "G19": g19_nonexistent_dep_refused,
    "G20": g20_heartbeat_wins_reclaim_race,
    "G21": g21_sweep_wins_with_no_concurrent_heartbeat,
}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--only", nargs="+", default=None,
                   help="run only the named flows (e.g. G1 G3)")
    args = p.parse_args()
    flows = args.only or list(ALL_FLOWS.keys())
    fails: list[tuple[str, str]] = []
    for name in flows:
        fn = ALL_FLOWS.get(name)
        if fn is None:
            print(f"  ?  {name:4s}  unknown flow")
            fails.append((name, "unknown flow"))
            continue
        try:
            fn()
            print(f"  ✓  {name:4s}  {fn.__doc__.splitlines()[0] if fn.__doc__ else ''}")
        except AssertionError as exc:
            print(f"  ✗  {name:4s}  {exc}")
            fails.append((name, str(exc)))
        except Exception as exc:
            print(f"  ✗  {name:4s}  {type(exc).__name__}: {exc}")
            fails.append((name, repr(exc)))
    print()
    if fails:
        print(f"FAIL: {len(fails)}/{len(flows)} flow(s) failed")
        return 1
    print(f"PASS: {len(flows)}/{len(flows)} flow(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

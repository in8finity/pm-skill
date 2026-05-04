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
    pm("replan", "--task", b, "--no-cascade")
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
        "--verifier", "skill:simplify", "--no-cascade",
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
    pm("executing", "--task", sha, "--agent", "g20-worker")

    # Sweeper would have observed the heartbeat tip BEFORE the worker
    # heartbeated — capture that snapshot now.
    prev_hb = store.latest_heartbeat(sha)
    prev_hb_sha = prev_hb["record_sha256"] if prev_hb else None

    # Worker heartbeats — extending the chain past the sweeper's snapshot.
    # Must match the agent that holds the working status (heartbeat.py
    # exit-12 lease check, see G22).
    pm("heartbeat", "--task", sha, "--agent", "g20-worker")

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


def g22_zombie_heartbeat_after_reclaim_refused() -> None:
    """G22: agent A claims, gets reclaimed, agent B re-claims. Zombie A
    tries to heartbeat → exit 12 (lease lost). Closes the stale-claim
    heartbeat hole that the preempt mechanism alone couldn't catch."""
    q = fresh_queue("g22")
    sha = json.loads(pm("plan", "--queue", q, "--title", "g22",
                        "--text", "zombie hb",
                        env_extra={"PM_WORKDIR": ""}).stdout
                     )["task"]["text_sha256"]
    # Agent A claims.
    pm("executing", "--task", sha, "--agent", "agent-A")
    assert_eq(store.status_value(store.latest_status(sha)), "working",
              "G22 setup: A holds working")

    # Sweeper reclaims (no heartbeat racing).
    prev_hb = store.latest_heartbeat(sha)
    store.reclaim(sha, reason="g22 force reclaim",
                  reclaimer="g22-sweep",
                  preempt_heartbeat=True,
                  preempt_prev_heartbeat_sha=(prev_hb["record_sha256"]
                                              if prev_hb else None))
    assert_eq(store.status_value(store.latest_status(sha)), "new",
              "G22 setup: reclaimed back to new")

    # Agent B re-claims.
    pm("executing", "--task", sha, "--agent", "agent-B")
    new_owner = (store.latest_status(sha).get("attributes") or {}).get("agent")
    assert_eq(new_owner, "agent-B", "G22 setup: B now owns")

    # Zombie A wakes up and tries to heartbeat.
    p = pm("heartbeat", "--task", sha, "--agent", "agent-A", check=False)
    assert_eq(p.returncode, 12,
              f"G22 zombie heartbeat must exit 12; got {p.returncode}")

    # B can still heartbeat normally.
    pm("heartbeat", "--task", sha, "--agent", "agent-B")


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


def g23_cancel_superseded_refused() -> None:
    """G23: supersede a task via replan, then try to cancel the original
    → exit 6 (superseded is absorbing). Closes R4 SupersededIsAbsorbing
    runtime gap previously violated by cancel.py."""
    q = fresh_queue("g23")
    sha = json.loads(pm("plan", "--queue", q, "--title", "g23",
                        "--text", "to be superseded",
                        env_extra={"PM_WORKDIR": ""}).stdout
                     )["task"]["text_sha256"]
    pm("executing", "--task", sha)
    pm("report", "--task", sha, "--title", "r", "--text", "x")
    pm("finished", "--task", sha, "--rejected")
    # Replan with edits → original becomes superseded.
    pm("replan", "--task", sha, "--text", "v2", "--no-cascade")
    assert_eq(store.status_value(store.latest_status(sha)), "superseded",
              "G23 setup: original must be superseded")

    p = pm("cancel", "--task", sha,
           "--reason", "G23 should refuse", check=False)
    assert_eq(p.returncode, 6,
              f"G23 expected exit 6 cancelling superseded; got {p.returncode}")
    assert_eq(store.status_value(store.latest_status(sha)), "superseded",
              "G23 superseded task must remain superseded")


def g24_supersede_clone_inherits_deps_and_carries_replan_of() -> None:
    """G24: bundle of replan-clone properties — R1 (replan refused on
    superseded), R5 (clone inherits dep set), R6 (clone's genesis
    TaskStatus carries replan_of), R8 (cascade-up skips working
    ancestor)."""
    q = fresh_queue("g24")
    # A is a dep that we'll leave in `working` (so cascade should skip it).
    a = json.loads(pm("plan", "--queue", q, "--title", "A", "--text", "dep A",
                      env_extra={"PM_WORKDIR": ""}).stdout
                   )["task"]["text_sha256"]
    pm("executing", "--task", a)  # A → working
    # B depends on A, will be replan-cloned.
    b = json.loads(pm("plan", "--queue", q, "--title", "B", "--text", "B v1",
                      "--depends-on", a,
                      env_extra={"PM_WORKDIR": ""}).stdout
                   )["task"]["text_sha256"]
    # B can't be claimed (A is working, not done), so B is still `new`.
    # Drive B's lifecycle to terminal so we can replan it: skip — actually
    # replan refuses if B is `new`/`working` (R2 skip-not-refuse for
    # reset_in_place; for supersede_and_clone the gate is just "not
    # superseded"). For supersede+clone, B can be in any non-superseded
    # state. Run replan with edits → supersede+clone path.
    out = json.loads(pm("replan", "--task", b, "--text", "B v2").stdout)
    new_b = out["target_result"]["new_task"]
    assert_eq(out["target_result"]["mode"], "supersede_and_clone",
              "G24 mode must be supersede_and_clone (--text given)")

    # R5: clone inherits dep set.
    new_b_obj = store.get_task(new_b)
    new_deps_records = (new_b_obj.get("links") or {}).get("dependsOn") or []
    a_record_sha = store.get_task(a)["record_sha256"]
    assert_eq(new_deps_records, [a_record_sha],
              "G24 R5: clone's dependsOn must equal original's (A only)")

    # R6: clone's genesis TaskStatus carries replan_of = original.
    genesis = store.latest_status(new_b)
    replan_of = (genesis.get("attributes") or {}).get("replan_of")
    assert_eq(replan_of, b,
              "G24 R6: clone's genesis status must carry replan_of=original")

    # R8: cascade-up skipped A (it was `working`, not terminal). Confirm
    # A is still working — replan didn't touch it.
    assert_eq(store.status_value(store.latest_status(a)), "working",
              "G24 R8: working ancestor must be untouched by cascade-up")

    # R1: trying to replan the now-superseded original → exit 6.
    p = pm("replan", "--task", b, check=False)
    assert_eq(p.returncode, 6,
              f"G24 R1: replan on superseded must exit 6; got {p.returncode}")


def g25_replan_skip_on_non_terminal_target() -> None:
    """G25: in-place replan of a task in `new` or `working` is a no-op
    (skip), not a refusal. The output records `skipped: true` and the
    target's status is unchanged. Closes R2 ResetOnlyOnTerminal coverage
    gap (skip semantics specifically — refusal on superseded is G23)."""
    q = fresh_queue("g25")
    sha = json.loads(pm("plan", "--queue", q, "--title", "g25",
                        "--text", "skip me",
                        env_extra={"PM_WORKDIR": ""}).stdout
                     )["task"]["text_sha256"]
    # Task is `new`. In-place replan should skip.
    out = json.loads(pm("replan", "--task", sha, "--no-cascade").stdout)
    target_result = out["target_result"]
    assert target_result.get("skipped") is True, \
        f"G25 expected skipped=True for replan of `new` target; got {target_result}"
    assert_eq(target_result.get("current"), "new",
              "G25 skipped result must record current status")
    assert_eq(store.status_value(store.latest_status(sha)), "new",
              "G25 target must remain new (no append, no transition)")

    # Now claim it → working. Replan again — also skips.
    pm("executing", "--task", sha)
    out2 = json.loads(pm("replan", "--task", sha, "--no-cascade").stdout)
    target_result2 = out2["target_result"]
    assert target_result2.get("skipped") is True, \
        f"G25 expected skipped=True for replan of `working` target; got {target_result2}"
    assert_eq(target_result2.get("current"), "working",
              "G25 skipped result must record working status")
    assert_eq(store.status_value(store.latest_status(sha)), "working",
              "G25 target must remain working")


def g26_cascade_preserves_done_descendants() -> None:
    """G26: cascade with mixed-state children — one done, one working.
    The done child must stay done; the working child must become
    rejected. Closes CC2 PreviousTerminalUntouched + CC3
    CascadeOnlyTransitionsNonTerminal coverage gaps."""
    q = fresh_queue("g26")
    parent = json.loads(pm("plan", "--queue", q, "--title", "P", "--text", "p",
                           env_extra={"PM_WORKDIR": ""}).stdout
                        )["task"]["text_sha256"]
    pm("executing", "--task", parent)
    # Done child.
    done_child = json.loads(pm("plan", "--queue", q, "--title", "C-done",
                               "--text", "d", "--parent", parent,
                               env_extra={"PM_WORKDIR": ""}).stdout
                            )["task"]["text_sha256"]
    pm("executing", "--task", done_child)
    pm("report", "--task", done_child, "--title", "r", "--text", "ok")
    pm("finished", "--task", done_child)
    # Working child.
    working_child = json.loads(pm("plan", "--queue", q, "--title", "C-work",
                                  "--text", "w", "--parent", parent,
                                  env_extra={"PM_WORKDIR": ""}).stdout
                               )["task"]["text_sha256"]
    pm("executing", "--task", working_child)

    pm("cancel", "--task", parent, "--cascade", "--reason", "G26")

    assert_eq(store.status_value(store.latest_status(parent)), "rejected",
              "G26 parent must be rejected")
    assert_eq(store.status_value(store.latest_status(done_child)), "done",
              "G26 CC2: done child must STAY done (not re-closed)")
    assert_eq(store.status_value(store.latest_status(working_child)), "rejected",
              "G26 working child must be rejected")


def g27_cascade_three_deep_transitive() -> None:
    """G27: a→b→c via parentTask. Cancelling a reaches c. Closes CC4
    CascadeIsParentTransitive coverage gap (G11 was only 2-deep)."""
    q = fresh_queue("g27")
    a = json.loads(pm("plan", "--queue", q, "--title", "A", "--text", "a",
                      env_extra={"PM_WORKDIR": ""}).stdout
                   )["task"]["text_sha256"]
    pm("executing", "--task", a)
    b = json.loads(pm("plan", "--queue", q, "--title", "B", "--text", "b",
                      "--parent", a,
                      env_extra={"PM_WORKDIR": ""}).stdout
                   )["task"]["text_sha256"]
    pm("executing", "--task", b)
    c = json.loads(pm("plan", "--queue", q, "--title", "C", "--text", "c",
                      "--parent", b,
                      env_extra={"PM_WORKDIR": ""}).stdout
                   )["task"]["text_sha256"]
    pm("executing", "--task", c)

    pm("cancel", "--task", a, "--cascade", "--reason", "G27")

    for t, label in ((a, "a (root)"), (b, "b"), (c, "c (grandchild)")):
        assert_eq(store.status_value(store.latest_status(t)), "rejected",
                  f"G27 CC4: {label} must be rejected by transitive cascade")


def g28_cascade_isolates_non_descendant_sibling() -> None:
    """G28: cascade on root + a sibling task with NO parent. Sibling
    must be untouched. Closes CC5 NonDescendantUntouched coverage gap."""
    q = fresh_queue("g28")
    root = json.loads(pm("plan", "--queue", q, "--title", "Root", "--text", "r",
                         env_extra={"PM_WORKDIR": ""}).stdout
                      )["task"]["text_sha256"]
    pm("executing", "--task", root)
    child = json.loads(pm("plan", "--queue", q, "--title", "Child",
                          "--text", "c", "--parent", root,
                          env_extra={"PM_WORKDIR": ""}).stdout
                       )["task"]["text_sha256"]
    pm("executing", "--task", child)
    # Sibling: no parent, in same queue.
    sibling = json.loads(pm("plan", "--queue", q, "--title", "Sibling",
                            "--text", "s",
                            env_extra={"PM_WORKDIR": ""}).stdout
                         )["task"]["text_sha256"]
    pm("executing", "--task", sibling)

    pm("cancel", "--task", root, "--cascade", "--reason", "G28")

    assert_eq(store.status_value(store.latest_status(root)), "rejected",
              "G28 root must be rejected")
    assert_eq(store.status_value(store.latest_status(child)), "rejected",
              "G28 child must be rejected")
    assert_eq(store.status_value(store.latest_status(sibling)), "working",
              "G28 CC5: non-descendant sibling must remain working")


def g29_reclaim_cascade_skips_non_working_descendants() -> None:
    """G29: parent + new child + done child — reclaim --cascade releases
    only the parent (the only working descendant); the new child stays
    new and the done child stays done. Closes RC2 NewDescendantsUntouched
    and RC3 TerminalDescendantsUntouched coverage gaps."""
    q = fresh_queue("g29")
    parent = json.loads(pm("plan", "--queue", q, "--title", "P", "--text", "p",
                           env_extra={"PM_WORKDIR": ""}).stdout
                        )["task"]["text_sha256"]
    pm("executing", "--task", parent)
    new_child = json.loads(pm("plan", "--queue", q, "--title", "C-new",
                              "--text", "n", "--parent", parent,
                              env_extra={"PM_WORKDIR": ""}).stdout
                           )["task"]["text_sha256"]
    # new_child stays in `new` — never claimed.
    done_child = json.loads(pm("plan", "--queue", q, "--title", "C-done",
                               "--text", "d", "--parent", parent,
                               env_extra={"PM_WORKDIR": ""}).stdout
                            )["task"]["text_sha256"]
    pm("executing", "--task", done_child)
    pm("report", "--task", done_child, "--title", "r", "--text", "ok")
    pm("finished", "--task", done_child)

    pm("reclaim", "--task", parent, "--cascade", "--reason", "G29")

    assert_eq(store.status_value(store.latest_status(parent)), "new",
              "G29 parent must be reclaimed to new")
    assert_eq(store.status_value(store.latest_status(new_child)), "new",
              "G29 RC2: already-new child must remain new (untouched)")
    assert_eq(store.status_value(store.latest_status(done_child)), "done",
              "G29 RC3: done child must remain done (untouched)")


def g30_reclaim_cascade_three_deep_transitive() -> None:
    """G30: a→b→c via parentTask, all working. Reclaim --cascade a
    reaches c. Closes RC4 CascadeIsParentTransitive coverage gap."""
    q = fresh_queue("g30")
    a = json.loads(pm("plan", "--queue", q, "--title", "A", "--text", "a",
                      env_extra={"PM_WORKDIR": ""}).stdout
                   )["task"]["text_sha256"]
    pm("executing", "--task", a)
    b = json.loads(pm("plan", "--queue", q, "--title", "B", "--text", "b",
                      "--parent", a,
                      env_extra={"PM_WORKDIR": ""}).stdout
                   )["task"]["text_sha256"]
    pm("executing", "--task", b)
    c = json.loads(pm("plan", "--queue", q, "--title", "C", "--text", "c",
                      "--parent", b,
                      env_extra={"PM_WORKDIR": ""}).stdout
                   )["task"]["text_sha256"]
    pm("executing", "--task", c)

    pm("reclaim", "--task", a, "--cascade", "--reason", "G30")

    for t, label in ((a, "a (root)"), (b, "b"), (c, "c (grandchild)")):
        assert_eq(store.status_value(store.latest_status(t)), "new",
                  f"G30 RC4: {label} must be reclaimed by transitive cascade")


def g31_reclaim_cascade_isolates_sibling() -> None:
    """G31: reclaim --cascade root + a sibling task with NO parent. The
    sibling must remain working. Closes RC5 NonDescendantUntouched gap."""
    q = fresh_queue("g31")
    root = json.loads(pm("plan", "--queue", q, "--title", "Root", "--text", "r",
                         env_extra={"PM_WORKDIR": ""}).stdout
                      )["task"]["text_sha256"]
    pm("executing", "--task", root)
    child = json.loads(pm("plan", "--queue", q, "--title", "Child",
                          "--text", "c", "--parent", root,
                          env_extra={"PM_WORKDIR": ""}).stdout
                       )["task"]["text_sha256"]
    pm("executing", "--task", child)
    sibling = json.loads(pm("plan", "--queue", q, "--title", "Sibling",
                            "--text", "s",
                            env_extra={"PM_WORKDIR": ""}).stdout
                         )["task"]["text_sha256"]
    pm("executing", "--task", sibling)

    pm("reclaim", "--task", root, "--cascade", "--reason", "G31")

    assert_eq(store.status_value(store.latest_status(root)), "new",
              "G31 root must be reclaimed")
    assert_eq(store.status_value(store.latest_status(child)), "new",
              "G31 child must be reclaimed via cascade")
    assert_eq(store.status_value(store.latest_status(sibling)), "working",
              "G31 RC5: non-descendant sibling must remain working")


def g32_reclaim_refuses_non_working_root() -> None:
    """G32: pm reclaim on a `new` task → exit 6; on a `done` task → exit
    6. Closes RC6 ReclaimRefusesNonWorkingRoot coverage gap."""
    q = fresh_queue("g32")
    # New task: never claimed.
    new_sha = json.loads(pm("plan", "--queue", q, "--title", "N",
                            "--text", "new", env_extra={"PM_WORKDIR": ""}
                            ).stdout)["task"]["text_sha256"]
    p = pm("reclaim", "--task", new_sha, check=False)
    assert_eq(p.returncode, 6,
              f"G32 RC6: reclaim of `new` must exit 6; got {p.returncode}")

    # Done task: drive through to done.
    done_sha = json.loads(pm("plan", "--queue", q, "--title", "D",
                             "--text", "done", env_extra={"PM_WORKDIR": ""}
                             ).stdout)["task"]["text_sha256"]
    pm("executing", "--task", done_sha)
    pm("report", "--task", done_sha, "--title", "r", "--text", "ok")
    pm("finished", "--task", done_sha)
    p = pm("reclaim", "--task", done_sha, check=False)
    assert_eq(p.returncode, 6,
              f"G32 RC6: reclaim of `done` must exit 6; got {p.returncode}")


def g33_parent_gated_by_pending_children() -> None:
    """G33: `pm next` skips a parent task whose children aren't all in
    a terminal status (done / rejected / superseded). Models the
    parent-rolls-up-children gate verified by
    system-models/planning_parent_gate.als#ParentBlockedByPendingChild.
    """
    q = fresh_queue("g33")
    par = json.loads(pm("plan", "--queue", q, "--title", "P", "--text", "parent",
                        env_extra={"PM_WORKDIR": ""})
                     .stdout)["task"]["text_sha256"]
    # Spawn two children with parent kept in `new`. plan.py only needs
    # the parent to have *any* latest_status (the genesis new is fine).
    json.loads(pm("plan", "--queue", q, "--title", "K1", "--text", "k1",
                  "--parent", par, env_extra={"PM_WORKDIR": ""}).stdout)
    json.loads(pm("plan", "--queue", q, "--title", "K2", "--text", "k2",
                  "--parent", par, env_extra={"PM_WORKDIR": ""}).stdout)

    assert_eq(store.status_value(store.latest_status(par)), "new",
              "G33 parent stays `new` (never claimed)")

    # pm next must return one of the kids, NOT the parent — even though
    # parent is older (created first) and would otherwise win the order.
    nxt = json.loads(pm("next", "--queue", q,
                        env_extra={"PM_WORKDIR": ""}).stdout)
    assert nxt is not None, "G33 expected a runnable kid, got null"
    nxt_slug = (nxt.get("attributes") or {}).get("slug")
    assert nxt_slug in ("k1", "k2"), \
        f"G33 expected k1 or k2 (a child), got slug={nxt_slug!r}"


def g34_parent_unblocks_after_children_settle() -> None:
    """G34: once every child is in a terminal status (mix done +
    rejected to also exercise the rejected-is-terminal branch), the
    parent becomes runnable. Models the dual of G33; verified by
    system-models/planning_parent_gate.als#ParentRunnableAfterChildrenSettle
    and #RejectedChildIsTerminalForGate.
    """
    q = fresh_queue("g34")
    par = json.loads(pm("plan", "--queue", q, "--title", "P", "--text", "parent",
                        env_extra={"PM_WORKDIR": ""})
                     .stdout)["task"]["text_sha256"]
    kid1 = json.loads(pm("plan", "--queue", q, "--title", "K1", "--text", "k1",
                         "--parent", par, env_extra={"PM_WORKDIR": ""})
                      .stdout)["task"]["text_sha256"]
    kid2 = json.loads(pm("plan", "--queue", q, "--title", "K2", "--text", "k2",
                         "--parent", par, env_extra={"PM_WORKDIR": ""})
                      .stdout)["task"]["text_sha256"]

    # Drive kid1 → done, kid2 → rejected.
    pm("executing", "--task", kid1)
    pm("report", "--task", kid1, "--title", "k1 ok", "--text", "ok")
    pm("finished", "--task", kid1)
    pm("executing", "--task", kid2)
    pm("report", "--task", kid2, "--title", "k2 fail", "--text", "x")
    pm("finished", "--task", kid2, "--rejected")

    # Parent should now be returned by pm next.
    nxt = json.loads(pm("next", "--queue", q,
                        env_extra={"PM_WORKDIR": ""}).stdout)
    assert nxt is not None, "G34 expected the parent to be runnable, got null"
    assert_eq(nxt["text_sha256"], par,
              "G34 expected parent to be next once children are terminal")


def g35_childless_task_still_runnable() -> None:
    """G35: backward-compat — a flat queue (no parent links) is unchanged
    by the new gate. Closes the regression risk for depth-0 runs.
    Mirrors planning_parent_gate.als#ChildlessNewTaskRunnable.
    """
    q = fresh_queue("g35")
    a = json.loads(pm("plan", "--queue", q, "--title", "A", "--text", "a",
                      env_extra={"PM_WORKDIR": ""})
                   .stdout)["task"]["text_sha256"]
    nxt = json.loads(pm("next", "--queue", q,
                        env_extra={"PM_WORKDIR": ""}).stdout)
    assert nxt is not None and nxt["text_sha256"] == a, \
        "G35 childless task must remain runnable"


def g36_bulk_plan_chain_siblings() -> None:
    """G36: bulk-plan --chain-siblings auto-adds depends_on between
    consecutive specs sharing the same parent_slug. Composes with the
    parent-children gate (G33/G34) so a depth-≥1 expansion runs nested
    steps in array order."""
    q = fresh_queue("g36")
    spec = json.dumps([
        {"slug": "par", "title": "P", "text": "parent"},
        {"slug": "k1",  "title": "K1", "text": "first",  "parent_slug": "par"},
        {"slug": "k2",  "title": "K2", "text": "second", "parent_slug": "par"},
        {"slug": "k3",  "title": "K3", "text": "third",  "parent_slug": "par"},
    ])
    pm("bulk-plan", "--queue", q, "--chain-siblings", "--input", "-",
       env_extra={"PM_WORKDIR": ""},
       cwd=None) if False else subprocess.run(
        [PM, "bulk-plan", "--queue", q, "--chain-siblings", "--input", "-"],
        input=spec, text=True, capture_output=True, check=True,
        env={**os.environ, "PM_WORKDIR": ""},
    )

    # k1 has no auto-dep (first sibling); k2 depends on k1; k3 depends on k2.
    k1 = store.find_task_by_slug(q, "k1")
    k2 = store.find_task_by_slug(q, "k2")
    k3 = store.find_task_by_slug(q, "k3")
    k1_record = k1["record_sha256"]
    k2_record = k2["record_sha256"]
    assert_eq((k1.get("links") or {}).get("dependsOn") or [], [],
              "G36 k1 (first sibling) should have no auto-dep")
    assert_eq((k2.get("links") or {}).get("dependsOn"), [k1_record],
              "G36 k2 should depend on k1's record_sha256")
    assert_eq((k3.get("links") or {}).get("dependsOn"), [k2_record],
              "G36 k3 should depend on k2's record_sha256")

    # pm next must return k1 first — composes with parent-gate from G33.
    nxt = json.loads(pm("next", "--queue", q,
                        env_extra={"PM_WORKDIR": ""}).stdout)
    nxt_slug = (nxt.get("attributes") or {}).get("slug")
    assert_eq(nxt_slug, "k1",
              f"G36 expected k1 first under chained-sibling order; got {nxt_slug!r}")


def g37_replan_cascade_down() -> None:
    """G37: pm replan --cascade-down resets every dep-chain descendant
    that's currently terminal back to `new`. In-flight descendants
    (new/working) are skipped. Models the dual of cascade-up; verified
    by system-models/planning_replan.als#R9 and #R11.
    """
    q = fresh_queue("g37")
    # s1 → s2 → s3 chain via depends_on; all driven to done.
    s1 = json.loads(pm("plan", "--queue", q, "--title", "S1", "--text", "x",
                       env_extra={"PM_WORKDIR": ""}).stdout)["task"]["text_sha256"]
    s2 = json.loads(pm("plan", "--queue", q, "--title", "S2", "--text", "x",
                       "--depends-on", s1,
                       env_extra={"PM_WORKDIR": ""}).stdout)["task"]["text_sha256"]
    s3 = json.loads(pm("plan", "--queue", q, "--title", "S3", "--text", "x",
                       "--depends-on", s2,
                       env_extra={"PM_WORKDIR": ""}).stdout)["task"]["text_sha256"]
    for sha in (s1, s2, s3):
        pm("executing", "--task", sha)
        pm("report", "--task", sha, "--title", "ok", "--text", "ok")
        pm("finished", "--task", sha)
    assert_eq(store.status_value(store.latest_status(s2)), "done", "G37 s2 done before replan")
    assert_eq(store.status_value(store.latest_status(s3)), "done", "G37 s3 done before replan")

    # Replan s1 with cascade-down (no cascade-up: s1 has no upstream).
    pm("replan", "--task", s1, "--no-cascade", "--cascade-down")

    # Both descendants must now be `new` again.
    for label, sha in (("s1", s1), ("s2", s2), ("s3", s3)):
        cur = store.status_value(store.latest_status(sha))
        assert_eq(cur, "new",
                  f"G37 {label} should be `new` after cascade-down; got {cur!r}")


def g39_no_cascade_up_alias_still_works() -> None:
    """G39: back-compat — the original `--no-cascade-up` still works as
    an alias for `--no-cascade` after the rename. Anything that pre-
    dates the rename (allowlists, scripts, the worktree under .claude/)
    keeps functioning."""
    q = fresh_queue("g39")
    a = json.loads(pm("plan", "--queue", q, "--title", "A", "--text", "x",
                      env_extra={"PM_WORKDIR": ""}).stdout)["task"]["text_sha256"]
    b = json.loads(pm("plan", "--queue", q, "--title", "B", "--text", "y",
                      "--depends-on", a,
                      env_extra={"PM_WORKDIR": ""}).stdout)["task"]["text_sha256"]
    for sha in (a, b):
        pm("executing", "--task", sha)
        pm("report", "--task", sha, "--title", "ok", "--text", "ok")
        pm("finished", "--task", sha)
    # Use the OLD flag name explicitly.
    pm("replan", "--task", b, "--no-cascade-up")
    assert_eq(store.status_value(store.latest_status(a)), "done",
              "G39 A untouched by --no-cascade-up alias")
    assert_eq(store.status_value(store.latest_status(b)), "new",
              "G39 B reset by --no-cascade-up alias")


def g40_replan_cascade_down_parents() -> None:
    """G40: --cascade-down-parents resets the rollup parent in addition
    to depends_on consumers — closes the StaleRollupWitness hazard from
    planning_replan_with_parent_gate.als#P6. Tree shape: par with
    children k1→k2→k3 chained by deps; all four Done. Replan k2 with
    --no-cascade --cascade-down-parents → k2/k3 reset, par reset, k1
    untouched (P7 scope check)."""
    q = fresh_queue("g40")
    par = json.loads(pm("plan", "--queue", q, "--title", "P", "--text", "rollup",
                        env_extra={"PM_WORKDIR": ""}).stdout)["task"]["text_sha256"]
    k1 = json.loads(pm("plan", "--queue", q, "--title", "K1", "--text", "k1",
                       "--parent", par,
                       env_extra={"PM_WORKDIR": ""}).stdout)["task"]["text_sha256"]
    k2 = json.loads(pm("plan", "--queue", q, "--title", "K2", "--text", "k2",
                       "--parent", par, "--depends-on", k1,
                       env_extra={"PM_WORKDIR": ""}).stdout)["task"]["text_sha256"]
    k3 = json.loads(pm("plan", "--queue", q, "--title", "K3", "--text", "k3",
                       "--parent", par, "--depends-on", k2,
                       env_extra={"PM_WORKDIR": ""}).stdout)["task"]["text_sha256"]
    # Drive all four to done. Parent gate forces k1→k2→k3→par order.
    for sha in (k1, k2, k3, par):
        pm("executing", "--task", sha)
        pm("report", "--task", sha, "--title", "ok", "--text", "ok")
        pm("finished", "--task", sha)

    pm("replan", "--task", k2, "--no-cascade", "--cascade-down-parents")

    assert_eq(store.status_value(store.latest_status(par)), "new",
              "G40 par rollup must reset (P6)")
    assert_eq(store.status_value(store.latest_status(k1)), "done",
              "G40 k1 (sibling not depending on k2) must be untouched (P7)")
    assert_eq(store.status_value(store.latest_status(k2)), "new",
              "G40 k2 (target) reset")
    assert_eq(store.status_value(store.latest_status(k3)), "new",
              "G40 k3 (deps-descendant of k2) reset")


def g47_finished_enforces_full_sticky_chain() -> None:
    """G47: pm finished must use the full check_sticky_eligibility chain
    walk (not just the task's own latest context_id). Setup: a sticky
    parent claimed by ctx1, a NON-STICKY child planned under it that
    inherits no own binding. The previous own-binding-only check missed
    the parent's binding and let any agent close the child. With the
    full check, ctx2 is refused (StickyContextMismatch on
    'ancestor/dep'), exit 10."""
    q = fresh_queue("g47")
    ctx1 = pm("context-id").stdout.strip()
    ctx2 = pm("context-id").stdout.strip()
    env1 = {"PM_CONTEXT_ID": ctx1, "PM_WORKDIR": ""}
    env2 = {"PM_CONTEXT_ID": ctx2, "PM_WORKDIR": ""}
    par = json.loads(pm("plan", "--queue", q, "--title", "P",
                        "--text", "sticky parent", "--sticky",
                        env_extra=env1).stdout)["task"]["text_sha256"]
    pm("executing", "--task", par, env_extra=env1)
    # Child: NOT explicitly sticky in the spec, but inherits from parent
    # via plan.py's sticky inheritance. Both bound to ctx1.
    kid = json.loads(pm("plan", "--queue", q, "--title", "K",
                        "--text", "child", "--parent", par,
                        env_extra=env1).stdout)["task"]["text_sha256"]
    pm("executing", "--task", kid, env_extra=env1)
    pm("report", "--task", kid, "--title", "ok", "--text", "ok",
       env_extra=env1)

    # ctx2 attempt to finish the child — must be refused.
    p = pm("finished", "--task", kid, env_extra=env2, check=False)
    assert_eq(p.returncode, 10,
              f"G47 ctx2 finished must be refused (parent-bound to ctx1); "
              f"got {p.returncode}")


def g48_heartbeat_enforces_sticky_chain() -> None:
    """G48: pm heartbeat must enforce sticky-context (was missing
    entirely). A worker holding a sticky claim under ctx1 — heartbeat
    from ctx2 must be refused exit 10."""
    q = fresh_queue("g48")
    ctx1 = pm("context-id").stdout.strip()
    ctx2 = pm("context-id").stdout.strip()
    env1 = {"PM_CONTEXT_ID": ctx1, "PM_WORKDIR": ""}
    env2 = {"PM_CONTEXT_ID": ctx2, "PM_WORKDIR": ""}
    sha = json.loads(pm("plan", "--queue", q, "--title", "T",
                        "--text", "sticky", "--sticky",
                        env_extra=env1).stdout)["task"]["text_sha256"]
    pm("executing", "--task", sha, env_extra=env1)

    # Heartbeat from same context: ok.
    pm("heartbeat", "--task", sha, env_extra=env1)
    # Heartbeat from different context: must refuse exit 10.
    p = pm("heartbeat", "--task", sha, env_extra=env2, check=False)
    assert_eq(p.returncode, 10,
              f"G48 ctx2 heartbeat on ctx1-bound task must refuse; "
              f"got {p.returncode}")


def g46_sticky_rebinding_after_reclaim() -> None:
    """G46: a sticky task claimed by ctx1, reclaimed, then claimed by
    ctx2 (different) — the binding REBINDS to ctx2; subsequent
    operations from ctx1 are refused with exit 10. Verifies the
    runtime-level realization of
    planning_sticky_rebinding.als#RebindWitness (claim → reclaim →
    rebind 4-state trace) and SR1 (reclaim clears binding)."""
    q = fresh_queue("g46s")
    ctx1 = pm("context-id").stdout.strip()
    ctx2 = pm("context-id").stdout.strip()
    assert ctx1 != ctx2, "G46 needs two distinct contexts"

    # Plan + claim with ctx1.
    sticky_env1 = {"PM_CONTEXT_ID": ctx1, "PM_WORKDIR": ""}
    sha = json.loads(pm("plan", "--queue", q, "--title", "T",
                        "--text", "sticky", "--sticky",
                        env_extra=sticky_env1).stdout)["task"]["text_sha256"]
    pm("executing", "--task", sha, env_extra=sticky_env1)
    bound = store.status_context_id(store.latest_status(sha))
    assert_eq(bound, ctx1, "G46 ctx1 binds on first claim")

    # Reclaim. Latest status now has no context_id (binding cleared).
    pm("reclaim", "--task", sha, "--reason", "test rebind")
    cleared = store.status_context_id(store.latest_status(sha))
    assert cleared is None, \
        f"G46 SR1: reclaim must clear binding; got {cleared!r}"

    # Re-claim with ctx2. Must succeed and bind to ctx2.
    sticky_env2 = {"PM_CONTEXT_ID": ctx2, "PM_WORKDIR": ""}
    pm("executing", "--task", sha, env_extra=sticky_env2)
    rebound = store.status_context_id(store.latest_status(sha))
    assert_eq(rebound, ctx2, "G46 SR2/SR4: rebind to ctx2")

    # ctx1 (old context) trying to operate on the now-rebound task →
    # exit 10 (StickyContextMismatch).
    p = pm("report", "--task", sha, "--title", "x", "--text", "y",
           env_extra=sticky_env1, check=False)
    assert_eq(p.returncode, 10,
              f"G46 ctx1 must be refused after rebind; got {p.returncode}")


def g45_self_parent_refused() -> None:
    """G45: pm plan --parent <self-sha> exits 11 (NoSelfParent). The
    deterministic sha of `task:<queue>/<slug>` is computable by a
    caller, so the refusal must be at the gate — not relied on as a
    structural impossibility. Closes the long-standing modeling-debt
    item flagged in two prior reconciliations:
    planning_parent_gate.als#NoSelfParent is now enforced, not just
    assumed."""
    q = fresh_queue("g45")
    # Compute the prospective sha for slug "x" in this queue and try to
    # use it as --parent of the same plan call.
    own_sha = store.sha256_text(store.task_identity_text(q, "x"))
    p = pm("plan", "--queue", q, "--title", "X", "--text", "self",
           "--slug", "x", "--parent", own_sha,
           env_extra={"PM_WORKDIR": ""}, check=False)
    assert_eq(p.returncode, 11,
              f"G45 expected exit 11 on self-parent; got {p.returncode}")


def g44_superseded_child_is_terminal_for_parent_gate() -> None:
    """G44: parent-gate treats a `superseded` child as terminal — the
    parent becomes runnable even when its only child was replanned with
    body adjustment (which marks the original superseded). Closes the
    coverage gap on
    planning_parent_gate.als#SupersededChildIsTerminalForGate.
    """
    q = fresh_queue("g44")
    par = json.loads(pm("plan", "--queue", q, "--title", "P", "--text", "parent",
                        env_extra={"PM_WORKDIR": ""}).stdout)["task"]["text_sha256"]
    kid = json.loads(pm("plan", "--queue", q, "--title", "K", "--text", "kid",
                        "--parent", par,
                        env_extra={"PM_WORKDIR": ""}).stdout)["task"]["text_sha256"]
    # Drive kid to done so we can replan-with-edit (which supersedes it).
    pm("executing", "--task", kid)
    pm("report", "--task", kid, "--title", "ok", "--text", "ok")
    pm("finished", "--task", kid)
    # Replan with adjusted body → kid becomes `superseded`, a kid-rN clone
    # is created (with the same parent, so par now has two children).
    pm("replan", "--task", kid, "--no-cascade", "--text", "v2")
    assert_eq(store.status_value(store.latest_status(kid)), "superseded",
              "G44 sanity: original kid is superseded after replan-with-text")

    # The clone (kid-r1) is `new` — par should NOT be runnable yet,
    # because the clone is a non-terminal child.
    nxt = json.loads(pm("next", "--queue", q,
                        env_extra={"PM_WORKDIR": ""}).stdout)
    nxt_slug = (nxt.get("attributes") or {}).get("slug") if nxt else None
    assert nxt_slug != "p", \
        f"G44 par must NOT be runnable while kid-r1 is `new`; got {nxt_slug!r}"

    # Drive the clone to done.
    clone = store.find_task_by_slug(q, "k-r1")
    clone_sha = clone["text_sha256"]
    pm("executing", "--task", clone_sha)
    pm("report", "--task", clone_sha, "--title", "ok", "--text", "ok")
    pm("finished", "--task", clone_sha)

    # Now both children are terminal (one superseded, one done) — par
    # must be the next runnable task.
    nxt2 = json.loads(pm("next", "--queue", q,
                         env_extra={"PM_WORKDIR": ""}).stdout)
    assert nxt2 is not None and nxt2["text_sha256"] == par, \
        "G44 par must be runnable once {superseded kid + done clone} both terminal"


def g41_cascade_down_parents_two_level_chain() -> None:
    """G41: --cascade-down-parents walks the parentTask chain
    transitively. Tree: gp → p → kid (two-level rollup). Replan kid
    with --no-cascade --cascade-down-parents must reset kid, p, AND
    gp. Also covers de-duplication (gp would be visited via every
    path) and the rollup_parents output shape."""
    q = fresh_queue("g41")
    gp = json.loads(pm("plan", "--queue", q, "--title", "GP", "--text", "grandparent",
                       env_extra={"PM_WORKDIR": ""}).stdout)["task"]["text_sha256"]
    p = json.loads(pm("plan", "--queue", q, "--title", "P", "--text", "parent",
                      "--parent", gp,
                      env_extra={"PM_WORKDIR": ""}).stdout)["task"]["text_sha256"]
    kid = json.loads(pm("plan", "--queue", q, "--title", "K", "--text", "child",
                        "--parent", p,
                        env_extra={"PM_WORKDIR": ""}).stdout)["task"]["text_sha256"]
    # Drive bottom-up (parent gate enforces this anyway).
    for sha in (kid, p, gp):
        pm("executing", "--task", sha)
        pm("report", "--task", sha, "--title", "ok", "--text", "ok")
        pm("finished", "--task", sha)

    out = json.loads(pm("replan", "--task", kid,
                        "--no-cascade", "--cascade-down-parents").stdout)
    rollups = out.get("rollup_parents") or []
    rollup_shas = {r["task"] for r in rollups}
    assert rollup_shas == {p, gp}, \
        f"G41 expected rollup_parents = {{p, gp}}, got {rollup_shas}"
    for sha, label in ((kid, "kid"), (p, "p"), (gp, "gp")):
        assert_eq(store.status_value(store.latest_status(sha)), "new",
                  f"G41 {label} should be `new` after two-level cascade")


def g42_cascade_down_parents_no_parent_safe() -> None:
    """G42: --cascade-down-parents on a top-level task (no parent) is
    safe — no crash, empty rollup_parents list. Edge case the model
    handles trivially via empty set semantics."""
    q = fresh_queue("g42")
    a = json.loads(pm("plan", "--queue", q, "--title", "A", "--text", "x",
                      env_extra={"PM_WORKDIR": ""}).stdout)["task"]["text_sha256"]
    pm("executing", "--task", a)
    pm("report", "--task", a, "--title", "ok", "--text", "ok")
    pm("finished", "--task", a)

    out = json.loads(pm("replan", "--task", a,
                        "--no-cascade", "--cascade-down-parents").stdout)
    assert_eq(out.get("rollup_parents") or [], [],
              "G42 expected empty rollup_parents on top-level task")
    assert_eq(store.status_value(store.latest_status(a)), "new",
              "G42 target itself still resets")


def g43_cascade_down_parents_skips_inflight_parent() -> None:
    """G43: an in-flight rollup parent (currently `working` or `new`)
    is skipped — same policy as cascade-down for descendants. Avoids
    yanking a status out from under an active worker."""
    q = fresh_queue("g43")
    par = json.loads(pm("plan", "--queue", q, "--title", "P", "--text", "parent",
                        env_extra={"PM_WORKDIR": ""}).stdout)["task"]["text_sha256"]
    kid = json.loads(pm("plan", "--queue", q, "--title", "K", "--text", "kid",
                        "--parent", par,
                        env_extra={"PM_WORKDIR": ""}).stdout)["task"]["text_sha256"]
    # Drive kid to done; parent stays `new` (never claimed).
    pm("executing", "--task", kid)
    pm("report", "--task", kid, "--title", "ok", "--text", "ok")
    pm("finished", "--task", kid)
    assert_eq(store.status_value(store.latest_status(par)), "new",
              "G43 sanity: parent stays new")

    out = json.loads(pm("replan", "--task", kid,
                        "--no-cascade", "--cascade-down-parents").stdout)
    rollups = out.get("rollup_parents") or []
    assert len(rollups) == 1, f"G43 expected 1 rollup entry, got {rollups!r}"
    assert rollups[0].get("skipped") is True, \
        f"G43 expected the in-flight (`new`) parent skipped; got {rollups[0]!r}"
    assert_eq(rollups[0].get("current"), "new", "G43 skipped parent current=new")


def g38_replan_cascade_down_skips_inflight() -> None:
    """G38: cascade-down skips descendants that are currently `new` or
    `working` — they'll naturally observe the target's state when their
    own dep gate is checked. Verified by planning_replan.als#R10.
    """
    q = fresh_queue("g38")
    s1 = json.loads(pm("plan", "--queue", q, "--title", "S1", "--text", "x",
                       env_extra={"PM_WORKDIR": ""}).stdout)["task"]["text_sha256"]
    s2 = json.loads(pm("plan", "--queue", q, "--title", "S2", "--text", "x",
                       "--depends-on", s1,
                       env_extra={"PM_WORKDIR": ""}).stdout)["task"]["text_sha256"]
    pm("executing", "--task", s1)
    pm("report", "--task", s1, "--title", "ok", "--text", "ok")
    pm("finished", "--task", s1)
    # s2 stays `new` — never claimed.

    out = json.loads(pm("replan", "--task", s1,
                        "--no-cascade", "--cascade-down").stdout)
    descs = out.get("descendants") or []
    assert len(descs) == 1, f"G38 expected 1 descendant entry, got {descs!r}"
    assert descs[0].get("skipped") is True, \
        f"G38 expected the in-flight (`new`) descendant to be skipped; got {descs[0]!r}"
    assert_eq(descs[0].get("current"), "new", "G38 skipped descendant current=new")


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
    "G22": g22_zombie_heartbeat_after_reclaim_refused,
    "G23": g23_cancel_superseded_refused,
    "G24": g24_supersede_clone_inherits_deps_and_carries_replan_of,
    "G25": g25_replan_skip_on_non_terminal_target,
    "G26": g26_cascade_preserves_done_descendants,
    "G27": g27_cascade_three_deep_transitive,
    "G28": g28_cascade_isolates_non_descendant_sibling,
    "G29": g29_reclaim_cascade_skips_non_working_descendants,
    "G30": g30_reclaim_cascade_three_deep_transitive,
    "G31": g31_reclaim_cascade_isolates_sibling,
    "G32": g32_reclaim_refuses_non_working_root,
    "G33": g33_parent_gated_by_pending_children,
    "G34": g34_parent_unblocks_after_children_settle,
    "G35": g35_childless_task_still_runnable,
    "G36": g36_bulk_plan_chain_siblings,
    "G37": g37_replan_cascade_down,
    "G38": g38_replan_cascade_down_skips_inflight,
    "G39": g39_no_cascade_up_alias_still_works,
    "G40": g40_replan_cascade_down_parents,
    "G41": g41_cascade_down_parents_two_level_chain,
    "G42": g42_cascade_down_parents_no_parent_safe,
    "G43": g43_cascade_down_parents_skips_inflight_parent,
    "G44": g44_superseded_child_is_terminal_for_parent_gate,
    "G45": g45_self_parent_refused,
    "G46s": g46_sticky_rebinding_after_reclaim,
    "G47": g47_finished_enforces_full_sticky_chain,
    "G48": g48_heartbeat_enforces_sticky_chain,
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

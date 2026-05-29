---
name: pm-audit
description: >
  Cryptographic integrity attestation of a planning queue or the whole
  pm instance via hashharness `verify_work_package`. Re-checks every
  record's content hash, chain links, and schema binding across the
  queue's Task records plus each per-task TaskStatus / TaskReport /
  TaskHeartbeat chain — catching orphan records, tampered payloads,
  and `done` slipped in without proof. Unlike `verify_chain` (which
  followed a single named root), this covers every record in scope,
  closing the gap that pm's per-task chains have no common root. Use
  as a supervisor sweep before trusting a batch of agent output, or
  as a periodic integrity check.
---

# pm:audit — attest a queue (or the whole board) is intact

## When to use

- **Before trusting a batch of agent output** — supervisor wants to
  prove the audit trail of work hasn't been tampered with.
- **Periodic integrity check** — run after a long worker session, or
  on a cron, to catch silent corruption early.
- **After a backend incident** — disk-full crash, partial-write recovery,
  or moving the sqlite file between machines.
- **Investigating "this looks wrong"** — diff a suspicious queue's
  chain against expectations; an audit failure points right at the
  affected work package.

Not for "did this task succeed semantically" — that's the verifier on
`pm finished`. Audit is purely about chain integrity.

## Procedure

`../scripts/pm audit [--queue Q] [--json] [--verbose] [--chunk N]`

- **Without `--queue`** — scopes to the whole instance via
  `list_work_packages(prefix="planning:")`. On a seasoned store this
  can be thousands of work packages, so the call is automatically
  chunked (`--chunk` defaults to 50, tune up for fast networks or
  down for slow ones).
- **With `--queue Q`** — scopes to one queue: the queue work package
  itself (`planning:Q`) plus every per-task chain
  (`planning:task:<sha>`).
- **`--verbose`** — asks the backend for full per-item errors, not
  just error counts. Useful when a failure surfaces and you need to
  know which record specifically.
- **`--json`** — emit the `verify_work_package` payload verbatim.
  Default is a human-readable summary.

## Output (human form)

```
pm audit: queue=Q
  work_packages_checked = 9
  records_checked       = 41
  result                = OK
```

On failure:

```
pm audit: queue=Q
  work_packages_checked = 9
  records_checked       = 41
  result                = FAIL
  failed work packages (1):
    - planning:task:abc123...  errors=2  checked=5
```

With `--verbose`, up to 5 per-record errors are surfaced under each
failed work package.

## Output (--json)

```json
{
  "ok": true,
  "checked_work_packages": 9,
  "results": {
    "planning:Q":                          {"ok": true, "checked_items": 8, "errors_count": 0, ...},
    "planning:task:<sha>":                 {"ok": true, "checked_items": 5, "errors_count": 0, ...},
    ...
  }
}
```

Each `results[wp]` carries at minimum `ok`, `checked_items`,
`errors_count`; under `--verbose` it also carries `errors: [...]` with
per-record diagnostics.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Every work package in scope verified clean |
| 1 | At least one work package reported errors (see `results` for which) |
| 2 | Backend unreachable / RPC error (propagated from `mcp_client`) |

## What's actually verified

For each record in scope hashharness re-checks:

- the record's content hash (`text_sha256`) matches its payload,
- the chain links (`prevStatus` / `prevReport` / `prevHeartbeat` /
  `parentTask` / `spawnedAt` / `proof` / `dependsOn`) resolve to
  records that exist in the same instance,
- the record's `chain_predecessor` history is consistent (no
  bifurcations the head-CAS would have rejected if it had been
  honoured at write time),
- the record validates against its bound schema version.

Records reachable only through `task` links (not via the chain head)
are checked too — verify_work_package walks every row keyed by
`work_package_id`, not just the head's reachable set. That's the
specific gap `verify_chain` couldn't cover for pm's topology.

## Cost

`verify_work_package` is sequential server-side. On the seasoned
4779-work-package, 18555-record instance the whole-instance audit
takes ~30s wall (with default `--chunk 50`). One queue is usually
sub-second.

## Notes

- Audit is read-only and idempotent. Two audits running concurrently
  don't interfere.
- An empty queue (`--queue does-not-exist`) returns OK with
  `records_checked = 0` — there's nothing to attest, which is itself
  a clean answer.
- Pair with `pm sweep` (claim recovery) and a future `pm prune`
  (terminal-task archival) — audit reports the state, sweep heals
  liveness, prune keeps the working set small.

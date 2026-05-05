# Proposal: bulk-tips endpoint for hashharness MCP

> **Audience:** maintainer of [`hashharness`](https://github.com/in8finity/hashharness).
> **From:** observed performance ceiling in a real consumer (`hashharness-pm`).
> **TL;DR:** adding `find_tips_bulk(work_package_ids, type)` would let consumers fetch the tip of N independent chains in one MCP call instead of N. Single index-aided server query, single round trip; today the same workload costs N round trips. ~30 lines server-side.

## What hurt

`hashharness-pm`'s dashboard renders the planning board: queue tree, current status of every task, owner, sticky binding. It's a long-lived view; users keep it open in a browser tab with auto-refresh.

To compute "current status of every task" today the dashboard does:

```
1. find_items({type: Task, limit: 10000})           # 1 round trip, returns all Tasks
2. for each task:
     find_tip({work_package_id: task_wp(sha),       # N round trips
                type: TaskStatus})
```

That's **N+1 round trips per render**, where N is the number of tasks in the system. For a queue with 100 tasks running on localhost MCP, observed render time is **5–8 seconds** — every click and every auto-refresh.

The DB query inside each `find_tip` is fast: hashharness has the right index on `(work_package_id, type, created_at DESC LIMIT 1)`. The latency is **per-call HTTP/JSON-RPC overhead**, not the query.

## Workaround in the consumer (what `pm dashboard` does today)

After this proposal landed in our local code, the dashboard switched to:

```
1. find_items({type: Task,        limit: 10000})    # 1 round trip
2. find_items({type: TaskStatus,  limit: 100000})   # 1 round trip
3. group all TaskStatuses by work_package_id, pick latest by created_at, in client memory
```

Two round trips total, regardless of N. **But** step 2 transfers *every historical TaskStatus* in the system over the wire, including superseded entries that never appear on the dashboard. For a long-lived queue with hundreds of replans / claim races, that's tens of MB of redundant data per render.

The right fix is server-side.

## Proposed endpoint

```python
mcp_client.tool("find_tips_bulk", {
    "work_package_ids": ["wp:task:abc...", "wp:task:def...", ...],
    "type": "TaskStatus",
    "fields": ["text_sha256", "record_sha256", "attributes", "created_at"],  # optional projection
})
# →  {
#      "wp:task:abc...": {<tip-item>},
#      "wp:task:def...": {<tip-item>},
#      "wp:task:ghi...": null,                  # no tip yet (genesis-less)
#    }
```

Semantics:
- For each `work_package_id` in the input, return the tip of `(work_package_id, type)` — same record `find_tip` would have returned.
- Missing chain → `null` (don't error).
- `fields` projection is the same shape `find_items` already accepts.
- Order of result keys not guaranteed; consumers index by id.

Server implementation:
- One SQL query with `WHERE work_package_id IN (...) AND type = ...` + per-group `ORDER BY created_at DESC LIMIT 1`. Postgres-ish:

  ```sql
  SELECT DISTINCT ON (work_package_id) *
    FROM items
   WHERE work_package_id = ANY($1)
     AND type = $2
   ORDER BY work_package_id, created_at DESC;
  ```

- The existing index on `(work_package_id, type, created_at)` covers the query without scan.

## Cost

- Server: ~30 LoC (route + handler + SQL). The query optimisation is a free side effect of the existing index.
- Client (in `hashharness-pm`): ~10 LoC change in `dashboard.py:fetch_state` to switch from "bulk-fetch all statuses + group in memory" to "bulk-fetch tips by id." Same control flow, smaller payload.
- Compatibility: pure addition. Existing `find_tip` stays.

## Caps + safety

- Cap `len(work_package_ids)` at e.g. 10000 per call (return 400 above that). Same shape as `find_items`'s `limit`.
- The query uses `IN (...)` which is bounded by Postgres parameter limits; if hashharness ever needs >32000 ids, page server-side.

## Why this fits hashharness specifically

1. The append-only chain shape is exactly what makes per-tip lookup the natural primitive — you almost always want "what's the latest of this chain", and you almost always want it for many chains at once (dashboards, batch sweeps, `pm next`).
2. Existing `find_tip` is the singular form of this — bulk-tips is the obvious plural. Naming + semantics inherit cleanly.
3. Storage layer doesn't need new indexes; the existing `(wp, type, created_at)` index is already optimal.

## What this still won't fix

- **Slow `find_items` over very large item sets.** If a single queue has millions of TaskHeartbeats, bulk-fetching them is still the wrong shape; you'd want pagination + filtering by `created_at >`. Out of scope here.
- **`get_work_package` per-chain detail views.** Per-task drilldown still wants the full chain history; bulk-tips is for summary views only.
- **Network loss.** The bulk shape is more sensitive to single-request failure: lose one packet, lose all N tips. Consumers should still implement retry.

## Recommendation

Add `find_tips_bulk` as a sibling of `find_tip`. Same semantics, plural input + dict output. Costs ~30 LoC server, unlocks O(1) round-trip dashboard rendering for the broad class of consumers that need "current state of many chains at once."

I'm happy to PR the consumer-side switch in `hashharness-pm` once the endpoint lands, so the API can be validated against a real workload.

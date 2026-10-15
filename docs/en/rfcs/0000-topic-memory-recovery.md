---
title: Topic Memory blocked processing recovery
---

- Proposal Name: `topic_memory_blocked_processing_recovery`
- Start Date: 2026-10-15
- RFC PR: [oceanbase/powercontext#0000](https://github.com/oceanbase/powercontext/pull/0000)
- Tracking Issue: [#1885](https://github.com/oceanbase/powercontext/issues/1885)
- Implementation PR: [#1966](https://github.com/oceanbase/powercontext/pull/1966)
- Amends: [RFC 1417](1417_topic_memory.md), [RFC 1515](1515_artifact_processing_supervisor.md)
- Related: [RFC 1549](1549_artifact_family_unification.md), [RFC 1771](1771-unified-database-migrations.md)

# Summary

Provide Scope-authorized inspection and explicit, bounded retry of blocked Topic Memory processing. Reuse the existing
work budget for the current attempt, processing intents for scheduling, and leases for fencing. Add one Topic-owned
recovery table containing a snapshot only when a frontier becomes blocked; store its retry acceptance in the same row.
Successful and superseded attempts do not become permanent execution history. Retain block snapshots and accepted
receipts without automatic expiry so a duplicate request cannot grant another allowance after restart or cleanup.

# Motivation

A failed Topic window can exhaust its allowance while later Sources remain behind the same Cursor. Stopping repeated
dispatch prevents wasted work, but operators also need to diagnose and recover that frontier without editing the
database. Recovery must preserve evidence and make the extra model cost an explicit administrative decision.

The necessary durable evidence is the blocked state and whether an administrator already authorized another allowance.
That requirement does not by itself justify retaining every attempted window, including ordinary successful work.
This proposal decides the storage lifecycle, ownership, retention, and reuse boundary before implementation.

# Guide-level explanation

An operator reads processing status for a Scope. The response reports the Cursor, Source head, retained target, and a
block when processing cannot continue. An active final attempt remains pending because it may still publish.

The operator corrects the cause, then submits the observed `block_id` to retry. Acceptance means one fresh bounded
allowance was granted and processing was requested; it does not promise generation success. Sources, Cursor, and target
remain intact. Each allowance keeps the existing ceilings of three attempts, 512 requests, and 64,000,000 reserved tokens.

If the response is lost, resending the same ID returns the original acceptance. The same applies after restart,
subsequent processing, another block, or disabling processing in an offline configuration change. An unretried block
that is no longer current conflicts. Ordinary flushes, reads, restarts, and model changes never grant new allowance.

Inspection requires `scope.read`; retry, including replay, requires `scope.admin`. Records contain controlled metadata,
not Source bodies, prompts, model output, credentials, or arbitrary exception messages.

# Reference-level explanation

## Ownership and storage reuse

The source inventory is `master` at `a1ed8553149ced3e15756642905a8ea315a3a4a8`.

| Existing storage | Lifecycle and reuse decision |
| --- | --- |
| `pc_topic_memory_work_budgets` | Authoritative allowance at a Cursor; updated during work and deleted on success or accepted retry. Extend it with current-attempt diagnostics, but do not retain old budgets as history. |
| Processing pending/intents | Mutable scheduling state and generation counters. Reuse them to request work in the acceptance transaction; they cannot identify an arbitrary historical block's acceptance after later requests. |
| Processing leases | Replaced or renewed operational ownership. Reuse them to validate publication rights; they cannot supply immutable historical diagnosis or receipts. |
| Artifact revisions and heads | Can participate in the same transaction. Reuse is feasible, but requires an additional internal Family and lifecycle restrictions described below. |

`ArtifactRepository.create` accepts a caller-owned connection, so atomicity is not a reason to rule out Artifact storage.
An Artifact design could use `block_id` as its identity and revision/head CAS to record acceptance with budget reset and
intent creation. It must also register a typed content model; prevent generic writes, publication, merge, and lifecycle
operations from changing the recovery record; enforce Scope inspection/admin permissions; and retain every exact
revision needed for replay through cleanup and backup. Reusing a publicly writable Family or deleting its head must
not erase authorization evidence.

This RFC chooses a small operational record instead: a block has no user-authored content, independent publication,
retrieval projection, or merge lifecycle. Extending the Artifact capability boundary to represent this one-way state
transition costs more semantics than it reuses. This is a lifecycle decision, not a claim that Artifact transactions or
access control are incapable of supporting it.

Topic Memory owns Source ranges, terminality, allowances, and request/token ceilings. The Supervisor owns execution,
fencing, and controlled Worker failure classification; a bounded metadata callback lets the domain retain a stopped
Worker's diagnosis without granting allowance. No shared execution-history table or generic job framework is introduced.
A future shared record needs a second concrete workflow with the same identity, retry, and retention contract.

## Mutable current attempt

Extend the existing budget row with nullable metadata for the latest admitted attempt: selected Source end, start and
finish observations, execution holder/group/generation, and controlled last error. Keep the existing `attempt_id` and
aggregate attempt/request/token counters authoritative. Initialize the current metadata in the same transaction that
admits an attempt; update it only when the attempt identity still matches. A later attempt supersedes that metadata.
Success deletes it with the budget in the atomic publication/Cursor transaction.

A stopped Worker's controlled failure must settle the matching current attempt before its terminal snapshot is frozen,
in the same transaction when both occur together. Late callbacks must not rewrite a replacement attempt or immutable
block evidence. If a crash leaves the category genuinely unknown, retain null instead of inferring a timeout.

The budget's existing `source_through` remains cumulative coverage. It must never substitute for the selected end of
the last attempt. For example, selected ends `2, 1, 1` mean the final attempted range is `(0, 1]`, while cumulative
coverage can remain `2` and the retained target `3`.

## Durable block and receipt

Add `pc_topic_memory_work_blocks` with one row per distinct terminal block, not per attempt:

| Field group | Required evidence |
| --- | --- |
| Identity | Server-generated `block_id`; `scope_id`, `binding_name`, Cursor `source_after`, and final `attempt_id`. |
| Range and target | Nullable exact selected `source_through`; separate `budget_source_through` cumulative coverage; `target_source_through` captured from the retained target at block observation. |
| Diagnosis | Reserved `attempts`, `requests`, `tokens`; terminal `error_code`; nullable controlled `last_error_code`; first `blocked_at` observation. |
| Acceptance | Initially null `retried_at` and `retry_request_generation`; set together exactly once on acceptance. |

Use `block_id` as the primary key and a unique key on `(scope_id, binding_name, source_after, attempt_id)` to make repeated
observation converge on one identity. Constraints validate ranges and nonnegative usage without requiring a fabricated
selected end for legacy data. Block evidence is immutable after creation; only the acceptance pair transitions once.
The same row represents both the diagnosed block and the durable receipt, so receipt persistence needs no second table.

Observe current Cursor, budget, and target under the publication-compatible lock order. Only the dispatcher that reaped
that exact Worker, or the failed Worker itself, can attest that it stopped. Other callers check current lease/fence
ownership before finalizing an unfinished attempt; use locking current reads on MySQL, not a stale repeatable-read
snapshot. A live final attempt stays pending. An obsolete owner cannot regain publication rights by racing block
observation or retry.

When observation needs both locks, acquire lease before Source head, then reread Cursor/budget under the head lock. If
the attempt changed meanwhile, leave it pending and retry observation later; do not acquire a replacement lease lock
under the head lock. Retry consumes only a finalized block whose attempt still matches, so it does not reverse that
lock order to rediscover a live owner.

The first observation can persist a metadata snapshot, including from a status read; it grants no allowance and requests
no work. Snapshot creation never materializes Source content or calls a model. Its target is an observation-time fact,
not an invented target at an unknown historical failure time.

## Retry transaction and identity

After authorization, serialize on the Scope's existing Source-head write lock and load the exact Scope/binding/block:

1. If acceptance already exists, return it before checking today's budget or processing configuration. Make no writes.
2. Otherwise require the same authoritative Cursor and final attempt, an exhausted budget, available processing, and
   no live attempt that can publish. An obsolete unretried block conflicts; an unknown or cross-Scope ID is not found.
3. In one transaction, remove that exact exhausted budget, request pending work and an intent using existing repositories,
   and set the block's acceptance time and accepted request generation. Preserve Sources, Cursor, and retained target.

Rollback restores all of these changes. A crash after commit but before response is covered by step 1. Concurrent callers
for the same block receive one acceptance; a later block needs its own observed ID. A caller-supplied unknown ID never
creates a block or grants allowance. Retry does not refund ambiguous or failed work in an existing allowance.

## Public contract

Add HTTP operations for processing status, exact block read, and retry, with corresponding Python Client SDK and CLI
operations. Proposed HTTP paths are `/v1/topic-memory/processing/status`, `/v1/topic-memory/processing/blocks/get`, and
`/v1/topic-memory/processing/retry`, all using POST. The MCP tool catalog is unchanged.

Status exposes current Cursor/head/target and `idle`, `pending`, or `blocked`. Block read returns the stable snapshot and
acceptance; retry returns that same accepted record. Use 409 for a stale unretried block and 404 for an unknown or
cross-Scope block after authorization. Replaying an accepted retry must work even if processing is now disabled.

`source_through` is nullable when the exact selected end is unknown; `budget_source_through` always names the recorded
cumulative coverage. Do not label cumulative legacy coverage as an exact attempted range. OpenAPI owns the precise wire
schema; regenerate HTTP models, SDK/host operation metadata, and desktop types together in the implementation.

## Retention, cleanup, and storage growth

Retain every block snapshot and acceptance indefinitely within the retained database. No TTL, per-attempt history,
successful-attempt history, bulk purge, or automatic receipt compaction is introduced. Unaccepted blocks also remain
readable after becoming stale. A backup/restore must preserve budgets, blocks/receipts, Cursor, targets, and processing
state consistently. Partial restore or record deletion is not a supported recovery operation.

This retention is deliberately unbounded by time: its cost grows with distinct blocks, including those after repeated
administrative retries. It does not grow with every successful window. Future cleanup requires an explicit contract for
exact historical reads and receipt replay; silently deleting an acceptance would violate this RFC even if an unknown ID
is subsequently rejected. No cleanup operation may turn an old ID into authorization for another allowance.

Let `W` be processed windows, `A` mean admitted attempts per window, and `B` distinct blocks, including blocks after an
accepted retry. Full attempt history retains roughly `W × A` rows; this design retains `B` rows plus current budget rows.
Reads and mutations address an exact key; no normal observation, acceptance, or completion scans or rewrites history.

A synthetic SQLite 3.50.4 sizing run used 100,000 rows per layout, 4 KiB pages, 1,000 32-character Scope IDs, random UUIDs,
and each layout's indexes: primary/unique keys plus the existing frontier index for attempt history. Net database growth
excluding empty-schema pages was 57,536,512 bytes for successful
attempt metadata using the table definition at implementation commit `4cdf119b`, and 46,235,648 bytes for the 16-field
block/accepted-receipt layout above: about 575 and 462 bytes per row. This measures isolated schema storage, not the
runtime, model execution, write latency, OceanBase, or seekDB; active budget columns, WAL, backups, and other tables are
excluded. Final schemas and backend indexes require their own sizing.

As an illustrative workload, not a production observation, one million windows/day, `A = 1.1`, and 10,000 blocks/day
produce 1.1 million history rows versus 10,000 retained block rows per day. Applying those SQLite sample widths gives
about 633 MB versus 4.6 MB/day of additional retained records. Successful-attempt history is therefore an explicit
storage commitment even though a single allowance admits at most three attempts; the per-allowance ceiling is not a
historical retention bound. No backend performance improvement or acceptable deployment capacity is claimed.

## Migration and compatibility

The implementation must deliver explicit, versioned offline migrations under RFC 1771 for budget columns, the recovery
table, constraints, and indexes on every supported backend. Register affected objects, recognized baseline shapes,
verification, and backup coverage. Normal startup must not silently alter an existing database. Empty database
initialization follows the same supported revision chain. This RFC adds no migration code or runtime behavior itself.

Preserve existing budgets, counters, Sources, Cursor, targets, and intents. Existing rows have null current-attempt
metadata; stopped-writer migration establishes that no pre-upgrade Worker can publish. On first terminal observation,
keep recorded cumulative coverage, report the exact selected end and unavailable error/owner/time facts as unknown,
and use observation time for `blocked_at`. Never reconstruct a successful history or guess a model/Worker timeout from
`processing_failed`. Preserve exact block IDs and accepted receipt identities if a recognized existing format is
converted. Reject unsupported development schemas before writes rather than silently replacing their identities.

Mixed old/new writers and destructive downgrade after accepting retries are unsupported. Any rollback must preserve
accepted authorization evidence; a backup rollback to before acceptance cannot claim continuous idempotency across
that discarded database history. RFC 1417's exclusions of retry/reset APIs and durable Job history are amended only to
permit these management operations and terminal recovery records. RFC 1515 gains the bounded failure-metadata callback;
the Supervisor never resets a Topic budget.

## Acceptance criteria

- Blocking does not restart exhausted work, move Cursor, discard Sources/target, or prevent another Scope's progress.
- Live final attempts stay pending; stopped/revoked attempts retain diagnosis. Worker and model timeouts remain distinct
  through restart; exact selected ranges remain independent of cumulative coverage and target.
- Duplicate and concurrent retries, lost responses, restart, a later block, and disabled processing replay one accepted
  request without granting another allowance; stale, cross-Scope, unknown, and unauthorized requests cannot reset work.
- Success creates no retained execution history. Repeated observation preserves one block ID and immutable evidence;
  acceptance changes only its receipt fields.
- Supported legacy budgets migrate without resetting costs or inventing historical facts; file-backed restart, backup
  restoration, and actual supported backend transactions preserve identities and recovery semantics.
- OpenAPI, Python SDK, CLI, desktop, and advertised host management surfaces agree; MCP remains unchanged. SQLite-only
  tests or mocked inputs do not establish real OceanBase/seekDB or native host acceptance.

# Drawbacks

The design still adds a durable domain table and nullable budget columns, requires an offline schema upgrade, and
retains block records without a time bound. It cannot answer a full execution-history query or reconstruct every
intermediate failure. Operators must account for persistent blocks caused by repeated administrative retries.

# Rationale and alternatives

| Alternative | Decision |
| --- | --- |
| Keep every attempt, including successes | More forensic detail, but recovery does not require it and no history consumer or retention budget is established. |
| Keep only a block in the existing budget | Cannot replay acceptance after success/reset deletes the budget; retaining old generations changes its current-frontier ownership. |
| Reuse Artifact storage | Transactionally feasible, but requires a private Family and durable restrictions on revisions, capabilities, and cleanup. Prefer the narrower operational lifecycle. |
| Carry transient attempts and permanent blocks in one new table | Avoids widening budgets, but adds separate active-attempt storage beside the authoritative budget. Reuse the existing current-work record instead. |
| Create shared processing history now | Requires agreement on another real workflow's identity and retention semantics. A rename or a hypothetical generic consumer does not establish reuse. |
| Rely on logs or let flush reset budgets | Logs cannot guarantee durable replay; ordinary flush must not authorize new model cost. |

# Prior art

[RFC 1417](1417_topic_memory.md) defines frontier allowances and atomic publication;
[RFC 1515](1515_artifact_processing_supervisor.md) supplies common scheduling and fencing;
[RFC 1771](1771-unified-database-migrations.md) defines explicit schema upgrades.
[Issue #1885](https://github.com/oceanbase/powercontext/issues/1885) records the operational recovery requirement.

# Unresolved questions

Before acceptance, reviewers should decide whether the proposed budget-plus-block model is the smallest sufficient
durable lifecycle, whether the assessed Artifact alternative offers a concrete advantage, and whether indefinite
block/receipt retention is acceptable. Any accepted change to these choices must retain the identity and atomicity
guarantees above. Backend migration/sizing evidence remains an implementation acceptance requirement, not an assumed
result of accepting this design.

# Future possibilities

A separately justified forensic history, shared execution records validated by another workflow, or bounded retention
with explicit replay-expiry semantics can be proposed later. None is required for blocked Topic recovery.

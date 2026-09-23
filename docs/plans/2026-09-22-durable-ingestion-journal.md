# Durable ingestion journal implementation plan

Status: proposed

Date: 2026-09-22

Baseline: `f1467c7` on `goal/open-brain-session-authority`

Scope: public Open Brain behavior and a safe cutover path for a single canonical Brain. This plan
contains no private hostnames, account identifiers, source selections, credentials, or live capture
content.

## Outcome

Every normalized capture reaches a durable queue before the canonical writer begins materializing
it. One Mac Mini writer drains that queue in deterministic order. A crash can leave an item pending,
but it cannot silently lose an acknowledged capture or create two canonical commits for one delivery.

The target flow is:

```text
source adapter
    -> authority and admission checks
    -> durable ingestion journal
    -> Brain-scoped single-writer fence
    -> capture reservation and canonical commit
    -> raw archive, ledger, and rebuildable projections
```

The journal is part of the canonical Brain. Client outboxes and collector custody remain upstream
safeguards. They do not become alternate Brain stores. They may replace their body with metadata
after verifying either a durable custody receipt or an immediate terminal capture receipt from the
Mac Mini Brain.

Completion requires all of the following:

1. Every capture entrypoint uses the same journal boundary before canonical materialization.
2. A successful `queued` response is a verifiable transfer of durable custody for the complete
   normalized envelope to the Mac Mini Brain.
3. Exactly one fenced writer drains journal items by increasing journal sequence.
4. Restart at any tested transition produces one canonical capture or one visible pending or
   quarantined journal item, never silent loss.
5. Pending journal content is not searchable, exported, published to managed Markdown, or returned
   through record APIs.

## Current state

The repository already has most of the correctness primitives, but they sit on different sides of
the writer boundary.

| Existing primitive | Location | What it guarantees now | Gap this plan closes |
| --- | --- | --- | --- |
| Capture reservation and stages | `packages/engine/src/open_brain_engine/engine/capture.py` | A `captures` row resumes blob, source, and projection work after a crash | Reservation currently occurs after obtaining the canonical writer fence |
| Engine recovery | `packages/engine/src/open_brain_engine/engine/local.py` | Incomplete `captures` rows resume on open | It has no earlier Brain-owned ingress queue to drain |
| Delivery idempotency | `CaptureSubmission.request_sha256()` and ADR 0003 | Same delivery ID and digest returns the original result; changed digest conflicts | The identity must also govern journal enqueue and replay |
| Brain writer fence | `packages/engine/src/open_brain_engine/storage/locks.py` and ADR 0007 | One logical canonical sequencer commits at a time | Enqueue should not require this long-lived writer fence |
| Destination outbox | `packages/connectors/src/open_brain_connectors/outbox/` | A client can retain a delivery before the Brain accepts it | It is optional, client-side, and cannot be the Brain journal |
| Collector custody | `packages/collector/src/open_brain_collector/custody.py` | Provider intake survives collector failure | It is source-specific custody, not canonical ingress |

The existing `captures` table remains the canonical capture stage machine. The new journal sits in
front of it and hands work to it. Replacing `captures` would add migration risk without improving
the source-to-writer boundary.

## Architecture decision

### Options

| Option | Shape | Benefits | Costs | Decision |
| --- | --- | --- | --- | --- |
| Brain-owned SQLite journal | Add immutable ingress metadata, a durable payload spool, and append-only lifecycle events to the Brain database | Correct pre-writer durability, atomic state changes with existing capture rows, deterministic drain, no new service | Schema migration and a new queued outcome | **Recommended. This is the proper architecture.** |
| Treat `captures.stage = 0` as the journal | Move the current reservation earlier and reuse one table | Smallest diff | Mixes ingress with canonical capture state, still strains compound source transactions, and gives weak queue diagnostics | Reject |
| Reuse the connector outbox inside the engine | Point every source at the existing filesystem queue | Reuses tested retry code | Reverses package dependencies, makes an optional client component part of core correctness, and cannot atomically terminalize with SQLite capture state | Reject |

### Fixed decisions

- The journal belongs to the Brain database. No source adapter owns a private canonical queue.
- Enqueue is a short SQLite transaction and does not require the canonical writer fence.
- Canonical processing still requires the existing Brain-scoped exclusive writer fence.
- The core remains foreground-only. This work adds no daemon, listener, scheduler, or background
  thread. Engine open, a capture call, or an explicit owner drain command performs bounded work.
- A verified `queued`, `accepted`, or `duplicate` receipt authorizes the client outbox or collector
  to replace its body with a metadata-only terminal record. A queued receipt is terminal for source
  custody but non-terminal for canonical ingestion.
- Existing delivery identity semantics remain unchanged. The journal stores the current request
  digest for idempotency and a separate envelope digest for corruption detection. A replay never
  overwrites the first durable envelope.
- Static validation, authority evaluation, privacy narrowing, item-size checks, and request-digest
  calculation happen before the enqueue transaction. New-delivery rate, concurrency, storage, and
  journal capacity checks happen after the transaction confirms the delivery is new. Canonical side
  effects happen only in the writer.
- No journal item is visible to search or read APIs until its canonical capture stages permit that
  visibility.
- The writer's monotonic journal sequence is internal operational metadata. Scoped receipts expose
  an opaque ingestion ID, never the global sequence or Brain-wide queue position.

## Journal contract

### Durable data model

Schema version 10 adds these objects in `local_schema_catalog.py`:

| Object | Required fields | Rules |
| --- | --- | --- |
| `capture_ingestion_items` | monotonic internal `journal_sequence`, opaque unique `ingestion_id`, unique `delivery_id`, existing `request_sha256`, `envelope_sha256`, submission path, byte count, enqueue timestamp | Metadata is immutable. Updates fail closed; guarded deletion is allowed only after a terminal event has another durable idempotency home. The global sequence is owner-only. |
| `capture_ingestion_payloads` | `delivery_id`, strict versioned `journal.v1` envelope bytes | No in-place update. The row may be removed only after a terminal lifecycle event exists. |
| `capture_ingestion_events` | monotonic event sequence, delivery ID, closed event kind, attempt number, exact bounded custody or capture receipt metadata, timestamp | Append-only while active. Updates fail closed; guarded terminal compaction may remove the complete item history. No raw content or exception string is stored. |
| `capture_ingestion_tombstones` | delivery ID, request digest, final `discarded` result, decision timestamp | Compact immutable replay protection for explicitly discarded items after their active history is removed. |
| `capture_ingestion_pending` | SQL view of items whose latest event is non-terminal and whose payload exists | Used by bounded drain and metadata-only status reporting. |

The lifecycle event set is closed and versioned:

- `queued`: the complete envelope and initial event committed together;
- `attempt_failed`: a retryable processing attempt ended without a terminal receipt;
- `accepted`: canonical capture completed and the receipt was recorded;
- `duplicate`: the existing canonical receipt was recovered for the same delivery and request digest;
- `quarantined`: processing cannot continue automatically without owner action;
- `discarded`: an owner explicitly removed a quarantined payload while retaining metadata and the
  reason.

`accepted`, `duplicate`, and `discarded` are terminal for the journal. `queued` is terminal only for
the upstream source's custody obligation. A quarantined item retains its payload in the Mac Mini
Brain. An owner retry appends a new `queued` event rather than changing prior history.

Journal append-only history is an active-custody mechanism, not a second permanent content ledger.
After `accepted` or `duplicate`, guarded compaction removes the payload, item, and event history only
after the durable `captures` row can answer future idempotent replay. After explicit owner discard,
the same transaction writes the compact tombstone before removing active history. Pending and
quarantined items are never compacted. Active item and byte limits are exact; the ordinary database
storage watermark also bounds retained canonical and tombstone metadata.

### Versioned envelope

Add a closed `journal.v1` serialization for the complete normalized `CaptureSubmission`. It records
the admitted privacy decision and all provenance and authority fields required to reproduce the
same writer input. Unknown fields, missing fields, duplicate keys, invalid enum values, and an
envelope digest mismatch fail closed.

Do not change the bytes produced by the existing `request_value()` or `request_sha256()` methods.
Those bytes are an established idempotency contract. The separate `envelope_sha256` protects the
stored journal body but does not redefine duplicate delivery behavior.

### Custody receipt

The engine return type becomes the closed union `CaptureOutcome = CaptureReceipt |
CaptureCustodyReceipt`. A `CaptureReceipt` keeps its current accepted and duplicate byte shapes. A
`CaptureCustodyReceipt` has this exact bounded information:

| Field | Meaning |
| --- | --- |
| `contract_version` | Exact `capture-custody.v1` discriminator |
| `status` | Exact value `queued` |
| `ingestion_id` | Random opaque identifier; never the writer sequence |
| `brain_id` and `issuer_epoch` | Mac Mini Brain identity and current fenced epoch |
| `delivery_id` and `request_sha256` | Stable replay key and immutable request binding |
| `requested_tier` and `final_admitted_tier` | Proof that admission may narrow but never widen privacy |
| `queued_at` | Canonical UTC enqueue time retained for exact replay |
| `protection_acknowledgement` | Required nullable forward-compatibility slot; null under the current local-durability policy |

The receipt contains no capture ID, canonical path, payload, source reference, title, queue depth,
or writer sequence. The exact receipt is stored with the queued event so a same-digest replay returns
the same bytes.

This implementation does not claim independent backup, replication, or a completed receipt-
protection finalizer. Its acknowledgement is null, matching the current terminal receipt contract,
and its durability claim is limited to the product's documented local operating-system and disk
boundary. A future deployment policy that requires independent protection must reject a null
acknowledgement and retain the upstream body until a separately specified finalizer succeeds.

A client verifies contract version, Brain ID, issuer epoch, delivery ID, request digest, tier
narrowing, and the configured protection policy before replacing its body with a metadata-only
custody record. A mismatch retains or quarantines the upstream body. The Mac Mini journal then owns
retry, quarantine, and canonical completion.

Any immediate `accepted` or `duplicate` result used to release an upstream body must carry the same
Brain, epoch, delivery, request, privacy, and protection bindings. An unbound legacy
`CaptureReceipt` may still support owner display compatibility, but it never authorizes client
outbox or collector body removal.

### Enqueue and receipt behavior

The engine first validates the complete submission, evaluates authority, narrows privacy, enforces
the item-size bound, and calculates both digests without creating state. Enqueue then runs in one
short `BEGIN IMMEDIATE` transaction:

1. Look up the delivery ID across active journal items, canonical `captures`, and discard tombstones.
   The same request digest returns the stored custody receipt, current terminal capture receipt, or
   stable discarded result without applying new-delivery capacity limits. A changed request digest
   returns the existing stable conflict result.
2. For a new delivery, enforce the new-delivery rate and concurrency gates, current storage
   watermark, and exact journal item and byte capacity.
3. Generate the opaque ingestion ID and insert immutable item metadata, the complete payload, the
   exact custody receipt, and the `queued` event.
4. Commit SQLite before returning any success or attempting the writer.

After enqueue, the same foreground call makes one bounded writer attempt. If it acquires the writer
fence, it processes the item and returns the existing terminal capture receipt. If the writer is
busy or canonical processing has a retryable failure after journal commit, it returns the stored
custody receipt. It must not return `writer_queue_full`, because the Brain already accepted custody.

The public CLI and MCP result schemas gain the queued variant and treat it as a successful custody
transfer; the CLI exits zero and MCP returns a tool result rather than an error. Existing terminal
accepted and duplicate response bytes remain stable. The connector outbox verifies the custody
receipt, writes a metadata-only terminal record, and does not consume another retry attempt.
Collector custody checkpoints the intake after the same verification. Scoped adapters may observe
only their opaque receipt; journal status, sequence, drain, retry, discard, and content inspection
remain owner-only operations guarded by an explicit owner authority.

### Writer drain

The writer performs a bounded drain while holding the existing Brain writer fence:

1. Recover incomplete existing `captures` rows first.
2. Select pending journal items by increasing `journal_sequence`, bounded by item and byte limits.
3. Deserialize and verify one envelope, then call a lock-aware internal capture materializer that
   uses the existing reservation and stage machine without reacquiring the writer fence.
4. Append the terminal receipt event. A later guarded compaction transaction removes active journal
   state only after the canonical capture row or discard tombstone durably owns replay semantics.
5. Continue past quarantined items. Leave retryable failures pending and expose bounded metadata in
   status and doctor output.

A crash after canonical capture but before the journal terminal event is safe. Recovery replays the
same delivery ID, receives the existing duplicate result, appends the terminal event, and compacts
the payload.

## Implementation phases

<!-- model: sonnet -->

## Phase 1: freeze the contract and add schema 10

Estimate: 1.5 to 2 focused days.

Changes:

- [ ] Add ADR 0019 for journal ownership, queued receipt semantics, retention, and the foreground-only
  drain model.
- [ ] Add strict journal envelope and receipt types in
  `packages/engine/src/open_brain_engine/engine/contracts.py`, including the explicit
  `CaptureOutcome` union and `capture-custody.v1` verifier.
- [ ] Add schema 10 tables, indexes, triggers, and view in
  `packages/engine/src/open_brain_engine/engine/local_schema_catalog.py`; bump the state schema and
  runtime compatibility declarations.
- [ ] Extend the explicit migration coordinator for schema 9 to 10. Keep Portable Brain v5 unchanged,
  but refuse export with a stable `ingestion_pending` result while any pending or quarantined journal
  payload exists. A successful restore creates an empty journal because the export gate proves no
  acknowledged ingress payload was omitted.
- [ ] Update schema catalog, migration, fresh-Brain, restore, and runtime-compatibility tests.

Exit gate: a schema 9 fixture migrates to schema 10 without changing existing capture, ledger,
search, identity, privacy, or source rows; malformed journal rows and forbidden mutations fail
closed.

<!-- model: sonnet -->

## Phase 2: implement journal storage and writer recovery

Estimate: 2 to 2.5 focused days.

Changes:

- [ ] Add `packages/engine/src/open_brain_engine/engine/ingestion.py` with enqueue, lookup, bounded drain,
  terminalization, compaction, quarantine, retry, and metadata-only status operations.
- [ ] Split capture processing into an enqueue-facing method and a writer-locked materializer. Preserve
  the existing `captures` reservation, stage numbers, and fault injection points. Reuse the terminal
  receipt contract's required nullable protection-acknowledgement slot without claiming that the
  deferred independent-protection finalizer already exists.
- [ ] Add fault points after journal commit, after capture reservation, after canonical completion, and
  after terminal event but before payload compaction.
- [ ] Update `BrainEngine._recover()` to finish incomplete capture stages first, then perform one bounded
  journal drain under the same writer lease.
- [ ] Add separate count, byte, item-size, batch-size, retry, and storage-watermark limits. The journal
  never evicts an item to make space.

Exit gate: every injected crash boundary recovers to one terminal capture or one observable pending
or quarantined item, with no searchable pre-commit content and no duplicate canonical commit.

<!-- model: sonnet -->

## Phase 3: route every capture surface through the journal

Estimate: 2 to 2.5 focused days.

Changes:

- [ ] Route `CaptureTasks.accept` and `CaptureTasks.submit` through enqueue, then attempt the bounded
  writer drain.
- [ ] Preserve source revision compare-and-swap in `sources.py`: the surrounding source operation keeps
  its writer fence, while its normalized capture must be journaled before materialization by a
  lock-aware internal path.
- [ ] Preserve Markdown import pending-revision behavior in `markdown_import.py`: an import revision
  stays unsearchable until the journaled capture completes and activation succeeds.
- [ ] Update owner CLI, local MCP, scoped MCP, destination-bound operations, plugin capture sink,
  collector live capture, and connector outbox result mapping. Every path propagates its existing
  required `EffectiveAuthority`; none substitutes `None` for owner authority.
- [ ] Teach source-specific custody and the client outbox to verify either a custody receipt or an
  immediate terminal capture receipt. A verified custody receipt compacts the upstream body without
  consuming retry or age limits; wrong-Brain, stale-epoch, changed-digest, widened-tier, malformed,
  or missing required-protection receipts retain or quarantine it.

Exit gate: contract tests prove each entrypoint creates or reuses a journal item, and direct adapter
writes to canonical capture storage are absent.

<!-- model: sonnet -->

## Phase 4: add owner operations and adversarial tests

Estimate: 1.5 to 2 focused days.

Changes:

- [ ] Add owner-only journal status, bounded drain, quarantined retry, and confirmed discard commands.
  Default output is metadata-only and never prints envelope content.
- [ ] Extend `open-brain status --json` and doctor output with pending count, quarantined count, oldest
  age, retained bytes, and last bounded failure code.
- [ ] Test simultaneous enqueue, writer contention, FIFO sequence, journal-full behavior, disk
  watermark refusal, duplicate replay, changed-digest conflict, corrupt envelope quarantine, and
  guarded terminal compaction with discard tombstone replay.
- [ ] Extend collector custody and connector outbox crash matrices so upstream deletion happens only
  after exact custody-receipt verification and never after a mismatched or unprotected receipt.
- [ ] Re-run authority tests for owner, public-job, and destination-bound captures, including stale and
  narrowed grants.

Exit gate: an operator can see and recover every non-terminal item without reading content or
bypassing authority, and concurrency tests show one canonical writer order.

<!-- model: haiku -->

## Phase 5: document, verify, and cut over the Mac Mini

Estimate: 1 to 1.5 focused days, including full native checks.

Changes:

- [ ] Update `docs/capture-contract.md`, architecture diagrams, CLI and MCP examples, runtime
  compatibility notes, and the operator runbook.
- [ ] Run `make verify`, `make native-audit`, `make homebrew-smoke`, `git diff --check`, and
  `actionlint .github/workflows/ci.yml`.
- [ ] Test schema migration and crash recovery on a disposable Brain populated with synthetic captures.
- [ ] Before the real Mac Mini migration, stop capture launchers and take the supported backup. After
  migration, a Portable export is permitted only after status proves no pending or quarantined
  journal payload exists. Install the verified build, migrate once, confirm schema 10 and journal
  status, perform a bounded drain, then resume launchers.
- [ ] Keep the pre-migration snapshot until capture, search, restart recovery, collector custody, and
  destination outbox smoke checks all pass.

Exit gate: the Mac Mini reports schema 10, an empty or explained pending journal, one active writer
epoch, successful capture and search smoke tests, and no upstream custody item deleted before a
verified custody or terminal capture receipt.

## Test matrix

| Boundary | Injected stop or condition | Required result after restart |
| --- | --- | --- |
| Before journal commit | Transaction abort or storage refusal | No item, no capture, stable refusal |
| After journal commit | Process exits before returning the custody receipt | One pending item; upstream retains its body; exact replay returns the stored receipt |
| After capture reservation | Process exits before raw archive | Existing capture stage resumes; journal remains pending |
| After raw archive | Process exits before canonical publication | Existing stage resumes without a second raw object |
| After canonical completion | Process exits before terminal journal event | Replay returns duplicate and terminalizes the journal |
| After terminal event | Process exits before payload compaction | Terminal receipt remains; later compaction safely removes only the spool payload |
| Writer busy | Another foreground writer holds the fence | Verifiable custody receipt with opaque ingestion ID; upstream compacts its body; no false refusal or duplicate |
| Same delivery, same request digest | Concurrent or repeated submit | One journal item and one canonical capture |
| Exact replay while journal is full | Existing delivery and digest after active capacity is exhausted | Stored custody, capture, or discarded result; new-delivery limits do not mask idempotency |
| Same delivery, changed request digest | Concurrent or repeated submit | Stable conflict; first envelope remains unchanged |
| Corrupt payload | Digest or strict decode fails | Quarantined metadata, retained payload, no canonical side effect |
| Receipt binding mismatch | Wrong Brain, epoch, digest, tier, or protection acknowledgement | Upstream body remains retained or quarantined; no custody transfer is recorded |
| Scoped queued response | Multiple principals enqueue around the same time | Opaque ingestion ID reveals no global sequence or queue depth |
| Portable export with pending payload | Any acknowledged item is pending or quarantined | Stable `ingestion_pending` refusal; no incomplete export is created |

## Rollout and rollback

Schema 10 is a one-way compatibility boundary. Older binaries must refuse it rather than write
through it. Rolling back executable code after migration is not safe unless that code understands
schema 10.

Use this rollout order:

1. Merge and publish a build only after the complete verification gate passes.
2. Validate migration, restart, and queued recovery on a disposable synthetic Brain.
3. Pause Mac Mini capture launchers and take the supported pre-migration backup.
4. Upgrade and migrate the Mac Mini, then run status, bounded drain, capture, search, and restart
   smoke checks.
5. If acceptance fails, keep launchers stopped and restore the pre-migration snapshot with the prior
   binary. Do not attempt an in-place schema downgrade.

## Pull request sequence

1. **Contract and schema:** ADR, envelope contract, schema 10 migration, compatibility tests.
2. **Journal and writer:** store, drain, recovery, fault injection, core crash matrix.
3. **Adapter cutover:** capture surfaces, upstream custody behavior, owner operations, docs.
4. **Release gate:** full verification evidence and synthetic migration report. No private deployment
   identifiers or captured content enter the public repository.

Each pull request must leave the branch green. The schema pull request may add unused tables, but no
pull request may route only some ordinary capture adapters around the journal once adapter cutover
begins.

## Definition of done

- One normalized capture API is the only route from adapters to canonical materialization.
- The journal is durable, bounded, strict, observable, and content-private by default.
- A verified custody receipt transfers payload responsibility to the Mac Mini without exposing the
  Brain-wide journal sequence.
- The existing single-writer fence orders every canonical capture commit.
- Every crash-matrix case proves no silent loss and no duplicate canonical commit.
- Existing outbox, collector custody, source revision, Markdown import, authority, privacy,
  migration, portable restore, native packaging, and full repository checks pass.
- The public documentation states that the Mac Mini Brain is the final canonical store; laptops and
  other clients retain bounded delivery custody only until a verified custody or terminal capture
  receipt.

Expected implementation effort: 8 to 10 focused engineering days, followed by a guarded Mac Mini
cutover window of about 60 to 90 minutes if the synthetic migration and native checks are green.

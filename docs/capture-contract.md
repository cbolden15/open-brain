# Capture contract

The capture envelope preserves source, capture timestamp, normalized content type, immutable provenance, and the owner's one-line reason for saving an item.

Owner-authored captures require a non-empty one-line `capture_why`. Automated playlist captures may represent missing owner context explicitly as `automation_absent`; they are restricted to `hold` or `reference` and cannot become an idea or action candidate.

Intent is closed: `reference`, `idea`, `action_candidate`, or `hold`. Ideas and action candidates create review proposals only. Third-party content never silently creates a task.

Share intake accepts an owner-authored URL, reason, and optional shared text. Authentication, bounds, and JSON validation happen before durable queueing. Capture success requires immutable private raw persistence followed by either a durable private hold or receipt-bound event and distillation work. Capture creates no Markdown knowledge page or task.

Retries resume from durable boundaries. A capture with an existing extraction event retries only distillation queueing instead of fetching mutable source content again.

## Durable ingress journal

After normalization, authority evaluation, privacy narrowing, size checks, and digest calculation,
every capture path writes one complete `journal.v1` envelope to the Brain-owned SQLite ingress
journal before canonical materialization. The enqueue transaction does not take the canonical
writer fence. It commits immutable delivery metadata, the payload, a `capture-custody.v1` receipt,
and the `queued` event together before a successful custody response is returned.

The journal is schema 10 and is drained only by the existing Brain-scoped exclusive writer. The
writer recovers incomplete `captures` rows first, then processes journal sequence in increasing
order. A retryable failure remains pending; an undecodable or otherwise unsafe item is quarantined
with its payload retained. `accepted`, `duplicate`, and owner-confirmed `discarded` events are
terminal. Guarded compaction removes an accepted or duplicate payload only after the canonical
capture row can answer future replay; discard writes a tombstone before removing active history.

The public outcome is a closed union. Existing accepted and duplicate receipt bytes remain stable.
A queued custody receipt contains only the contract version, `queued` status, opaque ingestion ID,
Brain ID, issuer epoch, delivery ID, request digest, requested and final privacy tiers, enqueue
time, and nullable protection acknowledgement. It never contains payload, title, source reference,
capture ID, queue depth, or the global journal sequence. A queued result transfers local custody to
the Brain's documented disk boundary; it does not claim independent backup or replication.

Pending and quarantined journal payloads are owner-only operational state. They do not appear in
search, record reads, exports, published Markdown, or scoped adapter responses. A client outbox or
collector may replace its body only after verifying the exact custody receipt bindings (or a bound
terminal receipt). If verification fails, the upstream body stays retained or is quarantined.

## Privacy tiers

Every capture carries one privacy tier from the closed set `public`, `work`, `personal`, `secret`, and `unknown`. The immutable privacy decision is recorded before persistence. Local owner capture and Markdown import keep their existing default decision unless the owner passes an explicit tier, and the owner-only explicit tier authority is never merged with the remote startup-policy tier set described below. See [privacy model](privacy-model.md).

## Admission limits

Admission is enforced in the engine's public capture boundary, before durable state exists. The limits are per process, because the core is one foreground process: the rate window, concurrency counter, and waiter count live in memory and recover in process. The CLI and MCP surfaces ship the compiled defaults; the limits are engine constructor configuration, not a flag a submitting client can change.

| Limit | Default |
|---|---|
| `max_envelope_bytes` | 8 MiB |
| `max_body_bytes` | 4 MiB |
| `requests_per_minute_per_principal` | 120 requests |
| `max_concurrent_admissions` | 8 |
| `max_writer_waiters` | 16 |
| `storage_high_free_bytes` | 2 GiB |
| `storage_critical_free_bytes` | 512 MiB |

Storage watermarks are absolute free bytes on the Brain root's filesystem, not usage ratios. A ratio threshold would refuse captures on an ordinary laptop whose large disk is mostly full while tens of gibibytes remain free, so both watermarks are byte floors. The probe runs before each submission's write path, so freeing space recovers capture without restarting anything.

Batch bounds are not admission limits, and no batch submission path exists: the per-cycle item and byte batch bounds are enforced by the outbox drain path described under Offline outbox below.

## Admission results

Every refusal is one of eight stable named values carried by `CaptureAdmissionError`. A refusal leaves no capture row, source revision, blob, search document, or receipt.

| Result | Meaning | Retryable |
|---|---|---|
| `envelope_too_large` | The canonical request envelope exceeds `max_envelope_bytes` | No |
| `body_too_large` | The payload body exceeds `max_body_bytes` | No |
| `rate_limited` | The principal exceeded `requests_per_minute_per_principal` in the sliding minute | Yes |
| `admission_busy` | The process is at `max_concurrent_admissions` for this submission path | Yes |
| `writer_queue_full` | The bounded writer-waiter queue is at `max_writer_waiters` | Yes |
| `storage_high` | Brain-root free bytes are below `storage_high_free_bytes` | Yes |
| `storage_critical` | Brain-root free bytes are below `storage_critical_free_bytes` | No, until space returns |
| `tier_not_permitted` | The requested tier is outside the trusted policy's allowed capture tiers | No |

Retryable means an identical retry may later be admitted unchanged. Recovery needs no process restart: the rate window ages out, the concurrency counter releases after each admission, and free space is re-probed per submission.

## Which gates apply to which path

Size checks (envelope and body) and storage watermark checks apply to every submission path: owner capture, Markdown import, public-job, and destination-bound. The rate limit, concurrency cap, and bounded writer wait apply only to non-owner submissions, which today are the public-job and destination-bound paths.

Owner-originated capture and Markdown import are exempt from the rate, concurrency, and bounded-wait gates so existing local behavior is unchanged after upgrade. Owner paths keep their immediate busy behavior under writer contention instead of queueing behind remote submissions.

## Destination-bound submission

Destination-bound clients submit through one bounded operation governed by a trusted startup policy the owner selected at launch. The CLI command is `capture-submit` with `--policy`. MCP uses `--allow-capture-submit` with `--session-policy`; `--capture-policy` remains a compatibility alias for capture-submit-only launchers. Every form reads the same `launcher-policy.v1` JSON file, an exact schema with no extension fields, treated as trusted owner input, never as client input.

The policy is validated against this Brain's durable identity before any submission exists. A policy naming another Brain is refused with `destination_mismatch`, a superseded issuer epoch with `issuer_mismatch`, and an unexpected authorization generation with `stale_policy`. An `external_provider` egress mode fails closed with `consent_unavailable`, because the destination-bound path grants no egress authority.

The policy's `allowed_capture_tiers` set is the sole tier authority for this path. A request may select one tier inside that set. A missing tier becomes `unknown`, and it is accepted only when the set itself includes `unknown`. A tier outside the set is refused with `tier_not_permitted` before any engine call, leaving no partial state. No client flag can widen the set.

Non-owner admission gates apply: the principal is the tenant and actor pair from the policy identity, and rate, concurrency, and bounded writer waiting are enforced before the writer lease. Replay is idempotent on the delivery ID and request digest, and a reused delivery ID with different bytes conflicts rather than overwriting.

## Digest and receipt binding

The requested tier is inside the immutable request digest for the public-job and destination-bound paths only. The owner-path digest keeps its exact historical bytes, so existing local replay identity is unchanged. The destination-bound digest additionally binds the destination Brain ID and issuer epoch.

Every receipt binds `requested_tier` and `final_admitted_tier`. The two differ only when admission narrowed the tier at the canonical boundary. Destination-bound receipts additionally carry `delivery_id`, `request_sha256`, `destination_brain_id`, and `issuer_epoch`; every other path leaves those four unset so existing receipt bytes stay unchanged.

A synthetic destination-bound submission looks like:

```sh
open-brain capture-submit "synthetic destination text" \
  --policy /absolute/path/to/launcher-policy.json --privacy-tier work
```

The policy file is owner-prepared trusted input; the example text and any principal or provider names in examples are synthetic.

## Offline outbox

The optional `open-brain-connectors` package adds a client-side outbox for destination-bound captures that must survive process restart. It is not in the core dependency graph and has no listener, daemon, thread, or scheduler; every operation is a foreground command the owner starts and that exits when done. The command surface is `open-brain-outbox`, documented in the [CLI reference](cli.md).

### Durable enqueue

Each delivery is one immutable `outbox.v1` envelope held as one JSON file named for its delivery ID, written with owner-only file and directory modes where the platform supports them. There is no index file; every operation rescans the directory and recovery is that same scan. An unparseable item file surfaces as a metadata-only `corrupt` entry and is never deleted.

An enqueue writes a same-directory temporary file, fsyncs it, applies the owner-only file mode, atomically renames it onto the item name, and fsyncs the parent directory where the platform exposes that primitive. `queued` is reported only after the rename and the directory sync complete, so a queued item always survives process restart. Re-enqueuing byte-identical content under the same delivery ID reports `already_queued`; the same delivery ID with different bytes reports `delivery_conflict` and writes nothing.

Envelope payloads are restricted to the text family. Every destination-bound surface accepts only text today, so the outbox carries text payloads only.

### Capacity

The store enforces one item cap and one byte cap, plus a per-item byte limit on the serialized envelope. Admission of a new item reserves a fixed headroom of 2048 bytes beyond the item's encoded size, so the attempt and quarantine metadata a later state transition appends always fits and state transitions are never refused for capacity. The per-item limit defaults to 248 KiB, derived from the stdio transport's 256 KiB request-document cap with margin, so every accepted item can always be serialized into one bounded request document; an item over the limit returns the stable `item_too_large` result before any file is written. A full outbox returns the stable `outbox_full` result, writes no partial item, never evicts or overwrites an active or quarantined body, and never reports `queued` for a body that was not persisted. Quarantined, terminal, and corrupt items count against both caps for as long as their files remain. Capacity is recovered by terminal records, which replace the body with a smaller metadata-only record.

### One drain cycle

The drain is owner-invoked and runs to completion of exactly one bounded cycle. It first takes an exclusive lease: one marker file per outbox directory created exclusively with the owner-only mode. A second concurrent drain observes the marker and returns the stable `drain_busy` result without touching any item. A stale marker older than the reclaim bound, 600 seconds by default, is reclaimed only when the process ID recorded inside it is no longer alive, and the reclaim is reported in the cycle summary. An unparseable or unreadable marker never authorizes a reclaim; removing one is owner territory.

Queued items are attempted in enqueue-time then delivery-ID order. Before any delivery attempt, an item whose age has reached its retry age limit or whose recorded attempts have reached its retry attempt limit is quarantined with reason `age_exhausted` or `attempts_exhausted`. Both windows are half-open: the boundary value itself is exhausted. An item that reaches its attempt limit through a retryable failure inside a cycle stays queued, and the next cycle's pre-attempt check quarantines it. An item that changed or vanished mid-cycle is skipped and counted.

Per-cycle batch bounds cap how many items and how many envelope bytes one cycle attempts, measured from the on-disk item sizes at the start of the cycle. A cycle that would exceed either bound stops after the last admitted item, reports the untouched remainder in the stable `batch_too_large_items` summary field, and leaves those items queued.

### Receipt verification and quarantine

A terminal receipt is verified against the item before anything is removed. Verification binds the destination Brain ID, issuer epoch, delivery ID, request digest, and final admitted tier, where narrowing from the requested tier is allowed and widening is refused. A verified accepted or duplicate receipt replaces the body with a metadata-only terminal record. A verification failure quarantines the item with reason `receipt_mismatch` and never removes the body, so stale-epoch and wrong-Brain receipts never authorize removal.

Destination refusals keep the retryable split of the admission results above: a retryable refusal is recorded as one attempt and the item stays queued, while a terminal refusal quarantines the item under its stable result code. The drain never reinterprets the retryable flag. A transport failure is recorded as a retryable `transport_error` attempt and never quarantines. Delivery conflicts, malformed receipts, and policy mismatches are terminal and quarantine the item; on the stdio path, a success exit whose result document is unparseable, not an object, or shape-invalid is a malformed receipt and quarantines with reason `receipt_malformed`. Quarantined items stop automatic retries and keep counting against capacity until the owner resolves them.

### Terminal records and the acknowledgement slot

Both a verified receipt and an owner-confirmed discard replace the item file with one metadata-only terminal record. The record is a store-level format, `outbox.terminal.v1`, not an `outbox.v1` contract surface. It keeps the delivery ID, destination Brain ID, issuer epoch, tenant, principal, request digest, enqueue timestamp, attempt count, last attempt time, and terminal timestamp; the request digest is the only payload-derived value. A receipt-kind record embeds the verified receipt cross-bound to the record identity, so a tampered record surfaces as corrupt; a discard-kind record carries no receipt. The body bytes are removed on both paths.

The terminal receipt contract carries a required nullable `protection_acknowledgement` key. It is null today, verification ignores it, and the slot is reserved for a future finalizer.

### One request digest

The outbox and the destination share the digest definition from Digest and receipt binding above; the outbox exposes it under one alias so both sides call the same engine helper. The envelope is self-verifying, because the stored digest must equal that digest recomputed from the envelope's immutable fields, so a tampered body or digest never parses. The digest preimage excludes the delivery ID and the policy reference: the destination deduplicates on the delivery ID, and the policy decides only whether a request may be attempted at all.

### Owner operations

Owner retry moves a quarantined item back to queued under a new bounded window anchored at the retry time while preserving the delivery ID, destination, request digest, and enqueue time. The fresh window extends the bounds, one age window and one attempt allowance on top of what is already recorded, instead of resetting the counters. Owner discard requires an explicit confirmation and leaves a metadata-only terminal record. Owner conversion never edits or retargets the quarantined original: it enqueues a new immutable envelope with a fresh delivery ID, the requested destination and epoch, the same payload and request fields, a recomputed request digest, and a lineage reference to the original delivery ID. The original stays quarantined until the owner resolves it. No rollback path retargets an existing item.

# ADR 0019: Brain-owned durable ingestion journal

Status: accepted

Date: 2026-09-22

## Decision

The canonical Brain owns a SQLite ingestion journal in front of the existing
`captures` stage machine. Source adapters retain their own outboxes or provider
custody only until the Brain returns a verified receipt. They are not alternate
canonical stores.

Enqueue commits the complete normalized `journal.v1` envelope, immutable item
metadata, and an exact `capture-custody.v1` receipt in one short SQLite
transaction. It does not acquire the long-lived canonical writer fence. The
receipt status is `queued`; it proves local durable custody, not canonical
materialization. It binds an opaque ingestion ID, Brain ID, issuer epoch,
delivery ID, request digest, requested and admitted privacy tiers, enqueue
time, and the nullable protection acknowledgement. It never exposes the global
journal sequence, queue depth, source content, canonical path, or capture ID.

The existing Brain-scoped writer fence drains pending entries in increasing
journal sequence. It recovers incomplete canonical captures before journal
work, records terminal `accepted` or `duplicate` journal events, and can leave
retryable work pending or quarantined. This remains a foreground operation:
engine open, one capture call, or an explicit owner drain may do bounded work.
The core adds no listener, scheduler, daemon, or background thread.

Active journal history is custody state, not a second permanent content ledger.
After a terminal canonical result has a durable idempotency home, guarded
compaction can remove the active item, payload, and events. An explicitly
discarded item retains a compact immutable tombstone. Pending and quarantined
payloads are never exported, searchable, published, or visible through record
APIs. Portable Brain v5 remains unchanged; export refuses with
`ingestion_pending` while one active payload remains.

## Consequences

`CaptureSubmission.request_value()` and `request_sha256()` remain unchanged.
The journal stores that established request digest separately from the digest of
the full envelope, which detects journal-body corruption without altering
delivery idempotency. A schema-9 Brain migrates to schema 10 only through the
explicit runtime-admission coordinator. A Portable Brain v5 restore creates an
empty journal because export is allowed only after active acknowledged ingress
payloads are gone.

The current local-durability policy leaves `protection_acknowledgement` null.
Deployments that require independently protected custody must reject that value
and retain upstream bodies until a separately specified finalizer completes.

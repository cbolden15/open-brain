# Portable Brain v6

Schema 11 exports Portable 6. The product version is not a storage compatibility
indicator. Portable 1–5 imports keep their existing validation paths, including
the application's standalone Portable 4 import refusal.

Portable 6 retains the Portable 5 record and privacy/issuer byte contracts and
requires two additional canonical JSON sidecars:

- `history/sources/lifecycle-v1.json` retains lifecycle versions, explicit
  withdrawal operations, request digests, and exact receipts.
- `history/sources/admission-v1.json` retains source namespaces, aliases,
  revision request/key/order evidence, terminal intake submissions and receipts,
  managed delivery envelopes and receipts, routing replay evidence, and the
  source control epoch.

Validation checks closed fields and types, sorted unique row identities,
namespace and envelope hashes, source/revision membership, withdrawal receipt
hashes, sequential lifecycle versions, and terminal delivery/intake links. The
manifest commits to every sidecar's exact bytes. A Portable 5 validator refuses
these additional files.

Admission validation also compares retained intake claims with canonical capture
payloads, provenance, file bytes, and revision predecessor/order evidence. It
preserves valid privacy narrowing at admission and rejects a history-only
revision claimed as a promoted head. Observed delivery version 2 retains original
and transformed hashes plus normalization and privacy-policy evidence; original
and transformed hashes are adapter attestations, not verification of raw files
that are absent from the archive. The frozen capture format does not retain the
supplied title or provider delivery ID as independent equality witnesses.

Export uses the engine's writer fence and one SQLite read snapshot. Unresolved
source intake or managed delivery reservations refuse export with
`ingestion_pending`, as do pending ingestion-journal payloads. Collector queues,
physical root bindings, local runtime sessions, consent, and cursor keys are
not archive authority.

Clean import installs lifecycle and admission rows in the existing hidden-stage
restore transaction before rebuilding current search. Withdrawn sources retain
their history and receipts but have no current projection. Promotion and
same-import-ID retry audit the archive-bound authority, privacy, source records,
search rows, and index. Restore preserves revision continuity evidence; it does
not create a new physical collector binding or reactivate a withdrawn source.

Sharing approvals and public-copy eligibility are outside Portable 6.

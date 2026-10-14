# Portable Brain v11

Slack capture uses local state schema 16, runtime session 11 and Portable metadata 11. The preceding facet format remains schema 15/runtime 10/Portable 10. Frozen Portable 1–10 validators and catalogs retain their own meanings.

Portable 11 retains the Portable 10 issuer, privacy, source lifecycle and admission, sharing, historical authority, original capture metadata, capture custody, facet state, facet operations and facet events. It adds two required canonical sidecars:

- `history/developments/evidence-v1.json`: immutable evidence-binding operations and receipts for derived captures, including source state, supporting developments and superseding developments.
- `history/developments/append-v1.json`: frozen derived publication append requests, draft content and resulting proposal identities.

The Portable 11 catalog commits to the Portable 10 catalog and the development evidence and approval-dependency formats. Archives cannot be relabeled as an older format. Derived evidence is engine authority; collector candidate records, extraction queues and attention priorities are private operational state.

## Evidence and publication

A development capture binds its supporting capture IDs through an engine-owned operation. Its retained receipt records the source identities and revisions inspected at binding time. Supporting and superseding developments remain distinct relationships. Collection does not approve publication or create facet vocabulary.

Publication approval validates transitive supporting developments and their frozen source state inside the approval transaction. A changed source head, withdrawn or unavailable source, changed route, or incompatible revision makes the approval fail rather than silently replacing its evidence. Existing proposal approvals and rejections remain terminal decisions.

Development-backed proposals use review binding 6. It retains the base review
meaning in `base_schema_version` (3, 4 or 5) and commits to every reachable evidence
receipt through `development_evidence`. Each commitment contains a capture ID and
the immutable receipt-row digest; a null digest commits to the absence of a
receipt. Supporting captures, supporting developments and superseding developments
all participate in the bounded, cycle-free closure. The owner review digest covers
these commitments. Approval and full Portable validation reject missing,
substituted or newly attached receipts that contradict the frozen binding.

Portable 11 explicitly admits binding 6, with catalog versions
`approval_dependencies: 2` and `development_review_binding: 6`. Frozen Portable
1–10 validators do not admit it. Ordinary inherited proposals retain their existing
binding semantics.

This unreleased candidate changes the schema-16 development review contract and
Portable-11 catalog. Earlier schema-16 candidates containing development proposals
without binding-6 commitments are unsupported upgrade/export sources, including
rejected or approved proposals. Rejection and recreation do not repair retained
history. No automatic conversion is provided; preserve the original state and its
matching backup. Supported schema-15 migration and Portable-10 import remain intact.

Export captures inherited and development evidence from the same schema 16 snapshot. Validation checks the closed sidecar structures, canonical requests and receipts, source identities, and cross-file evidence relationships. Restore validates before materialization and layers the development installer over the inherited facet/base restore path. Ready-state auditing and import-ID replay protections still apply.

## Migration and older readers

The supported schema 15-to-16 transition requires exclusive admission and settled historical snapshot state. It installs the development tables and advances the durable runtime compatibility record to 11/16. Migration is transactional; a committed upgrade must be recognized as committed during recovery rather than retried as an unchanged old store.

Clients require the exact current state/runtime pair 16/11. Desktop and Obsidian reject the former 15/10 pair and unknown future pairs. Do not weaken these checks or run an older installed executable against upgraded state. Portable 10 remains a historical archive format; it is not a label for a Portable 11 export.

## Operational backup and rollback

Portable export preserves canonical Brain evidence. It does not preserve the collector's staged observations, processing operation reservations, candidate decisions, deferred attention entries, source selections, credentials or scheduling state. Back up private collector state through the documented collector recovery workflow alongside the matching Brain backup.

Before a private rollout, verify restore in a disposable directory with a matching runtime. To stop a failed rollout, disable scheduling and pause admission while retaining pending work and receipts. Do not delete queues to force progress. An older binary is not a rollback tool for a migrated Brain, and restoring a prior backup does not selectively undo accepted captures.

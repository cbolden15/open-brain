# Portable Brain v7

Local schema 12 and runtime session 7 export Portable 7. The product version
does not establish storage compatibility. Portable 1–6 imports retain their
existing validation, including the application's standalone Portable 4 refusal.

Portable 7 requires `history/sharing/approvals-v1.json` in addition to the
unchanged Portable 6 inventory. The new sidecar stores exact job identity,
preview requests and frozen bytes, decisions, copy envelopes and links,
revocations, and immutable operation receipts. Provider consent and physical
source bindings are local operational state and are absent from the archive.

Decision and revocation receipts explicitly contain `dto_version: 1`, `brain_id`
and `issuer_epoch`. These fields participate in the retained receipt digest and
must match the exact preview and mutation request. Revocation does not rewrite
the original decision receipt or grant future source revisions approval.

Validation checks closed fields and types, row uniqueness, request and receipt
digests, decision versions, provider and destination bindings, the separate
original and copy captures, retained managed delivery and observation evidence,
and the exact admitted copy payload and privacy. A recomputed manifest or
sidecar hash cannot make contradictory evidence valid. Export uses one writer
fence and SQLite snapshot; unresolved capture or link custody returns
`ingestion_pending`.

Clean import restores validated evidence in a hidden stage and audits it before
promotion. Same-import retries audit the staged authority again. Imported
undecided previews remain inspectable but require a fresh local preview before
execution. The restored Brain computes current eligibility from its own source
head, lifecycle, route, links, and current local provider consent; the archive
does not grant a new external session.

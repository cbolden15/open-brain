# Portable Brain v14

Portable14 inherits the exact Portable13 file contracts and adds one required
sidecar, `history/retraction/approvals-v1.json`. It contains sorted immutable owner
retraction approval requests and receipts under `ob.owner-retraction-evidence.v1`.
The catalog binds the frozen Portable13 catalog and owner approval contract1.

Each approval binds the complete selected membership, typed impact and counts,
Brain/issuer, principal, hashed preview identifier and exact lifecycle request.
Its receipt binds the approval hash and terminal journal receipt hash. Validation
rejects unknown fields, duplicate operations, noncanonical encodings, altered
count types, orphan journal references and mismatched membership or receipts
before destination mutation. Existing journal payload/event bytes stay v1.

Export includes an empty sidecar when no owner approvals exist. Operational
previews are excluded. Schema19 can retain lifecycle-ineligible search rows after
marker-only commit; normalization excludes only exact identities justified by
durable base heads and current lifecycle exclusion. Missing eligible identities
and arbitrary extra projections still refuse export. Full source history remains
inherited evidence, not new current eligibility.

Restore at schema19/runtime14 installs journal and approvals in the same database
transaction. A failed installation leaves neither family installed. Current owner
status and exact commit replay retain the old receipt even after issuer change;
old issuer evidence grants no new runtime authority. Preview custody never
survives restore. Earlier supported containers seed empty approvals, and frozen
Portable13 readers refuse Portable14 before mutation.

The owner workflow is documented in
`/Users/calebbolden/Projects/oss/open-brain-public/.worktrees/competitive-gap-reconciliation/docs/owner-retraction.md`.
The predecessor contract is
`/Users/calebbolden/Projects/oss/open-brain-public/.worktrees/competitive-gap-reconciliation/docs/portable-brain-v13.md`.

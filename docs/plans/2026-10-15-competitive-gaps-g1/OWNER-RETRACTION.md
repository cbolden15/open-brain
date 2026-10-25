# Owner retraction implementation contract

This slice implements owner-only preview, paged review, commit and status at
schema19/runtime14/Portable14. The schema18 journal DDL, request/event bytes and
Portable13 validators remain frozen. New approvals are immutable supplemental
evidence in the same engine database and transaction as the journal decision.

Migration19 adds two tables:

```sql
CREATE TABLE retraction_previews (
 preview_id TEXT PRIMARY KEY, principal_id TEXT NOT NULL,
 brain_id TEXT NOT NULL, issuer_epoch INTEGER NOT NULL,
 created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
 impact_bytes BLOB NOT NULL, impact_sha256 TEXT NOT NULL
);
CREATE TABLE retraction_approvals (
 operation_id TEXT PRIMARY KEY REFERENCES lifecycle_journal_requests(operation_id),
 request_bytes BLOB NOT NULL, request_sha256 TEXT NOT NULL UNIQUE,
 receipt_bytes BLOB NOT NULL, receipt_sha256 TEXT NOT NULL UNIQUE
);
```

Approvals reject UPDATE and DELETE with immutable-evidence triggers. Preview rows
are operational, owner-protected and excluded from export. Migration19 advances
the compatibility floor to runtime14, preserves all earlier migration bytes and
increments projection policy generation. Older clients refuse before mutation.
The existing journal adapter admits exact18/13 and19/14 coordinates, retaining
the same journal evidence format at both coordinates.

The new module
`$REPO/.worktrees/competitive-gap-reconciliation/packages/engine/src/open_brain_engine/engine/owner_retraction_contracts.py`
owns strict canonical validation. Its `approval_record(request, receipt)` helper
validates and returns the exact object `{request,receipt}`. Public IDs and errors
are body-free; errors have a closed `code`. Its `validate_approval_records(records,
journal_evidence)` validates sorted unique operations against the existing journal
requests and committed receipts before Portable destination mutation.

Approval request has exact keys `contract:"ob.owner-retraction-approval.v1"`,
`operation_id`, `brain_id`, `issuer_epoch`, `principal_id`,
`preview_binding_sha256`, `lifecycle_request_sha256`, `impact`.
The binding is SHA-256 of the opaque preview identifier, never its raw value.
`impact` has exact keys `target:{kind,id}`, `capture_ids` (ordered list),
`membership_version` (integer or null), `membership_sha256`, `items` (all sorted
typed effects), `counts` (the six impact count fields), `preimage_sha256`.
Counts must equal the retained typed effects and selected membership. Capture
target selects exactly its ID, with null membership version; session target has
positive membership version and the exact journal session membership/digest.
Preview approval binds all pages, not just the visible page.

Receipt has exact keys `contract:"ob.owner-retraction-receipt.v1"`,
`operation_id`, `approval_sha256`, `journal_receipt_sha256`, `counts`, `receipt_id`.
`approval_sha256` hashes the canonical request. The receipt ID is `retreceipt_`
plus SHA-256 of the canonical receipt excluding `receipt_id`. Stored
`receipt_sha256` hashes the complete canonical receipt. All canonical JSON uses
the engine canonical encoding with duplicate-key/type/field refusal.

Preview IDs are `retprev_` plus43 unpadded base64url characters from32 random
bytes. At most16 live previews per owner, fixed one-hour expiry. Initial preview
retains exact immutable impact under the engine writer fence after current owner
admission. Paging uses a read snapshot, revalidates the complete impact and expiry,
and never renews it. Pending approvals without a copy identity refuse preview
with `operation_pending`: their impact has no public v1 representation yet.

Commit accepts only preview ID and canonical UUID4 operation ID from the local
owner. Under one writer transaction, check existing approval and full binding
before preview expiry/freshness. Exact terminal replay returns the retained DTO
even after expiry, consumption, issuer change or restore; changed preview binding
with the same operation refuses. An existing journal-only operation ID conflicts.
First commit revalidates the whole impact, performs the existing journal
retraction, persists approval and receipt, updates eligibility projections and
consumes the preview atomically. Fault before transaction commit leaves no public
approval or retraction; loss of acknowledgment is exact replay, not recapture.
Status is read-only, owner-admitted, operation-ID-only and returns retained counts.
Raw preview identifiers are absent from accepted Portable evidence.

Portable14 requires `history/retraction/approvals-v1.json` with exact object
`{contract:"ob.owner-retraction-evidence.v1",records:[{request,receipt},...]}`,
operations sorted lexically. Empty evidence is still required. It inherits the
exact Portable13 file set after removing this new path. The new catalog binds
the Portable13 catalog and approval contract1. Restore validates all approvals
against the inherited journal before mutation, restores journal and approvals
atomically, retains old issuer receipts and starts with no operational previews.
Predecessor inputs seed empty approval state. Never reinterpret old receipts or
relax Portable13 to accept this new evidence.

The owner CLI is exactly `retract preview --capture ID|--session ID`,
`retract preview --preview-id ID --item-page N`,
`retract commit --preview-id ID --operation-id UUID`, and
`retract status --operation-id UUID`, with common local `--json`/`--data-dir`.
Output follows the approved preview/commit DTOs in
`$REPO/.worktrees/competitive-gap-reconciliation/docs/plans/2026-10-15-competitive-gaps-g1/PERSISTENCE.md`.
No MCP/plugin/desktop mutation surface or provider parser activation is included.

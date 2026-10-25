# Portable Brain15

The development candidate pairs Portable15 with local schema20 and runtime
session15. It adds mixed owner/consumer lifecycle evidence and durable consumer
intake to the frozen pre-lifecycle Portable12 base. Portable1–14 validators and
migrations1–19 retain their original bytes and meanings. Older readers refuse
the successor; clients upgrade together under exclusive runtime admission.

The successor validates these explicit evidence families:

| Family | Contents |
|---|---|
| `history/capture-lifecycle/journal-v2.json` and digest payloads | Shared requests/events, group membership, session membership, outcomes, retraction barriers and custody. |
| `history/consumer-intake/evidence-v1.json` and `objects/<sha256>` | Accepted operation approvals, complete typed input/normalized/provenance objects, and checked scope/archive/source/outcome projections. |
| `history/retraction/approvals-v1.json` | Unchanged owner approval/receipt bytes, checked against successor replay and exact session membership. |

Hashes alone do not establish validity. Validation reconstructs every accepted
operation, checks the complete reachable object union and kinds, verifies exact
provider identities and normalized capture preimages, and cross-checks capture
metadata, stable source namespaces, source revision chains and current membership.
Scope/archive/outcome projections derive from shared events and accepted proof;
contradictory exported projections refuse before destination writes.

Nonterminal consumer work blocks export. Explicit recovery may complete it;
explicit halt retains its immutable intent and custody without activating or
materializing anything. Completed materializer records remain retained history,
subject to the shared staging and retraction predicates.

An interrupted materializer can leave a stage0–2 capture row or partially written
source file. For terminal halted consumer work only, Portable15 validates these
unfinished projections against the retained envelope/reservations and exact
expected capture bytes. Its disposable export snapshot omits those unfinished
rows and only their validated partial paths. The source Brain is unchanged.
Restore retains the complete intent/custody and leaves that unfinished projection
unmaterialized; status counts and semantic re-export remain equal. Completed
stage3 records are preserved. Unknown or damaged partial rows/files refuse export.

Restore installs validated journal and immutable consumer/owner approval authority
before rebuilding source and search projections. It checks reconstructed
projections before promotion. Old13/14 clean snapshots use explicit predecessor
validation and successor installation; their owner-only validators are not
broadened to admit consumer envelopes. Retraction barriers, original privacy,
scope identities and old-issuer restrictions survive clean restore.

The exact catalog is in
`$REPO/.worktrees/competitive-gap-reconciliation/packages/engine/src/open_brain_engine/portable/v15_catalog.py`.
Engine APIs are documented in
`$REPO/.worktrees/competitive-gap-reconciliation/docs/consumer-intake.md`.

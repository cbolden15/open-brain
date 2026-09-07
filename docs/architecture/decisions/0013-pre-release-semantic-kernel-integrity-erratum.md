# ADR 0013: Pre-release semantic-kernel integrity erratum

Status: accepted

Date: 2026-09-07

## Context

Adversarial review of the first Secure Node semantic-kernel implementation exposed three gaps in
the accepted Brain Protocol v1 schemas. An effect outcome reconciliation could not identify the
unknown receipt it replaced inside the only mutation operation. Effect receipts participated in
provenance traversal but could not receive a purge resolution. Commit history and `changes` used
bare item IDs that could not distinguish proposal revisions or multiple member transitions under
one purge.

The review also found implementation invariants that need explicit contract treatment: loaded
state must receive the same lifecycle validation as new items, accepted revisions must preserve
proposal provenance, origin supersession must cite the prior head, purge reviews must name their
exact intent, overlapping purge closures cannot produce order-dependent state, and receipt evidence
must form a one-to-one delivery and commit graph.

No Secure Node ledger, package, or public protocol release exists. Correcting v1 now is safer than
preserving an internally contradictory pre-release shape.

## Decision

Brain Protocol v1 receives one coordinated pre-release erratum.

### Effect reconciliation is a commit item

Every effect receipt carries the required nullable field `reconciles_receipt_id`. An initial effect
receipt uses `null`. A reconciliation names the exact current receipt, which must have outcome
`unknown`, and records a terminal `succeeded` or `failed` outcome. It preserves the effect ID,
external identity, compartments, and provenance of the unknown receipt. A terminal effect cannot
advance, and an `unknown` receipt cannot reconcile another receipt.

The reconciliation receipt is an ordinary `commit` batch item. Its predecessor identity is inside
the canonical batch bytes and commit digest. There is no separate mutation path for reconciliation.

### Every provenance descendant is resolvable

`effect_receipt` joins the purge subject kinds, using its Brain-scoped receipt ID. A descendant
effect receipt enters suppression and purge-pending state with every other closure member and must
receive an explicit `purge` or reviewed `retain` resolution. It cannot use `replace`, because the v1
provenance schema cannot cite an effect receipt as a replacement record source.

A new purge must include a `purge` resolution for its target record in the initiating commit. The
target is tombstoned immediately and cannot be retained or replaced. Distinct purge IDs with
intersecting closures are rejected so item order cannot change lifecycle state.

A retain or replace review is an accepted proposal revision whose body is exactly:

```json
{
  "kind": "purge_resolution_review",
  "purge_id": "prg_...",
  "subject_kind": "record | revision | proposal | effect_receipt",
  "subject_id": "...",
  "resolution": "retain | replace",
  "replacement_record_id": null
}
```

For `replace`, `replacement_record_id` is the exact replacement record rather than `null`. The
review proposal uses owner provenance without source links, which prevents the review record from
recursively joining the closure it resolves. Its compartments equal the subject compartments for
retain and the union of subject and replacement compartments for replace.

### Ledger history uses discriminated item references

The new `ledger-item-ref` schema is a strict union with these variants:

| Item kind | Identity fields |
|---|---|
| `record` | `record_id` |
| `proposal` | `proposal_id`, `proposal_revision_id` |
| `revision` | `revision_id` |
| `decision` | `decision_id` |
| `purge_transition` | `purge_id`, `subject_kind`, `subject_id` |
| `effect_receipt` | `receipt_id` |

Accepted commits preserve the ordered reference sequence for the batch. A full reference may occur
in only one commit. The `changes` feed carries this same reference rather than a duplicated kind
and ambiguous bare ID. Filters continue to select by item kind.

The pure commit-materialization boundary receives both the validated batch and the proposed commit.
It rejects changed metadata plus missing, extra, reordered, or mismatched item references before
the repository may persist commit evidence.

Commit, receipt, and delivery evidence is one-to-one. Commit cursors and delivery IDs are unique
within a Brain. Every stored receipt names one stored commit and one delivery binding with identical
Brain, cursor, digest, issuer epoch, policy digest, and sequencer epoch values.

### Loaded state cannot bypass transition rules

Complete Brain state validation enforces artifact ownership, revision chains and approvals,
proposal histories and heads, exact decision targets, effect histories, purge relationships,
origin heads, processor-output uniqueness, provenance label unions, tombstone-closure suppression,
and evidence bindings. Retain and replacement exceptions apply only inside the purge that approved
them. Suppression and purge-pending sets are exact consequences of stored tombstones and purge
resolutions; orphan lifecycle flags are invalid. An accepted revision preserves the exact body,
schema, compartments, and provenance of its approved proposal.

A record that repeats an existing `origin_id` must directly cite the current origin head in its
record provenance. The full source-label union therefore applies before the prior head becomes
superseded.

## Compatibility

This is a wire-shape correction to an unpublished protocol. Schema version `1` remains the only
accepted version, and conformance fixtures change atomically with the schemas. There is no durable
data migration. If byte-stable v1 is published before this change lands, the correction instead
requires a new protocol version.

ADRs 0001 through 0012 remain accepted. This ADR narrows their ambiguous pre-release details and
does not change the Open Brain and Secure Node product split.

## Rejected alternatives

- Private composite strings in `Commit.item_ids` are rejected because public `changes` consumers
  could not reconstruct the same identity.
- A standalone reconciliation helper is rejected because `commit` is the only protocol mutation.
- Silently excluding effect receipts from erasure closure is rejected because they carry provenance
  and potentially sensitive external metadata.
- Adding a universal event ID to all six item schemas is architecturally sound but adds a redundant
  identity when the discriminated semantic reference already identifies each accepted item.

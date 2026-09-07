# ADR 0006: Provenance-closed owner erasure

Status: Accepted

Date: 2026-09-04

## Context

The canonical ledger is append-only, but captured sensitive evidence and sensitive metadata must
remain owner-erasable. Hiding one source body is insufficient when proposals, artifact revisions,
summaries, projections, and model outputs derived from that body can still reveal it.

Erasure must preserve an opaque audit history while making the sensitive payload and unapproved
derived copies unrecoverable. The contract must also remain effective when semantic data is
exported and later imported.

## Decision

Model owner erasure as a purge that first appends a tombstone and then resolves the transitive
provenance closure of the target. Query suppression is immediate for the target and every
descendant. Each closure member receives an explicit retain-or-purge resolution; a replacement is
valid only when it is owner-reviewed and no longer contains the target data.

Purge completion requires cryptographic and physical cleanup, not just logical suppression. A final
purge receipt is issued only after the full closure is resolved and cleanup has passed. BrainPack
exports carry tombstones and omit purged payloads; imports apply tombstones before indexing or
exposing payloads.

## Contract invariants

1. A purge is owner-authorized and begins by appending a tombstone. The immutable ledger retains
   opaque record identity, ciphertext integrity, policy scope, and audit transitions; plaintext
   digests and sensitive metadata remain inside the encrypted envelope.
2. On tombstoning, query immediately suppresses the target and every transitive provenance
   descendant, including records, proposals, artifact revisions, summaries, and cached model
   output. Suppression occurs before a result can be exposed.
3. The purge walks the complete transitive provenance graph. Every descendant is explicitly
   resolved: purge it, retain it by an explicit decision, or replace it with an owner-reviewed
   revision that no longer contains the target data. Silence never retains a descendant.
4. Purging destroys wrapped payload keys, removes ciphertext blobs, and invalidates all affected
   projections and caches. Projection cleanup includes rebuilding or removing derived material that
   could reveal the target.
5. A final purge receipt records the resolution of the full descendant closure and is issued only
   when no member remains unresolved, every retained member has an explicit decision, and physical
   projection and cache cleanup passes.
6. Future BrainPack exports include purge tombstones and omit purged payloads. An importer honors
   carried tombstones before indexing, query exposure, or other projection work. Previously created
   external copies cannot be recalled.

## Consequences

Provenance is a correctness requirement for processing, not optional lineage metadata. Nodes must
track enough provenance to enumerate a target's transitive descendants and must track purge
resolution and cleanup status until a final receipt is possible. Projections and model caches are
disposable operational state, but purge still requires their cleanup before completion.

## Rejected or deferred alternatives

- Deleting or hiding only the source payload is rejected because descendant knowledge could still
  disclose it.
- Treating retention as the default is rejected because unreviewed derived copies would survive.
- Removing audit history entirely is rejected because the system must retain immutable purge
  transitions without retaining sensitive plaintext.
- The storage-specific traversal algorithm, key-wrapping implementation, and projection cleanup
  mechanism are deferred. They must satisfy this closure and final-receipt contract.

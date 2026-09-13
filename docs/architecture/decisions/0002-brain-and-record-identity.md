# ADR 0002: Brain and record identity

Status: Accepted

Date: 2026-09-04

## Context

A Brain is an ownership, key, history, sequencing, and portability boundary. Capture can be
offline and delivery is at least once, so a request retry, its accepted record, the ordered commit,
and its receipt must be distinguishable without relying on a host, a filesystem path, or a
projection.

The protocol also needs a durable way to explain where information came from. Stable identity and
explicit provenance prevent a replacement Node, a projector rebuild, or a processor restart from
turning records into untraceable copies.

## Decision

All canonical identities are scoped to exactly one Brain. A Brain has its own identity, key,
ledger, grant, cursor, receipt, and operation namespaces. A host or placement has no semantic
identity role.

Within a Brain, the protocol distinguishes these stable identifiers:

- A delivery identity names one logical producer submission and is reused for its retries.
- A record identity names one immutable canonical record. A source record also preserves its stable
  origin identity when the source supplies one.
- An artifact, proposal, and decision each have a Brain-scoped identity; artifact state advances by
  append-only revisions.
- A cursor names a position in one Brain's monotonic accepted-commit sequence.
- A receipt identifies acceptance of a specific commit digest at a specific cursor.

Records declare provenance explicitly. Derived records and proposals cite their input record
identities; processor-produced output also identifies the processor version. Provenance does not
cross a Brain boundary except through a separately owner-approved declassification bridge that
records receipts in both Brains.

One logical sequencer accepts commits for a Brain at a time. Protocol v1 supports manual cold
transfer only. An epoch-bound transfer certificate names the next Node key and prior ledger head;
grants, commits, and receipts from older epochs are rejected. If the prior sequencer cannot be
proven stopped, writes pause.

## Contract invariants

- One commit, grant, processor instance, cursor, and receipt belongs to one and only one Brain.
  No global cursor or unscoped global search exists.
- A delivery identity is stable across retries of the same logical submission and is bound to its
  destination Brain. It is not a substitute for record, artifact, or receipt identity.
- A record identity is immutable: corrections and reprocessing append new, provenance-linked
  records rather than changing an earlier record's identity or contents.
- Record envelopes carry schema identity and version, producer identity, stable origin identity
  when available, captured and observed times, ciphertext integrity, and applicable provenance.
  Unknown historical values remain explicitly unknown.
- A cursor is monotonic only within its Brain. A receipt binds the accepted commit digest, Brain
  cursor, issuer epoch, and policy digest; it is durable proof of acceptance, not proof that a
  projection has updated.
- Provenance links are explicit and directed. Derived output remains in the source Brain and has
  the union of every input compartment label. Reducing labels or moving information between Brains
  requires the declassification bridge.
- Brain placement, process identity, database keys, file paths, and transport addresses are not
  Brain, record, delivery, cursor, or receipt identities.

## Consequences

Outboxes can safely retain a delivery identity and retry until they obtain the same receipt.
Processors, importers, and projectors can checkpoint Brain-local cursors without creating a
cross-Brain authority channel. Provenance supports evidence-linked queries, review, and the
transitive closure required for owner-authorized erasure.

Manual transfer prioritizes a single authoritative order over availability during uncertainty. A
future active-active design must introduce a new identity and ordering model rather than treating
host replication as equivalent to a Brain sequencer.

## Rejected or deferred alternatives

- Host names, synchronized folders, current runtime roots, and deployment placement as canonical
  identities are rejected. They are private configuration and are not portable.
- Reusing a delivery ID as a record ID, or using a projection filename as canonical identity, is
  rejected because retries, canonical evidence, and materialized views have different lifecycles.
- Automatic failover and concurrent canonical writers are deferred. A signed local-first replica
  mesh, including causal ordering and merge semantics, is the proper future option if active-active
  collaboration is required.
- Identifier encodings, allocation algorithms, certificate formats, and storage indexes are M1
  implementation details, provided they preserve these semantic bindings.

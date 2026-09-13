# ADR 0005: Information-flow labels

Status: Accepted

Date: 2026-09-04

## Context

A Brain is an ownership and authority boundary. Within a Brain, records and derived knowledge may
have different access requirements. Retrieval organization must remain useful without becoming an
authorization mechanism, and automatic classification must not select a security boundary from
body text.

The system therefore needs a mandatory information-flow rule that applies equally to evidence,
derived records, proposals, artifact revisions, summaries, and projections.

## Decision

Use compartments as mandatory all-of authorization labels inside one Brain. A record may carry
several compartment labels, and a reader must hold every one of them. A processor's derived output
receives the union of every input record's labels and remains in the source Brain.

Use spaces only as mutable groupings for navigation and retrieval. A space never grants access,
relaxes a compartment requirement, or selects a Brain. Narrowing a label set or moving selected
knowledge between Brains is an explicit owner-approved declassification operation through a
declassification bridge.

## Contract invariants

1. A compartment is an authorization label inside one Brain. Reading a labeled record or derived
   output requires all labels carried by that item; possession of any subset is insufficient.
2. Every processor output, including derived records and proposals, carries the union of all input
   compartment labels. This propagation preserves the stricter requirements of each input.
3. Derived output remains in its source Brain. A processor, projector, or importer has one
   principal, one Brain grant, and one checkpoint; it cannot use label propagation to cross a Brain
   boundary.
4. A space is mutable organization only. It may support navigation or retrieval but grants no
   authority and cannot override a grant or a compartment label.
5. Lowering a label set or copying knowledge across Brains requires the only permitted mechanism:
   a separate owner-approved declassification bridge that cites the source and records linked
   receipts in both Brains.
6. A destination Brain is selected by a one-Brain grant or explicit owner choice before body
   decoding. Text classification may suggest a space or artifact type, but it cannot route a body
   to a Brain. Unknown organization within a known Brain enters that Brain's quarantine
   compartment; an item with no known destination Brain remains encrypted in its local outbox.

## Consequences

Queries, processors, projectors, and importers must preserve and enforce compartment labels. A
federated owner UI must authorize each Brain separately. Product features may add spaces freely,
but they cannot use space membership as access control. Any useful reduction or cross-Brain sharing
of labels becomes an auditable owner decision instead of an implicit transformation.

## Rejected or deferred alternatives

- Any-of label access is rejected because one sensitive input could be disclosed to a principal
  holding only a different label.
- Allowing processors, spaces, or classifiers to narrow labels is rejected because it makes derived
  access depend on unreviewed automation.
- Routing bodies to a Brain by content classification is rejected because it asks a classifier to
  choose an authority boundary.
- Replacing compartments with spaces is rejected because organization is mutable and non-authority.
  The exact label vocabulary and projection layout remain implementation and policy work.

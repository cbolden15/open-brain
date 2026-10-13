# ADR 0020: Independent owner-defined facets alongside spaces

Status: accepted architecture; implementation acceptance is tracked separately

Date: 2026-10-13

Issue: [#78: Add owner-defined faceted classification](https://github.com/cbolden15/open-brain/issues/78)

## Context

Spaces provide one organizational axis. A record can also belong to overlapping projects,
areas, and topics that do not fit a single hierarchy. Facets provide those independent axes
without changing the record's content or making copies of it.

The existing [space workflow](../../spaces-inbox.md) routes captures, and the existing
[review workflow](../../review-publication.md) uses routed sources and a destination space.
Those contracts remain supported. Facets extend organization rather than replacing routing,
publication destinations, evidence relationships, or privacy classification.

## Decision

Keep spaces separate. Add an engine-owned classification model with owner-declared dimensions,
values belonging to those dimensions, and record assignments. Open Brain ships no built-in
dimensions or values. A record can carry zero or more values, including multiple values from
one dimension. Membership in one facet or canonical page does not exclude membership in another.

### Stable identities and mutable names

Dimensions and values have stable IDs independent of their names. Each value belongs to one
dimension. Assignments and saved queries reference IDs, so renaming a dimension or value does
not change membership or invalidate a query. Display names remain owner-editable.

Assigning the same value twice is idempotent. Removing an assignment changes classification
state without changing source content, provenance, trust, privacy, or content revision identity.
Archive dimensions and values instead of deleting their retained history. Precise archive behavior
for active assignments and saved queries must be specified before implementation.

### Immutable classification history

Classification mutations append durable history, with operation identity and replay semantics.
The history covers creation, rename, archive, assignment, and assignment removal. Current
membership is projected into SQLite for retrieval; that projection is not the only retained
evidence of a mutation.

Classification changes do not manufacture content revisions or rewrite immutable capture bytes.
The implementation contract must define how assignments attach to logical records, how proven
source successors inherit classification, and how historical reads expose classification state.
It must distinguish those operations from the existing space-routing behavior.

### Exact conjunctions and saved queries

The first increment supports conjunctions of exact value IDs. Every selected value must match,
including when two selected values belong to the same dimension. Repeated query terms normalize
to one term. OR, NOT, hierarchy operators, and a general query language are deferred.

Persist a versioned structured query rather than a string whose meaning depends on mutable
names. Results require deterministic ordering and bounded pagination. Query identity, filters,
classification changes, and authorization must participate in cursor validation as specified by
the implementation contract.

### Live selection and frozen publication evidence

A saved facet query selects current matching source records. A proposal freezes the selected
record revisions and classification state, the query version, and the target canonical page
revision for inspection and approval. Approval revalidates its bound evidence through the
existing conflict-preserving publication workflow. It does not silently substitute a newly
evaluated result set.

Single-value pages and composite pages are independent canonical pages. They retain stable page
identities when republished and can share source records. A composite page links from its
applicable facet indexes rather than being nested under one parent facet.

Cross-space source selection needs an explicit facet-query publication contract. It must define
the destination space and source eligibility without silently weakening existing route bindings
or rerouting sources. The current manual publication path retains its established behavior.
Whether changes to the broader query result set invalidate a pending proposal remains a contract
decision; newly matching records never enter an already inspected proposal implicitly.

### Portable records and managed Markdown

Versioned portable records preserve dimension and value IDs, names, archive state, assignments,
mutation history, and saved queries. Extend the supported Portable export and clean-import path
together. An older reader must refuse unsupported facet evidence rather than silently discard it.
The exact format version, schemas, and catalog compatibility need an implementation contract.

The current baseline is [Portable Brain v5](../../portable-brain-v5.md), which supports clean
import. Standalone v4 import remains refused. Facet round-trip work extends the current supported
path; it does not require enabling standalone v4 import.

Managed vault frontmatter and facet index notes are projections of classification state. Use a
reserved `facets` mapping with array values, including for a single value, instead of adding
arbitrary keys at the frontmatter root:

```yaml
facets:
  project: [apollo]
  area: [research]
  topic: [pricing]
```

This example is the human-readable projection. Stable IDs remain authoritative in retained
classification records and saved queries. Emit deterministic frontmatter and one generated
index/MOC note per value without duplicating source records. Reconciliation must define how
owner edits and renames are detected without treating display names as identities or silently
overwriting conflicting edits. That policy is required before vault implementation.

### Authority and compatibility

Facet membership is organization, not access control. Query results, proposal evidence, and
generated indexes obey existing effective authority and privacy projection. Facets never grant
access, alter trust, or authorize publication.

CLI and MCP share engine contracts. Facet reads and mutations need explicit, independently
grantable capabilities; search or read permission does not imply mutation permission. Review
proposal and decision authority remain separate. Exact operations, envelopes, bounds, and session
quotas must be specified before adding those surfaces.

Local schema migration is transactional and interruption-safe under exclusive runtime admission.
Existing runtime compatibility checks must not be relaxed. Assign schema and runtime version
changes from the actual current implementation rather than historical documentation.

## Alternatives

| Approach | Tradeoff |
|---|---|
| Independent facets alongside spaces | Selected. Adds a complete classification model while preserving routing and publication contracts. |
| Unified classification with spaces as a constrained dimension | A coherent broader redesign, but it requires joint migration of routing, publication destinations, and compatibility. Deferred. |
| Open-ended tags or arbitrary metadata | Does not provide the closed dimension/value model, stable query references, or controlled mutation semantics required here. Not selected. |

## Delivery and acceptance

1. Specify the remaining contracts for assignment identity, archive behavior, pagination,
   cross-space publication, portable representation, and owner-edited vault reconciliation.
2. Add engine storage and mutation/query contracts with interruption-safe migration, replay,
   archive, and immutable-content tests. Prove export/import/reopen/re-export before acceptance.
3. Add owner CLI operations and saved-query review/publication with frozen evidence and conflict
   tests. Preserve existing space and manual publication workflows.
4. Add independently authorized MCP operations with limits and tests for denied operations and
   privacy-filtered results.
5. Add deterministic vault frontmatter and indexes with reconciliation tests, then update feature
   documentation and release acceptance evidence.

All automated fixtures use synthetic dimensions and values. This ADR accepts the architecture;
it does not certify an implemented feature or a published release.

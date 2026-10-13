# ADR 0020: Independent owner-defined facets alongside spaces

Status: accepted architecture and implementation behavior; implementation acceptance is tracked separately

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
Archive dimensions and values instead of deleting their retained history. Archived values remain
visible on existing records and usable in saved queries, with an explicit archived status. Block
new assignments to an archived value, allow explicit assignment removal, and retain all history.
Archiving a dimension applies the same restriction to its values.

### Immutable classification history

Classification mutations append durable history, with operation identity and replay semantics.
The history covers creation, rename, archive, assignment, and assignment removal. Current
membership is projected into SQLite for retrieval; that projection is not the only retained
evidence of a mutation.

Assignments attach to logical records and follow proven source revisions. Independent captures
remain independent; equal content or shared URLs do not transfer classification. Canonical page
assignments follow the stable page identity through publication updates. Classification history
is separate from content history, so historical inspection can expose classification at a given
time without rewriting a retained content revision.

Classification changes do not manufacture content revisions or rewrite immutable capture bytes.
They do not inherit the source-revision restrictions of space routing or change its behavior.

### Transaction and retry contracts

Commit each mutation's immutable history, current classification state, retry receipt, and
retrieval-generation change in one engine-owned transaction. CLI, MCP, and vault reconciliation
call these same operations rather than implementing classification independently.

Mutations carry an operation ID and the expected classification version. Retrying the same
operation and arguments returns the original receipt. Reusing an operation ID with different
arguments fails. A stale mutation conflicts instead of overwriting another change. Repeating an
existing assignment remains idempotent.

### Exact conjunctions and saved queries

The first increment supports conjunctions of exact value IDs. Every selected value must match,
including when two selected values belong to the same dimension. Repeated query terms normalize
to one term. OR, NOT, hierarchy operators, and a general query language are deferred.

Persist a versioned structured query rather than a string whose meaning depends on mutable
names. Order results by acceptance time followed by stable record ID. Pagination is bounded;
continuation cursors bind the query, filters, authorization, and relevant data generation. Refuse
stale continuations rather than maintaining long-lived search snapshots.

A canonical page's own facet assignments and its saved source-selection query are separate
facts. Assigning a value to a page does not implicitly change the records selected as its evidence.

### Live selection and frozen publication evidence

A saved facet query selects current matching source captures. The first increment does not use
canonical pages recursively as synthesis inputs. Canonical pages can carry facets and appear in
facet search; recursive synthesis requires a separately defined provenance contract.

A proposal freezes the query version, selected source revisions, relevant facet memberships,
destination, and target canonical page revision for inspection and approval. Approval revalidates
this bound evidence through the existing conflict-preserving publication workflow. A change to
bound evidence conflicts; unrelated records arriving or display-name renames do not invalidate
the draft. Approval never substitutes a newly evaluated result set. Newly matching records wait
for the next proposal.

Single-value pages and composite pages are independent canonical pages. They retain stable page
identities when republished and can share source records. A composite page links from its
applicable facet indexes rather than being nested under one parent facet.

Facet-query publication can select sources across spaces within the caller's effective authority.
For a new page, the owner explicitly chooses its destination space. Updates retain the existing
page's destination. Selection does not reroute sources, alter their privacy, or silently weaken
existing route bindings. The current manual publication path retains its established behavior.

### Portable records and managed Markdown

Versioned portable records preserve dimension and value IDs, names, archive state, assignments,
mutation history, and saved queries. Introduce an explicitly versioned Portable extension with
export and clean import together. An older reader must refuse unsupported facet evidence rather
than silently discard it. Assign the exact format version, schemas, and catalog compatibility
against the current supported implementation.

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
classification records and saved queries. Updating managed-vault facet frontmatter must not
manufacture a content revision or alter previously reviewed bytes. Retained content evidence
and generated classification metadata have independent histories.

Treat facet frontmatter as an editable projection with explicit reconciliation. Compare owner
edits against the last generated version, show the proposed assignment changes, and require an
explicit apply operation before importing them through the engine's version-checked mutations.
Do not silently overwrite owner work or treat an accidental deletion as an unassignment.

Emit deterministic frontmatter and one generated index/MOC note per value without duplicating
source records. Use stable IDs for index-file identity; renames update titles and links. Preserve
modified generated notes and report conflicts. Index notes are rebuildable views and are excluded
from authoritative portable content.

### Owner CLI operations

Provide a focused command family for dimensions, values, assignments, saved queries, and history.
Resolve supplied names to stable IDs and reject ambiguous input. Scripts receive structured
responses with stable identities and effective retry IDs. Exact command names and response
schemas belong to the implementation specification.

### Authority and compatibility

Facet membership is organization, not access control. Query results, proposal evidence, and
generated indexes obey existing effective authority and privacy projection. Facets never grant
access, alter trust, or authorize publication.

CLI and MCP share engine operations and retry rules. Facet reads, catalog management, record
assignment, and saved-query writes need explicit, independently grantable capabilities. Record
assignment permission does not permit dimension/value creation, rename, or archive. Search or
read permission does not imply mutation permission. Review proposal and decision authority
remain separate.

Use existing session-budget conventions initially, with bounded responses and explicit exhaustion
errors. Test denied operations through the actual MCP dispatcher, not only discovery. Counts,
previews, query results, and generated indexes must not reveal inaccessible records. Specify
exact wire operations, response schemas, and numeric quotas before exposing the interfaces.

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

1. Translate the accepted behavior above into engine and interface specifications: record and
   classification identities, transaction schemas, ordering and cursor bindings, publication
   evidence, Portable schemas, CLI/MCP envelopes, and vault reconciliation previews. These
   specifications implement the accepted decisions rather than reopening them.
2. Add engine storage and mutation/query contracts with interruption-safe migration, replay,
   stale-mutation, archive, and immutable-content tests. Interrupt migrations and replay mutations.
   Prove export/import/reopen/query/re-export preserves both evidence and behavior before
   acceptance; file comparison alone is insufficient.
3. Add owner CLI operations and saved-query review/publication with frozen evidence and conflict
   tests, including cross-space selection, explicit destinations, and newly matching records.
   Preserve existing space and manual publication workflows.
4. Add independently authorized MCP operations with limits, actual-dispatch tests for denied
   operations, and privacy-filtered counts, previews, and results.
5. Add deterministic vault frontmatter and indexes with reconciliation tests. Run a synthetic
   end-to-end scenario covering classification, revision inheritance, composite queries,
   publication, archive, vault reconciliation, and export/import. Complete the repository's
   required checks and packaged acceptance, then update feature documentation and release
   acceptance evidence.

All automated fixtures use synthetic dimensions and values. This ADR accepts the architecture and
implementation behavior; it does not certify an implemented feature or a published release.

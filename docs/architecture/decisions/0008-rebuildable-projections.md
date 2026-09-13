# ADR 0008: Rebuildable projections

Status: Accepted

## Context

The canonical authority of a Brain is its append-only ledger of records, artifact revisions,
decisions, and receipts. Markdown, search, and user interfaces are useful surfaces, but none may
become a second authority or coordinate canonical writes.

Projectors consume the public ordered change feed. They must preserve Brain and compartment
boundaries while still allowing Markdown to be an editable human-facing surface.

## Decision

Markdown, full-text search, and vector views are disposable projections of ordered commits.
Projectors own only their checkpoint and materialized output; they rebuild from the Brain's ordered
change feed and do not read internal storage tables or write canonical files.

Every projection is isolated per Brain and per compartment scope. A projector runs with one
Brain grant and only materializes records for which its scope is authorized. Search and vector
implementations must ensure unauthorized records cannot affect candidates, ranking, counts,
snippets, metadata, or other observable output.

Markdown is a first-class projection and editing surface. Generated files carry stable artifact and
revision identities. An edit becomes a proposal against the stated base revision; it never mutates
the ledger or a projection database directly.

## Contract invariants

- The ledger is canonical. A projection appearing, changing, missing, or being deleted does not
  establish, alter, or remove a canonical commit.
- Projectors consume ordered commits and keep an independent checkpoint. A discarded projection can
  be rebuilt by replaying that ordered history within its authorized scope.
- No projection combines Brains. Compartment labels remain mandatory during materialization and
  retrieval; derived output preserves the required input-label union.
- Markdown, search, and vector state are noncanonical and disposable. Index layout, ranking
  internals, cache contents, and output paths are implementation details.
- A Markdown edit identifies its target artifact and base revision, then enters the canonical path
  as a proposal. A stale base revision becomes a competing proposal rather than overwriting an
  accepted revision.
- A projector does not watch internal database tables or canonical folders, and it does not write
  canonical records except through a separately authorized protocol client action.

## Consequences

Projection loss, corruption, or schema replacement is recoverable by replay rather than manual
repair of canonical knowledge. Projectors may be upgraded or moved independently, provided their
new output is rebuilt from authorized ordered commits. Markdown remains convenient for review and
editing, while acceptance and conflict resolution remain in the ledger.

## Rejected or deferred alternatives

- Markdown or synchronized folders as canonical authority are rejected because they cannot provide
  atomic commits, stable conflict handling, or one-writer sequencing.
- Direct edits to a ledger, a projection database, or generated Markdown are rejected; each bypasses
  proposal and decision semantics.
- A shared cross-Brain or cross-compartment search/vector index is rejected because it would weaken
  mandatory authorization boundaries and can leak through retrieval behavior.

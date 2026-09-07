# ADR 0001: Brain Protocol v1

Status: Accepted

Date: 2026-09-04

## Context

Open Brain needs a public, transport-neutral contract that lets a local application, a remote
extension, and a future Node implementation share the same semantics. The canonical authority is
an append-only Brain ledger. Markdown, search indexes, user interfaces, queues, and storage
layouts are projections or implementation concerns, not alternate protocol authorities.

The protocol must be small enough to be stable while still expressing capture, derived knowledge,
review, correction, long-running work, and effect reconciliation. Those actions do not require a
separate public endpoint for every domain object.

## Decision

Brain Protocol v1 exposes exactly these four semantic operations:

```text
commit(batch)                 -> receipt and cursor
query(request)                -> evidence-linked results
changes(after, filter, limit) -> ordered page and next cursor
inspect(reference)            -> record, operation, job, or receipt state
```

`commit` atomically accepts one validated batch for one Brain and advances that Brain's cursor.
It is the only protocol mutation. Source and derived records, artifact proposals, decisions,
corrections, routing outcomes, purge transitions, and effect receipts are typed commit contents.

`query` returns only authorized, evidence-linked results. `changes` is the ordered feed consumed by
processors and projectors. `inspect` resolves a named record, operation, job, or receipt without
creating a fifth domain-specific operation.

Every operation runs under an authenticated, short-lived, versioned capability grant for exactly
one Brain. A client that needs several Brains opens separate sessions and combines only the answers
it is authorized to receive.

## Contract invariants

- Protocol v1 has exactly `commit`, `query`, `changes`, and `inspect`; no public CRUD, table-watch,
  projection-write, or domain-specific mutation endpoint is part of the contract.
- A `commit` batch has one Brain target and is atomically accepted or not accepted. An accepted
  commit returns a digest-bound receipt and that Brain's monotonic cursor.
- Typed commit contents retain their own semantic schemas. A proposal, decision, correction, or
  effect receipt is never represented as an implicit side effect of a query or projection update.
- `changes` orders accepted commits within one Brain only. Its cursor is an opaque protocol value,
  not a cross-Brain ordering mechanism.
- Authorization applies before query candidate selection, ranking, counts, snippets, or body
  exposure. A result cites the supporting record or accepted artifact revision permitted by the
  grant.
- Extensions use the protocol and ordered change feed. They do not access Node internals, canonical
  files, or implementation storage as an alternate contract.
- Projections are disposable and rebuildable from accepted ordered commits. Their appearance or
  update does not prove capture durability.

## Consequences

The public product can provide local and remote transports with identical semantics, while storage,
queue, scheduler, and search implementations remain replaceable. Extension roles can be narrow:
collectors commit records, processors and projectors consume changes, query surfaces query or
inspect, and actuators commit outcome receipts.

Protocol evolution must preserve these four operation meanings. A new capability belongs in a typed
commit schema, request schema, or a later versioned protocol decision, not in an unreviewed
ad-hoc endpoint.

## Rejected or deferred alternatives

- Domain-specific public endpoints for artifacts, decisions, routing, corrections, or actuators are
  rejected for v1. They duplicate the typed-commit model and widen the stable surface.
- Direct database access, canonical-file watching, and projection writes are rejected as extension
  contracts because they bypass authorization, ordering, provenance, and receipts.
- Storage tables, wire encodings, local sockets, HTTP details, queue layouts, and job-runner design
  are deferred to M1. They cannot alter the four semantic operations.

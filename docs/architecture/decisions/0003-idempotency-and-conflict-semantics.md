# ADR 0003: Idempotency and conflict semantics

Status: Accepted

Date: 2026-09-04

## Context

Capture delivery is at least once. Offline outboxes, processor restarts, imports, Markdown edits,
and external effects can all be retried or observed after an interrupted response. The protocol
must therefore give the same input the same result without silently accepting changed input,
overwriting accepted knowledge, or assuming an external action completed.

Deterministic outcomes must be visible as records, proposals, decisions, receipts, or explicit
conflict and unknown states. They cannot depend on mutable projection state or a worker's local
memory.

## Decision

For one Brain and stable delivery identity, a retry with the same commit digest returns the original
receipt and does not append another commit. Reuse of that identity with a different digest is a
conflict: the earlier acceptance remains immutable, and the outcome is a conflict record or
quarantine result rather than an overwrite.

Every artifact edit is submitted as a proposal with an explicit base artifact revision. A proposal
whose base is no longer the accepted head remains a competing proposal. It does not implicitly
merge, replace, or become the current artifact.

Every decision names the proposal revision it expects to decide. A decision may affect only that
expected revision. If the proposal has advanced or a different decision has changed the relevant
state, the caller receives an explicit revision conflict and must submit a new decision; the Node
does not silently retarget it.

Processor-produced output has an identity that includes the complete source record identities and
the processor version. Retrying unchanged processor work therefore follows the same replay rule;
changed sources or processor version produce distinct derived output for review rather than
mutating prior output.

An actuator timeout or ambiguous failure is an `unknown` external-effect outcome. It is neither a
success nor a failure and must not cause an automatic repeat of the effect. Reconciliation occurs
only when an effect receipt establishes the outcome against the approved effect and its external
identity.

## Contract invariants

- Delivery is at least once; acceptance is idempotent by the tuple of Brain identity, stable
  delivery identity, and commit digest.
- Same identity and same digest return the original receipt, including its original cursor. No
  duplicate acceptance or new cursor is created.
- Same identity and changed digest never overwrites, replaces, or aliases the earlier accepted
  content. It returns a deterministic conflict outcome that policy may record or quarantine.
- A correction is a new superseding record or proposal with provenance, never an in-place edit of
  canonical history.
- A proposal always identifies its base artifact revision. A stale base makes it a competing
  proposal, preserving both the existing accepted head and the proposed change for review.
- A decision always identifies the exact proposal revision it expects. Expected-revision mismatch
  is a conflict, not a last-writer-wins decision update.
- Processor output identity includes every source record identity and processor version. Processor
  restarts must not create duplicate derived records for unchanged inputs.
- An external effect changes status only through an explicit outcome receipt. `unknown` remains
  authoritative until reconciliation; it cannot be inferred from a timeout, lost response, or
  projection state.

## Consequences

Clients can retry after a lost response using the same delivery identity and receive a stable
answer. Review surfaces can show competing proposals instead of hiding a stale edit. Reprocessing
is auditable because a changed input set or processor version produces a distinct identity and
provenance chain. Actuator operators must reconcile ambiguity before another effect is authorized.

The Node and conformance suite must expose replay, digest-conflict, stale-base, expected-revision,
processor-restart, and unknown-effect outcomes as observable protocol behavior. The exact error
codes, conflict-record schema, and quarantine workflow remain implementation work.

## Rejected or deferred alternatives

- Last-writer-wins updates, implicit artifact merges, and silent decision retargeting are rejected.
  They erase competing intent and make current knowledge non-deterministic.
- Exactly-once network delivery is rejected as a requirement. Stable delivery identities and
  digest-bound receipts provide the required at-least-once replay behavior.
- Automatic actuator retries after an ambiguous outcome are rejected. The effect must first be
  reconciled by an outcome receipt.
- CRDT-style concurrent canonical editing, causal merge rules, and conflict-resolution UI are
  deferred with the active-active replica-mesh architecture.

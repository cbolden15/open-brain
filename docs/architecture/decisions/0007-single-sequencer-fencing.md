# ADR 0007: Single-sequencer fencing

Status: Accepted

## Context

A Brain is its own ownership, key, history, sequencing, and portability boundary. Canonical
commits must retain one monotonic order even when producers are offline or a Node is moved. File
synchronization, automatic failover, and concurrent writers can create two apparently valid
histories during a partition.

Protocol v1 needs a transfer rule that preserves continuity without treating host placement as
identity or guessing whether a previous writer has stopped.

## Decision

Each Brain has exactly one logical sequencer authorized to accept canonical commits at a time.
Protocol v1 transfers that role only through a manual cold transfer. The Brain owner authority
signs a strictly monotonic epoch certificate for the next Node key. The certificate identifies the
Brain, the new epoch, the next Node key, and the prior ledger head.

The incoming Node may accept commits for the new epoch only after it verifies the owner signature,
the epoch increase, and continuity from the certificate's prior ledger head. The outgoing Node must
be proven stopped before the transfer is activated. If that proof or the required prior-ledger-head
continuity is uncertain, all canonical writes pause until an owner resolves the condition.

## Contract invariants

- A commit batch, grant, and receipt belong to exactly one Brain and carry their issuer epoch.
- Epochs are owner-signed and strictly monotonic for a Brain; an epoch certificate names one next
  Node key and one prior ledger head.
- Nodes and clients reject grants, commits, and receipts from an older epoch as current protocol
  authority. Older receipts remain immutable audit history; they do not authorize new work.
- A new epoch continues from the certified prior ledger head. It cannot silently begin an unrelated
  or competing canonical history.
- Protocol v1 permits manual cold transfer only. It provides no automatic failover or concurrent
  canonical writer mode.
- When the previous sequencer cannot be proven stopped, or continuity cannot be established,
  writes pause. Destination-bound outboxes retain and retry delivery after a valid sequencer is
  available.

## Consequences

Commit order and conflict behavior remain deterministic for each Brain. A transfer has an explicit
owner action and a period in which capture may wait in durable outboxes. Placement can change
without becoming part of Brain identity, but availability is deliberately favored less than
single-history safety during uncertainty.

## Rejected or deferred alternatives

- Automatic failover is rejected for protocol v1 because it cannot safely establish that the prior
  writer is inactive in every partition case.
- A signed local-first replica mesh with causal ordering and deterministic merge is deferred. It is
  the appropriate future architecture if concurrent offline canonical editing becomes a real
  requirement.
- Synchronized folders or Markdown files as a multi-writer coordination mechanism are rejected;
  they do not provide fenced commit authority or a canonical append order.

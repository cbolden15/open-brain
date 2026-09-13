# ADR 0004: Capability authorization

Status: Accepted

Date: 2026-09-04

## Context

The Brain Protocol exposes `commit`, `query`, `changes`, and `inspect`. Each call must be
authorized without weakening a Brain's ownership, key, history, sequencing, or portability
boundary. Extensions and query surfaces are protocol clients; they do not receive implicit
authority from placement, process trust, or access to a projection.

The authorization contract must make revocation prompt, prevent replay, avoid decoding an
unauthorized body, and prevent query-side disclosure through ranking or metadata. It must also
make the safe local transport posture the default.

## Decision

Use a short-lived, versioned capability grant for one principal and exactly one Brain. The Brain
owner authority enrolls principals and signs an epoch-bound issuer certificate for the active Node;
the Node issues grants under that certificate.

The capability contract is enforced at the Node boundary for every protocol operation. A federated
owner surface opens a separate authorized session for each Brain and merges only the results each
Brain has authorized.

## Contract invariants

1. A grant names exactly one Brain and one principal. It scopes allowed operations, compartments,
   record schemas, body visibility, limits, grant ID, issued and expiry times, issuer epoch, and
   policy digest. One `commit` batch targets that one Brain.
2. Grants are short-lived and versioned. A long-lived device enrollment credential may refresh a
   grant, but it cannot carry a capture body. Offline outboxes refresh a grant before sending a
   body and do not retain broad long-lived write tokens.
3. Before accepting a request body for processing, the Node authenticates the grant and checks the
   current revocation epoch. It streams only a bounded body to compute the declared digest, verifies
   the binding over method, Brain, delivery ID, nonce, and body digest, and decodes the payload only
   after that verification succeeds.
4. The Node remembers each request nonce through grant expiry and rejects reuse. A mutating retry
   uses a new nonce with the same stable delivery ID: matching content returns the original receipt;
   changed content conflicts.
5. Revocation increments the relevant principal or policy epoch. Grants from older epochs fail
   immediately. Receipts bind the commit digest, Brain cursor, issuer epoch, and policy digest.
6. Query authorization precedes candidate selection, ranking, counts, and snippet generation. The
   reference Node keeps a separate FTS projection per compartment so unauthorized records cannot
   influence scores or leak through metadata.
7. Remote transport requires TLS and explicit enablement. A new Node binds to loopback by default.
   MCP is disabled until the owner grants a named MCP scope.

## Consequences

Every client, worker, importer, and query surface needs an explicit one-Brain grant. A multi-Brain
owner experience is a composition of separate authorized sessions, never an unscoped global
session. Protocol and conformance work must test expiration, epoch revocation, nonce replay,
digest-bound retry behavior, pre-ranking query authorization, and the local transport defaults.

## Rejected or deferred alternatives

- Long-lived bearer credentials that carry capture bodies are rejected because revocation and body
  scope cannot be constrained tightly enough.
- A grant spanning several Brains or a global search session is rejected because it crosses a hard
  authority boundary implicitly.
- Authenticating only after payload decoding, or authorizing search after ranking, is rejected
  because either can expose unauthorized body-derived information.
- Public remote listeners and enabled-by-default MCP are rejected as defaults. Different transport
  implementations may be added later, but they must preserve these capability semantics.

# Brain Protocol v1

Status: frozen for M1 implementation

Brain Protocol v1 has exactly four semantic operations: `commit`, `query`, `changes`, and
`inspect`. Grant issuance, owner unlock, key rotation, key destruction, projection work, and purge
execution are Node control functions. They are not extra protocol operations.

The schemas under `open_brain_engine.protocol.schemas.v1` and
`open_brain_engine.protocol.validate_protocol_semantics` jointly form the executable wire
contract. JSON Schema enforces structure; the semantic validator enforces cross-field, byte-count,
Brain-boundary, epoch-continuity, and certificate-history rules that Draft 2020-12 cannot express.
The public M0 ADRs explain the contract, while conformance cases and deterministic signature
vectors prove that its executable forms agree.

Wire timestamps use UTC `Z` only. They contain either whole seconds or exactly three fractional
digits for milliseconds. Hours are `00` through `23`; minutes and seconds are `00` through `59`,
so hour `24` and leap-second `60` are outside this protocol profile. Other offsets, fractional
widths, and submillisecond values are invalid. The semantic validator constructs each instant from
its components and checks the calendar validity of every schema-declared timestamp, including nested
certificates and inspection metadata. It does not inherit permissive or version-specific behavior
from a language date parser. Timestamp-like fields inside opaque bodies remain application data.

## Canonical bytes and request binding

Protocol JSON is UTF-8 I-JSON. The strict decoder rejects duplicate object names, non-finite
numbers, invalid UTF-8, and values that RFC 8785 cannot represent. Request bodies and commit batches
use RFC 8785 JSON Canonicalization Scheme bytes from `rfc8785` 0.1.4. A digest is lowercase SHA-256
over those bytes.

The principal signature input is the RFC 8785 object containing exactly `method`, `brain_id`,
`delivery_id`, `nonce`, and `body_digest`. The request envelope also carries the grant identifier
and the Ed25519 signature. The Node validates the serialized grant, principal signature, request
binding, digest, authorization, clock, and nonce before decoding an operation body. Nonce
reservation occurs before operation execution and survives an operation rollback.

The `(brain_id, delivery_id, commit_digest)` tuple is the commit idempotency key. A changed digest
for an existing Brain and delivery identity returns a typed conflict. The canonical commit digest
appears only in the signed receipt returned to an authorized committer. It is not a public entity
identifier, lookup key, query identifier, or bodyless inspection field.

Sequencer continuity uses a separate `lhc_v1_` history commitment to the complete signed prior
receipt. Stop proofs, cold-transfer certificates, and later Node epoch certificates carry only that
commitment, never the prior cursor, commit ID, or canonical commit digest. The exact domain-separated
derivation is frozen in ADR 0010. A holder of the prior receipt can verify the link without making
the receipt's digest visible to later committers.

## Identifiers and cursors

Generated identifiers contain a three-letter role prefix, an underscore, and a lowercase unpadded
base32 encoding of 128 random bits. Their namespace is `(brain_id, identifier)`, except for the
Brain identifier itself. Prefixes distinguish records, revisions, proposals, decisions, commits,
receipts, grants, jobs, principals, keys, Nodes, transfers, stop proofs, deliveries, and nonces.
Identifiers are opaque. Content hashes are never identifiers.

Cursors and query watermarks are opaque tokens. A cursor is meaningful only inside one Brain. A
query-visible watermark is grant-scoped and can advance only for commits authorized by that grant.
The internal Brain-wide projection checkpoint is never serialized.

## Grants and proof of possession

A grant is a signed, secret capability for one Brain and one principal Ed25519 public key. It binds
the issuer key and epoch, policy epoch and digest, principal epoch, explicit scopes, an all-of
compartment set, allowed record-schema references, body visibility, per-grant limits, and a bounded
lifetime of 30 through 900 seconds. The expiry timestamp must equal the issue timestamp plus the
declared TTL. There is no bearer-grant fallback. Replaying a serialized grant with another private
key fails proof of possession.

Issuer and owner signing keys remain behind the `RootKeyCustodian`. Client private keys remain
behind the separate `PrincipalKeyCustodian`. The high-level client creates and uses a principal key,
signs requests, safely retries commits, and follows query continuations within the caller's budget.
Low-level clients may supply the same protocol values directly.

## `commit`

`commit` accepts one atomic batch with at most 128 record, proposal, revision, decision,
purge-transition, or effect-receipt items. Validation checks every item's Brain and provenance
boundary, then computes the proposed label count and active exact-label-set inventory before cursor
allocation. A failed item rejects the complete batch. No projection state participates in commit
validity.

Records are immutable. Their envelopes identify the content schema, producer, stable origin when
known, captured and observed times, compartments, provenance, and ciphertext digest. A null
ciphertext digest has an explicit state: `pending` for commit input or `unknown_historical` for
migrated data. Commit rejects already-encrypted record input. A newly persisted encrypted record is
`verified` and must carry the digest. Revisions name the expected base revision. Decisions name the
proposal and expected revision. Processor provenance requires both a processor identity and at
least one source. Provenance references stay inside one Brain and cannot name a tombstoned or
purge-pending ancestor. Derived outputs carry the union of all source compartments. Spaces never
grant authority.

An accepted or replayed result wraps the same signed receipt. Changed-digest and expected-revision
conflicts, plus `delivery_purged`, are distinct closed result variants. The successful receipt
contains the Brain-scoped commit ID, delivery identity, opaque cursor, commit digest, issuer epoch,
policy digest, sequencer epoch, Node signing-key ID, owner-signed Node epoch certificate, ordered
owner public-key certificate history, issue time, and Node signature. Every signed document signs
its complete RFC 8785 object with only its signature field omitted. An independent verifier pins
the genesis owner-key fingerprint and follows the owner and Node certificate links without Node
internals.

Owner-key validity intervals are half-open. History activation times strictly increase, retired
predecessors do not overlap successors, and a Node epoch certificate's issue time must fall inside
the certifying owner's interval. A Node certificate remains valid after that owner retires if it was
issued while the owner was valid.

## `query`

Query input is bounded literal text. The Node escapes and compiles it into a safe internal FTS5
expression; protocol callers cannot submit raw `MATCH` syntax. Candidate selection begins only
after authorization. Exact-label-set shards are searched in deterministic rounds of at most 32,
including for owners.

The first page binds the authorized active-shard inventory, grant and principal epochs, purge
generation, minimum cursor, requested top-k, and traversal position into an encrypted and
authenticated continuation. The public token exposes none of those fields. It cannot reveal shard
identities, record identifiers, scores, or corpus statistics. A purge touching the bound inventory
invalidates the token.

Intermediate pages are provisional. Final results use reciprocal-rank fusion with constant 60,
then deterministic record-ID ordering for ties. Only within-shard ranks enter fusion, so an
unauthorized shard cannot affect a score or count. Every returned result has at least one authorized
`query-evidence` value naming a supporting record or accepted revision and its provenance. Bodies
require explicit body scope. The Node validates every nested evidence contract and rejects a page
when either the evidence or its provenance crosses the page's Brain boundary.

Query may require a minimum ledger cursor. A lagging projection returns `projection_lag`, a retry
time, and an opaque grant-scoped watermark. The high-level client may wait or transparently restart
an invalidated query only within its caller-supplied budget.

## `changes` and `inspect`

`changes` authorizes before selecting entries and re-authorizes every page. Each entry carries its
commit cursor and ID, typed item kind, role-matched item ID, and transition. Records, proposals,
revisions, decisions, purge transitions, and effect receipts can therefore rebuild the full
accepted semantic history. Unauthorized items do not affect page contents, cursor placement,
counts, or metadata.

`inspect` authorizes every required compartment before entity selection. An absent entity and an
entity the caller cannot inspect return the same `not_found` bytes. Bodies and content-derived
metadata require body scope. M1 makes no constant-time database-access claim.

Changed-digest and revision conflicts are typed operation results, not inspectable entities. Policy
may later commit an ordinary quarantine record. If purge destroys delivery-sensitive material,
later idempotent replay returns `delivery_purged` rather than the original receipt.

## Durable jobs and erasure

The only durable job types in M1 are `purge` and `projection_rebuild`. Jobs have Brain-scoped IDs,
restart-safe pending/running/succeeded/failed states, compartment labels, at most eight attempts,
and state-consistent result or error references. They are inspected through `inspect`; there is no
public job mutation operation or generic scheduler.

Purge resolves every item in the transitive provenance closure as purge, retain, or reviewed
replacement. A replacement is rejected if it retains the target data. Application-controlled
database, journal, FTS, cache, blob, staging, log, error, and temporary sinks participate in residue
checks. Only opaque audit transitions and delivery-erasure tombstones remain.

## BrainPack semantic inventory

The ledger preserves Brain and schema versions, compartments, spaces, record envelopes, available
encrypted payload and blob references, links, tombstones, artifact revisions, proposals, decisions,
effect receipts, commit order, and verifiable schema references. Jobs, grants, nonces, projection
checkpoints, indexes, and host state are operational and excluded. M1 freezes this storage inventory;
it does not add a BrainPack encoder.

## Resource failures

Protocol limits are frozen in `open_brain_engine.protocol.RESOURCE_LIMITS`. A capacity denial uses
`resource-limit-failure`: stable code, plain message, retry safety, observed and accepted values,
and a retry time or corrective action when known. Live nonce rows remain until grant expiry plus
clock skew. Capacity exhaustion fails closed.

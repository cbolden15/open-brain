# ADR 0011: M1 authority, information flow, and resource bounds

Status: accepted

Date: 2026-09-06

## Context

Protocol schemas alone cannot prevent authorization or metadata leaks. M1 needs one ordering model
for grants, proof of possession, replay, reads, projection visibility, owner control, and resource
failure before later waves implement them.

## Decision

### Owner control and user presence

Grant issuance is a local owner-control capability outside the four semantic operations. No local,
HTTP, MCP, or other protocol transport can mint a grant. Grants are returned in memory and never
written in plaintext.

An interactive unlock creates an in-memory owner session for at most 15 minutes. A narrow local
grant may be issued during that session. Body-read, all-compartment, or maximum-lifetime grants,
owner or issuer changes, and key destruction require a user-presence proof no older than 60
seconds. A keyring read is not user presence.

`UserPresenceProvider` uses LocalAuthentication on interactive macOS. Linux and headless macOS use
audited passphrase re-entry against the root-key envelope. Production configuration cannot select a
fake provider.

Out-of-process control uses a Unix-domain socket inside a mode-`0700` runtime directory. The socket
uses mode `0600`, rejects a peer UID different from the Node UID, binds the grant to the requesting
principal public key, and returns bytes only on the authenticated connection. Loopback HTTP does not
expose this channel.

### Principal custody, grants, and receipts

`PrincipalKeyCustodian` is separate from Brain key custody. The high-level client prefers an OS
secret store, falls back to a passphrase-wrapped mode-`0600` file, and permits an explicitly
ephemeral memory-only key. Rotation advances the principal epoch. Revocation rejects grants from an
older epoch.

Each grant binds its issuer key and epoch, policy epoch and digest, principal key and epoch,
operations, all-of compartments, record schemas, body visibility, per-grant limits, and timestamps.
Its expiry must equal its issue time plus a TTL from 30 through 900 seconds.

Every request has an Ed25519 proof of possession over the exact five-field request-binding object:
method, Brain, delivery identity, nonce, and body digest. The larger request envelope carries that
binding, the grant ID, and the principal signature. The Node checks the serialized grant, epochs,
signature, binding, and digest before decoding the operation body. There is no bearer fallback.

Receipts are signed by the active Node key. The owner authority signs the Node key's epoch
certificate. Each receipt carries its Node key ID, sequencer epoch, epoch certificate, ordered
owner-key certificate history, issuer epoch, and policy digest. Every signed contract signs the RFC
8785 bytes of the full document with only the signature field omitted. Deterministic vectors cover
principal bindings, grants, owner and Node certificates, receipts, stop proofs, and cold transfers.
Clients verify the complete chain from a pinned genesis owner fingerprint.

Owner-key validity intervals are half-open: `valid_from <= instant < retired_at`, with no upper
bound when `retired_at` is null. History epochs and `valid_from` values strictly increase. Every
predecessor is retired before its successor becomes valid, and their intervals cannot overlap. A
Node epoch certificate must be issued inside the certifying owner's interval. Later owner retirement
does not invalidate a Node certificate that was validly issued.

### Clocks and replay

Owner-session and step-up age use the monotonic in-process clock. Grant expiry uses a persisted UTC
high-water mark and a fixed 300-second skew allowance. A backward wall-clock observation does not
move the high-water mark and fails closed when freshness cannot be proved.

Serialized instants use the canonical UTC whole-second or millisecond format frozen in ADR 0010.

Replay state is a dedicated encrypted operational store, outside the canonical ledger transaction
and BrainPack inventory. After grant and signature validation, the Node atomically reserves the
nonce before calling `commit`, `query`, `changes`, or `inspect`. The reservation does not roll back
with an operation failure. A live nonce stays until grant expiry plus skew. Capacity exhaustion
returns `nonce_capacity`; it never evicts a live row.

### Read isolation

`inspect` authorizes every required compartment before entity selection. Absent and unauthorized
entities return the same stable error content. Bodies and content-derived fields require body
scope. Database access is not claimed to be constant time.

`changes` authorizes before entry selection and re-authorizes each page. Every entry identifies its
typed committed item and role-correct item ID, so non-record commits remain reconstructable.
Unauthorized entries do not shift page boundaries or alter cursor information.

Query authorizes before candidate selection. The first page snapshots only authorized active exact
label sets. Each continuation round re-authorizes and visits no more than 32 shards. Owners receive
no larger limit. The encrypted continuation is grant-bound, expires with the grant, and is invalid
after a purge touches its snapshot.

Intermediate results are provisional. Final reciprocal-rank fusion uses no corpus-wide or
unauthorized statistics. Every result requires authorized evidence. A missing evidence link removes
the result rather than returning an unsupported text match. Each evidence value is validated as a
complete `query-evidence` contract, including its provenance boundary, and must name the same Brain
as its containing query page.

Projection work is asynchronous. A minimum cursor that has not reached the authorized projection
returns `projection_lag` with retry guidance and an opaque grant-scoped watermark. The Brain-wide
checkpoint remains internal.

### Limits and failures

The provisional limits are:

| Resource | Limit |
|---|---:|
| Compartments per record | 16 |
| Active exact-label sets per Brain | 256 |
| Authorized shards per query round | 32 |
| Records per exact-label set | 50,000 |
| Items per commit batch | 128 |
| Blob staging bytes per request | 67,108,864 |
| Concurrent requests per Brain | 32 |
| Concurrent requests per principal | 8 |
| Retained nonces per Brain | 100,000 |
| Retained nonces per principal | 10,000 |
| Literal query bytes | 4,096 |
| Query top-k | 100 |
| Query pages per grant | 256 |
| Durable job attempts | 8 |

The tracked generator creates 256 unique exact-label sets from 64 synthetic labels. It includes a
16-label boundary shard, a 50,000-record hot shard, 255 smaller shards, eight 32-shard query rounds,
and a 128-item commit batch. Cross-shard benchmark results use only within-shard BM25 ranks and
reciprocal-rank fusion with constant 60; raw BM25 scores are never compared between shards. Its
seed, catalog digest, distribution, fusion metadata, and macOS/Linux results live in
`release/m1-compatibility.json`. No private or real Brain data enters the benchmark.

A limit failure includes a stable code, message, retry safety, observed and accepted values, and a
retry time or corrective action where known. Commit validation rejects label and active-shard
limits before cursor allocation. Query, concurrency, staging, and nonce capacity also fail closed.

### Information flow and HTTP

Payloads, plaintext digests, grants, key bytes, query terms, snippets, and sensitive metadata never
enter logs, errors, security events, or unauthenticated responses. Security events contain only
event kind, Brain ID, optional principal ID, time, and a reason code. They cover denials,
revocations, replay rejection, fencing, and key destruction.

The HTTP adapter uses Starlette and Uvicorn, binds only a numeric loopback address, validates Host,
disables CORS, and rejects browser Origin unless explicitly configured. Remote binding has no M1
configuration. Every protocol route requires grant authorization. Anonymous `/healthz` returns only
a static process-liveness body with no version, Brain, readiness, count, or configuration data.

### Purge and replay

Purge closes over concurrent provenance descendants and resolves each target as purge, retain, or
reviewed replacement. A replacement that retains target data is invalid. Purge destroys scoped
keys, removes structural indexes and FTS rows, and scans every application-controlled durable sink
for plaintext residue.

After key destruction, an opaque delivery tombstone remains so an idempotent retry returns the
closed `delivery_purged` commit-result variant. Accepted and replayed results wrap a signed receipt;
changed-digest and expected-revision conflicts are separate closed variants. A purged retry never
returns a sensitive original receipt. Continuations bound to touched records return
`continuation_invalidated`; the high-level client may restart within its budget.

The versioned JSON schemas enforce structure. The public semantic validator additionally enforces
UTF-8 byte limits, exact Ed25519 and history-commitment encodings, Brain boundaries, nested query
evidence, grant timestamp arithmetic, epoch continuity, and certificate-history timing. Both layers
are mandatory protocol validation.

## Consequences

Later workers receive explicit authorization order, custody boundaries, resource values, and
failure contracts. Any implementation that exposes a fifth semantic operation, bearer grant,
Brain-wide read watermark, raw FTS syntax, owner limit exemption, or plaintext operational sink is
non-conformant.

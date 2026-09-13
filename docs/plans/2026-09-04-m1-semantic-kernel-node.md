# Secure Node M1: semantic kernel and node

Status: product scope renamed to Secure Node; `SN1-W0` (`M1-W0`) and `SN1-W1` (`M1-W1`) are
complete and independently accepted; `CORE-W0` shared portability is complete; `SN1-W2` remains
gated while the Open Brain release-surface reduction closes

Date: 2026-09-04

Base commit: `2e5fec28e3d946fee65a30f971d22679734d5071`

Branch: `goal/open-brain-m1`

Release-scope note: [ADR 0015](../architecture/decisions/0015-homebrew-only-distribution.md) removes
the Phase 4 package classifier, artifact policy, compatibility matrix, and clean-host harness named
in historical W0/W1 evidence below. Secure Node protocol and product behavior remain accepted; its
future verification must use direct package, boundary, and conformance tests rather than restoring
that release system.

## Objective

Build the first complete Secure Node implementation of Brain Protocol v1. Secure Node M1 ends with
a transactional semantic kernel, an advanced one-Brain node, encrypted-at-rest SQLite and
FTS5 persistence, envelope-encrypted blobs, authorization, ordered changes, evidence-linked query,
inspection, purge, and rebuildable projections. The implementation must preserve every completed
v0 and P4 contract while remaining absent from the default Open Brain dependency and runtime path.

Secure Node M1 is synthetic and local. It does not migrate established data, redirect a producer,
publish a package, deploy a service, or enable a public listener.

The Secure Node usability rule is that its opt-in path must be complete after the user selects it.
An `open-brain[secure-node]` source or wheel install must exercise Secure Node without private files
or manual cryptography dependency selection. The Secure Node client hides proof-of-possession
signing, safe retry, and query continuation by default; low-level protocol primitives remain
available for independent implementations. Polished Secure Node CLI, UI, and extension SDK
onboarding remain later work.

## Relationship to default Open Brain

The product-family authority is [`../product-family.md`](../product-family.md), and the revised
milestone map is [`product-roadmap.md`](product-roadmap.md). Plain `open-brain` is a separate
five-minute default with automatic private data-directory setup, direct SQLite-backed capture and
search, Portable Brain export, and no required daemon, grants, certificates, key custody, or manual
database setup. It makes no application-level encryption claim.

All completed W0 work and all current W1 files are Secure Node work. Stable `M1-W*` identifiers stay
in schemas, receipts, release evidence, filenames, and historical commits. Current prose names the
same workstreams `SN1-W*`; this is an alias, not a replay or migration of the engineering work.

Portable Brain v1 is the shared minimum record and export boundary. Secure Node protocol and
BrainPack v2 may add advanced metadata, but a lossless mapping from the shared Portable Brain v1
semantic inventory must close before `SN1-W2` creates a second persistent canonical model.
The mapping work is governed by [`2026-09-07-core-w0-shared-portability.md`](2026-09-07-core-w0-shared-portability.md).

## Source of truth

The accepted M0 contracts define the semantics. In descending precedence they are:

1. Brain Protocol v1: exactly `commit`, `query`, `changes`, and `inspect`.
2. Brain-scoped identity, immutable records, explicit provenance, and monotonic cursors.
3. Digest-bound idempotency, explicit conflicts, append-only revisions, and unknown effects.
4. Short-lived one-Brain capability grants, epoch revocation, nonce replay protection, and
   authorization before body decoding or query candidate selection.
5. All-of compartment labels, label-union propagation, and spaces that never grant authority.
6. Owner-authorized erasure over the transitive provenance closure.
7. One sequencer per Brain with manual cold-transfer fencing.
8. Disposable projections rebuilt from ordered commits.
9. Secure Node keeps its ledger and FTS encrypted at rest and stores sensitive payloads in
   envelope-encrypted blobs with owner-erasable scoped keys.
10. BrainPack v2 semantics and exclusions. M1 preserves the required semantic state; the complete
   BrainPack v2 import/export product remains a later milestone.

Before implementation, W0 records a sanitized content-hash manifest for the exact accepted M0
decision bundle and publishes the generic decisions in this repository. The published public ADRs,
schemas, and conformance fixtures then become sufficient implementation authority; contributors do
not need the private composition repository. Accepted decisions M1-D8 through M1-D33 refine M1
implementation without weakening the M0 invariants.

The current public repository remains authoritative for package boundaries, v0 compatibility, P4
artifact policy, and verification commands.

The accepted Secure Node contract specifically requires a transactional SQLite ledger
inside a per-Brain encrypted store boundary, content-addressed blobs encrypted with scoped data
keys, and an encrypted-at-rest FTS5 projection. Secure Node cannot claim completion by
deferring those controls to deployment policy.

## Fixed boundaries

- `packages/engine` owns the transport-neutral protocol types, semantic transitions, persistence
  ports, and the reference local persistence implementation.
- `packages/app` composes Secure Node and its transport adapters. It cannot make app types part
  of the engine contract.
- `packages/connectors`, `packages/legacy`, P4 tooling, the six existing artifact coordinates, and
  the current `BrainEngine` compatibility facade stay behaviorally unchanged. M1 may add required
  dependency, license, SBOM, package-resource, and classification declarations only after the W0
  characterization tests prove the old entries and behavior remain intact.
- New M1 state uses a separate versioned namespace under a temporary or explicitly initialized
  Brain root. It does not reinterpret the existing v0 `phase1.sqlite3` database as a v1 ledger.
- The new Node has no default startup path. Tests use synthetic temporary roots. Local and HTTP
  transports remain loopback-only and explicitly constructed. A static loopback `/healthz` may
  report process liveness only; it is an operational probe, not a fifth semantic operation.
- Storage tables, FTS layout, key-provider implementation, and scheduler layout remain private.
  Stable surfaces are protocol values, operation meanings, errors, receipts, schemas, and public
  factory capabilities.

## Architecture choice

Use an additive architecture inside the existing distributions:

| Layer | Planned location | Stability |
|---|---|---|
| Protocol values and errors | `packages/engine/src/open_brain_engine/protocol/` | Public and versioned |
| Semantic transition kernel | `packages/engine/src/open_brain_engine/ledger/` | Internal until a second implementation proves the seam |
| SQLite, blob, key, and FTS adapters | `packages/engine/src/open_brain_engine/storage/` and `projections/` | Private reference implementation |
| Secure Node composition | `packages/app/src/open_brain/node/` | Opt-in public factory, private internals |
| Local and loopback HTTP transports | `packages/app/src/open_brain/node/transports/` | Protocol-conformant adapters |
| v0 compatibility | Existing engine/app modules | Preserved, not rewritten during M1 |

`open-brain-engine` takes the small maintained RFC 8785 dependency required by protocol semantics.
Native database, cryptography, and OS-secret integrations are declared in an engine `secure-node`
extra. The top-level `open-brain[secure-node]` extra selects it; plain `open-brain` must not. A user
who explicitly selects Secure Node does not need to enumerate its transitive dependencies. The app
also owns a high-level Secure Node client that
automates principal-key creation, request signing, safe retries, and query continuation while the
engine retains the low-level transport-neutral contract.

For this milestone, the selected packaging is the `open-brain[secure-node]` extra inside the
existing app distribution. A separate `open-brain-secure-node` distribution is the stronger final
isolation option and remains pending the dependency, CLI, artifact-policy, and release-cadence
checks in the product-family contract. Folding Secure Node behavior into the default
`BrainEngine` path would mix product profiles and is rejected.

## SN1-W0 (`M1-W0`): close Secure Node implementation decisions and freeze executable schemas

<!-- model: opus -->

Completion note: the independent audit accepted this work at its original M1 identifiers and
pre-split package names. ADR 0012 preserves that evidence, reclassifies it as Secure Node, and
moves the default-package changes into `OB1-W0`; Secure Node W0 is not replayed.

- [ ] Publish generic protocol and semantic ADRs in the public repository without private paths,
      topology, policy names, or capture bodies.
- [ ] Create a sanitized M0 source manifest containing stable decision IDs and content SHA-256
      values, then make the public ADRs and schemas sufficient on their own. Public artifacts cannot
      name private repository paths or require private files at build, test, install, or runtime.
- [ ] Define versioned JSON schemas for grants, request bindings, commit batches, records,
      provenance, revisions, decisions, purge transitions, receipts, query, changes, inspect,
      minimal durable jobs, query evidence, opaque query continuations, cold-transfer certificates,
      sequencer-stop proofs, typed resource-limit failures, and redacted security audit events.
- [ ] Define one canonical byte representation for request and commit digests. Prefer a maintained
      RFC 8785 implementation and declare it as the engine's required semantic dependency. Record
      wheel-only and source-install evidence for every advertised Python version and supported
      platform. If no maintained implementation passes the complete matrix, stop M1 before writing
      digest-dependent code. Do not hand-roll or silently substitute another JSON encoding inside
      an implementation wave.
- [ ] Choose generated identifier encodings and validate that they are opaque, Brain-scoped, and
      distinct by role. Content hashes must not be exposed as public identifiers.
- [ ] Verify the encrypted SQLite/FTS backend. The preferred architecture is SQLCipher or a vetted
      encrypted SQLite VFS plus envelope-encrypted payload blobs and an injected Brain key provider.
      A plain SQLite database that relies only on host volume encryption does not satisfy the Secure
      Node contract. Probe the complete CPython 3.12, 3.13, and 3.14 matrix on macOS ARM64 and
      Linux x86_64, including source and wheel installation, keyed reopen, wrong-key rejection,
      FTS5, purge residue, and crash recovery. Every combination is required; a failure stops M1
      for operator resolution instead of silently narrowing support. Windows remains unsupported
      in M1 and is an explicit later portability milestone. W0 verified the dependencies under the
      pre-split engine `node` extra. `OB1-W0` renames that extra to `secure-node` and makes only
      `open-brain[secure-node]` select it. Record the M1 matrix in `release/m1-compatibility.json`
      without restoring the deleted P4 compatibility matrix.
- [ ] Freeze the dependency and release-evidence strategy before modifying package metadata. Keep
      the declared Python range `>=3.12,<3.15`, and update `uv.lock`, wheel metadata, licenses, SBOM
      inputs, and packaged resources directly. A clean `pip install 'open-brain[secure-node]'`
      equivalent must install the full Secure Node without asking users to enumerate transitive
      extras or compile native code on a supported wheel target. The Secure Node application
      dependency set includes the minimal Starlette and Uvicorn ASGI adapter; the engine remains
      transport-neutral.
- [ ] Freeze a `RootKeyCustodian` port and its bootstrap, unlock, restart, rotation, and destruction
      semantics. The reference setup uses OS-backed secret storage when available and an audited,
      passphrase-encrypted root-key envelope as the portable fallback. Plaintext root keys inside a
      Brain root are forbidden, and an in-memory test provider cannot satisfy M1 acceptance.
      OS-backed storage alone does not prove human presence.
- [ ] Freeze the audited AEAD cipher suite, nonce allocation and uniqueness rules, data-key wrapping
      format, SQLCipher or encrypted-VFS KDF parameters, and cryptographic version markers. The
      passphrase envelope uses versioned Argon2id parameters, a unique 128-bit-or-larger salt,
      recorded minimum memory/time cost, throttled failures, and explicit migration rules. W0 starts
      from the RFC 9106 memory-constrained profile and may raise its cost only after measuring the
      supported matrix. These choices cannot be selected ad hoc by storage workers.
- [ ] Freeze grant issuance as an owner-authenticated local control capability outside the four
      protocol operations. Issuer signing keys use the root-key custodian, grants have explicit
      minimum and maximum TTLs, and grant bytes are secret material. No local, HTTP, MCP, or other
      protocol transport may mint or persist plaintext grants.
- [ ] Make interactive unlock create an in-memory owner-control session lasting no more than 15
      minutes. Narrow local grants may be minted during that session. Body-read, all-compartment,
      or maximum-TTL grant issuance, plus issuer/owner changes and key destruction, require a fresh
      user-presence challenge no older than 60 seconds. An OS-keyring read without an explicit
      platform user-presence result does not satisfy step-up authentication.
- [ ] Freeze a `UserPresenceProvider` port. Production uses LocalAuthentication on macOS when
      available and audited passphrase re-entry on Linux or headless macOS. Tests may inject a fake
      provider, but production configuration cannot select it. Out-of-process owner control uses a
      separate Unix-domain socket beneath a mode-`0700` runtime directory with a mode-`0600` socket,
      verifies the peer UID, binds each grant to the requesting principal public key, and returns
      grant bytes only in memory. Neither loopback HTTP nor any of the four protocol operations may
      expose this control capability.
- [ ] Bind every grant to an Ed25519 principal public key and require a principal signature over the
      canonical method, Brain, delivery identity, nonce, and body digest for every request. Publish
      language-neutral signature test vectors. A stolen serialized grant must fail under another
      key. Freeze a separate `PrincipalKeyCustodian` boundary for client credentials; it cannot
      access the Brain's root-key custodian or Node signing keys. The high-level reference client
      creates, stores, and uses its key automatically. It prefers the OS secret store, falls back to
      a passphrase-wrapped owner-only file, and may use an explicitly ephemeral session-only key.
      Freeze client-key rotation and principal-epoch revocation. M1 implements no bearer-grant
      fallback.
- [ ] Freeze the receipt trust chain. The active Node key signs receipts, the Brain owner authority
      signs that Node key's epoch certificate, and each receipt carries the signing-key identifier
      and certificate chain. Independent clients verify against a pinned owner fingerprint and the
      owner-signed public-key history; they never need Node internals or private configuration.
- [ ] Generate a reproducible synthetic multi-label corpus and freeze provisional limits for labels
      per record, active exact-label-set shards per Brain, authorized query fan-out, and records per
      shard. Record the generator distribution and benchmark results. No real or private Brain data
      may be read. A later authorized milestone may revise the provisional numbers from real usage.
- [ ] Freeze a grant-bound opaque query-continuation schema and deterministic shard traversal. Every
      first page binds a snapshot of the authorized active-shard inventory; later shard creation
      cannot move its finish line. Every request processes at most the accepted per-round fan-out,
      re-authorizes, and carries a bounded top-k accumulator. The continuation is integrity- and
      confidentiality-protected and exposes no shard identities, candidate IDs, scores, or corpus
      statistics. Intermediate results are explicitly provisional; deterministic rank-based fusion
      that uses no unauthorized corpus statistics produces the authoritative final result. Owners
      receive no unbounded exemption. The high-level client follows continuations automatically
      within the caller's budget while a low-level page API exposes continuation state. A purge
      touching any record in the bound snapshot invalidates the continuation; the high-level client
      restarts transparently and the low-level API returns the typed invalidation result.
- [ ] Freeze an executable query-evidence contract: required supporting record or accepted-revision
      IDs, provenance fields, validity rules, body visibility, and authorization behavior. A result
      that lacks authorized evidence cannot satisfy `query`, even if its text matches.
- [ ] Freeze bounds for commit batch cardinality, blob staging bytes, concurrent requests, and
      retained nonce rows, including per-principal accounting. Live nonce rows cannot be evicted
      before grant expiry; capacity fails closed. Every typed resource failure includes a stable
      code, plain-language message, retry safety, and a retry time or corrective action when known.
- [ ] Freeze the authorization clock model. Owner-session duration and step-up freshness use a
      monotonic in-process clock. Grant expiry uses persisted UTC high-water time plus a fixed skew
      allowance; backward wall-clock movement fails closed, and nonce pruning waits until expiry
      plus skew.
- [ ] Keep replay protection in a dedicated encrypted operational store outside the canonical
      ledger transaction. After grant, principal signature, request binding, and body digest are
      verified, reserve the nonce before executing the operation; the reservation never rolls back
      with a failed commit. Apply durable replay protection to all four operations, size read quotas
      for the maximum allowed query pages, and benchmark paged reads alongside commits.
- [ ] Freeze information-flow rules for operational surfaces. Payloads, plaintext digests, grants,
      and sensitive metadata never enter logs or errors; SQLite temporary and FTS scratch state is
      memory-only or inside the encrypted Brain boundary; and every permitted durable sink is named
      for purge verification.
- [ ] Freeze `inspect` visibility before implementation: authorize all required compartments before
      entity selection, make absent and unauthorized entities return the same typed not-found
      response content, and require explicit body scope before returning bodies or content-derived
      values. M1 makes no constant-time database guarantee and states that boundary in the threat
      model.
- [ ] Freeze `changes` isolation: authorize before entry selection, prevent unauthorized records
      from affecting page contents or cursor information, and re-authorize every page.
- [ ] Preserve ADR 0003's Brain, delivery identity, and digest idempotency tuple. Before replaying an
      original receipt, authorize the caller for the complete original commit and receipt scope.
      Changed-digest and revision conflicts are typed operation results, not standalone inspectable
      entities; policy may separately commit a conflict or quarantine record. Once an authorized
      purge destroys the delivery's sensitive material, erasure overrides receipt replay: retain an
      opaque delivery tombstone and return typed `delivery_purged` instead of the original receipt.
- [ ] Freeze the minimal durable-job contract for Node-owned long operations such as purge and
      projection rebuild: identity, restart-safe states, result/error references, compartment
      authorization, and inspection. A generic scheduler, extension runner, and public job-mutation
      operation remain outside M1.
- [ ] Freeze loopback HTTP posture: numeric loopback bind only, grant authorization on every
      protocol route, Host validation, CORS disabled, browser Origin rejected unless explicitly
      configured, and no remote enablement. Permit only a static anonymous `/healthz` response with
      no version, Brain, readiness, count, or configuration data. Implement the adapter with
      Starlette and Uvicorn from the application Node dependency set.
- [ ] Freeze query input as bounded literal user text compiled into safe FTS expressions. Protocol
      v1 does not expose raw SQLite FTS5 `MATCH` syntax.
- [ ] Keep projection work asynchronous to the canonical commit transaction. Query accepts an
      optional minimum ledger cursor and returns typed `projection_lag` with retry guidance and an
      opaque, grant-scoped query-visible watermark when the projector is behind. The internal
      Brain-wide checkpoint is never serialized; the visible watermark advances only for commits
      authorized under the request grant, so unauthorized commits cannot affect response metadata.
      The high-level client waits only within the caller's budget; low-level clients manage the
      minimum cursor and scoped watermark directly.
- [ ] Serialize the canonical commit digest only inside the signed receipt returned to the
      authorized committer that supplied those bytes. The digest is verification evidence, never a
      record identifier, lookup key, query result identifier, or bodyless `inspect` field.
- [ ] Freeze the minimum BrainPack-required semantic inventory: Brain and schema versions,
      compartments, spaces, record envelopes, available encrypted payload/blob references, links,
      tombstones, artifact revisions, proposals, decisions, effect receipts, commit order, and
      verifiable schema references. This is a storage contract, not a pack encoder.
- [ ] Define schema migration and crash-recovery rules for the new Node namespace.
- [ ] Default each Brain to a platform-local application-data root. Refuse known network
      filesystems and known synchronized roots; document unrecognized synchronization tools as
      unsupported. Bind the sequencer lease to a generated machine-instance ID, Node identity, and
      epoch. A copied identity on another machine remains write-paused until the accepted cold
      transfer advances the epoch.
- [ ] Add characterization tests that freeze the v0 engine facade, default native boundary, package
      dependency graph, and current public imports before adding M1 modules.
- [ ] Keep product ownership executable through direct AST import checks and native-module audits.
      Do not recreate a generated whole-repository classifier or artifact policy.
- [ ] Package the protocol schema trees through ordinary package metadata, maintain
      `release/m1-compatibility.json`, and update `docs/threat-model.md` for same-user loopback
      trust, application-controlled purge sinks, synchronized-root refusal, clock/skew limits, and
      the absence of constant-time storage claims. These shared files remain coordinator-owned.

Planned files:

- `docs/architecture/brain-protocol-v1.md`
- `docs/architecture/m0-contract-manifest.json`
- `docs/architecture/decisions/0001-brain-protocol-v1.md` through
  `docs/architecture/decisions/0009-brainpack-v2.md`
- `docs/architecture/decisions/0010-m1-wire-storage-and-crypto.md`
- `docs/architecture/decisions/0011-m1-authority-information-flow-and-resource-bounds.md`
- `docs/threat-model.md`
- `release/m1-compatibility.json`
- `packages/engine/pyproject.toml`
- `packages/app/pyproject.toml`
- `uv.lock`
- `packages/engine/src/open_brain_engine/protocol/`
- `packages/engine/src/open_brain_engine/protocol/schemas/v1/`
- `packages/engine/tests/contract/protocol_v1/`
- direct package, compatibility, and product-boundary tests

Focused verification:

```bash
uv run pytest -q packages/engine/tests/contract/protocol_v1 tests/security
uv run ruff check packages/engine/src/open_brain_engine/protocol \
  packages/engine/tests/contract/protocol_v1 tests/security
uv run mypy packages/engine/src/open_brain_engine/protocol \
  packages/engine/tests/contract/protocol_v1 tests/security
git diff --check
```

Stop gate: no ledger implementation starts until public source traceability, the complete required
support matrix, schema, digest, identifier, crypto and both key-custody boundaries, user presence,
owner-control delivery, proof-of-possession, receipt trust, clock and replay semantics, safe root
placement, changes and inspect isolation, minimal jobs, migration, fencing proof, resource and FTS
limits, literal query grammar, asynchronous projection visibility, continuation/ranking/evidence,
loopback HTTP posture, information flow, erasure-over-replay behavior, and BrainPack semantic
inventory are accepted and their negative tests exist.

## SN1-W1 (`M1-W1`): pure Secure Node semantic kernel

<!-- model: sonnet -->

Completion note: focused protocol and ledger verification plus repository-wide `make verify`
passed, and a fresh independent review accepted the complete work with no P0, P1, P2, or P3
findings. `CORE-W0` must close before `SN1-W2` begins.

- [x] Implement immutable Brain, record, artifact, proposal, decision, effect, purge, commit, and
      receipt state values without filesystem or transport dependencies.
- [x] Implement pure transition rules for atomic batch validation, delivery replay, changed-digest
      conflicts, superseding records, proposal base revisions, decision expected revisions,
      processor output identity, and unknown external effects.
- [x] Implement all-of compartment authorization and deterministic union propagation for every
      derived output.
- [x] Implement provenance graph validation and deterministic transitive descendant discovery.
- [x] Compute the proposed post-batch label cardinality and observed exact-label-set state during
      pure validation from an explicit authoritative active-set/count snapshot. If any record
      exceeds the frozen label limit or the batch would exceed the Brain's active-shard limit,
      reject the whole batch with a typed limit error before cursor allocation or durable mutation.
      The repository supplies and updates that snapshot in one writer transaction; projection state
      is never an input to commit validity.
- [x] Reject any new provenance reference to a tombstoned or purge-pending ancestor, including a
      reference racing with closure processing. A commit cannot extend a purge closure after the
      tombstone transition becomes authoritative.
- [x] Reject cross-Brain references, unscoped global cursors, label narrowing, space-derived
      authority, and in-place canonical mutation.
- [x] Apply ADR 0013's pre-release v1 erratum: commit-bound effect reconciliation, effect-receipt
      purge subjects, discriminated ledger-item references, exact purge-review intent,
      deterministic non-overlapping purge closure, provenance-bound origin supersession, complete
      loaded-state lifecycle and label-union validation, exact ordered batch-to-commit binding, and
      one-to-one commit/receipt/delivery evidence.

Planned files:

- `packages/engine/src/open_brain_engine/ledger/model.py`
- `packages/engine/src/open_brain_engine/ledger/transitions.py`
- `packages/engine/src/open_brain_engine/ledger/policy.py`
- `packages/engine/tests/unit/ledger/`

Focused verification:

```bash
uv run pytest -q packages/engine/tests/unit/ledger
uv run ruff check packages/engine/src/open_brain_engine/ledger packages/engine/tests/unit/ledger
uv run mypy packages/engine/src/open_brain_engine/ledger packages/engine/tests/unit/ledger
git diff --check
```

## SN1-W2 (`M1-W2`): transactional ledger, encrypted payloads, and blobs

<!-- model: sonnet -->

- [ ] Add a root-confined SQLite repository with explicit schema versioning, `BEGIN IMMEDIATE`
      transactions, foreign keys, durability settings, mode-`0700` roots, mode-`0600` files, and no
      symlink traversal across the database, WAL/SHM, encrypted key envelope, wrapped-key, blob,
      staging, cache, projection, and replay surfaces on the supported macOS and Linux matrix.
      Windows filesystem custody remains outside M1.
- [ ] Implement the `RootKeyCustodian` adapter boundary and encrypted-store bootstrap without
      writing a plaintext root key beneath the Brain root. Keep SQLite temporary storage and FTS
      scratch state in memory or inside the accepted encrypted boundary.
- [ ] Persist commit order, delivery bindings, receipts, immutable record envelopes, encrypted
      payload references, provenance edges, artifact revisions, proposals, decisions, effect
      receipts, tombstones, purge resolutions, minimal operation/job state, and authoritative active
      exact-label-set reference counts.
- [ ] In the initial coordinator-owned ledger schema, reserve revocation epoch, sequencer lease,
      fencing, purge invalidation, active label-set/count, minimal job, and redacted security-event
      state already required by later waves. Any later storage change is an explicit additive
      migration integrated by the coordinator, never an unreviewed rewrite by another worker.
- [ ] Implement a dedicated encrypted replay store under the same root confinement but outside the
      canonical ledger transaction. Its nonce reservation commits independently before an operation
      executes, survives operation rollback and restart, and remains operational state excluded
      from BrainPack semantics.
- [ ] Persist every field in the frozen BrainPack semantic inventory without importing grants,
      credentials, jobs, leases, checkpoints, indexes, host paths, or process state into that
      semantic model.
- [ ] Within one `BEGIN IMMEDIATE` transaction, read the authoritative active-label-set counts,
      validate the complete batch, update those counts, and allocate one cursor. A rollback leaves
      no records, count changes, delivery binding, receipt, cursor gap, blob promotion, or projection
      checkpoint. Concurrent requests cannot validate against the same stale capacity snapshot.
- [ ] Encrypt each payload under a scoped data key supplied through the accepted key-provider
      contract. Keep plaintext digests and sensitive metadata inside the encrypted envelope.
- [ ] Stage and verify content-addressed ciphertext blobs before transaction visibility. Reconcile
      interrupted staging without allowing the writer to certify its own durable bytes.
- [ ] Enforce the frozen commit, staging-disk, and retained-state limits before resource allocation.
      Audit storage contains metadata-only event codes and opaque actor/Brain references, never
      payloads, plaintext digests, grants, or sensitive envelope metadata.
- [ ] Retain every nonce through its grant's expiry plus the frozen skew allowance and never evict a
      live row to satisfy capacity. Apply bounded per-principal accounting and return the frozen
      typed fail-closed result when capacity is exhausted; expired rows may be pruned only against
      the persisted UTC high-water clock.
- [ ] Return the original receipt for the same Brain, delivery identity, and digest only after the
      service re-authorizes the caller for the complete original commit and receipt scope. Return a
      typed conflict for changed bytes under the same delivery identity; do not create an implicit
      inspectable conflict entity. If the delivery has an erasure tombstone, return typed
      `delivery_purged`; the original receipt is no longer replayable.

Planned files:

- `packages/engine/src/open_brain_engine/ledger/repository.py`
- `packages/engine/src/open_brain_engine/storage/node_sqlite.py`
- `packages/engine/src/open_brain_engine/storage/node_blobs.py`
- `packages/engine/src/open_brain_engine/storage/node_keys.py`
- `packages/engine/src/open_brain_engine/storage/key_custody.py`
- `packages/engine/src/open_brain_engine/storage/replay.py`
- `packages/engine/tests/integration/ledger/`

Focused verification:

```bash
uv run pytest -q packages/engine/tests/integration/ledger
uv run ruff check packages/engine/src/open_brain_engine/ledger packages/engine/src/open_brain_engine/storage/node_*.py packages/engine/src/open_brain_engine/storage/key_custody.py packages/engine/src/open_brain_engine/storage/replay.py packages/engine/tests/integration/ledger
uv run mypy packages/engine/src/open_brain_engine/ledger packages/engine/src/open_brain_engine/storage/node_sqlite.py packages/engine/src/open_brain_engine/storage/node_blobs.py packages/engine/src/open_brain_engine/storage/node_keys.py packages/engine/src/open_brain_engine/storage/key_custody.py packages/engine/src/open_brain_engine/storage/replay.py packages/engine/tests/integration/ledger
git diff --check
```

## SN1-W3 (`M1-W3`): capability authorization, request binding, and fencing

<!-- model: opus -->

- [ ] Implement owner authority, epoch-bound issuer certificates, short-lived one-Brain grants,
      operation/schema/body/compartment limits, expiry, policy digests, and principal/policy
      revocation epochs.
- [ ] Keep owner and issuer signing keys behind `RootKeyCustodian`. Mint grants only through an
      owner-authenticated local control capability that is separate from `commit`, `query`,
      `changes`, and `inspect`; enforce the frozen TTL bounds. Protocol services and transports have
      no issuance route and cannot self-escalate. The separate owner-control Unix socket verifies
      the peer UID, accepts the requesting principal public key, and returns the bound grant only
      in memory.
- [ ] Enforce the frozen owner-session policy. Interactive unlock creates only a bounded in-memory
      control session; high-risk grants require a fresh explicit user-presence result. Treat a
      successful background OS-keyring read as key availability, not as proof that the owner is
      present. Use the production `UserPresenceProvider`; a fake provider is test-injected and
      cannot be selected by production configuration.
- [ ] Bind each grant to its principal's Ed25519 public key. Verify the principal signature over the
      canonical request binding before body decoding, and reject a valid grant paired with any
      other key. Keep private-key generation, custody, and signing automatic in the high-level
      reference client through `PrincipalKeyCustodian` while publishing language-neutral vectors
      for low-level clients. Enforce client-key rotation and principal-epoch revocation without
      giving the client access to Brain root or Node signing keys.
- [ ] Treat serialized grants as credentials: never include them in logs, errors, durable client
      storage, receipts, or audit events. Add negative tests for issuance and privilege escalation
      attempts through every protocol transport.
- [ ] Verify grant and current epoch before accepting body bytes for processing. Stream a bounded
      body to its declared digest, verify method/Brain/delivery/nonce/digest binding, then decode.
- [ ] After grant, principal signature, request binding, and body digest verification, reserve each
      nonce durably in the separate encrypted replay store before decoding or executing the
      operation. Reject reuse across all four operations. The reservation survives operation
      failure; a retry uses a new nonce and preserves the stable delivery identity. Capacity is
      bounded per principal, live rows are never evicted, and exhaustion returns the typed retry
      guidance frozen in W0.
- [ ] Bind receipts to commit digest, cursor, issuer epoch, and policy digest. Return the canonical
      commit digest only in the signed receipt to the authorized committer; reject its use as a
      public entity identifier, lookup key, query result identifier, or bodyless inspect value.
      Re-authorize the complete original scope before returning that receipt on an idempotent retry.
      Sign with the active Node key, attach its owner-signed epoch certificate and key identifier,
      and verify through the pinned owner fingerprint and signed public-key history.
- [ ] Emit metadata-only security events for authorization denials, revocations, replay rejection,
      fencing transitions, and key destruction. Event serialization cannot contain grant bytes,
      payload material, plaintext digests, or sensitive envelope metadata.
- [ ] Implement manual cold-transfer certificates with strict epoch increase, next Node key, prior
      ledger head, and the frozen sequencer-stop proof contract. The M1 reference implementation
      supports same-storage-root transfer only, using an identity-bound exclusive sequencer lease
      and an injected, independently validated stop-proof capability. A caller-provided boolean is
      not proof.
- [ ] Implement the same-Node lease with a process-held OS lock that releases automatically on
      process death plus the durable machine-instance ID, Node identity, and epoch record. Reject
      known network filesystems and synchronized roots before acquiring the lease. A restart of the
      same identity on the same machine instance at the current epoch may reacquire the released OS
      lock; a copied identity or a different Node remains paused until the accepted cold-transfer
      proof advances the epoch.
- [ ] Return a typed paused/unavailable result for cross-machine transfer until a separately
      reviewed attestation or key-destruction proof provider exists. Never infer remote process
      death or activate a new sequencer from operator intent alone.
- [ ] Keep loopback as the default. Remote TLS enablement and device enrollment refresh UX remain
      outside M1.

Planned files:

- `packages/engine/src/open_brain_engine/protocol/authorization.py`
- `packages/engine/src/open_brain_engine/ledger/fencing.py`
- `packages/engine/tests/security/protocol_v1/`

Focused verification:

```bash
uv run pytest -q packages/engine/tests/security/protocol_v1
uv run ruff check packages/engine/src/open_brain_engine/protocol/authorization.py packages/engine/src/open_brain_engine/ledger/fencing.py packages/engine/tests/security/protocol_v1
uv run mypy packages/engine/src/open_brain_engine/protocol/authorization.py packages/engine/src/open_brain_engine/ledger/fencing.py packages/engine/tests/security/protocol_v1
git diff --check
```

## SN1-W4 (`M1-W4`): commit, changes, and inspect

<!-- model: sonnet -->

- [ ] Implement `commit` as the only mutation and route every typed transition through the kernel
      and one SQLite transaction.
- [ ] Preserve Brain/delivery/digest idempotency. Authorize the original commit and receipt scope
      before replaying a receipt; return changed-digest and revision conflicts as typed operation
      results rather than standalone inspectable entities. When an authorized purge has replaced
      the delivery binding with an opaque erasure tombstone, return typed `delivery_purged` instead
      of recovering or replaying the old receipt.
- [ ] Implement `changes` as a Brain-local ordered feed with opaque cursors, stable pagination,
      authorized filters, and projector/processor-safe replay. Authorize before selecting entries,
      prevent unauthorized records from affecting page contents or cursor information, and
      re-authorize every page.
- [ ] Implement `inspect` only for the accepted record, operation, minimal durable-job, and receipt
      references. Purge progress is represented by its authorized operation/job state. Conflict is
      a typed operation outcome unless policy explicitly committed a separate conflict or quarantine
      record.
- [ ] Before selecting an inspect target, verify the grant and all required compartments. Return the
      same typed not-found result for an absent target and an unauthorized target, expose no body or
      content-derived identifier without explicit body scope, and prove the behavior with
      inspect-isolation conformance and security tests.
- [ ] Keep the typed `query` request and response contract frozen but do not expose a complete Node
      service yet. Query closes only after W5 supplies its real authorized projection dependency.

Planned files:

- `packages/engine/src/open_brain_engine/protocol/service.py`
- `packages/engine/src/open_brain_engine/ledger/service.py`
- `packages/engine/tests/contract/protocol_v1/test_commit_changes_inspect.py`
- `packages/engine/tests/integration/ledger/test_protocol_service.py`
- `packages/engine/tests/security/protocol_v1/test_changes_isolation.py`
- `packages/engine/tests/security/protocol_v1/test_inspect_isolation.py`

Focused verification:

```bash
uv run pytest -q packages/engine/tests/contract/protocol_v1/test_commit_changes_inspect.py packages/engine/tests/integration/ledger/test_protocol_service.py packages/engine/tests/security/protocol_v1/test_changes_isolation.py packages/engine/tests/security/protocol_v1/test_inspect_isolation.py
uv run ruff check packages/engine/src/open_brain_engine/protocol packages/engine/src/open_brain_engine/ledger packages/engine/tests/contract/protocol_v1/test_commit_changes_inspect.py packages/engine/tests/integration/ledger/test_protocol_service.py packages/engine/tests/security/protocol_v1/test_changes_isolation.py packages/engine/tests/security/protocol_v1/test_inspect_isolation.py
uv run mypy packages/engine/src/open_brain_engine/protocol packages/engine/src/open_brain_engine/ledger packages/engine/tests/contract/protocol_v1/test_commit_changes_inspect.py packages/engine/tests/integration/ledger/test_protocol_service.py packages/engine/tests/security/protocol_v1/test_changes_isolation.py packages/engine/tests/security/protocol_v1/test_inspect_isolation.py
git diff --check
```

## SN1-W5 (`M1-W5`): compartment-safe FTS, projection rebuild, and query

<!-- model: opus -->

- [ ] Project accepted, non-suppressed records and artifact heads from ordered commits only. Keep
      projection work asynchronous to the canonical commit transaction. Keep the Brain-wide
      projector checkpoint internal and publish only an opaque query-visible watermark bound to the
      request grant and derived from commits visible to that grant.
- [ ] Partition FTS by observed exact canonical compartment-label set, not by one label
      independently. Create a shard only when that exact set occurs. Never pre-create all `2^N`
      theoretical combinations. Derive shard creation and retirement from the ledger's
      authoritative active-label-set reference counts; projection state never decides whether a
      commit is legal.
- [ ] Before issuing any search, select only observed shards whose required labels are a subset of
      the caller's compartments. Bind the first page to a stable snapshot, traverse it in the frozen
      deterministic order, and process no more than the W0 per-round fan-out. Return an opaque,
      grant-bound continuation with a bounded top-k accumulator while authorized shards remain,
      re-authorize every page, and provide no unbounded owner exemption. Mark intermediate rankings
      provisional and produce the authoritative final ranking with the frozen rank-based fusion
      rule, never with statistics from unauthorized shards. Test synthetic multi-label isolation,
      snapshot stability, continuation, ranking, and performance at the accepted provisional
      limits. If purge changes any record bound to an active snapshot, invalidate its continuation;
      never return cached candidates from the stale snapshot.
- [ ] Keep one projector checkpoint per Brain and projection version. Checkpoints are operational
      state, never enter BrainPack semantic data, and are never serialized as query metadata.
- [ ] Rebuild an empty projection from cursor zero and prove byte- or result-equivalence after
      normalization.
- [ ] Implement `query` over authorized accepted artifact heads and supporting records only after
      the shard selector exists. Apply authorization before candidate selection, ranking, counts,
      snippets, or result metadata. Results carry evidence references and never expose unauthorized
      bodies or content-derived identifiers. Enforce the W0 evidence contract for every result:
      supporting record or accepted-revision identity, permitted provenance, validity, and body
      visibility must all be verifiable. Treat query text as bounded literal text compiled into safe
      FTS expressions; reject raw `MATCH` grammar as protocol input. Accept an optional minimum
      ledger cursor and return typed `projection_lag` with retry guidance and the grant-scoped
      query-visible watermark when the projector is behind. A commit outside the grant's authorized
      scope cannot advance or otherwise alter that visible watermark.
- [ ] Treat generated Markdown as a later projector client. M1 does not make existing canonical
      Markdown an authority or directly rewrite it.

Planned files:

- `packages/engine/src/open_brain_engine/projections/fts.py`
- `packages/engine/src/open_brain_engine/projections/rebuild.py`
- `packages/engine/tests/integration/projections/`
- `packages/engine/tests/contract/protocol_v1/test_query.py`
- `packages/engine/tests/contract/protocol_v1/test_query_continuation_and_evidence.py`
- `packages/engine/tests/security/protocol_v1/test_query_isolation.py`

Focused verification:

```bash
uv run pytest -q packages/engine/tests/integration/projections packages/engine/tests/contract/protocol_v1/test_query.py packages/engine/tests/contract/protocol_v1/test_query_continuation_and_evidence.py packages/engine/tests/security/protocol_v1/test_query_isolation.py
uv run ruff check packages/engine/src/open_brain_engine/projections packages/engine/tests/integration/projections packages/engine/tests/contract/protocol_v1/test_query.py packages/engine/tests/contract/protocol_v1/test_query_continuation_and_evidence.py packages/engine/tests/security/protocol_v1/test_query_isolation.py
uv run mypy packages/engine/src/open_brain_engine/projections packages/engine/tests/integration/projections packages/engine/tests/contract/protocol_v1/test_query.py packages/engine/tests/contract/protocol_v1/test_query_continuation_and_evidence.py packages/engine/tests/security/protocol_v1/test_query_isolation.py
git diff --check
```

## SN1-W6 (`M1-W6`): provenance-closed certified purge

<!-- model: opus -->

- [ ] Append the owner-authorized tombstone before cleanup and suppress the target plus its full
      transitive descendant set immediately in query and projection work.
- [ ] Once the tombstone or purge-pending transition is authoritative, reject every new commit that
      cites any member of that ancestry. Prove a concurrent or delayed descendant commit cannot
      extend the closure after it is calculated or after a purge receipt is signed.
- [ ] Require an explicit purge, retain, or owner-reviewed replacement resolution for every closure
      member. Silence never retains a descendant. `PURGE` destroys that member's wrapped keys and
      blobs and removes its searchable reference counts. `RETAIN` preserves its encrypted material
      under the explicit decision and keeps it suppressed unless that decision explicitly restores
      visibility. `REPLACE` purges the old payload and makes only an owner-reviewed replacement that
      no longer contains the target data eligible for projection; reject or keep suppressed any
      replacement that fails that validity check.
- [ ] Destroy wrapped payload keys, remove ciphertext blobs, invalidate caches and projections, and
      rebuild affected FTS shards. Apply the accepted backend's secure-delete equivalent to any
      sensitive working table, then checkpoint and truncate WAL or journal residue and perform the
      required compaction before cleanup can pass.
- [ ] Transactionally decrement active exact-label-set reference counts as searchable records leave
      the closure. Retire a zero-reference FTS shard only after canonical suppression is durable;
      projection loss or rebuild cannot change the authoritative counts.
- [ ] Prohibit payloads, plaintext digests, and sensitive metadata in application logs and error
      reports. Enumerate database, WAL/journal, FTS, cache, blob, staging, log, error-report, and
      temporary/scratch locations as the complete permitted durable-sink set; any new sink requires
      an ADR update and a purge test before use.
- [ ] Issue the final purge receipt only after the closure has no unresolved member and a separate
      root-confined reader proves logical and physical cleanup. For encrypted database, journal,
      FTS, and blob surfaces, prove decryption failure or key absence plus keyed structural absence;
      readable-byte scans alone do not prove cryptographic erasure. Scan plaintext canaries only in
      legitimate plaintext sinks such as logs, errors, temporary files, and staging. Memory-only
      scratch state must be gone after process teardown. M1 certifies only application-controlled
      sinks; the threat model explicitly leaves OS swap, hibernation, core dumps, and externally
      configured crash-report retention to deployment policy.
- [ ] Preserve only the opaque immutable audit transitions and tombstones required for BrainPack v2.
      They must contain no plaintext digest or sensitive metadata from the purged envelope.
- [ ] Replace each purged delivery binding with an opaque erasure tombstone. A retry returns typed
      `delivery_purged` and cannot recover the original receipt. Invalidate any query continuation
      whose bound snapshot included a purged record; low-level clients receive the typed result and
      the high-level client restarts within its caller budget.

Planned files:

- `packages/engine/src/open_brain_engine/ledger/purge.py`
- `packages/engine/tests/integration/ledger/test_purge.py`
- `packages/engine/tests/integration/ledger/test_purge_concurrency.py`
- `packages/engine/tests/security/protocol_v1/test_purge_closure.py`

Focused verification:

```bash
uv run pytest -q packages/engine/tests/integration/ledger/test_purge.py packages/engine/tests/integration/ledger/test_purge_concurrency.py packages/engine/tests/security/protocol_v1/test_purge_closure.py
uv run ruff check packages/engine/src/open_brain_engine/ledger/purge.py packages/engine/tests/integration/ledger/test_purge.py packages/engine/tests/integration/ledger/test_purge_concurrency.py packages/engine/tests/security/protocol_v1/test_purge_closure.py
uv run mypy packages/engine/src/open_brain_engine/ledger/purge.py packages/engine/tests/integration/ledger/test_purge.py packages/engine/tests/integration/ledger/test_purge_concurrency.py packages/engine/tests/security/protocol_v1/test_purge_closure.py
git diff --check
```

## SN1-W7 (`M1-W7`): Secure Node setup and local service operation

<!-- model: sonnet -->

- [ ] Add an explicit local setup factory that creates one Brain, its owner authority, key namespace,
      ledger, blob root, grant namespace, cursor, and projection namespace in a fresh root. Default
      to the platform-local application-data directory, reject known network filesystems and known
      synchronized roots, and explain that unrecognized synchronization tools are unsupported.
- [ ] Provision and unlock that Brain through the production `RootKeyCustodian`: prefer OS-backed
      secret storage and support the audited passphrase-encrypted fallback. Require explicit unlock
      after setup and restart, reject plaintext root keys inside the Brain root, and do not count the
      in-memory test custodian toward acceptance. A keyring read does not count as human presence;
      the bounded in-memory owner session and high-risk step-up policy from W0 govern issuance. Use
      LocalAuthentication on supported macOS hosts and audited passphrase re-entry on Linux or
      headless macOS through the production `UserPresenceProvider`.
- [ ] Expose grant issuance only through the owner-authenticated local control capability. The local
      and HTTP protocol adapters must prove that no route can mint a grant or raise its privileges,
      and no adapter may durably retain grant bytes. Serve owner control on a separate Unix-domain
      socket in an owner-only runtime directory, validate its mode and peer UID, bind the response
      to the requesting principal public key, and return it only in memory.
- [ ] Add a high-level Secure Node client that generates and stores its Ed25519 principal key through
      the separate `PrincipalKeyCustodian`, requests grants through the owner control flow, signs
      request bindings, safely retries stable deliveries, and follows query continuations
      automatically. The client never receives a Brain root or issuer key. Keep a low-level page API
      for independent clients, and publish deterministic vectors rather than requiring other
      languages to import Python internals. Prefer the OS secret store for principal keys, support a
      passphrase-wrapped owner-only fallback and explicit ephemeral keys, handle key rotation and
      principal-epoch revocation, verify the owner-to-Node receipt chain, restart purge-invalidated
      continuations, and wait for a requested grant-scoped projection watermark only within the
      caller's budget.
- [ ] Compose the four operations behind one Secure Node boundary. The Node receives protocol
      bytes and grants, not v0 task objects or direct extension imports.
- [ ] Add an in-process/local transport and a loopback-only HTTP adapter with identical conformance
      cases. Implement the ASGI boundary with Starlette and Uvicorn. Bind numeric loopback addresses
      only, require grants for all four protocol routes, validate Host, disable CORS, and reject
      browser Origin unless explicitly configured. The only anonymous route is static `/healthz`
      process liveness with no version, Brain, readiness, count, or configuration data. No listener
      starts at install, import, or ordinary v0 execution.
- [ ] Compose the minimal restart-safe job store only for Node-owned long operations such as purge
      and projection rebuild. Jobs are inspectable under their required compartments; M1 adds no
      generic scheduler, extension execution, or public job-mutation operation.
- [ ] Prove `open-brain[secure-node]` selects the engine `secure-node` extra on every supported
      wheel target while plain `open-brain` excludes it. Include a short synthetic Python example
      that initializes one
      Brain and exercises all four operations without private configuration or manual cryptography.
      Polished Secure Node CLI/UI onboarding remains later work.
- [ ] Keep Secure Node MCP disabled. A later Secure Node workstream may add named scopes and setup.
- [ ] Prove restart, interrupted response replay, busy writer, stale epoch, projection loss, and
      clean-root initialization behavior with synthetic fixtures. Enforce the frozen concurrent
      request and staging limits under those cases.

Planned files:

- `packages/app/src/open_brain/node/application.py`
- `packages/app/src/open_brain/node/client.py`
- `packages/app/src/open_brain/node/client_keys.py`
- `packages/app/src/open_brain/node/owner_control.py`
- `packages/app/src/open_brain/node/setup.py`
- `packages/app/src/open_brain/node/user_presence.py`
- `packages/app/src/open_brain/node/transports/local.py`
- `packages/app/src/open_brain/node/transports/http.py`
- `packages/app/tests/integration/node/`
- `packages/app/tests/integration/node/test_reference_client.py`
- `packages/app/tests/integration/node/test_http_boundary.py`

Focused verification:

```bash
uv run pytest -q packages/app/tests/integration/node
uv run ruff check packages/app/src/open_brain/node packages/app/tests/integration/node
uv run mypy packages/app/src/open_brain/node packages/app/tests/integration/node
git diff --check
```

## SN1-W8 (`M1-W8`): compatibility, conformance, and closure

<!-- model: opus -->

- [ ] Run a protocol conformance matrix covering atomicity, replay, digest conflicts, grant expiry,
      clock regression, proof-of-possession, principal-key rotation, receipt trust, revocation, nonce
      binding/capacity/rollback, label union, cross-Brain denial, purge/retain/replacement resolution
      and rejection of replacements that retain target data,
      concurrent purge closure, post-purge delivery retry, revision conflicts, cursor and changes
      isolation, commit-time label/shard limits, stable-snapshot query continuation and purge
      invalidation, final rank fusion, query evidence, literal query parsing, projection lag and
      grant-scoped watermark isolation from unauthorized commits,
      inspect isolation, minimal jobs, FTS isolation, projection rebuild, unsafe-root refusal,
      fencing, and restart recovery.
- [ ] Verify existing v0 CLI, engine tasks, installed wheels, connectors, and legacy quarantine
      remain unchanged in behavior. Package protocol schemas through ordinary project metadata and
      prove the Open Brain boundary with direct import and native-module tests. Do not restore the
      Phase 4 classifier, artifact policy, move reports, or six-artifact release matrix.
- [ ] Verify a clean engine/app source or wheel install can initialize and exercise one synthetic
      Brain through all four operations without the private composition repository. Test every
      required CPython 3.12, 3.13, and 3.14 combination on macOS ARM64 and Linux x86_64. Prove the
      `open-brain[secure-node]` installs its full Secure Node dependencies without a user enumerating
      transitive extras or compiling native code on a supported wheel target, while plain
      `open-brain` excludes them. Record the complete result in
      `release/m1-compatibility.json`; do not restore P4 evidence or claim Windows support.
- [ ] Prove every item in the frozen BrainPack semantic inventory survives commit and restart,
      records purge state correctly, and remains independent of projection rebuild.
- [ ] Prove clean setup and restart with a production root-key custodian, owner-authenticated grant
      issuance outside all protocol transports, bounded owner sessions, high-risk step-up,
      Argon2id envelope parameters, proof-of-possession, TTL enforcement, grant secrecy, and
      rejection of a plaintext Brain-root key. A background keyring read cannot satisfy the
      user-presence test. Prove macOS LocalAuthentication and Linux/headless passphrase adapters,
      production rejection of the fake provider, owner-control socket mode and peer checks,
      principal-key custody fallbacks, client-key rotation, and principal-epoch revocation.
- [ ] Prove the resource ceilings, redacted security-event trail, cryptographic erasure checks, and
      keyed structural cleanup across every encrypted durable sink. Use readable-byte canary scans
      for legitimate plaintext sinks. Tests must fail if payloads, grants, plaintext digests, or
      sensitive metadata reach logs, errors, temporary files, or audit events.
- [ ] Prove the high-level reference client hides signing, safe retries, and query continuation,
      while its low-level API and language-neutral vectors permit independent implementations.
      It also verifies the receipt trust chain, waits for requested grant-scoped projection
      watermarks, and
      restarts purge-invalidated queries within the caller budget. Every fail-closed result carries
      a stable code, plain explanation, retry safety, and available corrective guidance.
- [ ] Prove loopback HTTP exposes only the four authorized protocol routes plus the static anonymous
      `/healthz`; Host/Origin, CORS, non-loopback, unauthenticated metadata, grant issuance, and
      privilege-escalation negative tests all pass.
- [ ] Run an independent security and contract review. Resolve every P0-P2 finding before closure.
- [ ] Update public architecture documentation and the root instruction file only for verified M1
      behavior. Update `docs/threat-model.md` with same-user loopback trust, application-controlled
      purge sinks, synchronized-root refusal, clock/skew limits, and the absence of constant-time
      storage guarantees. Do not describe M2 features as implemented.

Focused verification:

```bash
uv run pytest -q packages/engine/tests/contract/protocol_v1 packages/engine/tests/integration/ledger packages/engine/tests/integration/projections packages/engine/tests/security/protocol_v1 packages/app/tests/integration/node
make verify
git diff --check
```

Required semantic-inventory test:

- `packages/engine/tests/contract/protocol_v1/test_brainpack_semantic_inventory.py`

## Commit and review rules

1. Each wave starts from a clean local checkpoint. Concurrent workers receive disjoint write scopes;
   that disjointness applies within the active wave, not permanently across later serial waves.
2. Focused tests are written red first, then implementation makes them pass.
3. Shared schemas, repository adapters, public facades, projection hooks, package metadata,
   `uv.lock`, and architecture docs remain coordinator-owned throughout M1. Workers return proposed
   additions or patches for those surfaces; the coordinator integrates them serially as explicit
   additive migrations or compatible API changes. Deleted Phase 4 release inventories must not be
   recreated.
4. `make verify` runs only from the coordinator after integration. Independent reviewers may run
   focused checks but cannot mutate files or certify their own fixes.
5. Every local commit uses a commit-message file. Nothing is pushed, published, deployed, started,
   redirected, or connected to live data under this plan.

## Secure Node M1 success criteria

Secure Node M1 is complete only when all of the following are true:

- A clean `open-brain[secure-node]` source or wheel install includes the Secure Node
  dependencies, can initialize one synthetic local Brain, and can use all four protocol operations
  through a documented high-level client without private files or manual cryptography. The complete
  CPython 3.12 through 3.14 matrix passes on macOS ARM64 and Linux x86_64 and is recorded in the M1
  compatibility record; M1 makes no Windows-support claim.
- Plain `open-brain` neither installs nor imports Secure Node-only dependencies, initializes no
  Secure Node state, starts no listener or daemon, and makes no application-encryption claim.
- One Brain has one transactionally ordered commit history, opaque cursor, grant namespace, key
  namespace, receipt namespace, and fenced sequencer epoch.
- Secure Node encrypts its SQLite ledger and FTS at rest and stores sensitive payloads in
  envelope-encrypted blobs whose scoped wrapped keys can be destroyed. A production root-key
  custodian provisions and unlocks the Brain without storing its plaintext root key in the Brain
  root. The passphrase fallback records the accepted Argon2id parameters and owner-only root/file
  permissions. The client principal key uses OS-secret, passphrase-wrapped, or explicit ephemeral
  custody without crossing into Brain root-key or Node-signing-key custody.
- Automated tests prove atomicity, idempotency, digest conflict, revocation, anti-replay binding,
  proof-of-possession, grant issuance isolation, clock regression, durable nonce reservation across
  rollback, changes and inspect isolation, all-of label enforcement, label union, transactional
  label/shard bounds, concurrent provenance-closed purge, purge/retain/replacement outcomes and
  rejection of replacements that retain target data,
  revision checks, cursor ordering, stable-snapshot query continuation and purge invalidation,
  literal query compilation, projection lag, final rank fusion, query evidence, minimal-job restart,
  FTS isolation, and projection rebuild.
- Purge tests prove decryption failure or key absence and keyed structural cleanup for encrypted
  database, journal, FTS, cache, and blob surfaces. Plaintext canaries are absent from staging,
  logs, error reports, and temporary files, while the permitted opaque audit transition and
  delivery-erasure tombstone remain.
- The frozen BrainPack semantic inventory survives commit, restart, purge, and projection rebuild
  without treating jobs, grants, checkpoints, indexes, or host state as semantic data.
- FTS uses only observed exact-label-set shards, rejects commits that would exceed frozen label or
  active-shard limits, retires zero-reference shards after purge, and pages broad authorized queries
  through bounded deterministic rounds without allowing unauthorized corpus statistics to affect
  results or exempting owners from bounds. Provisional limits come from the tracked synthetic
  generator and benchmark, never private data.
- Canonical commits remain bounded independently of projection work. Query can require a minimum
  ledger cursor, reports typed `projection_lag` with an opaque grant-scoped query-visible watermark,
  and treats input as bounded literal text rather than raw FTS5 syntax. The internal Brain-wide
  checkpoint is never exposed, and unauthorized commits cannot alter visible watermark metadata.
  The high-level client provides deterministic read-your-writes waiting within a caller-supplied
  budget.
- No unauthorized record influences change pages or cursors, query candidates, ranking, counts,
  snippets, metadata, evidence, or exposed bodies. `inspect` response content cannot distinguish an
  absent entity from one the caller lacks authority to see; no constant-time storage claim is made.
- Owner and issuer keys remain under root-key custody; grant issuance is owner-authenticated and
  unavailable through protocol transports; bounded owner sessions and high-risk step-up are
  enforced; grants are principal-key-bound and never enter plaintext durable storage, logs, errors,
  receipts, or audit events. Production user-presence adapters are achievable on supported hosts,
  owner-control delivery is peer-checked and memory-only, and the active Node signs receipts through
  an owner-signed epoch chain independently verifiable from a pinned owner fingerprint.
- Commit, staging-disk, concurrent-request, and nonce-retention limits fail with typed results, and
  metadata-only security events record denials, revocations, replay rejection, fencing, and key
  destruction without recording sensitive material. Errors state a stable code, retry safety, and
  corrective action when available; live nonces are never evicted early. Replay reservations for
  all four operations survive restart and operation rollback without becoming canonical BrainPack
  state.
- Brain roots default to local application data and reject known network or synchronized storage.
  The sequencer lease binds machine instance, Node identity, and epoch; copied identities remain
  write-paused until a cold transfer advances the epoch.
- Loopback HTTP exposes the four authorized protocol routes plus only static anonymous process
  liveness at `/healthz`; it rejects non-loopback binding, unexpected Host/Origin, browser CORS,
  unauthenticated metadata, grant issuance, and privilege escalation.
- The public ADRs, schemas, examples, conformance vectors, and sanitized source manifest are
  sufficient for contributors and independent clients without access to the private composition
  repository. Artifact policy tracks the packaged schema trees, the M1 compatibility record is
  complete, and the public threat model states the accepted local and erasure boundaries.
- The completed v0 and P4 artifact evidence remains green as regression evidence. Its historical
  appliance boundary does not redefine the default Open Brain product.
- Full verification and independent review are green on the exact clean local commit.

## Explicitly out of scope

- Extension SDKs, sandbox drivers, conformance for third-party extensions, and generic collectors.
- Default-product CLI/UI/MCP migration to the new protocol.
- Caleb-specific Brains, grants, placement, source mappings, outboxes, or policy.
- Legacy importers, private capture bodies, real source inventory, or live query parity.
- Deployment, package publication, public listeners, producer redirection, cutover, or retirement.
- Windows filesystem custody, user-presence integration, locking, packaging, and support claims.
- Automatic failover, concurrent canonical writers, active-active replication, or CRDT merge.
- Complete BrainPack v2 import/export, backup, restore, DMG, notarization, or hosted operation.

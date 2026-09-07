# Codebase vs Plan Audit: M1-W0 executable protocol freeze

**Date:** 2026-09-07

**Plan:** `docs/plans/2026-09-04-m1-semantic-kernel-node.md`, lines 106-310

**Mode:** strict

**Commits reviewed:** `f1e3ceb..9945e48` plus the calendar-validation working diff

**Codebase root:** repository root

## Executive summary

- **Completion:** 100% (35 of 35 W0 requirements complete after local remediation)
- **Ship readiness:** NOT READY until the latest remediation passes independent rereview
- **W1 gate:** Locked until the independent rereview returns READY
- **Independent findings at `f23b4ce`:** two P1 and two P2 findings, all remediated locally
- **Independent finding at `5f1395a`:** one P2 timestamp-precision defect, remediated locally
- **Independent finding at `9945e48`:** one P2 timestamp-completeness defect, remediated locally
- **Current verification:** full `make verify` passed with Ruff, strict MyPy over 566 source files,
  3,364 tests, six Python artifacts, and the artifact-policy gate. The focused 193-test review set
  also passed.

The first independent review rejected commit `a6d92a9` with three P1, five P2, and three P3
findings. The remediation closes each finding in executable schemas, semantic validators,
mutation tests, reproducible evidence, or documentation. This report is the local strict audit,
not the independent gate result. A second independent review rejected `f23b4ce` with two P1 and
two P2 findings. The current working diff addresses those findings, but only a fresh independent
READY verdict can unlock W1. The follow-up review of `5f1395a` cleared all four and found one new
P2 caused by submicrosecond RFC 3339 values being truncated during temporal comparison. The current
working diff closes that precision ambiguity. The next review of `9945e48` proved the fix but found
that a trailing line terminator and impossible dates still reached contracts without temporal
comparisons. The latest diff validates every schema-declared timestamp while leaving opaque bodies
untouched.

## Requirement audit

### R1: Publish sanitized public protocol and semantic ADRs

- **Status:** COMPLETE
- **Evidence:**
  - `docs/architecture/decisions/0001-brain-protocol-v1.md:1` through
    `docs/architecture/decisions/0011-m1-authority-information-flow-and-resource-bounds.md:1`
    provide the public decision set.
  - `tests/contract/test_m0_source_traceability.py:105`
    scans the decoded public lineage for private location markers.
- **Notes:** No private path, topology, policy name, or capture body is required by the public
  contract.

### R2: Bind the accepted M0 source bundle by stable IDs and hashes

- **Status:** COMPLETE
- **Evidence:**
  - `docs/architecture/m0-contract-manifest.json:1`
    records nine ordered decision IDs and exact source/public SHA-256 values.
  - `tests/contract/test_m0_source_traceability.py:64`
    rejects duplicate keys; line 71 verifies exact public ADR bytes.
- **Notes:** Build, test, install, and runtime do not read a private repository.

### R3: Freeze the complete versioned JSON-schema catalog

- **Status:** COMPLETE
- **Evidence:**
  - `packages/engine/tests/contract/protocol_v1/test_schemas.py:57`
    pins the 25 public contracts plus the shared schema.
  - `packages/engine/tests/contract/protocol_v1/test_schemas.py:66`
    requires valid and meaningful negative conformance cases and runs semantic validation.
  - `packages/engine/src/open_brain_engine/protocol/validation.py:84`
    supplies executable cross-field validation where JSON Schema is insufficient.
- **Notes:** The catalog includes the originally listed contracts plus the necessary request
  envelope, owner and Node certificates, proposal, effect receipt, and closed commit result.

### R4: Freeze RFC 8785 bytes and prove supported installs

- **Status:** COMPLETE
- **Evidence:**
  - `packages/engine/src/open_brain_engine/protocol/canonical.py:25`
    performs strict protocol JSON decoding; lines 36-42 canonicalize and hash through `rfc8785`.
  - `packages/engine/tests/contract/protocol_v1/test_protocol_freeze.py:30`
    pins canonical bytes and digest; line 42 rejects unsupported values.
  - `release/m1-compatibility.json:6`
    binds the raw 12-cell installation receipts and probe hash.
- **Notes:** No alternate JSON encoder is permitted.

### R5: Freeze opaque, role-distinct, Brain-scoped identifiers

- **Status:** COMPLETE
- **Evidence:**
  - `packages/engine/src/open_brain_engine/protocol/identifiers.py:34`
    defines distinct role prefixes; lines 61-91 require Brain scope for non-Brain IDs.
  - `packages/engine/tests/contract/protocol_v1/test_protocol_freeze.py:47`
    proves opacity, role separation, and explicit Brain scope.
- **Notes:** Digests are not public identifiers.

### R6: Verify encrypted SQLite and FTS across the full matrix

- **Status:** COMPLETE
- **Evidence:**
  - `release/m1-compatibility.json:119`
    starts the 12 passing macOS ARM64 and Linux x86_64 source/wheel cells for Python 3.12-3.14.
  - `release/m1-compatibility-receipts.json:1`
    preserves sanitized commands, setup hashes, stdout, stderr, and probe output.
  - `tools/m1/compatibility_probe.py:84`
    verifies logical canonical and FTS deletion; line 98 checks before and after checkpoint/vacuum.
  - `tests/phase4/test_m1_compatibility_baseline.py:119`
    asserts complete cell coverage; line 172 binds summary to raw receipts.
- **Notes:** Windows remains explicitly unsupported for M1.

### R7: Freeze dependencies and additive release evidence

- **Status:** COMPLETE
- **Evidence:**
  - `tests/phase4/test_m1_compatibility_baseline.py:65`
    pins `>=3.12,<3.15`, the engine `node` extra, and top-level app dependencies.
  - `tests/phase4/test_m1_compatibility_baseline.py:45`
    preserves the v0 facade and six artifact coordinates.
  - `packages/engine/pyproject.toml:47` and
    `packages/app/pyproject.toml:87` package the
    raw compatibility receipts in source distributions.
- **Notes:** Full artifact construction and policy verification passed.

### R8: Freeze a usable RootKeyCustodian port

- **Status:** COMPLETE
- **Evidence:**
  - `packages/engine/src/open_brain_engine/protocol/custody.py:73`
    exposes opaque-handle bootstrap, unlock, restart, derive, data-key generation, public-key,
    sign, encrypt/decrypt, wrap/unwrap, rotate, and destroy operations.
  - `packages/engine/tests/contract/protocol_v1/test_protocol_freeze.py:131`
    pins both custody ports, exact envelope fields, the expected-associated-data decrypt input,
    and key-selected rotation.
- **Notes:** Secret key bytes do not cross the port. Decryption receives the complete expected
  associated data instead of trying to use its digest as AEAD input.

### R9: Freeze cipher, wrapping, SQLCipher, and passphrase profiles

- **Status:** COMPLETE
- **Evidence:**
  - `packages/engine/src/open_brain_engine/protocol/freeze.py:42`
    freezes AES-256-GCM, nonce and key sizes, AES-KW, Argon2id, and SQLCipher parameters.
  - `docs/architecture/decisions/0010-m1-wire-storage-and-crypto.md:40`
    defines the privacy-preserving ledger-history commitment and database derivation; the payload
    section freezes separate ciphertext and wrapped-key envelopes with ciphertext integrity.
- **Notes:** Later storage code consumes these values rather than selecting new ones.

### R10: Keep grant issuance in owner-authenticated local control

- **Status:** COMPLETE
- **Evidence:**
  - `docs/architecture/decisions/0011-m1-authority-information-flow-and-resource-bounds.md:17`
    excludes issuance from every protocol transport and plaintext durable storage.
  - `packages/engine/src/open_brain_engine/protocol/freeze.py:79`
    freezes the peer-UID-checked Unix-socket control boundary.
- **Notes:** The four semantic operations remain the only protocol operations.

### R11: Freeze owner-session and step-up timing

- **Status:** COMPLETE
- **Evidence:**
  - `packages/engine/src/open_brain_engine/protocol/freeze.py:33`
    fixes owner sessions at 900 seconds and step-up freshness at 60 seconds.
  - `docs/architecture/decisions/0011-m1-authority-information-flow-and-resource-bounds.md:20`
    lists the actions requiring fresh presence.
- **Notes:** Keyring access alone is not user presence.

### R12: Freeze UserPresenceProvider and owner-control delivery

- **Status:** COMPLETE
- **Evidence:**
  - `packages/engine/src/open_brain_engine/protocol/custody.py:148`
    defines the production-facing presence port.
  - `docs/architecture/decisions/0011-m1-authority-information-flow-and-resource-bounds.md:26`
    selects LocalAuthentication or audited passphrase re-entry; line 30 freezes socket modes and
    peer UID checking.
- **Notes:** Fake presence remains test-only.

### R13: Freeze principal proof of possession and separate custody

- **Status:** COMPLETE
- **Evidence:**
  - `packages/engine/src/open_brain_engine/protocol/schemas/v1/grant.json:9`
    binds issuer/policy/principal epochs, keys, policy digest, schemas, visibility, and limits.
  - `packages/engine/src/open_brain_engine/protocol/validation.py:79`
    extracts the exact signed five-field request binding from the larger envelope.
  - `packages/engine/src/open_brain_engine/protocol/custody.py:129`
    defines separate client credential custody.
  - `packages/engine/tests/contract/protocol_v1/test_signature_vectors.py:31`
    verifies principal proof of possession; line 46 rejects another key and changed binding.
- **Notes:** Bearer fallback is frozen off.

### R14: Freeze an independently verifiable receipt trust chain

- **Status:** COMPLETE
- **Evidence:**
  - `packages/engine/src/open_brain_engine/protocol/schemas/v1/receipt.json:14`
    binds issuer/policy/sequencer epochs, Node certificate, owner history, and signature.
  - `packages/engine/src/open_brain_engine/protocol/validation.py:185`
    enforces receipt-to-certificate, ordered owner-history continuity, non-overlapping half-open
    validity intervals, and certifier validity at Node-certificate issuance.
  - `packages/engine/tests/contract/protocol_v1/test_signature_vectors.py:64`
    verifies every signed contract vector; line 91 verifies the pinned-owner-to-Node-to-receipt
    chain with only public material. Named deterministic tests verify valid signatures for activation,
    retirement, adjacent rotation, rotation gaps, backdating, and overlap boundaries.
- **Notes:** Key and signature encodings require exact Ed25519 byte lengths. Owner intervals are
  half-open, so issuance at `valid_from` passes and issuance at `retired_at` fails.

### R15: Generate a reproducible bounded synthetic corpus

- **Status:** COMPLETE
- **Evidence:**
  - `tools/m1/synthetic_corpus.py:43`
    deterministically generates the bounded shard catalog.
  - `tests/phase4/test_m1_compatibility_baseline.py:211`
    verifies 256 label sets, 16 labels, 50,000 records, generator hash, and benchmark coverage.
- **Notes:** The generator uses synthetic public data only.

### R16: Freeze opaque continuation, bounded traversal, and final fusion

- **Status:** COMPLETE
- **Evidence:**
  - `packages/engine/src/open_brain_engine/protocol/schemas/v1/query-continuation.json:1`
    exposes only a protected opaque token.
  - `packages/engine/src/open_brain_engine/protocol/freeze.py:16`
    fixes fan-out at 32 and RRF at 60.
  - `tools/m1/synthetic_corpus.py:87`
    fuses within-shard ranks with a bounded top-k accumulator.
  - `tests/phase4/test_m1_compatibility_baseline.py:268`
    proves cross-shard ordering does not compare raw FTS scores.
- **Notes:** Owners receive no exemption.

### R17: Freeze executable authorized query evidence

- **Status:** COMPLETE
- **Evidence:**
  - `packages/engine/src/open_brain_engine/protocol/schemas/v1/query-evidence.json:1`
    requires record or accepted-revision evidence, provenance, compartments, and visibility.
  - `packages/engine/src/open_brain_engine/protocol/schemas/v1/query.json:7`
    requires at least one evidence value for every result.
  - `packages/engine/src/open_brain_engine/protocol/validation.py`
    recursively validates each page evidence contract and binds its Brain to the page Brain.
- **Notes:** Named mutations reject both a self-consistent foreign-Brain evidence value and a
  nested provenance boundary violation. Body exposure remains conditional on body visibility.

### R18: Freeze resource bounds and actionable failures

- **Status:** COMPLETE
- **Evidence:**
  - `packages/engine/src/open_brain_engine/protocol/freeze.py:13`
    records every global and per-principal bound.
  - `packages/engine/src/open_brain_engine/protocol/schemas/v1/resource-limit-failure.json:1`
    defines stable code, message, retry safety, observed/accepted values, scope, and guidance.
  - `packages/engine/tests/contract/protocol_v1/test_schemas.py:314`
    pins the actionable failure fields.
- **Notes:** Capacity behavior fails closed.

### R19: Freeze monotonic and persisted-UTC clock semantics

- **Status:** COMPLETE
- **Evidence:**
  - `packages/engine/src/open_brain_engine/protocol/freeze.py:33`
    freezes owner, step-up, grant, and 300-second skew limits.
  - `docs/architecture/decisions/0011-m1-authority-information-flow-and-resource-bounds.md:58`
    defines monotonic sessions, persisted UTC high-water time, and fail-closed rollback behavior.
- **Executable precision:** The common timestamp schema and semantic parser accept only canonical
  UTC whole seconds or exactly three fractional digits. Validly signed submillisecond counterexamples
  are rejected before temporal comparison. A schema-derived timestamp-field catalog proves semantic
  coverage, and calendar validation rejects impossible dates even when `date-time` is only an
  annotation in the JSON Schema implementation.
- **Notes:** Grant timestamp arithmetic has an executable mutation test at
  `packages/engine/tests/contract/protocol_v1/test_schemas.py:117`.

### R20: Freeze durable replay protection outside the ledger

- **Status:** COMPLETE
- **Evidence:**
  - `packages/engine/src/open_brain_engine/protocol/freeze.py:61`
    fixes a separate encrypted nonce store and pre-operation reservation.
  - `docs/architecture/decisions/0011-m1-authority-information-flow-and-resource-bounds.md:63`
    defines retention, non-rollback, and fail-closed capacity behavior for all four operations.
  - `release/m1-compatibility.json:119`
    includes paged-read measurements in every matrix cell.
- **Notes:** W0 freezes the boundary; the store implementation belongs to a later wave.

### R21: Freeze operational information-flow rules

- **Status:** COMPLETE
- **Evidence:**
  - `packages/engine/src/open_brain_engine/protocol/schemas/v1/security-audit-event.json:1`
    is a closed redacted event shape.
  - `packages/engine/tests/contract/protocol_v1/test_schemas.py:106`
    rejects grant, payload, digest, body, and path fields.
  - `docs/threat-model.md:50`
    names application-controlled encrypted and purge surfaces.
- **Notes:** External OS swap, snapshots, core dumps, and crash systems remain an explicit
  deployment boundary.

### R22: Freeze inspect visibility and indistinguishable not-found content

- **Status:** COMPLETE
- **Evidence:**
  - `packages/engine/src/open_brain_engine/protocol/schemas/v1/inspect.json:130`
    separates metadata/body visibility and conditionally permits bodies.
  - `packages/engine/tests/contract/protocol_v1/test_schemas.py:261`
    pins identical not-found content; line 277 rejects payload and commit-digest metadata leaks;
    line 303 checks role-matched IDs.
  - `docs/threat-model.md:75`
    explicitly excludes constant-time database claims.
- **Notes:** Authorization-before-selection remains an implementation obligation for the Node wave.

### R23: Freeze isolated, complete changes pages

- **Status:** COMPLETE
- **Evidence:**
  - `packages/engine/src/open_brain_engine/protocol/schemas/v1/changes.json:10`
    types all six commit item kinds and binds role-correct IDs.
  - `packages/engine/tests/contract/protocol_v1/test_schemas.py:232`
    proves every accepted item identity can appear in the feed.
  - `docs/architecture/decisions/0011-m1-authority-information-flow-and-resource-bounds.md:76`
    freezes selection-before-authorization and per-page reauthorization.
- **Notes:** The feed can reconstruct non-record semantic commits.

### R24: Freeze idempotency, conflict, and erasure-over-replay results

- **Status:** COMPLETE
- **Evidence:**
  - `packages/engine/src/open_brain_engine/protocol/schemas/v1/commit-result.json:1`
    closes accepted, replayed, digest conflict, revision conflict, and delivery-purged variants.
  - `packages/engine/src/open_brain_engine/protocol/validation.py:229`
    validates nested receipts for accepted and replayed results.
  - `packages/engine/tests/contract/protocol_v1/test_schemas.py:212`
    exercises every typed outcome.
- **Notes:** Conflict values are results, not inspectable entities.

### R25: Freeze the minimal durable-job contract

- **Status:** COMPLETE
- **Evidence:**
  - `packages/engine/src/open_brain_engine/protocol/schemas/v1/durable-job.json:1`
    permits only purge and projection rebuild with restart-safe states, compartments, at most eight
    attempts, and state-consistent result/error references.
  - `packages/engine/tests/contract/protocol_v1/test_schemas.py:283`
    rejects the prior unbounded failed-job mutation.
- **Notes:** No generic scheduler or public job mutation was introduced.

### R26: Freeze safe loopback HTTP posture

- **Status:** COMPLETE
- **Evidence:**
  - `packages/engine/src/open_brain_engine/protocol/freeze.py:81`
    freezes static health, numeric loopback, disabled CORS, and rejected default browser origin.
  - `docs/architecture/decisions/0011-m1-authority-information-flow-and-resource-bounds.md:132`
    specifies Host validation and the static anonymous health response.
  - `tests/phase4/test_m1_compatibility_baseline.py:65`
    verifies Starlette and Uvicorn are direct app dependencies.
- **Notes:** Adapter implementation remains in its planned later wave.

### R27: Freeze literal query input and reject raw FTS syntax authority

- **Status:** COMPLETE
- **Evidence:**
  - `packages/engine/src/open_brain_engine/protocol/schemas/v1/query.json:31`
    declares the 4,096-byte extension constraint.
  - `packages/engine/src/open_brain_engine/protocol/validation.py:121`
    enforces the UTF-8 byte count.
  - `packages/engine/tests/contract/protocol_v1/test_schemas.py:201`
    rejects 4,098 UTF-8 bytes that fit within 4,096 code points.
- **Notes:** Compilation to FTS remains internal to the later query implementation.

### R28: Freeze asynchronous projection visibility

- **Status:** COMPLETE
- **Evidence:**
  - `packages/engine/src/open_brain_engine/protocol/schemas/v1/query.json:116`
    defines typed projection lag with retry guidance and opaque watermark.
  - `packages/engine/src/open_brain_engine/protocol/schemas/v1/query.json:141`
    defines the separate continuation-invalidated result.
  - `docs/architecture/decisions/0011-m1-authority-information-flow-and-resource-bounds.md:89`
    keeps the Brain-wide checkpoint internal and the visible watermark grant-scoped.
- **Notes:** Commit validity remains projection-independent.

### R29: Keep commit digests confined to signed receipts

- **Status:** COMPLETE
- **Evidence:**
  - `packages/engine/src/open_brain_engine/protocol/schemas/v1/receipt.json:12`
    places the canonical digest in the signed receipt.
  - `packages/engine/src/open_brain_engine/protocol/schemas/v1/inspect.json:81`
    defines a closed metadata allowlist without a commit digest.
  - `packages/engine/tests/contract/protocol_v1/test_schemas.py:277`
    rejects bodyless digest disclosure.
  - `packages/engine/src/open_brain_engine/protocol/schemas/v1/common.json`
    gives continuity documents only an opaque `lhc_v1_` history commitment to the complete signed
    prior receipt; no prior cursor, commit ID, or canonical commit digest is serialized.
- **Notes:** Canonical commit digests remain only in receipts. History commitments are continuity
  evidence, not IDs or public lookup keys.

### R30: Freeze the minimum BrainPack semantic inventory

- **Status:** COMPLETE
- **Evidence:**
  - `docs/architecture/brain-protocol-v1.md:138`
    lists required semantic and excluded operational state.
  - `docs/architecture/decisions/0009-brainpack-v2.md:14`
    defines the portable semantic inventory and verification requirements.
  - `packages/engine/src/open_brain_engine/protocol/schemas/v1/record.json:28`
    distinguishes pending, verified, and explicitly unknown historical ciphertext integrity.
  - `packages/engine/tests/contract/protocol_v1/test_schemas.py:167`
    rejects persisted encrypted envelopes as client commit input.
- **Notes:** This is a storage contract, not a BrainPack encoder.

### R31: Freeze schema migration and crash recovery

- **Status:** COMPLETE
- **Evidence:**
  - `docs/architecture/decisions/0010-m1-wire-storage-and-crypto.md:85`
    defines versioned namespaces, authenticated migration, staging, atomic manifest replacement,
    cleanup, intents, and restart behavior.
  - `release/m1-compatibility.json:119`
    records passing unclean-process recovery in every supported cell.
- **Notes:** The new namespace never reinterprets the legacy database.

### R32: Freeze safe roots and single-sequencer transfer proof

- **Status:** COMPLETE
- **Evidence:**
  - `docs/architecture/decisions/0010-m1-wire-storage-and-crypto.md:100`
    rejects known network and synchronized roots and fixes owner-only modes.
  - `packages/engine/src/open_brain_engine/protocol/schemas/v1/cold-transfer-certificate.json:18`
    binds the next public key and opaque prior history commitment.
  - `packages/engine/src/open_brain_engine/protocol/validation.py:241`
    binds the owner certificate to the Node stop proof and exact next epoch.
  - `packages/engine/tests/contract/protocol_v1/test_signature_vectors.py:129`
    tests pair continuity and rejects the prior epoch mutation.
- **Notes:** Uncertain transfer pauses writes.

### R33: Preserve v0 behavior and artifact coordinates

- **Status:** COMPLETE
- **Evidence:**
  - `tests/phase4/test_m1_compatibility_baseline.py:39`
    pins completed P4 evidence bytes.
  - `tests/phase4/test_m1_compatibility_baseline.py:45`
    pins the v0 facade and all six artifact coordinates.
- **Notes:** The full 3,364-test repository suite passed after the calendar-validation remediation.

### R34: Extend the coordinator-owned classification manifest

- **Status:** COMPLETE
- **Evidence:**
  - `docs/v0-package-classification.json:4866`
    classifies semantic validation; line 7308 classifies raw receipts; line 7786 begins new schema
    subjects; line 13067 classifies the matrix runner.
  - `tests/phase4/test_move_manifest.py:48`
    pins the additive runtime count and validates the manifest.
  - `docs/ai/workstreams/20260901-open-brain-public-execute-goal-63-through-phase-4-p4a-p4b-and-p4c-with-independently-reviewed-pack-8a3f9b/P4-W0-MOVE-REPORT.md:1`
    and the paired import report were refreshed through the canonical generator.
- **Notes:** Existing classification records and rules were preserved.

### R35: Extend artifact policy and threat boundaries additively

- **Status:** COMPLETE
- **Evidence:**
  - `release/v0-artifact-policy.json:259`
    requires protocol schemas in the engine wheel; line 315 requires them in the engine sdist.
  - `release/v0-artifact-policy.json:96`
    and line 296 force-include raw receipts in app and engine source distributions.
  - `docs/threat-model.md:33`
    covers loopback trust, strict decoding, receipt verification, purge sinks, safe roots, clock
    behavior, and the absence of constant-time storage claims.
  - `tests/phase4/test_m1_compatibility_baseline.py:86`
    verifies packaged protocol and evidence resources.
- **Notes:** Completed P4 evidence bytes remain unchanged.

## Initial independent findings and closure

| Finding | Resolution | Executable evidence |
|---|---|---|
| P1: grant and receipt authority fields missing | Added issuer and policy epochs, policy digest, schema restrictions, body visibility, limits, and receipt bindings | `packages/engine/src/open_brain_engine/protocol/schemas/v1/grant.json:9`; `packages/engine/src/open_brain_engine/protocol/schemas/v1/receipt.json:12` |
| P1: receipt and transfer not independently verifiable | Added typed owner/Node certificates, exact signed payload rules, contiguous epochs/history, prior heads, public keys, stop-proof pairing, and deterministic signatures | `packages/engine/tests/contract/protocol_v1/test_signature_vectors.py:64` |
| P1: purge probe accepted no-op deletion | Added SQL row, FTS row, and FTS match checks before and after checkpoint/vacuum; reran all 12 cells | `tests/phase4/test_m1_compatibility_baseline.py:238`; line 253 |
| P2: changes only represented records | Added typed item kinds and role-bound IDs for every commit item | `packages/engine/tests/contract/protocol_v1/test_schemas.py:232` |
| P2: record/provenance authority fields missing | Added schema, producer, origin, times, ciphertext state/digest, Brain boundary, processor identity, and source requirements | `packages/engine/tests/contract/protocol_v1/test_schemas.py:133`; line 180 |
| P2: RootKeyCustodian was lifecycle-only | Added scoped cryptographic operations with opaque handles and typed envelopes | `packages/engine/src/open_brain_engine/protocol/custody.py:73` |
| P2: negative cases were placeholders | Added meaningful fixtures plus named mutations for foreign Brain, job bounds, query bytes/kind, inspect leakage, trust continuity, and strict encodings | `packages/engine/tests/contract/protocol_v1/test_schemas.py:66` |
| P2: typed commit outcomes missing | Added closed accepted/replayed/conflict/delivery-purged result schema and tests | `packages/engine/tests/contract/protocol_v1/test_schemas.py:212` |
| P3: request vector did not match named schema | Split exact binding from request envelope and tested their projection | `packages/engine/tests/contract/protocol_v1/test_schemas.py:85` |
| P3: raw matrix receipts absent | Added reproducible runner and hash-bound sanitized raw receipts | `tests/phase4/test_m1_compatibility_baseline.py:172` |
| P3: benchmark compared raw BM25 across shards | Replaced cross-shard score comparison with within-shard ranks and RRF-60 | `tests/phase4/test_m1_compatibility_baseline.py:268` |

## Second independent findings and local remediation

The independent rereview of exact commit `f23b4ce` returned NOT READY with zero P0, two P1, two
P2, and zero P3 findings. The table records the current local remediation. These rows are not an
independent acceptance verdict.

| Finding | Local remediation | Executable evidence |
|---|---|---|
| P1: continuity certificates disclosed a prior canonical commit digest | Replaced the cursor, commit ID, and digest ledger head with a domain-separated commitment to the complete signed prior receipt; regenerated stop and transfer vectors | `test_ledger_history_commitment_is_domain_separated_and_reproducible`; `test_continuity_heads_expose_only_an_opaque_history_commitment`; `test_cold_transfer_is_bound_to_the_node_stop_proof` |
| P1: owner validity did not constrain Node certification or rotation | Enforced strictly increasing activation, retired predecessors, non-overlap, and half-open certifier validity at Node issuance | `test_receipt_owner_certification_accepts_half_open_interval_boundaries`; `test_receipt_owner_certification_rejects_validly_signed_invalid_chronology` |
| P2: query pages bypassed nested evidence Brain checks | Recursively validate every evidence contract, then bind its Brain to the page Brain | `test_query_page_rejects_evidence_from_another_brain`; `test_query_page_recursively_rejects_evidence_provenance_boundary` |
| P2: decrypt could not receive required AEAD associated data | Added expected associated data to `decrypt`, added data-key and ciphertext bindings to typed envelopes, and made rotation select an exact key | `test_key_custody_envelopes_and_aead_inputs_are_exact` |

## Follow-up finding from the `5f1395a` rereview

The strict review of `5f1395a` cleared the preceding four findings and returned NOT READY with zero
P0, zero P1, one P2, and zero P3 findings.

| Finding | Local remediation | Executable evidence |
|---|---|---|
| P2: Python truncated schema-valid submicrosecond timestamps before owner-validity comparison | Restricted wire timestamps to canonical UTC whole seconds or exactly three millisecond digits in both schema and semantic parsing; added both valid-format tests and fully signed reproductions of the two counterexamples | `test_protocol_timestamps_reject_noncanonical_or_submillisecond_precision`; `test_protocol_timestamps_accept_exact_millisecond_precision`; `test_receipt_rejects_validly_signed_submillisecond_timestamp_ambiguity` |

## Follow-up finding from the `9945e48` rereview

The strict review of `9945e48` verified the precision and chronology correction and returned NOT
READY with zero P0, zero P1, one P2, and zero P3 findings.

| Finding | Local remediation | Executable evidence |
|---|---|---|
| P2: trailing line terminators and impossible calendar dates passed contracts without temporal comparisons | Replaced the schema's newline-tolerant end anchor, added centralized semantic validation for every timestamp field discovered from the schema catalog, and explicitly skipped opaque application bodies | `test_semantic_timestamp_catalog_covers_every_schema_timestamp_field`; `test_protocol_timestamp_rejects_every_trailing_line_terminator`; `test_every_top_level_wire_timestamp_rejects_an_impossible_calendar_date`; `test_nested_inspect_timestamp_rejects_an_impossible_calendar_date`; `test_timestamp_like_fields_inside_opaque_bodies_remain_application_data` |

## Integration audit

- `packages/engine/src/open_brain_engine/protocol/resources.py:12`
  discovers installed schemas and loads conformance/signature resources.
- `packages/engine/src/open_brain_engine/protocol/__init__.py:31`
  exports strict decoding, semantic validation, signed-byte projection, and cold-transfer pairing.
- `release/v0-artifact-policy.json:259` and
  `packages/engine/pyproject.toml:47` connect the
  public contract and raw evidence to built artifacts.
- `tests/phase4/test_m1_compatibility_baseline.py:86`
  validates the package-policy-manifest connection.

No broken integration, orphaned import, or unclassified W0 runtime/resource was found.

## Risk analysis

- **Regression:** Low. The v0 facade, P4 evidence hashes, package boundaries, Python range, and six
  artifact coordinates are pinned and passed.
- **Security:** The reviewer independently cleared the findings from `f23b4ce` and `5f1395a`. The
  remaining timestamp-completeness P2 from `9945e48` is covered by passing local regression tests
  and remains a gate blocker until independently verified.
- **Performance:** The provisional shard, fan-out, top-k, commit, nonce, and concurrency limits are
  executable constants. Synthetic macOS/Linux measurements passed their frozen bounds.
- **Delivery:** The Linux x86_64 matrix used official containers under x86_64 emulation on Apple
  Silicon. This satisfies the recorded W0 environment but is not evidence from separate physical
  Linux hardware.

## Critical gaps

Independent rereview of the exact calendar-validation remediation remains open. W1 stays locked.

## Integration issues

None found.

## Quality concerns

None blocking. The record schema deliberately distinguishes pending commit input, verified
persisted ciphertext, and unknown historical integrity. The storage wave must enforce that newly
persisted records use the verified state, as the frozen contract requires.

## Recommended fixes

Commit the exact verified calendar-validation remediation and request an independent rereview of
that commit. Do not start W1 without a READY verdict.

## Optional enhancements

- Repeat the Linux matrix on physical x86_64 hardware before a release candidate if native-host
  evidence is desired.
- Assemble exact transitive dependency license texts during the later M1 release-candidate wave.

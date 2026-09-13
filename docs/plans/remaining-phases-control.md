# Remaining phases control

This document is the execution authority for the remaining Open Brain replacement work in Phases 7 and 8. The extraction plan defines the intended outcome. This document defines the gates that decide whether work may advance.

Product-scope note: these are legacy replacement and cutover gates. They do not define the default
Open Brain installation or move Secure Node controls into it. Current product work follows
[`product-roadmap.md`](product-roadmap.md).

The live machine-readable status is maintained outside the public repository in the governed workstream. It refers to gates by the IDs below and records the exact commit that passed verification.

## Gate rules

1. A wave passes only when its behavior, test, documentation, migration-evidence, and final-verification gates all pass.
2. A failed or blocked required gate stops advancement. Missing tools, authority, legacy executables, production evidence, or elapsed stabilization time are recorded as blockers. They are never converted to synthetic passes.
3. Every comparison uses the Phase 7 parity harness as the normalization and decision boundary. Scenario tests may construct inputs, but may not implement a second comparison path.
4. Synthetic evidence proves only synthetic readiness. Production, deployment, repository publication, cutover, rollback, and retirement claims require explicit owner approval and direct evidence.
5. Each wave ends at a clean local commit after both its fast check and the full integration check pass. No wave may start from uncommitted prior-wave work.

## Verification contract

The fast check is specific to the wave and must include its focused tests plus Ruff and MyPy for changed Python surfaces. The full integration check is always:

```bash
make verify
```

The coordinator records both results and the resulting clean commit SHA in the workstream status record. A later edit invalidates the checkpoint until the applicable checks are rerun.

## Phase 7 waves

### P7-W0: synthetic parity harness and capture scenarios

| Gate | Pass condition | Fail condition |
|---|---|---|
| `P7-W0-BEH` | The versioned parity harness compares complete normalized legacy and Open Brain inputs for all nine parity facets. The five capture scenarios use that harness for provenance, intent-derived routing, and review gating. Third-party advice produces a review proposal and never an applied task. | A scenario bypasses the harness, raw content enters parity metadata, an inventory is incomplete, or a reviewable intent can become an action without review. |
| `P7-W0-TST` | All Phase 7 parity tests pass, including mismatch regressions for provenance, routing, and review proposals. Ruff and MyPy pass for the changed surfaces. | Any required test, lint, or type check fails. |
| `P7-W0-DOC` | This control document and the live status record identify the harness as the sole comparison boundary and distinguish synthetic from production evidence. | Another comparison path is described as authoritative or evidence scope is ambiguous. |
| `P7-W0-MIG` | Synthetic legacy/Open Brain pairs are compared automatically. A versioned installed-artifact identity is verified. Executable legacy comparison is either evidenced directly or recorded as blocked. | Synthetic data is represented as production evidence, or unavailable executable legacy comparison is silently omitted. |
| `P7-W0-FULL` | `make verify` passes and the exact tree is committed locally with a clean worktree. | Full verification fails or the checkpoint is dirty. |

Fast check:

```bash
uv run pytest -q tests/parity/phase7 && uv run ruff check src/open_brain/parity tests/parity/phase7 && uv run mypy
```

Executable differential runs use `open_brain.parity.runner` only as an orchestrator and
evidence validator. The public runner does not spawn or inspect executables. The synthetic
runner can claim only synthetic readiness. The distinct live runner can claim live parity
only when the artifact attestation, both containment attestations, and the Open Brain
adapter-to-artifact binding are independently live-scoped; synthetic evidence is rejected at
each of those boundaries. Both runners require an externally verified synthetic fixture,
an owner-supplied containment provider, and an independent containment-attestation verifier.
Live CLI comparison normalizes the two status schemas to command, completion, exit, and
redaction semantics; exact field behavior remains bound to the independently executed adapter
test evidence rather than treating implementation-specific field names as differences.
Until those authorities and both predecessor adapters are provisioned, `P7-W0-MIG` remains
blocked even when all public runner tests pass.

### P7-W1: read-only shadow observer

| Gate | Pass condition | Fail condition |
|---|---|---|
| `P7-W1-BEH` | Metadata-only snapshot comparison observes extraction class, routing/content kind, provenance, provider/privacy tier, resource class, and redaction. No writer can be acquired or shared. | Any write-capable adapter, shared writable identity, missing provenance, privacy mismatch, or raw log residue is accepted. |
| `P7-W1-TST` | Focused shadow tests cover matching observations and each fail-closed condition. Ruff and MyPy pass. | Any required check fails. |
| `P7-W1-DOC` | Operator documentation states snapshot-only behavior and the explicit gate for live shadow reads. | Documentation implies that synthetic shadow evidence authorizes production use. |
| `P7-W1-MIG` | Synthetic/read-only snapshot evidence is recorded. Live mirrored or production reads remain blocked until owner approval. | A live read occurs without approval or a missing live run is represented as passed. |
| `P7-W1-FULL` | `make verify` passes and a clean local checkpoint commit exists. | Full verification fails or the checkpoint is dirty. |

Fast check:

```bash
uv run pytest -q tests/integration/operations/test_shadow.py && uv run ruff check src/open_brain/operations/shadow.py tests/integration/operations/test_shadow.py && uv run mypy
```

### P7-W2: ordered cutover rehearsal and rollback

| Gate | Pass condition | Fail condition |
|---|---|---|
| `P7-W2-BEH` | All eight surfaces follow the fixed order and require snapshot, old-writer disable, new-service enable, one-writer proof, synthetic smoke, verification, and rollback evidence. Every rollback trigger fails closed. | A surface is skipped or reordered, writers overlap, a required receipt is absent, or a rollback trigger is ignored. |
| `P7-W2-TST` | Focused tests cover ordering, writer handoff, smoke, verification, authority, rollback triggers, and redacted diagnostics. Ruff and MyPy pass. | Any required check fails. |
| `P7-W2-DOC` | The runbook documents the eight-surface order, stop points, rollback criteria, and owner approval boundaries. | Operators cannot determine when to stop or roll back. |
| `P7-W2-MIG` | A fully synthetic rehearsal is recorded. Production cutover and real-capture evidence remain owner-gated. | Rehearsal is called production-ready or any live transition runs without approval. |
| `P7-W2-FULL` | `make verify` passes and a clean local checkpoint commit exists. | Full verification fails or the checkpoint is dirty. |

Fast check:

```bash
uv run pytest -q tests/integration/operations/test_cutover.py && uv run ruff check src/open_brain/operations/cutover.py tests/integration/operations/test_cutover.py && uv run mypy
```

### P7-W3: operational evidence and Phase 7 reconciliation

| Gate | Pass condition | Fail condition |
|---|---|---|
| `P7-W3-BEH` | Metadata-only receipts model capture-to-ledger, review approve/reject, nightly, playlist, social/web, backup, and temporary restore flows. Phase 7 reconciliation rejects missing or contradictory receipts. | A required flow is absent, raw sensitive content is retained, or synthetic receipts assert production execution. |
| `P7-W3-TST` | Focused operational-evidence and Phase 7 reconciliation tests pass. Ruff and MyPy pass. | Any required check fails. |
| `P7-W3-DOC` | Operational docs map each receipt to its runbook step and name all owner-gated production checks. | A required flow has no operator procedure or owner gate. |
| `P7-W3-MIG` | The synthetic checkpoint is complete. Production parity, all eight live surfaces, and all eight operational flows are directly evidenced after approval. | Any owner-gated row is inferred from synthetic evidence. |
| `P7-W3-FULL` | `make verify` passes, independent safety review is READY, and a clean local checkpoint commit exists. | Full verification or independent review fails, or the checkpoint is dirty. |

Fast check:

```bash
uv run pytest -q tests/integration/operations/test_cutover_verification.py tests/parity/phase7/test_reconciliation.py && uv run ruff check src/open_brain/operations/cutover_verification.py tests/integration/operations/test_cutover_verification.py tests/parity/phase7/test_reconciliation.py && uv run mypy
```

## Phase 8 waves

### P8-W0: replacement and migration evidence

| Gate | Pass condition | Fail condition |
|---|---|---|
| `P8-W0-BEH` | A closed evidence model proves both predecessor code repositories map to Open Brain, with no maintained fork. It requires capability disposition, migration rehearsal, runtime-reference scan, rollback criteria, operational documentation, and explicit retirement approval. | Either predecessor is omitted, a capability is unaccounted for, or retirement can become ready without all prerequisites and owner approval. |
| `P8-W0-TST` | Focused replacement-evidence tests cover both predecessors, missing evidence, stale evidence, rollback readiness, and approval gates. Ruff and MyPy pass. | Any required check fails. |
| `P8-W0-DOC` | Migration, rollback, operations, and retirement runbooks explain evidence collection without private topology. | A required proof has no safe operator procedure. |
| `P8-W0-MIG` | Copy-only/idempotent migration rehearsal and restore evidence bind to versioned Open Brain artifacts. Live migration remains owner-gated. | Production data is mutated, legacy source is copied, or rehearsal evidence is missing. |
| `P8-W0-FULL` | `make verify` passes and a clean local checkpoint commit exists. | Full verification fails or the checkpoint is dirty. |

Fast check:

```bash
uv run pytest -q tests/integration/release/test_replacement_evidence.py && uv run ruff check src/open_brain/release/replacement.py tests/integration/release/test_replacement_evidence.py && uv run mypy
```

### P8-W1: stabilization and public-release audit evidence

| Gate | Pass condition | Fail condition |
|---|---|---|
| `P8-W1-BEH` | Seven consecutive approved production days contain health, queue, review, ledger, backup, redaction, nightly-cycle, and capture-integrity evidence. Any rollback or integrity fix restarts the clock. | A day is missing, a required check fails, or the clock is not restarted. |
| `P8-W1-TST` | Focused stabilization/release-evidence tests pass. The local release checks available in the environment run against the exact tree. | Any required automated check fails. |
| `P8-W1-DOC` | Release and stabilization procedures list every audit artifact, manual review, and approval boundary. | The release can be represented as ready without required evidence. |
| `P8-W1-MIG` | Old repositories remain read-only rollback references during the window; all fixes are evidenced in Open Brain only. | A predecessor receives maintained application-code changes or rollback availability is lost early. |
| `P8-W1-FULL` | `make verify` and the complete release audit pass against the exact clean commit and generated archive. | A required audit tool/check is unavailable or fails; record blocked rather than waive. |

Fast check:

```bash
uv run pytest -q tests/integration/release/test_stabilization.py tests/integration/release/test_release_evidence.py && uv run ruff check src/open_brain/release tests/integration/release && uv run mypy
```

### P8-W2: owner-gated publication and retirement

| Gate | Pass condition | Fail condition |
|---|---|---|
| `P8-W2-BEH` | Open Brain is the sole maintained application-code repository; production, CI, docs, and contributor paths reference it; both predecessors have zero executable runtime references. | Any executable dependency or maintained fork remains. |
| `P8-W2-TST` | Clean-install, restore, runtime-reference, public-archive, and repository-settings evidence all pass. | Any required check fails or cannot be run. |
| `P8-W2-DOC` | Public migration/security docs and local predecessor archival notices are reviewed and point to the approved Open Brain repository. | URLs are unapproved, private topology leaks, or notices are missing. |
| `P8-W2-MIG` | Owner approves publication, cutover completion, rollback-boundary transition, and retirement separately. Source commit IDs are preserved only in the private migration record. | Any outward-facing or destructive action lacks explicit approval. |
| `P8-W2-FULL` | Public CI/archive verification passes, retirement evidence is complete, and final independent review is READY. | Any owner gate, verification, or review remains unresolved. |

Fast check: the exact approved publication/retirement runbook commands recorded at execution time. Full check: `make verify`, complete public release audit, public CI/archive verification, and the final replacement reconciliation.

## Non-authorized actions

This control document does not authorize reading private legacy source, production access, migration of live data, deployment, service changes, real captures, cutover, rollback, remote creation, push, publication, repository archival, checkout deletion, or release. Each action remains blocked until the owner explicitly approves that action at its execution gate.

# CORE-W0: shared portability mapping

Status: complete; focused and full verification pass; independent review accepted

Historical scope note: the shared record model and Secure Node envelope mapping remain current.
[ADR 0015](../architecture/decisions/0015-homebrew-only-distribution.md) later removed the package
classification, artifact policy, and Phase 4 harness named in this closure record.

Date: 2026-09-07

Starting commit: `1aa62f8763c6c41e635a2d6ebae1780a37672157`

Branch: `goal/open-brain-five-minute-install`

## Objective

Freeze and implement the pure inbound mapping from one validated Portable Brain v1 export to the
Secure Node semantic kernel. The mapping must preserve the shared semantic inventory and every
manifest-declared source byte without treating Secure Node envelope IDs or controls as the original
record model.

This milestone prevents `SN1-W2` from persisting an incompatible second canonical form. It does not
implement Secure Node storage or the five-minute default product.

## Source of truth

Use these contracts in descending order:

1. [`../product-family.md`](../product-family.md) for the product split and upgrade promise.
2. [`../architecture/decisions/0014-shared-record-import-envelope.md`](../architecture/decisions/0014-shared-record-import-envelope.md) for the mapping decision.
3. [`../portable-brain-v1.md`](../portable-brain-v1.md) and its executable schemas for source bytes.
4. [`../architecture/brain-protocol-v1.md`](../architecture/brain-protocol-v1.md), ADR 0013, and the
   executable protocol schemas for Secure Node envelopes.
5. The completed `SN1-W1` semantic kernel for pure batch and provenance validation.

If these contracts cannot represent one source family without loss, stop and write a reviewed ADR.
Do not weaken a fixture, invent historical security metadata, or defer the mismatch into `SN1-W2`.

## Architecture options

| Option | Result | Decision |
|---|---|---|
| Product-neutral shared records wrapped by Secure Node `record` envelopes | Preserves source meaning and bytes; keeps both product stores replaceable | Selected; correct architecture |
| Convert Portable history directly into native Secure Node proposals, decisions, revisions, and effects | Makes imported history look active but must invent or collapse incompatible state | Rejected |
| Store the entire Portable export as one opaque record | Easy byte round trip but loses record-level identity, provenance, query, and purge behavior | Rejected |

## Mapping inventory

| Portable item | Shared family | Identity | Secure Node representation |
|---|---|---|---|
| `brain.toml` | `brain` | `tenant_id` | One immutable import record plus the re-encoded Secure Brain ID |
| Space `_space.md` | `space` | `space_id` | One immutable import record |
| Canonical page Markdown | `page` | `page_id` | One immutable import record with capture provenance |
| Other Portable-valid owner Markdown | exact attachment | no stable Portable record ID | Preserve path, digest, and bytes without inventing a record envelope |
| Capture JSON | `capture` | `capture_id` | One immutable import record |
| Event JSONL row | `event` | `record_id` with an `event_` prefix | One immutable import record with path and row ordinal |
| Measurement JSONL row | `measurement` | `record_id` with a `measurement_` prefix | One immutable import record with path and row ordinal |
| Proposal JSON | `proposal` | `proposal_id` | One immutable historical import record |
| Decision JSON | `decision` | `decision_id` | One immutable historical import record |
| Publication JSON | `publication` | `publication_id` | One immutable historical import record |
| Action JSON | `action` | `action_id` | One immutable historical import record |
| Routing JSON | `route` | `route_id` | One immutable historical import record |
| Content-addressed blob | `source_blob` | private digest binding | Exact payload attachment, not a public record identity |
| Portable manifest | import evidence | `export_id` | Validates the source snapshot; excluded from canonical Brain records |

Actor IDs, role and claim IDs, receipt IDs, space links, source references, privacy values, trust
values, review outcomes, and nested evidence remain exact inside the protected shared body. Actor
IDs also map to producer principal envelope IDs. No operational value becomes shared data.

## Work packages

### 1. Contract and review corrections

- [x] Correct the roadmap's impossible durable-import gate and assign that proof to `UP1-W0`.
- [x] Record the semantic-ID versus envelope-ID rule and the non-native historical mapping.
- [x] Assign missing default status/doctor, upgrade abort, plaintext cleanup, ordering, root-safety,
      and packaging-fallback requirements to their owning future workstreams.
- [x] Run an independent adversarial review. It returned four P1 and one P2 finding.
- [x] Resolve all five findings in ADR 0014 and this plan before code changes: timestamps,
      multi-batch validation, normative lineage, exact identity and batch context, and full-plan
      evidence.

### 2. Product-neutral shared model

- [x] Add immutable `SharedBrain`, `SharedRecord`, `SharedBlob`, `SharedAttachment`, and
      `SharedImportEvidence` values under `open_brain_engine.portability` with strict construction
      checks.
- [x] Decode only a previously validated immutable `PortableSnapshot`.
- [x] Extract one record per logical Portable item, including each JSONL row, plus separate exact
      inventories for content-addressed blobs and manifest-declared files without stable record IDs.
- [x] Preserve exact source bytes, schema URI, path, ordinal, identity, actor, timestamp, space, and
      provenance fields.
- [x] Reconstruct and validate the complete manifest-declared file set without the source manifest.
- [x] Preserve exact source manifest evidence and compute one canonical digest over the complete
      record, blob, evidence, envelope-ID, and batch inventory.

### 3. Secure Node inbound adapter

- [x] Implement the frozen identity transformation and collision checks from ADR 0014.
- [x] Require an immutable `ImportEnvelopeContext` with canonical observation time, compartments,
      policy digest, issuer and sequencer epochs, and one caller-generated delivery ID per batch.
- [x] Build strict shared-envelope bodies and `pending` Secure Node records under an explicitly
      supplied target compartment and policy digest.
- [x] Apply ADR 0014's per-family lineage table. Topologically order with its exact tie-break and
      split records into batches no larger than the protocol limit.
- [x] Compute every batch digest as RFC 8785 SHA-256 over the exact ordered wire object. Prove an
      item reorder changes the digest.
- [x] Validate every batch through the existing pure ledger kernel. Between batches, use only the
      private discarded `unknown_historical` staging state defined by ADR 0014; never report it as
      encrypted or durable state.
- [x] Create no grant, receipt, key, nonce, job, service state, ciphertext digest, or encryption
      claim.

### 4. Schemas and conformance fixture

- [x] Add a versioned shared-envelope JSON Schema outside the frozen Portable v1 schema catalog.
- [x] Add packaged conformance vectors for identity mapping, family coverage, exact digests,
      provenance edges, timestamp handling, full-plan evidence, and expected Secure Node envelope
      IDs.
- [x] Use the existing complete Portable Brain fixture as the source. Do not create a smaller
      second source fixture that can drift.
- [x] Register every runtime, schema, conformance, test, and public-document file in the canonical
      package classification and artifact policy.

### 5. Negative and boundary proof

- [x] Reject changed source bytes, digest mismatch, unsafe or operational paths, incomplete
      identity maps, duplicate semantic IDs, derived-ID collisions, missing provenance, invalid
      compartment sets, and noncanonical JSONL reconstruction.
- [x] Test `brain.toml`, space records, microsecond and longer Portable fractions, plus an export
      with more than 128 records and a lineage edge crossing the batch boundary.
- [x] Test omitted, extra, reordered, unreferenced, oversized, and digest-mismatched attachments.
      An oversized Portable-valid blob stays mapped and exposes bounded chunks for later
      persistence. Owner Markdown without canonical page identity stays byte-exact and receives no
      fabricated record ID.
- [x] Prove same UUID text under different Portable families produces distinct record envelopes.
- [x] Prove content digests never become protocol record IDs.
- [x] Prove import records preserve Portable privacy facts without converting them into Secure Node
      authorization or retrospective encryption claims.
- [x] Prove wheel-installed code can load the schema and conformance resources without checkout
      paths.

### 6. Final-review remediation and closure

The independent review of implementation commit `10d0846b56144ac8ad316452bd31cf6756844d05`
returned two P1 findings and one P2 finding. The first correction:

- recomputes every batch digest from its exact mapped wire records before accepting a plan;
- reconstructs semantic Portable files from the mapped Secure Node envelope bodies;
- binds every shared family to its identifier prefix, source schema, path family, and ordinal rule
  in the published schema; and
- exposes a packaged semantic-envelope validator that checks the exact source-byte binding.

The tests reproduced all three defects before the corrections. A correction review then found one
P2 gap: the public validator did not compare `source_brain_id` with the tenant inside the decoded
Portable bytes. Exact implementation commit `79f3b44dda4ec96227e28c4e890170f834e78072`
closes that gap in both the packaged validator and aggregate construction, with all 11 shared
families covered.

At that exact clean commit, the focused gate passes 123 tests plus Ruff, MyPy, and diff integrity.
Repository-wide verification passes 3,537 tests, strict MyPy on 593 source files, all six Python
artifacts, and artifact policy. A fresh independent reviewer returned `READY` with P0/P1/P2/P3
`0/0/0/0` after rejecting source-Brain mismatches for all 11 families and reconfirming the original
closure properties. `CORE-W0` is complete. Nothing has been pushed or published.

## Planned files

- `docs/architecture/decisions/0014-shared-record-import-envelope.md`
- `docs/plans/2026-09-07-core-w0-shared-portability.md`
- `docs/product-family.md`
- `docs/portable-brain-v1.md`
- `docs/plans/product-roadmap.md`
- `packages/engine/src/open_brain_engine/portability/`
- `packages/engine/tests/contract/test_shared_portability.py`
- `packages/app/pyproject.toml` (documentation inclusion only)
- `docs/v0-package-classification.json`
- `release/v0-artifact-policy.json`
- `tools/phase4/move_manifest.py`
- `tools/phase4/acceptance_harness.py`
- focused Phase 4 and release-policy tests plus regenerated move/import reports

No application runtime, default CLI, persistence, key-custody, service, connector, legacy,
migration, or deployment code is in scope. App build metadata changes only to package these public
documents in the existing source distribution.

## Focused verification

Run sequentially:

```bash
uv run pytest -q packages/engine/tests/contract/test_shared_portability.py \
  packages/engine/tests/contract/test_portable_brain_v1.py \
  packages/engine/tests/unit/ledger
uv run ruff check packages/engine/src/open_brain_engine/portability \
  packages/engine/tests/contract/test_shared_portability.py
uv run mypy packages/engine/src/open_brain_engine/portability \
  packages/engine/tests/contract/test_shared_portability.py
git diff --check
```

Then run the real repository gate:

```bash
make verify
```

The exact committed tree must pass both checks. A fresh read-only reviewer then checks the mapping
contract, source and target semantics, negative coverage, package resources, and diff. Every P0
through P2 finding must be resolved before the milestone closes.

## Completion and stop conditions

`CORE-W0` is complete when the full packaged fixture maps through valid bounded import batches,
the pure Secure Node kernel accepts those batches from an empty Brain through the discarded
multi-batch staging validator, and the trusted inverse reconstructs every manifest-declared byte
and identity exactly. The high-cardinality and timestamp fixtures must also pass. Focused checks,
`make verify`, and independent implementation review must pass at the same committed source
identity.

Stop at that boundary. Do not start `SN1-W2`, `OB1-W0`, durable import, outbound Secure Node export,
live migration, publication, deployment, or production work inside this milestone.

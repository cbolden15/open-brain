# Facet contract kit (C0)

Reconciliation note: PR79 now targets schema15/runtime10/Portable10 over main14/9/9.
Earlier commit and catalog pins below describe the original verified branch only.
C0/O0 wire revisions and fixture bytes remain pinned; integrated verification is recorded in
`docs/design/facets-main-reconciliation.json`. Future workers must use its verified candidate.

The kit defines the stage-one interfaces before facet storage and adapters are implemented.
It is an internal development contract. It does not enable facet commands, grant new capabilities,
or register Portable v10. The accepted behavior remains in
[the stage-one specification](owner-defined-facets-stage-one.md).

## Consumer inputs

Start E, A, T, P, X, and V from the verified C0 commit recorded in the workstream handoff. Pin
that commit, the contract revision and the fixture manifest SHA-256 in each task. P, X, and V
can prepare concurrently; their final integration requires verified engine outputs and the
producer evidence assigned in the task manifest. Repin every continuation to its exact inputs.

| Input | Purpose |
|---|---|
| `packages/engine/src/open_brain_engine/engine/facet_contracts.py` | Immutable request/response values, protocols, normalization, canonical semantic request hashes |
| `packages/engine/src/open_brain_engine/engine/facet_schema.json` | Closed wire vocabulary, including publication and vault boundaries |
| `packages/engine/src/open_brain_engine/engine/facet_evidence.py` and its schema | Draft evidence validation and synthetic replay assertions |
| `packages/engine/tests/fixtures/facets/` | Synthetic evidence, expected outcomes, hashes, acceptance ownership and task inputs |
| `packages/app/tests/facet_fake.py` | Explicit scripted outcomes for adapter development without a Brain |
| `docs/design/facet-task-manifest.json` | Exclusive file reservations, exact consumer inputs and task verification gates |

Repository-relative paths in this document resolve from the consumer's own worktree.
The evidence schema is a draft, separate from the released Portable schema catalogs. Its
validation does not prove transaction durability, authorization, publication recovery, or restore.
`wire-cases.json` contains independent examples for all 23 operations and rejected raw inputs.
Treat each example as a separate case; repeated synthetic operation IDs across those examples
do not describe one executable mutation sequence. The three evidence sidecars form the coherent
history used for replay assertions.

## Using the contracts

Use `request_from_wire(operation, arguments)` for decoded objects, and `parse_wire(raw)` at a
raw JSON boundary. For the larger publication request bound, pass
`operation="review.propose_from_facet_query"` to `parse_wire`. The parser rejects duplicate keys,
invalid UTF-8 and non-integer numeric tokens. The request constructor expands defaults and
normalizes names and selector sets before hashing. Serialize responses with `response_to_wire`.

Pass `EffectiveAuthority` separately as a keyword argument to a protocol method. Authority is
never a request field. The production engine must revalidate it at admission and on retries.
The contract classes enforce grammar; the later engine enforces current state and authority.

Semantic request hashes cover the canonical `{method, arguments}` envelope, with normalized
arguments. Operation IDs remain in those arguments. Session and transport identifiers are absent.
Do not compute an independent adapter hash or compare raw user JSON for idempotency.

The draft evidence vocabulary also reserves `facet.query.attach.publication` for the publication
stage machine. It binds the operation, page, query, query selection version and publication IDs.
It is not a public operation and is absent from the public request parser and task protocols.

## Using the fake

Import `FakeFacetEngine` from `packages.app.tests.facet_fake` in tests only. It implements
`FacetTasks`, `FacetPublicationTasks`, and `FacetVaultTasks`. Configure each anticipated call
with `fake.configure(operation, response_or_error, expected_request=arguments)` and pass the fake
to the consumer under test. A response may be a real typed response or a closed wire object.

Each call consumes one configured result and records its typed normalized request and the exact
trusted authority object. A retry needs another configured result. `fake.assert_exhausted()`
catches missing expected calls. An unconfigured call raises instead of synthesizing success;
invalid response fixtures fail during configuration. The fake does not implement persistence,
permission checks, or replay semantics. Tests against it prove interface behavior only.

## Verification and handoff

Run the focused C0 suites from the repository root:

```sh
uv run --frozen pytest -q packages/engine/tests/contract/test_facet_contracts.py packages/engine/tests/contract/test_facet_evidence.py packages/engine/tests/contract/test_facet_boundary_adversarial.py packages/engine/tests/contract/test_facet_contract_integration.py packages/app/tests/unit/test_facet_adapter_contracts.py
```

The coordinator also runs `make verify`, `git diff --check`, and
`actionlint .github/workflows/ci.yml` before declaring C0 verified. Protocol assignments in the
adapter tests are checked by the repository's strict mypy gate.

A contract change updates the types, schema, fixture bytes and manifest together. Advance the
internal contract revision when accepted contract bytes change after a verified handoff, record
affected consumers, and rerun their checks. A changed manifest hash invalidates previous fixture
verification. Historical Portable v1–v5 schemas remain unchanged. V6 catalog freeze waits for
integrated F17–F22 evidence and independent review.

Contract revision `facets-c0-v2` admits retained canonical revision UUIDs, including the UUIDv5 IDs produced by historical Portable restore. New facet IDs remain UUIDv4. This correction invalidates evidence-schema and fixture-manifest checks from revision v1; consumers must rerun them on the new pin.

Revision `facets-c0-v3` also requires a new publication to select every provenance capture. Only updates may inherit prior provenance. The cross-space create fixtures now select both sources; dependent binding checks must rerun.

C0 revision `facets-c0-v4` rejects no-op receipts unless their complete requested effect and
expected version match one reachable historical event boundary. The original receipt remains
valid after later changes. Positive no-op cases include removal from unclassified subjects and
existing archived memberships; impossible versions and membership effects are negative fixtures.
Verified revision commit: `ed74327f34ec2526fd36b1737e36b05bcd2cdc62`. Fixture-manifest SHA-256:
`95e85b535eeb4263a6e430970ecf1d49de8d0b72de7425e41be703897d93dbc5`.

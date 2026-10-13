# Obsidian O0 contract kit

Revision: `obsidian-o0-v1`. Obsidian is the Stage 2 interface. No standalone desktop feature
work or remaining Stage 2 task is part of O0.

## Frozen inputs

Stage 1 passed full checks at `abeab52136a668fd851a6d4a7c72aa8fa67baedb`, followed by
the independent F01–F22 review and handoff `490bd05609e1cf3046946da1b935096de987d024`.
C0 revision is `facets-c0-v4`, commit `ed74327f34ec2526fd36b1737e36b05bcd2cdc62`.
Its fixture manifest SHA-256 is
`95e85b535eeb4263a6e430970ecf1d49de8d0b72de7425e41be703897d93dbc5`.
The task manifest also pins the frozen schema 11 and Portable 6 catalog digests.

The O0 fixture manifest is `packages/obsidian-plugin/tests/fixtures/facets/manifest.json`.
Its SHA-256 is recorded in the task manifest and handoff when verification completes.
Changing a frozen input invalidates affected consumer checks. The coordinator owns contract
revisions, fixtures, registration, integration and the ledger; workers cannot silently revise them.

## Executable surfaces

| Surface | Contract |
|---|---|
| `tools/generate_obsidian_facet_types.py --check` | Deterministic TypeScript aliases and operation maps for all 23 authoritative C0 operations. The full repository Python suite checks drift. |
| `packages/obsidian-plugin/src/facet-wire.ts` | Closed schema validation, semantic checks, strict JSON, normalized requests, full frames, negotiated capabilities and managed identity DTOs. Imports the authoritative schema instead of copying it. |
| `packages/obsidian-plugin/src/facet-inspection.ts` | Closed existing publication inspection with required v5 `facet_selection`, selected versus inherited evidence and matching outer fields. |
| `packages/app/src/open_brain/services/plugin_facet_contracts.py` | Pure descriptor and UTF-8 framing helpers for B. `decode_request` accepts only explicitly supplied available operations. `response_size` measures the exact envelope for future engine page admission. |
| `packages/engine/src/open_brain_engine/engine/managed_record_identity.py` | Read-only `resolve_managed_record(engine, workspace_id=..., relative_path=..., authority=...)`. Immutable result exposes `.to_wire()`. No bridge registration or schema migration. |

Existing plugin registration, manual publication parsers and bridge behavior are unchanged.
An older runtime has no facet descriptor and remains usable for its existing commands.
The kit is preparatory code: importing it does not enable facet features.

## Negotiation and transport

Protocol remains `open-brain-client`, version 1. The handshake adds this closed descriptor:

```json
{"facet_capabilities":{"version":1,"operations":[
  {"operation":"facet.query.page","dto_versions":[1],"request_bytes":65536,"response_bytes":250000}
]}}
```

`negotiateFacets` intersects descriptor entries, existing `operations` and client-known names.
Unknown future operation names are ignored; incompatible DTO versions disable their operation.
Duplicate entries, unknown descriptor fields, invalid numbers or limits above client maxima
make the descriptor incompatible. Absence means `upgrade_required`. An empty intersection is
unavailable. Fixture hashes establish development pins, not runtime compatibility or authority.

The normal handshake parser accepts additive fields. A compatible entry may advertise a smaller
limit; B must enforce that negotiated limit as well as the codec's hard maximum. The descriptor
must list only registered operations allowed by the trusted runtime's effective authority.
Direct calls must check the same intersection, then apply current grants, scope and quotas.
The descriptor itself never grants authority.

| Class | Request bytes | Response bytes |
|---|---:|---:|
| Ordinary facet operations | 65,536 | By response class below |
| `review.propose_from_facet_query` | 1,048,576 | 4,096 |
| Lists and `facet.query.page` | 65,536 | 250,000 |
| Single reads (`get`, `state`, `preview`) and resolver | 65,536 | 1,048,576 |
| Other mutations, including vault apply | 65,536 | 4,096 |

These are actual UTF-8 JSON bytes including envelope and one LF. Strings use normal JSON
escaping; Python encodes with `ensure_ascii=False`. Frame request IDs are `plugin_` plus a
canonical UUIDv4. A frame is exactly one line; duplicate keys, invalid UTF-8, fractional/exponent
integer tokens, unsafe integers, unknown fields and malformed identifiers are refused.
Responses must match the expected request ID and closed success/error envelope. Errors carry
only a known public `code`; no path or untrusted exception text is exposed.

The 1 MiB publication allowance does not enlarge its 64 KiB Markdown field. B must implement
bounded live reads before decoding, operation-specific refusal before dispatch, pre-effect
receipt reservation, page admission and session quota accounting. Page admission must satisfy
both the existing conservative engine DTO size and the full UTF-8 plugin envelope. Keep existing
operation limits. The O0 serializer's rejection after receiving a result is not a commit guard.

## Managed identity and custody

`workspace.record.resolve` accepts exactly `dto_version: 1`, `workspace_id` and a normalized
relative POSIX Markdown path, at most 4,096 UTF-8 bytes. It does not accept a record ID, frontmatter
identity or authority claim. The result contains `status: "ok"`, `dto_version: 1`, `workspace_id`,
`record_id`, `revision_id` and `{kind, id}` as `subject`. Canonical subjects use the page ID;
source subjects use a logical source ID with matching current capture record/revision anchors.

Real Stage 1 managed notes map only canonical pages. A codec fixture can express a source
result, but no source-file mapping is claimed or enabled. Search results can classify sources
by engine ID without a managed file.

The engine helper requires owner-local effective authority, a registered active canonical note,
current eligibility and an explicit observation after the latest materialization/replacement.
It verifies root custody, confined reads and retained device/inode/mode/link count. Protected
content must match accepted managed bytes and the current canonical revision. Registered facet
metadata may differ; unregistered metadata and tampered identity fields may not.
The helper rechecks file/root state and returns closed `not_found` on unavailable custody.
It never creates an observation, projection, preview or semantic mutation.

Atomic editor saves invalidate the retained inode baseline. B must provide an explicit safe
observation action or explain the existing `open-brain workspace observe` command. Silently
calling `workspace.reconcile` is prohibited because that operation also accepts content.
Incomplete or oversized vault inventories may conservatively make resolution unavailable.

## Publication inspection

The new parser requires the complete existing `ReviewPublicationService.show` result with
`status: "shown"`, proposal status/identity, title/Markdown, destination, page and target,
operation, provenance capture IDs, selected capture IDs, evidence, review token, predecessor
hash/publication, projection status and `facet_selection`. It does not alter the legacy manual
parser. B/P must explicitly route facet inspections through it.

`facet_selection` is the engine's closed v5 binding, including actor/tenant/role claim,
query/edit/selection/attachment versions, selector, source states and membership snapshots.
Validation checks source and membership order, unique IDs, safe versions, create/update
predecessors, selected/provenance relationships and agreement with the outer inspection.
Patch bindings and unknown fields are refused. The parser validates evidence structure;
only the real engine validates authority, freshness, digests and decisions transactionally.
The displayed evidence checksum covers its excerpt; it must not be equated with the immutable
source checksum in `source_states`. Both are retained and checked for digest grammar.

The shared fixtures include exact Python-encoded frames for all 23 operations and one inspection
captured from a real Stage 1 proposal using synthetic data. Independent tests also cover frozen
cross-space create/update bindings, including inherited provenance.

## Verification and next gate

Focused checks:

```sh
uv run --frozen pytest -q packages/app/tests/unit/test_obsidian_facet_contract_kit.py packages/engine/tests/integration/test_managed_record_identity.py
npm --prefix packages/obsidian-plugin test -- tests/facet-wire.test.ts
npm --prefix packages/obsidian-plugin run typecheck
```

The integrated gate is `make verify`, `git diff --check` and `actionlint .github/workflows/ci.yml`,
followed by independent review. The repository gate includes existing desktop regression checks;
it adds no desktop features. O0 must pass before B/U/P/V/T/I dispatch. Full Stage 2 O01–O15
remain pending: codec/fake tests cannot prove live stream admission, persistence, authorization,
publication recovery, reconciliation integration, restore or actual Obsidian usability.

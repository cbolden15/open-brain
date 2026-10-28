# Compact search response and pagination contract

Status: proposed contract, 2026-10-28. Implementation has not started.
Issue: [#127](https://github.com/cbolden15/open-brain/issues/127), compact search only.
Baseline: `main` at `d7e0bb90f511`; schema/runtime/Portable 23/16/18.

Compact search returns projected titles, record identities, kind and space without excerpts. It provides exact visible counts and cursor continuation. Title grouping is a display operation: records keep their identities and revisions, and nothing is merged or deleted.

## Architecture and compatibility

| Approach | Decision |
|---|---|
| Shared engine operation with global grouping, exact visible counts and bounded continuation | Recommended. CLI and MCP use one authorization and pagination implementation. Full-set projection/count cost needs measurement before implementation acceptance. |
| Remove excerpts and group only the current page in each adapter | Rejected. Groups repeat unpredictably, counts are incomplete, and CLI/MCP can disagree. |
| Separate group-list and group-member endpoints | Sound larger alternative. It gives each group independent browsing, but adds a second cursor workflow. The single continuation stream below covers this issue. Revisit if clients need independent group expansion. |

Add engine operation `search.compact.page`, DTO version 1, with its own closed request/response grammar and task method. Keep the frozen `search.page` v1 grammar and legacy response shapes unchanged. New compact fields must not be added to an existing `RecordSummary`.

Core MCP `brain_search` and `brain_search_page` accept `view: "compact"` only when a compact implementation is injected and the session has the existing `search` capability. Missing `view`, or `view: "full"`, keeps each tool's current full response, defaults and bounds. Compact-only fields with a full view are invalid. Compact mode on either tool dispatches the same new operation and shares the negotiated search/read session budget; changing tool names cannot reset that budget.

Expose equivalent CLI flags on `search` and `search-page`: `--compact`, `--group-by title|none`, `--count-only`, `--target-bytes`, and `--cursor`. The latter four require `--compact`; existing query/filter/mode/limit flags map to the request below. Compact mode is lexical by default on both commands. JSON uses the exact success envelope below; human output renders the same selected rows and counts with the existing terminal-control sanitization.

MCP `tools/list` advertises the compact argument branch only when callable. For explicit discovery, extend `brain_contract_describe` with the closed opt-in request `{"extensions":["compact_search_v1"]}`; this first version accepts that exact extension list only. Its separate closed response is:

```json
{
  "status": "ok",
  "contract_version": "compact-search.v1",
  "operations": [
    {"name": "search.compact.page", "dto_version": 1, "required_grants": ["search"]}
  ],
  "limits": {
    "request_bytes": 65536,
    "default_limit": 20,
    "maximum_limit": 100,
    "default_target_bytes": 4096,
    "minimum_target_bytes": 2048,
    "maximum_target_bytes": 8192,
    "cursor_ttl_seconds": 900
  }
}
```

Advertise that operation only with both the implementation and search authority; otherwise the opt-in describe call returns `unsupported_capability`. An empty describe request retains the existing response exactly; old T03 consumers must never receive the new grammar implicitly. Older servers reject the extension instead of fabricating compact support. Plugin/Obsidian and the separate harness MCP adapter are outside this first implementation; neither advertises compact support. A same-named tool is not evidence of this capability.

This adds no durable record, migration, Portable content or runtime admission requirement. Coordinates remain 23/16/18. Cursor files remain private operational state. Search does not grant content-read or history-read; a subsequent body read still requires its own capability. The OAuth decision and compatibility-history cleanup in #127 are separate work.

## Request

The engine request has exactly these fields; only `dto_version` and `query` are required. MCP wrappers additionally accept `view` and remove it before engine dispatch.

| Field | Type, default and meaning |
|---|---|
| `dto_version` | Integer `1`. |
| `query` | Existing search query grammar: 1–500 Unicode characters, at most 4096 UTF-8 bytes; nonblank and containing a letter or number. |
| `filters` | Existing closed object, default `{space_ids: [], payload_families: [], record_types: []}`. Each supplied object contains all three keys; array bounds, identifier forms and empty-array semantics match `search.page` v1. |
| `mode` | `lexical` (default), `hybrid_preferred`, or `hybrid_required`. |
| `group_by` | `title` (default) or `none`. |
| `limit` | Integer 1–100, default 20. Counts record references across all rows, not groups. |
| `target_bytes` | Integer 2048–8192, default 4096. Hard maximum for the canonical UTF-8 success envelope, including counts and cursor. |
| `count_only` | Boolean, default false. Compute counts without returning rows or allocating continuation state. |
| `cursor` | Null (default) or an existing opaque handle, at most 128 characters. Non-null is invalid with `count_only: true`. |

Unknown or duplicate keys, invalid UTF-8, NUL, lone surrogates, nonfinite numbers, fractional/exponent integer tokens and booleans in integer fields are invalid. Tool arguments cannot supply authority, grants, projection rules, ranking versions or a raw continuation offset.

Illustrative core MCP request; all IDs in examples are synthetic:

```json
{
  "dto_version": 1,
  "query": "release planning",
  "view": "compact",
  "filters": {"space_ids": [], "payload_families": [], "record_types": []},
  "mode": "lexical",
  "group_by": "title",
  "limit": 20,
  "target_bytes": 4096,
  "count_only": false,
  "cursor": null
}
```

## Success response

All top-level fields shown below are required. No query echo, excerpts, body, provenance, source locators, scores, filesystem paths or private cursor bindings are returned. The tool description and MCP text wrapper continue to label all results as untrusted reference data. Title equality does not establish duplicate content or shared trust.

```json
{
  "status": "ok",
  "dto_version": 1,
  "view": "compact",
  "group_by": "title",
  "count_only": false,
  "mode_used": "lexical",
  "count_scope": "lexical_matches",
  "total_matches": 3,
  "total_groups": 2,
  "results": [
    {
      "group_id": "1111111111111111111111111111111111111111111111111111111111111111",
      "title": "Release planning",
      "title_truncated": false,
      "record_type": "source",
      "payload_family": "text",
      "space_id": null,
      "match_count": 2,
      "member_offset": 0,
      "members": [
        {
          "record_id": "capture_00000000-0000-0000-0000-000000000001",
          "revision_id": "capture_00000000-0000-0000-0000-000000000001"
        },
        {
          "record_id": "capture_00000000-0000-0000-0000-000000000002",
          "revision_id": "capture_00000000-0000-0000-0000-000000000002"
        }
      ],
      "members_complete": true
    },
    {
      "group_id": "2222222222222222222222222222222222222222222222222222222222222222",
      "title": "Release checklist",
      "title_truncated": false,
      "record_type": "canonical",
      "payload_family": "text",
      "space_id": null,
      "match_count": 1,
      "member_offset": 0,
      "members": [
        {
          "record_id": "page_00000000-0000-0000-0000-000000000003",
          "revision_id": "revision_00000000-0000-0000-0000-000000000004"
        }
      ],
      "members_complete": true
    }
  ],
  "complete": true,
  "next_cursor": null,
  "warnings": []
}
```

Each row has exactly the keys in the example. `record_type` is `source|canonical`; `payload_family` retains its existing four-value enum. `space_id` is an authorized space ID or null for an unassigned record, never an undisclosed-space placeholder. Each nonempty `members` array contains only `{record_id, revision_id}` pairs using existing record/revision identity rules. Revision IDs let a separately authorized client request an exact body without another search. All counts and offsets are nonnegative JSON integers within the shared safe-integer range; `match_count` is positive for a returned row.

`total_matches` counts distinct `(record_type, record_id)` matches in the entire eligible result set before grouping. `total_groups` counts groups in that same set, not rows on this page. Counts do not depend on `limit`, the cursor position, title truncation or the byte budget. They are exact for `count_scope`, never estimated or inferred from a limited page. `warnings` retains only the existing `model_unavailable` fallback warning when applicable; a stale projection is an error, not a partial-count warning.

With `count_only: true`, return the same envelope with `count_only: true`, `results: []`, `complete: true` and `next_cursor: null`, even when counts are positive. An ordinary zero-match response has both counts zero and those same empty/terminal fields. In every response, `complete == (next_cursor == null)`.

## Visibility and count scope

Create one eligible result relation inside one existing engine read snapshot. Apply the query and filters together with current lifecycle/head/availability, principal, session, tier, space, egress, consent, sharing and final projected-record checks. Use that relation for rows, group identities, counts, ranking and continuation. Index rows alone are not a sufficient count oracle. A projector `not_found` row contributes nothing. A visible row whose indexed title/body disagrees with its current projection fails the compact request with `projection_stale`, including when the row would occur after the requested page.

| Actual mode | `count_scope` | Meaning of the exact totals |
|---|---|---|
| Lexical, including fallback | `lexical_matches` | All current authorized lexical matches, before any page limit. |
| Hybrid | `hybrid_candidates` | The distinct authorized result set produced by the existing fusion of up to 50 lexical and 50 semantic candidates. At most 100 records. This is not a corpus-wide semantic-match count. |

Compact search does not change lexical matching or the hybrid candidate/ranking algorithms. `hybrid_preferred` fallback returns `mode_used: "lexical"`, lexical count scope and `warnings: ["model_unavailable"]`. `hybrid_required` fails if the model is unavailable. No model/helper is invoked for lexical requests, including counts. There is no silent hybrid or network fallback.

Hidden records must not affect returned groups, counts, ordering, page boundaries or visible-state invalidation. Count and grouping logic must use the same final visibility checks as rendering; a raw `COUNT(*)` or global title histogram followed by result filtering is forbidden. Use the existing authority-aware generation rules, rather than substituting a global content-update counter. Existing explicit policy/key/root changes still invalidate cursors as prescribed below. This is an output non-disclosure invariant, not a timing-isolation claim.

## Grouping and ordering

For `group_by: "title"`, group by the complete NFC-normalized, public-projected title plus `(space_id, record_type, payload_family)`. Comparison is exact and case-sensitive. Do not trim, case-fold, use raw private titles or include denied records. Different spaces and source/canonical records remain separate even if their titles match. Null space IDs form their own partition. Empty projected titles remain singleton groups, so unrelated untitled records do not collapse together.

`group_id` is an opaque 64-character lowercase hexadecimal identifier stable within one cursor chain, scoped to its authority and generation. Derive it from the complete public grouping key with a private keyed binding; singleton keys also include `(record_type, record_id)`, including empty-title and `group_by: "none"` rows. Never expose an unkeyed digest of private source identity. Clients may join fragments by this ID but cannot use it to read or mutate a record. Separate first-page searches may issue different IDs.

Preserve the current total record ordering, including its existing ranking-version-specific tie breakers. A group's position is its best member's position in that order. Members of a group follow that same record order. Deliver each group contiguously before moving to the next group. Grouped display therefore intentionally differs from the flat relevance order: a group's lower-ranked members precede the next group's best member. `group_by: "none"` uses singleton groups and exactly preserves the flat order; every row has `match_count: 1`, `member_offset: 0` and `members_complete: true`.

Return at most 256 UTF-8 bytes of a projected title, truncating only at a code-point boundary and including a final ellipsis within that bound. Set `title_truncated` exactly when bytes were omitted. Grouping and ordering always use the complete title, so equal shortened display strings do not collapse distinct groups. The human renderer may sanitize controls after selection, without changing membership or counts.

## Pagination and response bounds

`limit` is a maximum number of member references, not rows. A page contains at most `limit` rows and at most `limit` members in total. Stop before the next member would exceed either that count or `target_bytes`. Measure the complete canonical serialized success envelope, including all row metadata, counts and the actual/null successor cursor. Full-group metadata is repeated only when a group needs another fragment.

For each fragment, `match_count` is the group's full eligible member count; `member_offset` is the number already delivered in earlier pages. `members_complete` is true exactly when `member_offset + len(members) == match_count`. A group larger than the page budget continues first on the next page, with the same `group_id` and the advanced offset. A member occurs once in an uninterrupted successful traversal. Replaying an earlier cursor deliberately repeats its page.

Example: one title has 125 matches and `limit` is 50. With sufficient bytes the fragments contain 50, 50 and 25 members at offsets 0, 50 and 100. All three responses report `total_matches: 125`, `total_groups: 1` and `match_count: 125`. The first two have `members_complete: false` and a successor cursor. Only the last is terminal. A byte-limited page may contain fewer than 50 members without implying the search is complete.

Look ahead far enough to distinguish exhaustion from an exact-fit page. Issue a cursor only when at least one eligible member remains. Bind continuation to the last member actually returned, never a candidate inspected and dropped for size. A nonterminal success always returns at least one member; if even one member plus required metadata cannot fit, return `response_too_large` without advancing continuation. Do not silently omit a group or trim its IDs.

MCP adapters must also measure the final encoded tool response, including duplicated structured/text content and JSON escaping, against the existing negotiated transport/session budgets. If necessary, choose a smaller valid page and regenerate its successor from the final emitted member. If no member fits, fail rather than discard already selected results. Byte bounds reduce response size but do not promise compatibility with an unknown client tokenizer or token cap.

## Cursor contract and errors

Reuse the authenticated, root-bound `CursorStore`: handles are opaque, at most 128 characters, and each issued handle retains the existing 900-second TTL. Private state records the group anchor and the last emitted member, not a public integer offset or an indefinitely retained result snapshot.

Bind a cursor to operation/DTO version, normalized query, canonicalized filters, requested and actual mode, grouping/version, ranking/version, `limit`, `target_bytes`, the full existing authority binding, and the existing visible generation/projection/root/key/embedding bindings. Query normalization means the existing outer whitespace trim, without additional case folding. Filters are validated and sorted as in current paging. Effective defaults are bound, so omitting a field and supplying its default are equivalent. Continuations must repeat the effective search parameters.

Resolve authority and generation on every continuation. Do not keep a SQL transaction open across calls. Successful continuation requires the original visible result set still to be current; otherwise the client restarts with a null cursor. Reusing a live cursor under unchanged conditions returns the same members, groups and counts without consuming it. A newly allocated successor handle need not be byte-identical on replay.

| Condition | Closed error code and behavior |
|---|---|
| Unsupported compact operation or missing advertised implementation | `unsupported_capability`; never substitute the full response. |
| Invalid fields, versions, identifiers or count-only/cursor combination | `invalid_arguments`. |
| Missing search authority | Existing capability refusal; no counts or group information. |
| Forged/malformed/missing handle, wrong purpose, changed query/filter/grouping/limit/budget, or different authority binding | `cursor_invalid`. |
| Expired issued handle, rotated key, moved/copied root, changed visible generation, or lexical/hybrid availability switch under the same request | `cursor_stale`. A removed/unknown handle retains existing `cursor_invalid` behavior. |
| A current visible index/projection mismatch | `projection_stale`; no partial rows or totals. |
| Required semantic resource unavailable | `model_unavailable`. |
| No complete member fits the payload/transport budget | `response_too_large`. |
| Existing shared call quota exhausted | `operation_pending`. |
| Existing shared encoded-byte quota exhausted | `response_too_large`. |
| Cursor custody, bounded lock acquisition or engine snapshot unavailable | `operation_pending`. |

Errors retain the existing bounded `{status: "error", error: {code: ...}}` shape inside the appropriate transport wrapper, without query/body residue, hidden counts or failing record IDs. No new error namespace or privilege grant is required.

## Acceptance and remaining feasibility check

| Case | Required result |
|---|---|
| Same title in one authorized partition | One logical group; every record/revision pair remains reachable. |
| Same title across spaces, record types or payload families | Separate groups, even when the requesting owner can see all of them. |
| Composed/decomposed title, case differences, blank titles and equal truncated titles | NFC equality only; case differences and different full titles stay distinct; blank titles stay singletons. |
| Group larger than both member and byte limits | Contiguous fragments, correct offsets and exact totals; no missing or repeated member during forward traversal. |
| Exact-fit page, empty set, and count-only with positive matches | Cursor exists only for remaining members; terminal/count-only semantics match the contract. |
| Hidden records inserted, changed, routed, retracted or given a matching title | Public rows/counts/order/boundaries stay the same; preserve existing visible-generation invariants. |
| Visible record/policy mutation, cursor replay and every bound-argument change | Existing stale/invalid distinction; replay repeats rows without consuming continuation. |
| Hybrid candidates, unavailable resource and fallback | Exact totals for the declared candidate/lexical scope; no corpus-wide semantic total claim. |
| Stale projection beyond the first page; inaccessible final projection | Stale visible candidate fails the whole count/page; inaccessible candidate contributes nothing. |
| Hostile/long titles and MCP structured/text duplication | No excerpt or private locator; actual complete response fits the selected payload and transport budgets. |
| Legacy CLI/MCP/T03 requests and empty describe | Existing shapes, defaults, strict grammar and fixtures remain unchanged. Unsupported adapters refuse compact arguments. |

Before implementation acceptance, measure exact counting and global grouping on synthetic 1,000- and 10,000-record corpora, with both mostly unique titles and one dominant title. Record first-page/count-only latency, continuation latency, peak memory and final MCP bytes, separately from existing full search. Full-set projection is more work than today's lazy lexical page; its cost is unresolved. Share the engine candidate/projection path, use bounded batches or SQLite sorting rather than an unbounded list of projected bodies, and never turn an internal scan cap into an allegedly exact total. If acceptable cost requires a new persistent index or a change to ranking/candidate semantics, revise this contract before adding either.

## Implementation anchors

Paths are repository-relative. The frozen source and existing test contracts remain the compatibility authority.

| Area | Existing source |
|---|---|
| Eligibility, lexical/hybrid ranking, snapshots, authority binding and page bounds | [engine/paging.py](../../../packages/engine/src/open_brain_engine/engine/paging.py) |
| Cursor custody, replay, expiry and rotation | [engine/cursors.py](../../../packages/engine/src/open_brain_engine/engine/cursors.py) |
| Frozen values and closed grammar | [engine/t03_contracts.py](../../../packages/engine/src/open_brain_engine/engine/t03_contracts.py), [engine/t03_schema.json](../../../packages/engine/src/open_brain_engine/engine/t03_schema.json) |
| CLI/MCP dispatch and byte/call budgets | [services/local_entrypoints.py](../../../packages/app/src/open_brain/services/local_entrypoints.py), [services/local_mcp.py](../../../packages/app/src/open_brain/services/local_mcp.py), [services/t03_adapters.py](../../../packages/app/src/open_brain/services/t03_adapters.py) |
| Projection and deliberately distinct source/canonical results | [Retrieval design](../../retrieval.md) |

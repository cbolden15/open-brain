# Reading records and retained history

The Core v0.1 candidate provides filtered search pages and complete projected text. Existing `open-brain search` keeps its bounded first-page behavior. For continuation, use `search-page`:

```sh
open-brain search-page "synthetic launch" --limit 50 --record-type source --payload-family reference_or_file --json
```

Existing released packages may lack these operations despite sharing version `0.1.0`.
Use the [feature/version matrix](core-v01-features.md). The [first-use guide](first-use.md)
executes the full synthetic workflow; this page supplies reference templates with explicit
placeholder IDs and queries.

Each result identifies its `record_id`, immutable `revision_id`, record type, source identity when applicable, and ordered provenance. Payload filters use `text`, `event`, `measurement`, or `reference_or_file`. Repeat `--space-id`, `--payload-family`, or `--record-type` to select multiple values. Spaces narrow relevance within your existing authority.

Use the returned `next_cursor` with the same query, filters, mode and limit to request the next page. Stop only when `complete` is true. A changed dataset or policy returns `cursor_stale`; deliberately restart the search. Tampered, expired, relocated-Brain or mismatched-authority cursors are refused. A cursor grants no access. Optional `hybrid_preferred` falls back visibly when unavailable; `hybrid_required` returns `model_unavailable` when the model is unavailable. No model is downloaded by searching.

Copy IDs from a result and replace the uppercase placeholders:

```sh
open-brain read RECORD_ID --expected-revision-id REVISION_ID --json
```

Read responses contain `content.kind=untrusted_text`, `content.text`, UTF-8 byte offsets, a continuation cursor and a completion flag. Append text in byte-offset order, preserving Unicode and newlines. These bytes represent the complete public projection of the retained record. Capture normalizes text to NFC and line endings to LF before storage; compare reconstructed text with that retained projection when the input uses decomposed Unicode. The default chunk target is 32 KiB; `--target-bytes` can request a smaller chunk. Supply `--cursor` with the same record and expected revision for continuation. Current reads through an old source capture anchor resolve the current proven source head; an old expected revision is refused with `revision_changed`.

Retained revisions require explicit history operations:

```sh
open-brain history list RECORD_ID --json
open-brain history show RECORD_ID --expected-revision-id HISTORICAL_REVISION_ID --json
open-brain relationship list RECORD_ID --json
open-brain decision history RECORD_ID --json
```

A source history follows only proven identity links. Equal text or a shared URL does not merge independent captures. Canonical history follows the publication page, preserving exact reviewed source membership. Unproven historical ordering remains unproven. There is no restore or revert operation.

`relationship decide --help` describes owner-only duplicate, supersedes and contradiction decisions. Decisions bind both exact revisions and the expected relationship version; creation expects version 0 and returns version 1. Reusing an operation ID requires the identical request. Self-edges, stale endpoints and supersession cycles are refused. Relationships do not rewrite sources, merge records or choose a truth winner.

MCP grants are independent and off by default:

```sh
open-brain mcp --allow-search --allow-content-read --allow-history-read
```

Search alone does not grant complete content or historical reads. Grant only the capabilities wanted for that foreground session. Bridge clients discover negotiated operations with `contract.describe`; MCP clients use `tools/list` and `brain_contract_describe`; owner relationship mutations are not advertised to agents. Embedded source instructions remain data and cannot widen these grants. Negotiated paged search and current reads share a bucket of 500 calls and 16 MiB encoded output. Historical listing and reads use a separate bucket with the same limits. One response, including its wrapper, is capped at 1 MiB.

Obsidian and the optional desktop expose capture, routing, draft inspection, approval and opening the managed vault. Approval uses the inspected revision/token, and source text is displayed as untrusted content. A failed refresh after approval is reported as an approved publication needing refresh. Explicit continuation and cancellation keep reads bounded. The plugin must be opened in the same managed vault as its configured Brain.

The local storage format is schema 10 with runtime session 5. Historical source bytes and IDs remain intact. Portable 5 exports include versioned metadata over unchanged legacy evidence. Supported Portable 1–3 imports remain available; standalone Portable 4 import is refused. Legacy connector deliveries without revision ordering and expected-head evidence refuse changed updates; ordered connector integration belongs to the later milestone.

For synthetic checks, add `--data-dir` with an absolute, canonical disposable Brain path. Native installation, exact GUI acceptance and provider/service readiness have their own evidence; see the candidate handoff before testing a build.

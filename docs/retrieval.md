# Retrieval design

Default Open Brain uses SQLite FTS5 for local search. Search runs inside the same
`phase1.sqlite3` database as the live document projection. It needs no model, network access,
downloaded index, daemon, or dependency beyond the SQLite bundled with the native application.

## Ownership and data flow

`search_documents` is the authoritative, rebuildable search projection. It contains result and
capture identity, record type, payload family, space, public-safe title and body text, trust,
bounded provenance, canonical path, and update time. Durable captures and Portable Brain files
remain the source records. Deleting the projection or its FTS index must never delete Portable
Brain data.

`search_documents_fts` is a contentful FTS5 table in `phase1.sqlite3`. It contains an unindexed
`result_id` plus the NFC-normalized, public-safe `title` and `body` copied from
`search_documents`. Queries join the two tables by `result_id`. A small ordinary
`search_fts_identity` table assigns each result ID one stable integer FTS rowid. Update and delete
triggers resolve that indexed mapping instead of scanning the FTS table, and the mapping does not
depend on SQLite's implicit rowid remaining stable across `VACUUM`. Result IDs are immutable once
inserted. SQLite insert, update, and delete triggers keep all three structures synchronized in the
transaction that changes the projection. A failed transaction changes none of them. The mapping and
duplicated text are derived and may be cleared and rebuilt.

The existing `.open-brain/indexes/search.sqlite3` database has different ownership. It is a
disposable snapshot used by Portable Brain operations. Default Open Brain never queries or
incrementally synchronizes it. Until its owning portability operation
replaces it, it may be stale. Status reports it separately as non-authoritative and never prints its
absolute path. This report exists to make a potentially stale plaintext residue visible, not because
default retrieval depends on the snapshot.

## Public indexing boundary

Search must not reveal that a protected value exists. Redacting only the returned excerpt is too
late because a protected query could still select or rank a record.

One engine-owned search-projection helper is the mandatory title/body write boundary. Capture,
review publication, reconciliation, Portable materialization, and the full re-derivation operation
must call it; direct SQL writes of title or body outside that boundary are forbidden. Routing may
change metadata such as `space_id` without rewriting indexed text. The helper:

1. Normalizes it to Unicode NFC.
2. Applies `project_public_result_text`, including the capture's protected source reference.
3. Stores that exact public projection for FTS5 to index.

The helper also derives trust from durable capture and publication evidence. Direct owner canonical
content and owner source material store `owner`; an approved or edited canonical publication stores
`reviewed`; ordinary third-party source material stores `third_party`; and unknown or mixed origin
stores `unverified` even if imported publication records exist. Reconciliation rejects an edited
frontmatter trust label instead of accepting it as authority, and repairs a stale stored trust value
only when the frontmatter agrees with freshly derived durable evidence. Projection writes strictly
validate the durable provenance shape, source-reference link, and durable source-origin relationship;
malformed or unlinked provenance fails closed. Bounded retrieval output has a separate fallback that
can label a legacy malformed origin `unknown` without granting it trust. Tests exercise every current
writer so a bulk path cannot bypass projection or trust derivation.

Search and identifier fetches serve canonical title and body text from `search_documents`, not by
reading canonical Markdown during the query. The canonical file reader remains available to the
page-reading operation, but it does not change matching, ranking, or FTS snippet offsets.
Reconciliation is the sole writer that refreshes projected canonical text after a direct file edit.

Every returned title, snippet, and explanation crosses the projection boundary again with the same
capture-derived protected source reference used during indexing. This second pass contains malformed
or legacy index rows. Identifier fetch reprojects the complete title and body before whitespace
collapse or the 500-code-point clamp. Search verifies the complete stored title and body against the
current projection before it trusts a bounded FTS snippet; a stale row is omitted rather than
revealing that a protected query matched. Identifier fetch and page read apply `allowed_space_ids`
before resolving content; an empty or non-matching allow-list returns nothing. Page title and
Markdown also cross the same NFC-normalized, capture-aware public projection before return. The
durable capture payload and Portable Brain content do not change.

Human output derives its visible label from bounded origin: owner-authored records use `owner`,
third-party records, including reviewed third-party canonical pages, use `third-party`, and unknown
or mixed origin uses `unverified`. Machine-readable results retain the derived stored `trust` value,
including `reviewed`, and add the bounded public `source_origin`. Neither representation returns a
raw source reference.

Human output renders each result on one line. It collapses whitespace and replaces C0, C1, and
Unicode format controls, including bidirectional overrides and isolates, before printing the title,
excerpt, trust label, and public origin. JSON uses escaped control characters and retains the exact
public-projected field values for machine consumers.

## Tokenization and query grammar

The FTS table uses:

```sql
tokenize='unicode61 remove_diacritics 2'
```

Stored text and queries are normalized to NFC first. Matching is case-insensitive, and a query
without diacritics can match the corresponding Latin text with diacritics. The first release does
not claim general word segmentation for CJK script runs. A complete CJK token is retrievable, but a
word inside an unsegmented run may not be.

The CLI accepts one query containing 1 through 500 Python Unicode code points after NFC
normalization. The count is `len(normalized_query)`, not UTF-8 bytes, UTF-16 units, or grapheme
clusters. It rejects blank text, NUL, over-limit text, and text with no Unicode letter or number
using the fixed message `invalid query`. Errors never echo input.

User input is never passed to `MATCH` as FTS5 syntax. The compiler:

1. Splits the normalized query only at Unicode whitespace.
2. Drops any chunk with no Unicode letter or number. If no chunks remain, it returns `invalid query`.
3. Doubles embedded quote characters and wraps each remaining chunk in an FTS5 phrase literal.
4. Builds engine-owned phrase, conjunction, and disjunction expressions from those literals.

Quotes, parentheses, `AND`, `OR`, `NOT`, `NEAR`, `*`, and column-looking input such as `title:` are
therefore text, not syntax. Punctuation inside a retained chunk is tokenized by `unicode61`; this
keeps code-like text such as `api_client_v2` searchable without enabling prefix queries.

Ordinary search uses the generated disjunction so an incidental missing word does not erase all
results. Ranking distinguishes complete-title matches, complete-query phrases, conjunctions, and
disjunction-only matches. The relevance fixture fixes this recall behavior; there is no raw
advanced-query mode in the first release.

## Ranking and snippets

One SQL statement defines an authorized projection relation containing the `space_id`,
`allowed_space_ids`, `payload_family`, and `record_type` predicates. Every FTS candidate CTE joins
that relation before its rows can enter the rankable result relation, rendering, ordering, or
`LIMIT`. An empty allow-list returns no rows, `NULL` space IDs are denied whenever an allow-list is
present, and an allow-list larger than 256 IDs is rejected with a fixed validation error. A denied
row can never be returned or consume a result slot, even if its lexical match would otherwise score
first.

The first release uses one shared FTS5 table. SQLite may execute its global `MATCH` scan before the
relational join, and `bm25()` corpus statistics include every indexed public-projection row,
including rows outside a scoped caller's allow-list. Authorization still gates every rankable and
renderable candidate inside SQL; it is never a post-ranking Python filter. Per-space indexes or
custom authorized-corpus scoring would also isolate ranking statistics, but they add storage and
multi-space query complexity. That architecture is deferred pending performance, index-size, and
multi-space search evaluation.

Results sort by these keys:

1. Complete-query phrase in the title.
2. Every retained query chunk in the title.
3. Complete-query phrase in either indexed field.
4. Every retained query chunk across the indexed fields, then disjunction-only matches.
5. FTS5 `bm25()` in ascending order, with result ID weight `0.0`, title weight `10.0`, and body
   weight `1.0`, followed by canonical-before-source, NFC title, and result ID tie-breakers in
   SQLite binary order.

The API does not expose SQLite's floating-point score. Tests assert relative order instead.

FTS5 `snippet()` chooses the best indexed column, wraps matches with `[` and `]`, uses `…` for an
omission, and returns at most 24 tokens. The engine then reapplies the public projection and limits
the excerpt to 500 Python Unicode code points. This simple clamp may split a grapheme cluster but
never a Python code point.

Explanation precedence is fixed. `title match` means every retained query chunk matched the title
column, whether or not those chunks also form a phrase. Otherwise `exact phrase match` means the
complete generated phrase matched either indexed field. Every other returned row is `lexical match`.
An explanation never includes the query.

## Freshness, deletion, and rebuild

Capture, publication, reconciliation, routing, and Portable materialization update the projection
through the phase1 transaction boundary. SQLite triggers make title and body changes visible to FTS
in that same commit. Deleting a projection row removes its FTS row in the same commit.

The local CLI invokes the existing bounded reconciliation task immediately before each
search. Direct owner edits therefore become visible in that command or the command fails safely if
the canonical inventory is invalid. The engine retrieval capability remains read-only; it searches
the last committed projection and never scans or mutates
the vault. Invalid or unrelated files are not silently overwritten.

The live-index rebuild acquires the existing shared-writer lease, clears only derived FTS state,
and repopulates it from `search_documents` in one transaction. It is an engine maintenance
operation, not a default CLI command. The rebuild never touches the disposable portability
snapshot. Ordered results before and after a complete rebuild must match.

A separate engine-internal maintenance operation performs full projection re-derivation after a
public-projection rule change or suspected projection leak. It validates canonical Markdown and
durable provenance, reads source text and protected source references from durable captures,
re-creates every source and canonical `search_documents` row with the current projection rules, and
rebuilds the stable identity map and FTS table under the existing writer lease. Missing, malformed,
or unlinked durable input fails closed without publishing a partial replacement. This is the operation meant by
“rebuildable projection”; rebuilding FTS alone cannot repair text produced by an older redaction
rule. Neither rebuild is exposed on the read-only retrieval task or the default CLI.

Existing pre-release version-1 databases receive one atomic adoption step when the FTS table is
first created: normalize and publicly project existing search rows, create the FTS table and
stable identity mapping and triggers, and backfill FTS. The adoption joins every legacy projection
row to its durable capture to obtain the protected source reference; a missing capture or malformed
or unlinked provenance fails and rolls back the whole step. `BrainEngine.open` performs adoption under its existing
shared-writer lease before exposing tasks. `PRAGMA user_version` stays at 1. A read-only view cannot
adopt a database; it raises `ReadViewUnavailableError` when the required FTS table, mapping, or
triggers are absent. OB1-W5 replaces this pre-release setup with the supported migration ledger
before an external release.

FTS5 is a required artifact capability, not a fallback. First creation must fail closed if SQLite
cannot create the configured virtual table. The macOS arm64 and Linux x86_64 native smoke journeys
must each capture and retrieve case, diacritic, phrase, and title-ranking canaries through the frozen
binary, thereby exercising table creation, `MATCH`, `bm25()`, and `snippet()`. If either artifact
lacks FTS5, W3 stops and the release does not ship.

## Diagnostics

Default `status` reads one SQLite snapshot and reports the authoritative live projection, stable
identity-map, and actual FTS document counts plus whether the three result-ID sets agree. It also
reports the disposable snapshot's presence, generation, document count, `authoritative=false`, and
`freshness=potentially_stale` when present.

`doctor --check search-index` uses one read transaction. It succeeds only when the live FTS objects
are readable; projection, identity-map, and FTS counts agree; the mapping FTS rowid and result ID are
bijective with the projection; and FTS title/body bytes equal the public projection. It also runs a
bounded in-memory canary through the configured tokenizer, `MATCH`, `bm25()`, and `snippet()` so a
binary with incomplete FTS5 support fails the check. A stale or absent disposable snapshot does not
fail this check because default retrieval does not use it.

## Relevance acceptance

The versioned synthetic W3 fixture contains title and body hits, phrase and separated-term hits,
composed and decomposed diacritics, one complete CJK token, code-like text, allowed and denied
spaces, stable ties, mutable rows, protected values, and unknown automation provenance. It contains
no committed database or private value.

Release acceptance is exact for this first fixture version:

1. Each query declares an exact ordered expected prefix and named result IDs that must be excluded
   from that prefix. With `k` equal to the prefix length, precision at `k` and recall at `k` are both
   1.0; a single-answer query therefore places its answer first.
2. Phrase, title-versus-body, authorization, and deterministic-tie queries match their complete
   expected relative order, including a title conjunction ahead of a body phrase.
3. Only the named no-result query returns an empty set; distractors do not enter a measured top `k`.
4. Update, delete, and complete rebuild scenarios preserve their named result and ordering contract.
5. Hostile query and protected-value cases return the fixed safe outcome without input residue.

The corpus includes irrelevant distractors for phrase matching, title-versus-body ranking,
disjunction recall, and the deliberate source/canonical duplicate case. Human rendering cases also
include an OSC escape, carriage return, and bidirectional override and isolate controls.

There is no tolerated regression percentage in the first release. A later corpus may add aggregate
metrics, but it cannot weaken these named cases. A positive query missing the top three or a named
ordering failure is also evidence for a retrieval change.

The synthetic fixture is a regression floor, not the only product signal. Default Open Brain sends
no search telemetry. An owner may opt in by submitting a minimized, non-private failed-search case,
or contributors may run a consented evaluation against a separately held corpus. A retrieval-change
proposal may proceed when that evidence names the query class, expected top-k result, observed
no-result or ranking failure, and before/after metric. Before merge, the failure must become either a
synthetic regression or a documented evaluation that cannot safely be committed. Embeddings still
require the plan's separate proof that a local dependency-free hybrid improves the named class.

## Source and canonical result policy

A source record and its canonical page are distinct search documents and may both appear. The source
preserves what was captured; the canonical result represents owner knowledge or reviewed
publication. The first release does not collapse them by capture ID. Canonical-before-source ranking
and the visible record type make the relationship explicit, and `LIMIT` counts result documents,
not unique captures. The relevance fixture includes this deliberate duplicate case so a later
grouping change cannot happen accidentally.

## Deferred retrieval work

Embeddings are deferred. The first release adds no embedding model, model download, vector store,
network call, or runtime dependency. That decision can reopen only when the completed relevance
fixture identifies a named search failure and measured evidence shows a local, dependency-free
hybrid improves it.

The architecturally complete multilingual options are a locally shipped word segmenter or a
measured character n-gram index. Both are pending cross-platform artifact, index-size, and ranking
evaluation. Until then, the CJK limitation stays visible.

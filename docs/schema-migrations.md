# Local SQLite migration contract

Status: W5 implemented and locally verified; owner safety audits and exact-head CI remain pending.
Base: `5a51797`, the merged W4 commit on `goal/open-brain-five-minute-install`.
Authority: [OB1 product completion plan](plans/2026-09-08-ob1-product-completion.md),
“Schema sequencing” and “OB1-W5”.

## Scope and ownership

Local schema version 2 is the first supported release schema. This contract governs only
`.open-brain/state/phase1.sqlite3`. Portable Brain remains version 1. Event storage and the
disposable `.open-brain/indexes/search.sqlite3` retain their existing schemas and versions.

Use a small, dependency-free shared migration primitive extracted from
`storage/sqlite.py`, with a separate engine-owned local catalog and classifier. Reuse the existing
`Migration` value, checksum algorithm, ledger representation, and ordered transaction algorithm.
Keep event catalog defaults and `inspect_event_schema` separate; preserve their public imports.
The shared primitive must not import engine code or know about captures, FTS, or Markdown.

This is the selected architecture. A general migration framework would add dependencies and
configuration without improving the two fixed migrations. Copying the event runner into the engine
would create two implementations of ordering, checksums, and rollback. Neither is needed here.

`storage/migrations.py` owns the shared primitive. `engine/local_schema_catalog.py` owns the frozen
SQL, and `engine/local_schema.py` owns local versions, recognition, and guarded connection functions.
`_LocalStore` consumes those functions and validates again inside application write transactions.
Read connections retain their validated transaction until the consumer closes them.

## Recognized input states

Recognition uses schema structure, not just table names or `user_version`. Historical fixture SQL
must come from the indicated commits, independently of the new catalog.

| State | Required evidence | Writable opening | Read-only opening |
| --- | --- | --- | --- |
| Absent | No database file | Create through migrations 1 and 2 | Report absent; do not create |
| Legacy, version 0 | No ledger; complete W2 structure | Apply 1 and 2 | Report legacy; retrieval requires upgrade |
| Pre-ledger, version 1 | No ledger; exact W2, W3, or W4 supported structure | Adopt with 1 and 2 | Report pre-ledger; retrieval requires upgrade |
| Supported old, version 1 | Exact migration-1 structure and valid ledger row 1 | Apply only 2 | Report supported old; retrieval requires upgrade |
| Current, version 2 | Exact target structure and valid ledger rows 1 and 2 | Validate; no migration work | Allow current readers |
| Newer | `user_version > 2`, or a structurally readable ledger contains a version above 2 | Refuse | Report newer; refuse retrieval |
| Invalid | Every other layout, version, or ledger combination | Refuse | Report invalid; refuse retrieval |
| Recovery required | SQLite reports read-only recovery failure and a confined private rollback journal passes the checks below | Let SQLite restore committed state, then classify and migrate or refuse | Report recovery required; never recover |

W2 is commit `24b2845`: eight base tables, search index, and `route_identity_idx`, with all
capture-submission and route columns already present. W3 is `aaa902f`: W2 plus the live FTS objects.
W4 is `5a51797`: W3 plus three Markdown import tables and their partial index. W2 declares
`search_documents.result_id TEXT PRIMARY KEY`; W3/W4 new databases declare it `NOT NULL` too.
Because W3 used `CREATE TABLE IF NOT EXISTS`, upgraded W2 databases can retain the earlier nullable
declaration. Recognize both historical search-table declarations for W3/W4, then normalize them in
migration 2. Do not widen that exception to arbitrary column differences.

The version-0 fixture is the complete W2 structure with its version unset. Partial predecessors
missing capture-submission or route columns are outside the accepted legacy contract. Remove the
opportunistic ALTER helpers rather than continuing undocumented repairs. Their already-materialized
W2 result is accepted. A zero-byte file or an existing empty database is invalid, not absent.
Only a file created by the current bootstrap attempt may enter the empty-schema creation path.

Compare declared column order, types, nullability, defaults, primary keys, unique and ordinary
indexes, foreign keys, CHECK constraints, partial-index predicates, and trigger definitions. Normalize
insignificant DDL whitespace and the historical `IF NOT EXISTS` spelling using fixed known forms;
do not build a general SQL parser. Recognize SQLite-owned autoindexes and FTS shadow objects
explicitly. Reject unexpected application tables, views, triggers, and partial FTS/import groups.
Pin the FTS5 tokenizer and all four W3 trigger bodies, not just their names.

For an existing ledger, require the exact ledger structure, ordered contiguous versions starting
at 1, exact names/checksums, well-formed application timestamps, and equality between the last
ledger version and `user_version`. An empty ledger on an existing database is invalid. Version 2
without a ledger is invalid. Never repair or overwrite ledger mismatches.

## Catalog and atomic execution

Use the existing `schema_migrations(version, name, checksum, applied_at)` ledger in the local
database. Checksums use the existing canonical serialization of version, name, and ordered SQL
statements followed by SHA-256. Names and statement tuples are frozen in the catalog and checked
against `tests/fixtures/local-schema/catalog-checksums.json`. A later SQL change requires a new migration, not
an edited checksum for an applied version. Application timestamps use the injected UTC clock.

Exactly two entries exist:

| Version and name | Responsibility |
| --- | --- |
| 1, `local_baseline` | Idempotently create the complete W2 baseline and its indexes. Use the historical W2 search-table declaration. Preserve existing records and already-present W3/W4 objects. |
| 2, `local_search_and_import` | Idempotently create W4 import structures and the W3 live-search structures; normalize the search table, project its text safely, and rebuild derived FTS once. |

Migration 1's baseline creation must include `route_identity_idx`, currently created by the route
helper. It does not invent route identities, reset operation stages, or regenerate capture records.
A ledger-backed version 1 contains only the baseline. Pre-ledger W3/W4 adoption may temporarily
have their later structures while migration 1 runs, but both migrations commit together.

Before mutation, validate the complete recognized input, ledger, search identities, required capture
joins, and import relationships. Check database integrity and foreign keys on upgrade; reject null
result IDs, orphaned search captures, unusable protected references, and inconsistent active import
links. Preserve valid pending import reservations, including revisions without a capture and files
without an active revision. Do not classify pending durable work as corruption.

The write path has one transaction owner:

1. Read-only preflight classifies an existing database before writable setup or application recovery.
   Open the confined path without changing journal mode or permissions during validation. Preserve
   root identity and no-follow protections. For creation, distinguish the newly created file from
   an existing empty file without a check-then-create race.
2. Open with persistent setup deferred. Apply only connection-local FULL synchronous, foreign-key,
   and five-second busy-timeout settings. Acquire `BEGIN IMMEDIATE` using the existing journal mode
   and classify under the write lock before any persistent setup, schema write, or application
   write. A concurrent successful migrator becomes a validated no-op.
3. Create the ledger for an accepted no-ledger input, execute each pending statement with `execute`,
   and append its ledger row. Run the local postcondition checks and set `user_version=2` inside
   this transaction. No per-migration commit is allowed.
4. Commit only after target structure, ledger, referential integrity, and rebuilt projection/FTS
   consistency agree. Roll back on exceptions, cancellation, a failed projection callback, or
   commit failure; close the connection on every failure path.
5. After the accepted transaction commits, establish WAL and private file permissions, then return
   the configured connection. All ordinary connections validate before exposing SQL to consumers.
   Current read-only readers never take a write lock. Current writable opens validate without
   migration statements; when persistent setup is already correct, use a read transaction for that
   validation and skip persistent setup entirely.

The current generic `connect_database` applies WAL and permission changes on opening. Calling it
before refusal checks would violate this ordering. Add the smallest confined opening mode needed
to defer those effects; keep all existing event/index callers' defaults unchanged. The locked
classification is the authority for mutation, not the earlier read-only inspection. Persistent
setup must never run between preflight and that locked check. Same-user hostile replacement remains
outside the existing filesystem guarantee.

If post-commit WAL or permission setup fails, close without running application operations and
return a bounded storage-configuration failure. The schema migration has committed and must not be
reported as rolled back. A retry validates the current ledger and retries configuration, without
reapplying migrations. This operational failure is distinct from refusing invalid or newer input.
Test the race between preflight and the locked check, plus failure after commit but before setup
completes. Migration under a recognized rollback-journal database uses FULL synchronous durability;
WAL is required before returning it for ordinary engine operations.

Use explicit transaction control compatible with the project's Python connection configuration.
Do not use `executescript`: it can commit a pending transaction under legacy transaction control.
[Python transaction documentation](https://docs.python.org/3/library/sqlite3.html#transaction-control-via-the-isolation-level-attribute)
describes this behavior. `BEGIN IMMEDIATE` serializes writers and can fail with `SQLITE_BUSY`;
report the existing bounded storage failure rather than retrying indefinitely.
[SQLite transaction documentation](https://sqlite.org/lang_transaction.html)
defines the locking and rollback behavior.

Rejected input must preserve the database's schema, rows, ledger, `user_version`, main-file bytes,
and permissions. No application recovery, DDL, chmod, or journal-mode change may precede refusal.
Read-only WAL access must include committed WAL state; never use `immutable=1` to bypass locking
on a live database. SQLite may maintain WAL shared-memory coordination files even for a reader;
the contract does not promise unchanged transient lock/shared-memory bytes.
[SQLite WAL documentation](https://sqlite.org/wal.html#read_only_databases)
explains this distinction. Refusal tests cover both checkpointed and live-WAL fixtures.

SQLite rollback-journal recovery is the one exception to byte-level refusal. An interrupted
transaction can spill uncommitted pages into the main file; a read-only connection cannot restore
them. Only when SQLite reports `SQLITE_READONLY` during inspection and the adjacent journal is a
no-follow regular file owned by this OS user, has one link, grants no group/other access, and has a
nonzero header may the deferred writable opener allow SQLite recovery. Its size must exceed 512
bytes and be no larger than twice the database size plus 1 MiB. This bounds the candidate check
without loading database contents into Python. SQLite validates the journal and performs recovery;
Open Brain never replays or deletes journal records itself.

Recovery can restore main-file bytes and remove the hot journal before the recovered schema is
classifiable, including when that committed schema is subsequently refused as invalid or newer.
It does not authorize changes to committed application state, migration history, or version.
After recovery, the normal locked classification still precedes migrations, WAL configuration,
permission changes, and application work. An unsafe journal is refused intact. This exception is
required for retry after a process crash under rollback-journal mode; the subprocess test forces
dirty-page spill before abrupt exit. See [SQLite hot-journal recovery](https://sqlite.org/lockingv3.html#dealing_with_hot_journals).

An interruption before commit leaves the prior committed schema and data intact. A subsequent supported
write open retries the same pair or pending suffix. A successful current reopen performs no DDL,
FTS rebuild, migration-history update, or version assignment. There is no automatic downgrade or
in-place destructive repair command. A failed first creation may leave an empty file; creation
failure tests must verify cleanup of only the file owned by that attempt, or a bounded diagnostic
that does not silently adopt an existing empty file on retry.

## Migration 2: one safe search backfill

Preserve every capture, receipt, immutable revision, operation stage, import-root identity, and
active/pending import relationship. Migration is not recovery or reimport. Existing source and
canonical result IDs remain the result set; missing and inactive import revisions stay excluded.

Use one deterministic normalization path for fresh and adopted schemas. After validating the old
shape, detach the four known FTS triggers and clear derived FTS rows and identities. Rebuild
`search_documents` through a fixed temporary table with the W4 `NOT NULL` result-ID declaration,
copy all rows with explicit columns, then replace the old projection table and recreate its index.
Even fresh and already-canonical inputs use this same transaction-local sequence. Temporary names
are reserved and their pre-existence is invalid. This normalization makes W2 upgrades and new
installs structurally identical without altering immutable records. Both catalog entries use
`CREATE ... IF NOT EXISTS`; that syntax alone cannot tighten an existing column declaration.

Register one connection-local, deterministic two-argument SQLite function that delegates to
`search_projection.public_search_text(text, protected_source_reference=reference)`. It accepts text
and a nonempty text reference only, has no file/network/database side effects, and returns no raw
values in errors. Stream rows through SQL rather than collecting the full Brain in Python. Use the
existing per-record text limits where defined; do not silently truncate historical content to make
a fixture pass. The callback exists during migration only and is removed before returning the
connection. Its projection behavior is pinned by contract vectors alongside migration checksums.

For each copied search row, join the capture on `capture_id` and apply the function to both title
and body using that capture's protected `source_reference`. Validate missing joins before copying;
an inner join must never silently drop a result. Preserve the remaining columns, including trust,
provenance, timestamps, and canonical paths. Do not reconstruct trust from title text or overwrite
the W4 distinction between canonical review state and source confidence.

Create the FTS table with `unicode61 remove_diacritics 2`. Populate identities once in `result_id`
order, then insert each projected result into FTS exactly once. Reinstall the four existing W3
triggers only after this backfill, so copying the projection cannot populate FTS twice. Validate
equal result-ID sets, one identity/FTS row per projection, and equal projected title/body contents.
Internal FTS rowids may change; public result IDs and deterministic ranking tie-breaks may not.

## Every local opening uses the same policy

| Existing entry point | W5 responsibility |
| --- | --- |
| `_LocalStore.__init__`, `connect`, and `transaction` | Use guarded local write-open; remove constructor scripts, ALTER helpers, and `_adopt_live_search` startup behavior. Validate on each connection before ordinary work. |
| `BrainEngine` and `_ensure_phase1_state_schema` | Keep writer-lease checks; remove the independent version bump. SQLite owns migration serialization, including callers without a BrainEngine lease. |
| `materializer.py` and `portable_index.py` | Their `_LocalStore` creation uses the same migration path. The latter's separate disposable-index connection stays unchanged. |
| `open_local_read_view`, `_ReadOnlyStore`, and maintenance phase1 reads | Use the same classifier on the connection consumed by the read. Inspect all states without upgrade; retrieval accepts current state only. |

Disposable indexes and the in-memory FTS capability probe do not enter the local catalog. Tests
should audit phase1 callers, not ban every `sqlite3.connect` in the repository.

Default CLI startup may upgrade through its existing writable engine opening. Explicit read-only
maintenance and scoped read views remain read-only. Report `absent`, `legacy`, `pre_ledger`,
`supported_old`, `current`, `invalid`, `newer`, and `recovery_required` distinctly. Errors expose bounded categories,
never SQL, absolute paths, note content, references, or callback exception details.

## Fixtures and implementation acceptance

Reuse one synthetic semantic dataset for notes, stable IDs, import revisions, expected visibility,
and query expectations. Materialize separate Markdown files, historical SQLite layouts, later MCP
requests, and Portable expectations. A schema-specific fixture must remain independent of the
current migration SQL; otherwise the upgrader would be testing its own output as historical input.
Commit deterministic `.sql` fixture builders under `tests/fixtures/local-schema/`, with source
commit comments and explicit columns. Never commit generated databases.

| Test family | Required proof |
| --- | --- |
| Recognized upgrade matrix | W2, W3-only, populated W4, complete W2 version 0, and valid ledger version 1 converge to the exact new-install schema and ledger. Include W2-upgraded nullable search declarations for W3/W4. |
| Data and visibility | Compare immutable values as well as counts; preserve route stages, imports, pending reservations, active/missing/restored revisions, canonical review state, and exported bytes. W2 has no FTS before upgrade: compare semantic query expectations, not nonexistent FTS rows. W3/W4 retain expected ordered results; post-upgrade FTS equals the active projection. |
| Refusal | Wrong/missing columns and constraints, unexpected triggers, partial object groups, orphaned joins, null result IDs, corrupt database, bad/empty/gapped/checksum-mismatched ledger, ledger/version disagreement, and synthetic version 3 preserve refusal invariants. |
| Atomicity and concurrency | Inject failures after DDL, ledger insertion, projection, backfill, and before commit; include callback failure and subprocess interruption. Reopen and compare the prior committed state, then retry. Race two upgrade opens and test a held writer lock with a bounded timeout. |
| Privacy and integration | Paths, protected literals, credentials, digest-shaped strings, and Unicode vectors never enter rebuilt FTS or results. Reopen is a no-op. Exercise engine, materializer, import, read-only maintenance, backup/restore, search, and verified Portable export; ensure other databases' catalogs and versions stay unchanged. |

Export continues to validate the Portable manifest and reports its `schema_version=1` in
`open-brain export --json`. It must never expose local version 2 as the Portable version or include
SQLite files and ledger rows in the export. Native packaging must include the extracted modules
without broadening the default dependency closure.

Local verification passed: integrated `make verify` (3,545 passed, 5 skipped), native/Homebrew smoke,
`git diff --check`, and `actionlint .github/workflows/ci.yml`. The
[W5 audit](audits/2026-09-09-ob1-w5-schema-migrations-audit.md) records evidence and the recovery
exception. Owner-denylist tree/history audits and both supported CI platforms at the exact candidate
head are still required before merging W5.

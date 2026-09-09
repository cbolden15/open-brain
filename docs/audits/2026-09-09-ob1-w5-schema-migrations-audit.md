# Codebase vs plan audit: OB1-W5 schema migrations

Date: 2026-09-09

Plan: `docs/plans/2026-09-08-ob1-product-completion.md`,
“Schema sequencing” and “OB1-W5”.

Contract: `docs/schema-migrations.md`.

Scope: W5 only, based on merged W4 commit `5a51797`. Design commit `14675a0` precedes the
implementation audited here on `feat/ob1-w5-schema-migrations`. Evidence paths below are relative
to the repository root.

## Assessment

The implementation covers the W5 migration requirements. Local repository and Homebrew checks pass.
The implementation adds one documented recovery exception to the original byte-preservation
contract. The workstream is not cleared for merge: owner-only safety audits and both supported
platform CI jobs remain pending. W6 and W7 have not started.

## Requirement evidence

| Requirement | Status | Evidence and result |
| --- | --- | --- |
| One dependency-free runner with separate local and event catalogs | COMPLETE | `packages/engine/src/open_brain_engine/storage/migrations.py:92` owns ordered transactional application without committing. `engine/local_schema_catalog.py:351` defines exactly two local entries. `storage/sqlite.py:40` retains event version 1 and its existing public migration wrapper. No dependency manifest changed. Engine paths in subsequent rows share the `packages/engine/src/open_brain_engine/` prefix. |
| Checksummed ledger, version agreement, strict historical recognition | COMPLETE | `engine/local_schema.py:90` classifies fixed schema shapes and validates contiguous ledger rows, names, checksums, timestamps, and `user_version`. The five historical layouts plus nullable W2-to-W3/W4 variants converge in `packages/app/tests/integration/engine/test_local_schema.py:75` and line 130. Frozen historical SQL is independent of the production catalog; `tests/fixtures/local-schema/catalog-checksums.json` pins both migration hashes. |
| All ordinary local openings enforce the same policy | COMPLETE | `engine/local_store.py:18` replaces constructor DDL and ALTER helpers with guarded opens, retains validated read transactions, and revalidates under the application write lock. `engine/local.py:322`, `engine/maintenance.py:174`, and `engine/backup_ports.py:344` use the same classification on consumed snapshots. Materializer and Portable index construction continue through `_LocalStore`; event and disposable-index schemas remain separate. |
| Atomic upgrade, safe refusal, no-op reopening, bounded concurrency | COMPLETE | `engine/local_schema.py:269` owns the migration transaction and rollback. `storage/sqlite.py:156` defers WAL and chmod until acceptance. The migration test module covers injected failure stages (line 203), concurrent subprocess upgrades (296), retained read snapshots (330), application write races (351), locked reclassification (371), post-commit setup failure (396), empty/corrupt files (419), and five-second writer contention (536). Existing current databases do not rerun DDL, FTS backfill, timestamp writes, or version assignment. |
| Preserve immutable records, import relationships, visibility, and safe FTS contents | COMPLETE | `engine/local_schema.py:183` validates upgrade data and pending reservations; line 230 delegates projection to the existing public-text function with explicit bounds; line 242 validates projection/identity/FTS agreement. Migration 2 normalizes the projection table and performs one FTS backfill. Tests compare immutable rows, result IDs, counts, ordering, rollback after callback failure, and pending import state. `test_local_schema.py:587` preserves reviewed imported records, Portable bytes, and ordered search trust across W4 adoption. |
| Synthetic, independently materialized historical fixtures | COMPLETE | `tests/fixtures/local-schema/` contains W2, W3, W4, legacy version 0, and ledger version 1 SQL plus explicit-column synthetic data. Its semantic JSON is checked against SQL records by `test_local_schema.py:569` and materialized into Markdown in the export test. Database files exist only in test temporary directories. |
| Keep Portable Brain at version 1 and report it in export JSON | COMPLETE | `engine/portability.py:54` requires an exact integer Portable version 1. `packages/app/src/open_brain/services/local_entrypoints.py:771` reports that version. Native smoke checks it at `tools/open_brain_dev/base_native.py:555` and line 725. The W4 export test compares exported record bytes before and after migration. Local ledger rows and SQLite files do not enter Portable output. |
| Include new modules in the native dependency audit | COMPLETE | `tools/open_brain_dev/base_native.py:42` requires both local-schema modules; line 50 requires the shared migration primitive. `tests/release/test_native_distribution.py` pins these requirements. The default dependency boundary is unchanged. |
| Recover an interrupted rollback-journal upgrade | DEVIATED | A read-only connection cannot restore pages spilled before a crashed transaction commits. `engine/local_schema.py:137` reports `recovery_required` only after SQLite's read-only failure and the private-journal candidate check at `storage/sqlite.py:289`. SQLite may restore committed bytes before locked classification. Invalid or newer recovered state still cannot migrate. This narrow exception is documented in the contract; crash, unsafe-journal, and candidate-predicate tests cover it at `test_local_schema.py:474`, line 497, and line 647. |
| Complete all local and merge gates | PARTIAL | Full verification, native/Homebrew smoke, owner-only audits, and exact-head CI are recorded separately below. No merge or release readiness is inferred from local tests. |

## Recovery and review findings

Read-only review found two correctness gaps during implementation. A reader could validate one
schema and later consume another snapshot; reads now retain the validated transaction and writes
reclassify after acquiring their application lock. Mandatory read-only preflight also prevented
retry after a rollback-journal crash; the recovery exception resolves that dead end.

The journal candidate check uses descriptor-relative no-follow opens, regular-file checks, current
user ownership, a single link, private permissions, a nonzero header, and a bounded size. The helper
does not decide whether the journal is hot or replay it. SQLite does that work. Tests refuse
symlinks, hardlinks, wrong ownership, unsafe mode, empty headers, undersized/oversized journals, and
FIFOs without changing the database. Same-user hostile replacement remains outside the existing
filesystem guarantee. There is no automatic destructive repair or downgrade.

The coordinator owned all edits and git state; the independent reviewer remained read-only.
The follow-up review confirmed both identified runtime blockers were repaired. The final bounded
review of the runtime, test additions, and migration contract found no remaining correctness,
privacy, or documentation blocker. This is review evidence, not a substitute for pending CI.

## Verification record

| Check | Result |
| --- | --- |
| `uv run --frozen pytest -q packages/app/tests/integration/engine/test_local_schema.py -x` | PASS: 61 tests in 7.17 seconds. |
| `make verify` | PASS: Ruff; MyPy across 577 source files; 3,523 tests passed, 5 skipped in 63.37 seconds; engine, app, and connector package builds. |
| `make homebrew-smoke` (includes `make native`) | PASS on macOS arm64: native build and audit, Homebrew installation, capture, search, Markdown import, verified export, status, doctor, and self-check; command exited 0 after cleanup. |
| `git diff --check` | PASS. |
| `actionlint .github/workflows/ci.yml` | PASS. |
| Owner-only `make audit` and `make audit-history` | NOT RUN: `PRIVATE_DENYLIST` is not configured. No substitute denylist was used. |
| macOS arm64 and Linux x86_64 CI at the implementation head | NOT RUN for this local branch. W4's passing jobs are not W5 evidence. |

The five local skips are the existing filesystem-dependent Markdown path cases. Linux CI remains
necessary to exercise its supported filesystem behavior. Local command logs remain outside git;
no generated database, native archive, environment dump, or private audit input is part of this change.

## Remaining gate

Before merge, freeze the candidate head and run the real owner-denylist tree/history audits in a
disposable single-branch clone containing that candidate and its ancestors. Both supported CI jobs
must pass at that same head. Publishing and W6 implementation remain separate actions.

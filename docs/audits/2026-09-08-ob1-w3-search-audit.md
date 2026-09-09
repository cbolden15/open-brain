# Codebase vs plan audit: OB1-W3 FTS5 search

**Date:** 2026-09-08

**Plan:** `docs/plans/2026-09-08-ob1-product-completion.md`, lines 317-384

**Product scope:** Five-minute Open Brain only

**Mode:** strict

**Commits reviewed:** `7ec92c9..f65458f`

**Codebase root:** repository root

## Executive summary

- **Completion:** 90% (9 of 10 W3 requirements complete)
- **Ship readiness:** NOT READY
- **Code gaps:** None found after the three independent review passes and remediation
- **Remaining gates:** Exact-head macOS arm64 and Linux x86_64 CI, plus the owner's private-denylist
  tree and history audits
- **Next workstream:** OB1-W4 remains locked until both external gates pass and W3 merges into
  `goal/open-brain-five-minute-install`

The implementation replaces the Python substring scan with SQLite FTS5 and keeps authorization,
ranking, snippets, and result limiting in the SQL query. The live projection is maintained with
same-transaction triggers and can be rebuilt from durable records without changing the separate
Portable Brain snapshot. Public output is projected before indexing and again before rendering.

Three concurrent read-only reviews returned `FIX_FIRST`. Their P1 and P2 findings covered nullable
SQLite identities, stale or malformed provenance, partial protected-reference leakage, incomplete
re-derivation, diagnostic NULL handling, lifecycle coverage, CLI label coverage, and an overly broad
legacy fixture exception. Each finding has a regression test in the reviewed range.

The first remote gate run exposed one pre-existing cross-platform type-check defect in the retained
Secure Node crypto probe: MyPy could not prune a `platform.system()` branch on Linux. The gate repair
uses `sys.platform`, passes targeted Linux and Darwin MyPy checks, and does not change the default
Open Brain runtime. The second run passed the complete Linux job and exposed a completed-worker pipe
race plus an incorrect source-checkout root. The second repair drains IPC after worker exit and uses
the actual app, engine, and legacy source roots. The third run passed Linux again, while its macOS
daemon remained alive but did not answer the aggressive readiness loop. Review found that the loop
could queue abandoned short-timeout socket connections faster than the single-threaded daemon drained
them. The latest repair uses one bounded protocol probe, excludes the unrelated HTTP listener from
that CLI-only test, and retains failure diagnostics. The cross-process test passed 10 consecutive
local runs, its 29-test related suite passed, and the full 3,400-test suite passed. The final
exact-head rerun remains part of R10.

## Requirement audit

### R1: Document the retrieval design before implementation

- **Status:** COMPLETE
- **Evidence:**
  - `docs/retrieval.md:7` distinguishes the authoritative live projection from durable source data.
  - `docs/retrieval.md:32` defines NFC normalization and the `unicode61` tokenizer behavior.
  - `docs/retrieval.md:97` defines literal query conversion and input bounds.
  - `docs/retrieval.md:137` defines BM25 weights, deterministic ranking, and snippets.
  - `docs/retrieval.md:201` defines status and doctor semantics.
- **Notes:** The documented CJK limitation and deferred dependency-free alternatives match the plan.

### R2: Run the full verification command before the installed-product smoke in CI

- **Status:** COMPLETE
- **Evidence:**
  - `.github/workflows/ci.yml:31` runs `make verify` before `make homebrew-smoke` on macOS arm64.
  - `.github/workflows/ci.yml:53` runs the same ordered pair on Linux x86_64.
  - `tests/release/test_native_distribution.py:165` locks the two-job workflow and command order.
- **Notes:** Remote execution of the exact PR head is still pending under R10.

### R3: Maintain a live FTS5 index transactionally

- **Status:** COMPLETE
- **Evidence:**
  - `packages/engine/src/open_brain_engine/engine/local_store.py:141` creates the stable FTS identity
    map with a non-null unique result ID.
  - `packages/engine/src/open_brain_engine/engine/local_store.py:147` creates the FTS5 table with the
    documented tokenizer.
  - `packages/engine/src/open_brain_engine/engine/local_store.py:155` through line 196 defines
    immutable identity and insert, update, and delete triggers.
  - `packages/app/tests/integration/engine/test_search_relevance.py:60` exercises schema creation and
    mutation synchronization.
- **Notes:** The FTS row ID is derived state. The public result ID remains stable.

### R4: Search with bounded SQL MATCH, ranking, snippets, and deterministic ties

- **Status:** COMPLETE
- **Evidence:**
  - `packages/engine/src/open_brain_engine/engine/retrieval.py:153` builds the authorized FTS query.
  - `packages/engine/src/open_brain_engine/engine/retrieval.py:171` applies weighted BM25 and FTS5
    snippets.
  - `packages/engine/src/open_brain_engine/engine/retrieval.py:231` orders by relevance, score,
    record type, title, and result ID before applying the SQL limit.
  - `packages/app/tests/integration/engine/test_search_relevance.py:186` covers ranking, Unicode, and
    literal queries; line 380 covers deterministic ties.
- **Notes:** No candidate-row Python ranking remains.

### R5: Treat user queries as bounded literals rather than raw FTS syntax

- **Status:** COMPLETE
- **Evidence:**
  - `packages/engine/src/open_brain_engine/engine/retrieval.py:426` normalizes and bounds query input.
  - `packages/engine/src/open_brain_engine/engine/retrieval.py:448` escapes every token as an FTS5
    string literal.
  - `packages/app/tests/integration/engine/test_search_relevance.py:186` covers quotes,
    parentheses, operators, column syntax, prefixes, NULs, and maximum-length input.
- **Notes:** Stable errors do not echo rejected input.

### R6: Enforce filters and authorization inside the ranked SQL path

- **Status:** COMPLETE
- **Evidence:**
  - `packages/engine/src/open_brain_engine/engine/retrieval.py:155` materializes the caller's allowed
    spaces before every FTS ranking branch.
  - `packages/engine/src/open_brain_engine/engine/retrieval.py:164` applies authorization, space,
    payload-family, and record-type predicates before matching.
  - `packages/engine/src/open_brain_engine/engine/retrieval.py:452` maps an empty allow-list to an
    empty SQL relation and bounds the number of allowed spaces.
  - `packages/app/tests/integration/engine/test_search_relevance.py:266` verifies authorization,
    filters, ranking, and limit behavior.
- **Notes:** Fetch and page-read operations use the same allow-list boundary.

### R7: Keep indexed and rendered text public-safe

- **Status:** COMPLETE
- **Evidence:**
  - `packages/engine/src/open_brain_engine/engine/search_projection.py:17` normalizes and projects
    complete fields before indexing.
  - `packages/engine/src/open_brain_engine/engine/search_projection.py:103` derives public text and
    trust from validated durable provenance.
  - `packages/engine/src/open_brain_engine/engine/retrieval.py:333` reprojects complete title and body
    fields before trusting a snippet or returning a result.
  - `packages/app/tests/integration/engine/test_search_relevance.py:316` and line 336 cover protected
    values and partial-reference leakage.
- **Notes:** Invalid or stale projection provenance fails closed or downgrades to unverified output.

### R8: Support update, delete, and complete deterministic rebuild behavior

- **Status:** COMPLETE
- **Evidence:**
  - `packages/engine/src/open_brain_engine/engine/reconciliation.py:72` updates live projection rows
    from owner-edited canonical Markdown under the writer lease.
  - `packages/engine/src/open_brain_engine/engine/reconciliation.py:234` validates durable inputs and
    atomically re-derives the full live projection.
  - `packages/engine/src/open_brain_engine/engine/local_store.py:307` rebuilds only the derived FTS
    rows inside the caller's transaction.
  - `packages/app/tests/integration/engine/test_search_relevance.py:426` covers ordered update and
    delete behavior; lines 615 and 661 cover FTS-only and full projection rebuilds.
- **Notes:** Rebuild leaves the separate Portable Brain snapshot untouched.

### R9: Expose a small CLI and accurate status/doctor evidence

- **Status:** COMPLETE
- **Evidence:**
  - `packages/app/src/open_brain/services/local_entrypoints.py:148` exposes only the query and
    `--limit` on the default search command.
  - `packages/app/src/open_brain/services/local_entrypoints.py:251` returns bounded trust, origin,
    title, excerpt, and explanation in JSON and safe one-line human output.
  - `packages/engine/src/open_brain_engine/engine/maintenance.py:214` compares authoritative
    projection, identity, and FTS state, including NULL-safe content checks.
  - `packages/app/src/open_brain/services/local_entrypoints.py:305` reports live search separately
    from the non-authoritative Portable snapshot.
  - `packages/app/tests/integration/services/test_local_entrypoints.py:384` through line 598 covers
    bounded JSON, reconciliation, trust/origin labels, and terminal control removal.
- **Notes:** The native smoke also exercises FTS5 creation, matching, BM25, snippets, diacritics,
  status, and doctor.

### R10: Pass every exact-head merge gate

- **Status:** PARTIAL
- **Evidence:**
  - Local `make verify` passed Ruff, MyPy, package builds, and 3,400 tests.
  - The repaired cross-process test passed 10 consecutive runs, and its 29-test daemon and legacy
    integration suite passed.
  - Local `make native` and `make homebrew-smoke` passed on macOS arm64.
  - Local `actionlint .github/workflows/ci.yml` and `git diff --check` passed.
  - `docs/ai/workstreams/20260908-open-brain-ob1-w3-search-1864f1/HANDOFF.md:8` records the local
    verification and the remaining blocker.
- **Notes:** The sanitized source commit is `f65458f`. The owner approved the exact
  `# no additional project terms` marker. The rewritten exact head still needs the tree and
  reachable-history audits plus both GitHub CI jobs. Earlier exact-head CI passed before the audit
  remediation, but the rewritten lineage must be verified again. The plan forbids merging without
  both gates.

## Critical gaps

- Exact-head Linux x86_64 native and Homebrew behavior is unverified.
- The owner-only private-content tree and reachable-history audit is unverified.

## Integration issues

None found in the reviewed implementation. Search is wired through the default local CLI, engine,
SQLite projection, diagnostics, native build smoke, and both CI job definitions.

## Quality concerns

No unresolved P0 through P2 code findings remain. The remaining risk is missing external evidence,
not a known implementation defect.

## Recommended fixes

1. **[CRITICAL]** Push the exact candidate and require both GitHub CI jobs to pass.
2. **[CRITICAL]** Run `make audit` and `make audit-history` from the frozen candidate using the
   owner's absolute, untracked `PRIVATE_DENYLIST` file.
3. **[HIGH]** Merge the unchanged candidate into `goal/open-brain-five-minute-install` only after
   both gates are green.

## Optional enhancements

None in W3. Embeddings and stronger multilingual segmentation remain deliberately deferred until
they have measured cross-platform value without increasing the default runtime dependency surface.

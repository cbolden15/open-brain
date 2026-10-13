# Workstream State

- ID: `20261013-open-brain-public-implement-facets-through-integrated-verification-and-independent-review-with-six-e88295`
- Repo root: /Users/calebbolden/Projects/oss/open-brain-public
- Remote identity SHA-256 fingerprint: `0aaeca2a6d1b421eaa7439b39b3228c836f6c7c55026bac150b0aeda757566cf`
- Worktree: /Users/calebbolden/Projects/oss/open-brain-public
- Branch: feat/facets-stage-one
- Objective: Implement facets through integrated verification and independent review with six workers
- Created date: 2026-10-13

## Execution contract

C0: `4a29944b4fead146c6f9bf5c535d0a1595799870`, revision `facets-c0-v1`.
Fixture manifest: `eceaa89f146eeb133f780916fb006da211e5ab479ea8231b63c705f3bde000ca`.
Owner-authorized cap: six concurrent workers plus one coordinator. Native E/X/T and terminal
A/P/V use isolated worktrees. No sandbox or approval bypass; local commits only.

The task manifest reserves exclusive files and exact inputs before dispatch. Shared contracts,
registration, integration and this ledger are coordinator-owned. Prepared P/X/V work can start
immediately; verified E remains their final integration prerequisite. X also consumes P/V evidence.
Fake and fixture results are labeled preparation, never persistence/authority/recovery/restore proof.

Status: six workers dispatched; runtime acceptance F01–F22 is pending.
Checkpoint:45 minutes per bounded slice, followed by evidence and dependency reassessment.
Initial reservations: six; active workers:6; total new sessions to create:3 terminal sessions;
three existing native sessions are reused. Independent review will use a freed slot.

## Active worker reservations

| Task | Executor/session | Worktree | State |
|---|---|---|---|
| E | native / /root/wire | /Users/calebbolden/Projects/oss/open-brain-facets-stage-one-e | running |
| A | codex-cli / 95334 | /Users/calebbolden/Projects/oss/open-brain-facets-stage-one-a | running |
| T | native / /root/check | /Users/calebbolden/Projects/oss/open-brain-facets-stage-one-t | running |
| P | codex-cli / 21768 | /Users/calebbolden/Projects/oss/open-brain-facets-stage-one-p | running |
| X | native / /root/evidence | /Users/calebbolden/Projects/oss/open-brain-facets-stage-one-x | running |
| V | codex-cli / 55885 | /Users/calebbolden/Projects/oss/open-brain-facets-stage-one-v | running |

Machine execution details and private reports are retained in the coordinator run ledger. No
worker completion or real-engine proof is recorded yet.

## Contract correction and first integration

Verified C0 correction `d5d11a80b2c085a52ad58260c220cfd8a80df32b` advances revision to `facets-c0-v2`; manifest SHA-256 `019d5cee0da5b574423a6321959e3b226ec885bfedfb695c3805c8ef78577678`. Historical canonical UUIDv5 revisions now pass evidence admission. 288 contract checks and scoped Ruff/mypy pass. All v1 evidence/fixture results require revalidation on this pin. Original C0 ancestry remains recorded.

V preparation `eb63d21` integrated as `ed2c381`: ten fixture/pure-function checks reported, real persistence/apply/export pending verified E. V session is idle awaiting dependencies. Coordinator additionally reserves local_migration.py and ingestion_migration.py for schema11 chaining and retained schema10 recognition.

## Integrated preparation and runtime composition

Current contract pin: `1c14378dc4dc40026c3d2a592b85ae03a43521c1`, revision `facets-c0-v3`, manifest `781a6b886baf6a904b974eb071ea30a36d2d8ff6d2f10f98fbd6da06de8653ba`. New publication provenance equals its selection; updates retain inherited provenance. 307 C0/publication preparation tests passed; affected consumers revalidate this pin.

A preparation commits `46ace4f` and `78bde2e` integrated. 87 fake/fixture tests reported; real-handler F15 pending. P preparation `fafd7f8` integrated, 23 pure tests; runtime continuation active. V preparation `eb63d21` integrated, 10 pure tests; apply/projection continuation active. X preparation `cfb5182`, composition `fb211de`, actor mapping `1b4e438`, and legacy/fault checks `ca68db2` integrated. X reports 32 tests including real empty-facet schema11 stage/reopen and rollback. Populated E/P/V evidence remains pending.

Coordinator migration/interface commit `cdc6501` has 89 focused scaffolding checks and 103 historical Portable compatibility checks. Version-gated admission `6c7de19` keeps historical validation defaults unchanged. Current integrated lint/typecheck passes 424 source files; migration/Portable focused checks pass 34 cases. Full repository gate and independent final review remain pending. Broader interface checks are running; any failure blocks their gate. Root owns all integration corrections, shared contracts, catalog registration and this ledger.

A first CLI parser integration attempted to add `--json` twice to leaf commands. The existing adapter parser already supplied it. Coordinator now preserves its option and suppresses leaf defaults while adding missing root options; broader interface tests are rerunning. Terminal P could not write Git metadata under its unchanged workspace sandbox; coordinator committed the five scoped files using existing coordinator permissions. No permission controls were relaxed.

## Engine and independent boundary gate

Focused engine integration pin `015480fff34608e87115a04240b1d1078def0033`: 48 real engine/migration/Portable checks and full lint/typecheck (428 files) passed. V resumed against this pin in its isolated worktree. Its additional custody DDL changes the development schema11 checksum, so E migration checks must rerun on the final integrated DDL.

Independent T `f1e2b6` integrated as `8306334`: first actual run passed 36 and failed 22, largely absent P/V composition plus three MCP filter failures and two overstrict test expectations. T correction `d8b8bda` preserves one-event retry guarantees and asserts the existing committed-upgrade wrapper; all 11 fault/migration tests pass. Coordinator fixed the real MCP outer authorization filter; ten real F15 tests pass. These are real-handler checks, not fake proof.

Full candidate `8306334` ran in isolated coordinator verification worktree `/Users/calebbolden/Projects/oss/open-brain-facets-verify-1`. Lint and mypy (431 files) passed; full pytest collection failed on a Portable-first circular import. X fix `b08b59f` integrated as `8425134`, with three fresh-process import-order regression checks. The full gate is invalid until rerun on the corrected candidate. Interface regression suite: 238 passed with five failures; parser repair and current-version updates resolved those in focused reruns (142 passed plus two current-version checks). Final full verification remains required.

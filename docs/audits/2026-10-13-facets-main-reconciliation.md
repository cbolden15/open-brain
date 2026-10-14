# PR79 reconciliation with current main

Status: reconciliation verified. Independent review is closed. PR79 remains unmerged.

## Inputs and compatibility

Main is pinned to `621161ad48bc8fcfa64966c7a618c18cc72b660a`; the original
feature tip is `7e161c2336c126c59c68b00b294e3823d5707491`. Their merge base is
`1012a5148bd7b6f7d6f07036a0e353985c2cfb90`. The merged candidate preserves main's
source continuity, historical authority, sharing and custody behavior.

Facets now append schema15/runtime10/Portable10. Main migrations1–14 and
Portable1–9 retain their meaning; the earlier unmerged facet schema11/Portable6
pins remain historical receipts. They are not alternate interpretations of main's
schema11 or Portable6. Portable10 inherits Portable9 validation and authority
restoration, then adds facet ledger, binding and custody evidence.

The C0 revision remains `facets-c0-v4`; its fixture manifest SHA-256 is
`95e85b535eeb4263a6e430970ecf1d49de8d0b72de7425e41be703897d93dbc5`.
O0 remains `obsidian-o0-v1`; its fixture manifest SHA-256 is
`bee455084d682e8e717f5f40e1a5cfad82ada7546fdf6412552a8e4844a50d9f`.
Both files were hashed again during reconciliation and match the original pins.
The coordinator-owned task manifest records the candidate and new catalog hashes.

## Integration and independent review

Engine, Portable and adapter workers used separate worktrees and exclusive file
reservations. The coordinator integrated their commits and owned shared contracts,
registration, compatibility floors, tests and the work ledger. A fourth worker
reviewed the integrated code independently. No worker pushed or merged.

Current canonical selection still rejects withdrawn or superseded provenance;
owner historical inspection remains available through its existing authority.
Adapter discovery and output revalidate current consent. Ordinary pending ingress
remains losslessly exportable as custody evidence, while unfinished source,
managed-delivery and sharing transitions retain their existing export gates.

Independent review ran 80 real integration cases covering main policy, mixed
historical Portable restoration, facet publication and managed-record identity.
It found two P1 issues:

1. Frozen core and harness packaging omitted both import-time facet JSON schemas.
   The shared native specification now includes them, with a distribution test.
2. The appended migration could commit schema15 before startup rejected unsettled
   historical authority. The migration now checks historical snapshot settlement
   inside the transaction before DDL and before commit, checks foreign keys, and
   revalidates runtime admission. Pending, missing and mid-migration registry
   changes leave the original database bytes and schema14 unchanged.

The reviewer independently reran the original migration reproductions and 14
authority/fault tests after the fix. Both findings are source-fixed, with no
remaining confirmed findings in the bounded review. Full and native execution
results below close this gate. The reviewer confirmed both
committed fixes at `91db46b03383af85a81baa8c40c1b84164f2eb02`.

## Verification

Candidate: `084c29423593f5a77b33e98ecc6357f3b74e749a`. `make verify` passed with
exit status 0. Verification ran in a separate frozen worktree because harness acceptance
intentionally rejects source edits during a run.

- Python: 7,045 passed, 5 skipped in 698.67 seconds. The skips reflect unsupported
  non-UTF-8 filenames and distinct NFC/NFD names on this filesystem. One warning
  comes from the intentional multi-threaded fork rejection test.
- Obsidian plugin: 528 tests, typecheck and production build passed.
- Existing desktop regressions: 259 frontend tests, 30 Python checks, 26 Rust tests,
  typecheck, frontend build and Rust formatting passed.
- Ruff, mypy across 617 source files, and source/wheel builds for all four Python
  packages passed. Actionlint and `git diff --check` passed.
- Gitleaks passed across the branch history; the final documentation commit receives
  another scan before push. Existing synthetic-fixture fingerprint handling is unchanged.

`make native-audit homebrew-smoke` passed on `91db46b`. The installed executable
passed capture, doctor, export, graph projection, local MCP, Markdown import,
Obsidian plugin, search and status checks. The existing product was preserved.
Candidate `084c294` changes only three test files; runtime, packaging, plugin,
developer tools and lockfile have no differences from the native-verified commit.

Focused follow-ups passed: 118 recovery/catalog/collector cases, 52 privacy and
historical-recovery cases, and 10 collector publication/custody recovery cases.
These are regression evidence, not a substitute for the final full check.

Earlier diagnostic runs are not passing receipts: they exposed stale current-version
assertions, old-format fixtures that accidentally exported the new format, catalog
registration expectations, and the migration/packaging issues above. Old-format
tests now construct actual old-schema inputs; their validators were not relaxed.

## Scope and remaining evidence

This gate reconciles Stage1 and O0. Live Obsidian facet dispatch and interface work
remain Stage2 O01–O15. Fake-based adapter checks prove only contracts and wiring;
persistence, authorization, publication recovery and restoration rely on the real
integration checks. Real-provider and actual Obsidian GUI qualification are separate.
No standalone desktop feature was added; existing strict compatibility checks were
advanced to the current runtime floor.

Schema migration raises the writer floor. A code revert does not downgrade a Brain;
retain a pre-upgrade backup and use the tested recovery and Portable paths. This
work does not merge the PR, publish a release or deploy.

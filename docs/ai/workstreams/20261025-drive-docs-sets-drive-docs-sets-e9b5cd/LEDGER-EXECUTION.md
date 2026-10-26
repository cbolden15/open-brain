# Ledger: Drive Docs sets execution

Milestone: execute `docs/superpowers/plans/2026-10-25-drive-docs-sets.md` (15 tasks) to PR-ready on `feat/drive-docs-sets`, no push. Plan base `7455e45`; branch base on `main` `df6b5df`.
Caps: 6 active, 12 total. Planning used 3 Fable plan authors plus 1 codegraph-explorer (separate milestone).
Planned: one task-tdd-implementer and one task-spec-compliance-reviewer per package (6); the rest reserved for remediation and the final whole-branch review. Review fixes go back to the same implementer (resumed, not a new child).

| # | Child | Package | Model | Status |
|---|---|---|---|---|
| 1 | task-tdd-implementer | P1 Tasks 1–7 | opus | DONE_WITH_CONCERNS; `7455e45..9f9ff3b` (7 commits); connectors contract 589 passed, 401 collector/app tests touching the modules passed; concerns: 2,000-entry `known` cap unreachable under the 131,072-byte checkpoint bound (coordinator decision: 500, spec 1.2, fix `997f19d`); `test_source_cli.py` catalog pin also updated; client-side 1 MiB export check unreachable with the real transport |
| 2 | task-spec-compliance-reviewer | P1 | opus | SPEC ✅; Important: checkpoint `mode` of list or dict raised a raw `TypeError` instead of `drive_invalid_checkpoint`; Minor: six-digit fraction timestamps; both fixed in `db957b8` (resumed child 1); re-review APPROVED. P1 complete at `db957b8` |
| 3 | task-tdd-implementer | P2 Tasks 8–11 | opus | DONE_WITH_CONCERNS; `db957b8..14880e9` (4 commits); collector plus intake contract 375 passed; `make test-tiers-audit` ok; deviations: edit test pins one head plus one `saved-revision-archive-*` file (existing sink behavior), ruff/mypy fixes to the Task 9 test landed in the Task 11 commit |
| 4 | task-spec-compliance-reviewer | P2 | opus | SPEC ✅, QUALITY Approved; Minors: `74be736` red alone (squash-merge accepted), no exact-replay archive guard for Drive (DOCS-005 notes it). P2 complete at `14880e9` |
| 5 | task-tdd-implementer | P3 Tasks 12–15 | opus | DONE_WITH_CONCERNS; `14880e9..2d5973d` (4 commits); docs module 4, collector 39 + 134 + 11, release 35 passed; added the 1 MiB transport paragraph from the global constraints; TDD order slip on Task 14 recorded |
| 6 | task-spec-compliance-reviewer | P3 | opus | SPEC ✅, QUALITY Approved with 3 doc-accuracy Minors (stall overstatement, DOCS-005 hash wording, backfill in one preview), all fixed in `f1d004c`. P3 complete at `2d5973d` |
| 7 | general-purpose (final whole-branch review) | `df6b5df..2d5973d` | fable | 0 Critical, 1 Important, 9 Minor. Important: parts bounded by 60,000 characters while the collector attests intakes at 131,072 bytes of ASCII-escaped JSON, so a mostly non-ASCII part stalls the set with `source_state_too_large`. Triage: fix the Important, the three P3 doc Minors, finding 2 and the live-gate lines; accept the rest. Report `.superpowers/sdd/final-review-report.md` (git-ignored; summary here) |
| 8 | task-tdd-implementer (final fix) | `2d5973d..f1d004c` | opus | DONE; `d824de6` splitter bounded by `MAX_PART_JSON_BYTES = 120_000` with non-ASCII tests, spec 1.3, plan constraint line; `f1d004c` doc corrections and three live-gate lines; 59 covering tests passed; lint, typecheck, diff-check clean. Re-review: finding 1 RESOLVED (worst-case payload 128,108 bytes under 131,072; 60,000 × é, 30,000 × emoji and 349,000 × 日 accepted by `observe_revision`; 400,000 Cyrillic characters raise `drive_content_too_large` per spec 1.3); two wording nits applied in the ledger commit |

Coordinator decisions recorded in the spec (owner may override): `known` cap 500 (1.2); part JSON byte bound 120,000 (1.3); watermark advances when a listing completes (1.1); sorted descriptor labels, widened notice pattern, revision-sink allow-list gains `google_drive` (1.1).

Minor findings accepted as recorded (final review triage): client-side 1 MiB export check unreachable with the real transport (harmless defense); empty backfill makes the first incremental bound 24 h before `date_floor` (harmless); format-maximum `known` entries would exceed 131,072 bytes (real Drive ids measure 82,787 bytes at 500); intermediate commit `74be736` red alone (squash-merge); a Doc crossing the 60,000-character limit in either direction starts new part records (now documented); a Doc whose name fails intake validation raises instead of skipping (rare; follow-up); `split_document` reports `too_large` for exactly 18 parts followed by blank lines; `date_floor` accepts every `date.fromisoformat` form; unknown `resource_type` in the manager surfaces as `drive_invalid_selection`; implementers ran on Opus while commit trailers name Fable per repo convention.

Unverifiable without the owner live gate (recorded in `docs/integrations/google-sources.md` "Live acceptance for Drive Docs sets"): `files.list` with an unknown or revoked parent returns 404 or an access-loss 403; the query grammar accepts a millisecond `modifiedTime` bound; `name contains 'Notes by Gemini'` matches a title ending in that phrase (fallback: drop the name clause); Gemini's real title suffixes.

## Frozen-tree gate

Frozen HEAD `f1d004c`, merge-base with `main` `df6b5df`. Logs under `$GATE` = `$SCRATCHPAD/drive-gate`, where `$SCRATCHPAD` is the coordinator session's scratchpad directory.

| Date | Step | Command | Result | Log |
|---|---|---|---|---|
| 2026-10-25 | gate | `make verify` (first run) | exit=2: pytest aborted at 33% with `OSError: [Errno 28] No space left on device` (disk at 100%; 13 GB of stale `pytest-of-$USER` roots removed) | $GATE/verify-f1d004c.log |
| 2026-10-25 | gate | `make verify` (rerun) | exit=0: lint ok, mypy 863 files ok, pytest 9566 passed, 9 skipped; plugin 702 passed | $GATE/verify-f1d004c-rerun.log |
| 2026-10-25 | gate | `actionlint .github/workflows/ci.yml` | ok | inline |
| 2026-10-25 | gate | `git diff --check` | clean | inline |
| 2026-10-25 | skipped | `make native-audit`, `make homebrew-smoke` | not run: no core app, native or packaging change (connectors, collector and docs only) | |

## After merge

- Owner live gate per the guide's "Live acceptance for Drive Docs sets" list, receipts outside Git.
- Follow-ups accepted as recorded above: Doc-name validation stall, exact-18-parts blank-line edge, `date_floor` form strictness.
- `docs/product-family.md:138` still calls the collector "planned" (drift noted in the 2026-10-25 integrations assessment; not touched here).

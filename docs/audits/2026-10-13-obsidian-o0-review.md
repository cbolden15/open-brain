# Independent O0 contract-kit review

Date: 2026-10-13. Mode: standard codebase-plan-auditor. Review is read-only, bounded to O0.

## Final O0 decision

**Overall O0 readiness: READY at e3c165f90753464271bcff2aacb1a8f45ad64872.** Both confirmed findings are fixed and independently verified. No unresolved blocking findings remain. The coordinator's exact-candidate `make verify` completed successfully; R inspected its nonempty log and records the attributed receipt below. This recommendation completes O0 review only, not the remaining Stage 2 implementation or O01–O15 acceptance.

The coordinator owns full `make verify`; this review did not duplicate it. Candidate 1 passed Ruff and mypy (436 source files), but its running pytest suite exposed the missing exhaustive application-surface registration below. That invalidated run was interrupted by the coordinator. The fresh complete gate on `e3c165f90753464271bcff2aacb1a8f45ad64872` finished with exit 0 according to the coordinator. R independently read the completed log; no additional test rerun was needed for this receipt-only continuation.

Worktree: /Users/calebbolden/Projects/oss/open-brain-obsidian-o0-review. Diff: `490bd05..e3c165f`; initial candidate `f7dd38d6780ebdaca4c5447166b42733f7451808`. Verified Stage 1: `abeab52136a668fd851a6d4a7c72aa8fa67baedb`; handoff `490bd05609e1cf3046946da1b935096de987d024`. C0: `ed74327f34ec2526fd36b1737e36b05bcd2cdc62`, revision `facets-c0-v4`, fixture manifest SHA256 `95e85b535eeb4263a6e430970ecf1d49de8d0b72de7425e41be703897d93dbc5`. O0 revision: `obsidian-o0-v1`; fixture manifest SHA256 `bee455084d682e8e717f5f40e1a5cfad82ada7546fdf6412552a8e4844a50d9f`, independently verified with all listed file hashes.

Normative documents read: /Users/calebbolden/Projects/oss/open-brain-obsidian-o0-review/docs/design/obsidian-plugin-contract-kit.md; /Users/calebbolden/Projects/oss/open-brain-obsidian-o0-review/docs/design/owner-defined-facets-stage-two-obsidian.md; /Users/calebbolden/Projects/oss/open-brain-obsidian-o0-review/docs/design/obsidian-o0-task-manifest.json. Task: /tmp/obsidian-o0/review-prompt.md. Skill: /Users/calebbolden/.codex/skills/codebase-plan-auditor/SKILL.md. The private report path follows the task's narrower write scope; no repository source, tests, docs, commits or runtime registration were changed by R.

## Confirmed findings and closure

### O0-R1 HIGH: real publication inspection rejected by an invented digest equality — FIXED

Location: /Users/calebbolden/Projects/oss/open-brain-obsidian-o0-review/packages/obsidian-plugin/src/facet-inspection.ts:80.

The parser requires `evidence.sha256 === facet_selection.source_states[index].sha256`. These digests cover different bytes. /Users/calebbolden/Projects/oss/open-brain-obsidian-o0-review/packages/engine/src/open_brain_engine/engine/contracts.py:510 documents ReviewEvidence as projected evidence whose digest covers the displayed excerpt. The real producer at /Users/calebbolden/Projects/oss/open-brain-obsidian-o0-review/packages/engine/src/open_brain_engine/engine/review.py:696 hashes `excerpt.encode("utf-8")`. The frozen source-state digest covers immutable source bytes.

Independent execution bundled the current TypeScript parser in memory with esbuild, loaded `publication_inspections[0]` from /Users/calebbolden/Projects/oss/open-brain-obsidian-o0-review/packages/obsidian-plugin/tests/fixtures/facets/o0-cases.json, and called `parseFacetPublicationInspection`. It printed excerpt digest `8dfe5bea558d9d18e5d0ab518d37d8179540541fc6f43422a14786cd053a7f4d` and source digest `1b978ed7de9cc541425295e5f25efd868178277e375a3f2c7783f9d2920b7ab5`, then rejected with `invalid_arguments`.

This finding was first raised by the independent test worker and independently confirmed here. The original 211 passing TypeScript tests used synthetic inspection evidence that repeated the source digest, so they missed the producer boundary. Python's real-inspection test checks shape and binding relationships but does not execute the TypeScript consumer.

Required repair: validate the excerpt digest's grammar separately, preserve capture identity/order and the rest of the closed inspection structure, and add the actual producer fixture to the TypeScript acceptance test. Do not redefine engine evidence or equate these digests. The parser is structural; the engine remains authoritative for cryptographic evidence and decision freshness. Repair `5a544d5` now checks digest grammar separately, retains capture order and all other closed constraints, and adds the actual shared producer fixture. Independent verification passes all 237 TypeScript tests and typecheck. A further independent probe creates a fresh synthetic Brain and proposal through the real Python producer and passes its current inspection directly into the bundled TypeScript parser: accepted schema 5, one selected source, distinct source/excerpt digests. The finding is closed.

### O0-R2 MEDIUM: exhaustive application surface registration omitted the new helper — FIXED

Location: /Users/calebbolden/Projects/oss/open-brain-obsidian-o0-review/tests/security/test_architecture_boundaries.py:45. The actual application tree has one extra member, `services/plugin_facet_contracts.py`, so the required architecture guard fails. Independent targeted execution on `5a544d5` gives one failure in 0.02s, identifying only that path. The helper was inspected: it is pure foreground codec/descriptor logic, with no listener, background service or dispatch side effect. Required fix is to register exactly this new path in the exhaustive expected set and rerun the guard. Do not relax the equality or exclusion guard. The coordinator identified the missing registration. Final repair `e3c165f` adds exactly the new helper path and preserves exhaustive equality. Independent execution of all five architecture-boundary tests passes in 0.04s; this finding is closed.

## Checked requirements

| O0 responsibility | Assessment and evidence |
|---|---|
| Pin Stage 1, C0, schemas and fixture inputs | COMPLETE. Manifest hashes independently match; generated aliases drift check passes. Source generator: /Users/calebbolden/Projects/oss/open-brain-obsidian-o0-review/tools/generate_obsidian_facet_types.py:38. |
| All 23 authoritative facet DTOs and normalization | COMPLETE. TypeScript imports the actual C0 schema and applies closed fields, scalar/integer/ID/time rules, defaults, NFC/set normalization and semantic relations at /Users/calebbolden/Projects/oss/open-brain-obsidian-o0-review/packages/obsidian-plugin/src/facet-wire.ts:86. Compared against /Users/calebbolden/Projects/oss/open-brain-obsidian-o0-review/packages/engine/src/open_brain_engine/engine/facet_validation.py:53 and the frozen corpus. |
| Additive negotiation and old-runtime compatibility | COMPLETE for the pure contract. /Users/calebbolden/Projects/oss/open-brain-obsidian-o0-review/packages/obsidian-plugin/src/facet-wire.ts:171 intersects known operations, advertised operations and compatible descriptor entries; malformed descriptors fail closed, absent descriptors disable facets, unknown future operations are ignored. Existing handshake parser and registration are unchanged. Smaller limits are retained for B to enforce. |
| Full transport envelopes and operation bounds | COMPLETE for codecs. /Users/calebbolden/Projects/oss/open-brain-obsidian-o0-review/packages/app/src/open_brain/services/plugin_facet_contracts.py:105 admits exact UTF-8 frames before returning arguments, with explicit availability. Lines 124/130 measure and encode the whole success response including LF. /Users/calebbolden/Projects/oss/open-brain-obsidian-o0-review/packages/obsidian-plugin/src/facet-wire.ts:201 and :209 check full request/response frames, request identity and closed responses. Real live buffering, quotas and pre-effect reservations remain B work. |
| Managed identity shape and real feasibility | COMPLETE for the read-only helper. /Users/calebbolden/Projects/oss/open-brain-obsidian-o0-review/packages/engine/src/open_brain_engine/engine/managed_record_identity.py:50 requires owner-local authority, SQL registration/active eligibility, observation generation/path/revision, retained file identity, protected accepted/current content, confined reads and repeated root/file checks. Immutable result has the documented closed shape. Real canonical lookup and refusal tests pass. Source-file mapping is explicitly unavailable rather than inferred. |
| Publication inspection and frozen v5 evidence | COMPLETE after the verified O0-R1 repair. Closed outer/binding fields, selected/inherited ordering, roles, versions, selector memberships and create/update predecessors are otherwise mapped to the real validator at /Users/calebbolden/Projects/oss/open-brain-obsidian-o0-review/packages/engine/src/open_brain_engine/portable/review_binding.py:77. |
| Scope and integration boundary | COMPLETE. Existing plugin dispatch/registration, manual inspection parsers, engine schema and Portable catalogs are unchanged. O0 modules are preparatory; no facet UI or live bridge availability is inferred. |

## Custody and security assessment

The identity helper uses established Stage 1 custody checks rather than editable frontmatter IDs. It allows only already-registered facet metadata to differ, compares protected content with both accepted managed content and the current canonical revision, and closes unavailable custody as `not_found`. Nonowner/restricted authority is refused before lookup. Tests prove no SQL or file mutations on resolution or refusal, using real synthetic Brains. Actual file/root replacement races are injected into the real reader without fabricating its results.

Two known feasibility constraints are explicitly documented in the kit and spec: canonical pages are the only proven managed mapping, and observation after materialization/atomic replacement is required. Resolution never observes or accepts content. B must expose an explicit safe observation action or explain the existing CLI command; silently calling reconcile would accept body edits. The reused reader performs bounded full-vault inventory twice and may conservatively return unavailable for oversized/incomplete inventories. These are disclosed O0 boundaries, not missing source-note functionality or newly invented implementation scope.

## Independent verification

All Python execution used `/Users/calebbolden/Projects/oss/open-brain-public/.venv/bin/python`, cwd `/Users/calebbolden/Projects/oss/open-brain-obsidian-o0-review`, and explicit `PYTHONPATH=/Users/calebbolden/Projects/oss/open-brain-obsidian-o0-review/packages/engine/src:/Users/calebbolden/Projects/oss/open-brain-obsidian-o0-review/packages/app/src`.

- `-m pytest -q packages/app/tests/unit/test_obsidian_facet_contract_kit.py packages/engine/tests/integration/test_managed_record_identity.py`: **48 passed in 8.02s** on `f7dd38d`.
- Isolated `npm ci --ignore-scripts --no-audit --no-fund` in `/Users/calebbolden/Projects/oss/open-brain-obsidian-o0-review/packages/obsidian-plugin`: 53 packages installed; no shared environment changed.
- `npm test -- tests/facet-wire.test.ts`: **211 passed** on `f7dd38d`.
- `npm run typecheck`: passed.
- Direct in-memory TypeScript execution of the real producer fixture: **failed**, reproducing O0-R1 as described above.
- On `5a544d5`, `npm test -- tests/facet-wire.test.ts`: **237 passed**; `npm run typecheck`: passed.
- On `5a544d5`, fresh Python real-producer inspection passed directly to the TypeScript parser: **accepted**, preserving distinct source/excerpt digests.
- On `5a544d5`, `-m pytest -q tests/security/test_architecture_boundaries.py::test_application_package_contains_only_foreground_local_surfaces`: **1 failed in 0.02s**, sole extra path is the new helper.
- On final `e3c165f`, `-m pytest -q tests/security/test_architecture_boundaries.py`: **5 passed in 0.04s**.
- `git diff --check 490bd05..HEAD`: passed. Review repository remains clean; ignored isolated dependencies are the only local setup artifact.

Proof kinds are kept separate. The 27 managed-identity tests are real producer/custody executions. The remaining Python kit cases are codec/hash checks plus real inspection production. Most TypeScript cases are pure codecs, and cannot prove server authority or effects. Future Stage 2 O01–O15, live bridge admission, persistent retry/recovery, plugin-created restore and actual Obsidian usability are not verified by this report.

## Exact-candidate full-gate receipt

Attribution: the coordinator executed `make verify` on `e3c165f90753464271bcff2aacb1a8f45ad64872` in the isolated verification worktree `/Users/calebbolden/Projects/oss/open-brain-facets-verify-1` and reports exit 0 and a clean worktree. R read the nonempty completed log `/tmp/obsidian-o0/full-candidate2.log` and confirmed these results:

| Check | Recorded result |
|---|---|
| Ruff and mypy | Passed; mypy checked 436 source files |
| Python suite | 4,023 passed, 5 expected filesystem skips in 701.65 seconds |
| Python distributions | Engine, app, connectors and collector source/wheel builds succeeded |
| Obsidian plugin | Typecheck/build passed; 528 tests passed across 10 files |
| Existing desktop regressions | Typecheck/build passed; 259 frontend tests and 1 packaging test passed |
| Rust checks | Formatting passed; 26 tests passed, no failures |

The five Python skips are explicitly logged in the Markdown import filesystem tests: one because the host filesystem rejects non-UTF-8 filenames, four because it does not preserve distinct NFC/NFD names. They are reported rather than counted as passing tests. The coordinator additionally reports `actionlint .github/workflows/ci.yml` and the full-branch diff check passed. Those two outcomes and the process exit/worktree status are coordinator receipts, not independent R reruns.

The R worktree was rechecked at the same exact candidate and remains clean. No source, test or contract changed during this final receipt continuation.

## Scope after O0

No additional O0 code fix or review gate remains. If production or contract bytes change after this candidate, assess the delta rather than automatically carrying forward this recommendation. The remaining Stage 2 tasks and O01–O15 require their own implementation and real integration/Obsidian evidence. Existing desktop regression checks do not add desktop feature work to the O0 scope.

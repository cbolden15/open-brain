# Independent stage-one facets audit

Date: 2026-10-13. Mode: standard codebase-plan-auditor. Reviewer: R, read-only.

## Decision and scope

**Code readiness: READY.** All 22 acceptance requirements are COMPLETE in the code mapping below. No unresolved blocking findings remain. The five verified findings raised during review are fixed and independently rechecked.

**Repository implementation gate: PASSED.** Coordinator receipt on `abeab52136a668fd851a6d4a7c72aa8fa67baedb`: make verify passed: Ruff, mypy (431 files), 3975 Python tests with 5 filesystem skips, four package builds, 291 plugin tests, 259 desktop frontend tests, 1 desktop packaging test and 26 Rust tests. The independent code review below is unchanged. This receipt authorizes the local catalog-freeze annotation, not publication or deployment.

Reviewed candidate: `abeab52136a668fd851a6d4a7c72aa8fa67baedb`, diff base `bd368ec`. The review worktree is `$HOME/Projects/oss/open-brain-facets-stage-one-r` and was clean at review completion. Original C0 `4a29944b4fead146c6f9bf5c535d0a1595799870` is an ancestor. Current C0 is `ed74327f34ec2526fd36b1737e36b05bcd2cdc62`, revision `facets-c0-v4`, fixture manifest SHA256 `95e85b535eeb4263a6e430970ecf1d49de8d0b72de7425e41be703897d93dbc5`.

Independently read catalog digest: `f482302bc9e7d163b9758cb1fd4de90f870a04c58e7cab384640c7314f124c28`. Separate schema-11 catalog checksum: `a4668f65694aeb50d5d9af283faa088ce66d3a298840ddc3c530ea341c5806c4`. Recommend freezing the unchanged v6 catalog after the exact-candidate full gate passes. Documentation/module-docstring freeze annotations do not authorize changing catalog contents or historical formats.

Plan: $HOME/Projects/oss/open-brain-facets-stage-one-r/docs/design/owner-defined-facets-stage-one.md. Accepted ADR: $HOME/Projects/oss/open-brain-facets-stage-one-r/docs/architecture/decisions/0020-independent-owner-defined-facets.md. Contract: /tmp/facets-stage-one/R.md, with final pins supplied by the coordinator. Skill: $HOME/.codex/skills/codebase-plan-auditor/SKILL.md. The task-specific private output path overrides the skill's default repository report path; no source, tests, repository docs or commits were written by R. Only synthetic temporary Brains were exercised.

COMPLETE means implemented and verified through source inspection plus the cited real runtime evidence; it does not turn a focused test result into a full release gate. Producer-reported and coordinator-reported checks are labeled separately below. Contract-only or injected-error proofs are not counted as durable state, authority, recovery or restore proofs.

## Requirement map

| Case | Status | Primary source evidence | Runtime proof and assessment |
|---|---|---|---|
| F01 | COMPLETE | [$HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/facets.py:290]($HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/facets.py:290) | Name normalization, stable IDs, archived-name reservation; real catalogue, rename and query tests. |
| F02 | COMPLETE | [$HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/facet_queries.py:46]($HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/facet_queries.py:46) | Logical source/page subject resolution and current heads; real proven successor and independent equal-content capture cases. |
| F03 | COMPLETE | [$HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/facet_store.py:120]($HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/facet_store.py:120) | Facet transaction changes metadata only; real exact content/provenance/privacy/revision assertions. |
| F04 | COMPLETE | [$HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/facets.py:290]($HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/facets.py:290) | Archive checks apply to new membership; real retained assignment, removal and unchanged-operation cases. |
| F05 | COMPLETE | [$HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/facets.py:213]($HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/facets.py:213) | Authority and subject scope precede retry/version handling; real later-state retry, divergent operation ID and principal binding cases. |
| F06 | COMPLETE | [$HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/facet_store.py:120]($HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/facet_store.py:120) | State/event/receipt/generation commit together under writer admission; real pre/post-commit faults, concurrent retries and reopen. |
| F07 | COMPLETE | [$HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/facet_queries.py:176]($HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/facet_queries.py:176) | Normalized selector conjunction and stable ordered results; real same-dimension AND and 201-result pagination. See nonblocking full-scan limitation. |
| F08 | COMPLETE | [$HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/facets.py:417]($HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/facets.py:417) | Cursor input/authority/generation binding with expiry/root custody; real scope/tamper/generation tests plus independent 16-minute expiry and directory-relocation executions. |
| F09 | COMPLETE | [$HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/facet_publication.py:168]($HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/facet_publication.py:168) | Explicit create destination, fixed update destination and separately authorized sources; real cross-space and unassigned-source publication. |
| F10 | COMPLETE | [$HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/facet_publication.py:268]($HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/facet_publication.py:268) | Selection and cumulative provenance bounds; real zero/33-source and cumulative-overflow refusal without partial proposals. |
| F11 | COMPLETE | [$HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/facet_publication.py:432]($HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/facet_publication.py:432) | Decision-transaction binding revalidation; real membership/head/route/query/attachment/target/authority conflict cases. |
| F12 | COMPLETE | [$HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/facet_publication.py:94]($HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/facet_publication.py:94) | Semantic snapshot excludes display-only changes and unrelated matches; real rename, new-match and unrelated-assignment cases. |
| F13 | COMPLETE | [$HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/review_bound.py:695]($HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/review_bound.py:695) | Independent pages share source evidence without relaxing historical binding versions; real shared evidence and P-reported 106 publication/manual/patch/legacy checks. |
| F14 | COMPLETE | [$HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/facet_publication.py:519]($HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/facet_publication.py:519) | Publication attachment and assignment stores remain distinct; real attachment/page-assignment separation. |
| F15 | COMPLETE | [$HOME/Projects/oss/open-brain-facets-stage-one-r/packages/app/src/open_brain/services/facets.py:206]($HOME/Projects/oss/open-brain-facets-stage-one-r/packages/app/src/open_brain/services/facets.py:206) | Actual CLI/MCP dispatch, grants, authority, budgets, retries and pre-effect reservation; R2/R3/R4 fixed, actual stdio and independent wrapper-size checks pass. |
| F16 | COMPLETE | [$HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/facet_schema_migration.py:100]($HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/facet_schema_migration.py:100) | Exclusive migration admission and durable upgrade handling; real migration faults, live old peer, bad checksum and unknown/newer schema refusal. |
| F17 | COMPLETE | [$HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/portable/facets_v6.py:244]($HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/portable/facets_v6.py:244) | V6 evidence replay and references validated before restore; real populated export/import/reopen/query/re-export and adversarial event/projection/receipt tests. R1/R5 fixed. |
| F18 | COMPLETE | [$HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/portable/facets_v6.py:356]($HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/portable/facets_v6.py:356) | Imported bindings preserve frozen evidence and validate current state at decision time; real valid/stale pending, cross-space destinations, cumulative/shared evidence and successor-route restores. |
| F19 | COMPLETE | [$HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/portable/versioned.py:1]($HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/portable/versioned.py:1) | Existing version dispatch preserved; independently executed real v1/v2/v3 imports, valid standalone-v4 refusal and v5 restore, plus earlier future-format/ingress refusal evidence. |
| F20 | COMPLETE | [$HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/facet_vault.py:815]($HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/facet_vault.py:815) | Preview custody, mapping/version/file guards and explicit apply; real rename/file races, expiry, missing mappings, archive additions and missing-subtree cases. |
| F21 | COMPLETE | [$HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/managed_workspace.py:1106]($HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/managed_workspace.py:1106) | Registered generated metadata uses verified content preimages; real facet-only/combined edits, index preservation, modified-view export refusal and generated-view exclusion. |
| F22 | COMPLETE | [$HOME/Projects/oss/open-brain-facets-stage-one-r/packages/app/tests/integration/services/test_facets_end_to_end.py:35]($HOME/Projects/oss/open-brain-facets-stage-one-r/packages/app/tests/integration/services/test_facets_end_to_end.py:35) | Real CLI/engine classify, proven revision, query, publication, archive, vault reconciliation, export/import/reopen and fresh query; independently passes after R5 repair. |

## Closed findings, ordered by original severity

### R1 HIGH: impossible unchanged-operation receipt accepted by Portable replay — FIXED

Original real-producer reproduction changed an assignment no-op request's expected version and receipt version to 999, recomputed its request digest, and production decoding accepted it despite final subject version 1. The repaired [$HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/facet_evidence.py:356]($HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/facet_evidence.py:356) requires the complete normalized no-op to match one reachable historical event-boundary state. It preserves legitimate later retries, version-zero absent removals and archived existing-assignment no-ops. The independently executed real rehashed archive regression now refuses the tampered import before destination creation. This closes the semantic receipt gap, rather than merely checking a checksum.

### R5 HIGH: proven successor facet publication could not export — FIXED

Original F22 execution succeeded through vault reconciliation, then failed export with `review source state changed` because a selected successor inherited its logical source's predecessor route. The facet-only producer path [$HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/review_bound.py:695]($HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/review_bound.py:695) now derives route state from proven revision lineage. [$HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/portable/facets_v6.py:295]($HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/portable/facets_v6.py:295) validates the frozen route against that lineage and retained route history, independently of the source's mutable current space. [$HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/portable/v3.py:277]($HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/portable/v3.py:277) exempts only the new facet binding version from the obsolete per-capture route check; older binding paths remain intact.

Independent targeted execution passes F22, successor/inherited-route export and restore, frozen stale-pending route preservation, and current-state approval refusal after reroute. The producer and validator agree on provenance from earlier source revisions. Later route changes do not invalidate historical archive evidence, but still prevent stale approval.

### R2 MEDIUM: CLI rename/query-update grammar differed from the spec — FIXED

The original parser interpreted dimension/value rename as NAME ID and lacked query-update `--name`. [$HOME/Projects/oss/open-brain-facets-stage-one-r/packages/app/src/open_brain/services/facets.py:443]($HOME/Projects/oss/open-brain-facets-stage-one-r/packages/app/src/open_brain/services/facets.py:443) now implements ID NAME and the documented flag. Actual CLI rename/update tests independently pass; this was not closed by parser shape alone.

### R3 MEDIUM: facet transport limits omitted wrappers and disagreed on Unicode sizing — FIXED

Originally Unicode history returned an oversized envelope or failed instead of continuing. A later partial repair still returned a 534,096-byte MCP list envelope above the 250,000-byte cap. A 66,201-byte wrapped mutation request also committed and returned an oversized receipt.

The final [$HOME/Projects/oss/open-brain-facets-stage-one-r/packages/app/src/open_brain/services/facets.py:206]($HOME/Projects/oss/open-brain-facets-stage-one-r/packages/app/src/open_brain/services/facets.py:206) checks actual request bytes and operation-specific wrapped receipt capacity before effects. [$HOME/Projects/oss/open-brain-facets-stage-one-r/packages/app/src/open_brain/services/mcp_protocol.py:158]($HOME/Projects/oss/open-brain-facets-stage-one-r/packages/app/src/open_brain/services/mcp_protocol.py:158) preserves raw line length, including whitespace/discarded metadata. [$HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/facets.py:76]($HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/facets.py:76) provides an immutable per-call page admission view using the actual trusted encoder, with no public request/authority signature change and no weakened cursor limit binding.

Independent final-candidate real MCP reproduction: a 66,000-character request ID returns `invalid_arguments`, with event state unchanged; a 3,900-character ID returns `response_too_large`, also before mutation. A history of 100 events with 115-emoji names returns four complete wire envelopes of 243,156, 246,636, 246,636 and 7,724 bytes. All event sequences appear exactly once. Targeted real stdio tests additionally cover raw metadata/whitespace, normal UTF-8, exact-fit page admission and cursor continuity. Single-item failure is retained only when that item cannot fit the actual envelope.

### R4 MEDIUM: denied MCP review lost its closed error code — FIXED

The original private-source/source-scope/destination-scope cases denied access but surfaced `operation_failed` instead of `not_found`. Review publication now preserves approved facet/T03 error codes through the service boundary. All three actual MCP cases independently pass. No access bypass was established by the original finding.

## Nonblocking limitation

[$HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/facets.py:640]($HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/facets.py:640) materializes the full eligible result set before [$HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/facets.py:417]($HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/facets.py:417) validates the cursor and applies page limits. Output ordering, filtering and continuation are correct, including the real 201-result test. CPU, file I/O and memory therefore scale with all matches on each page, even for a small limit or stale cursor. This is a confirmed source-level performance limitation, not a measured new release blocker. A later optimization can validate cursor custody first and apply keyset bounds before projecting all records. It is not a new stage-one scope mandate.

## Integration and custody assessment

The inspected managed vault path joins owner authority, root custody, retained ID/name mappings, file fingerprints, subject versions, mutation evidence and prepared projection state. Apply and recovery compare retained preimages before replacing generated content. Changed owner files are retained and surface a projection conflict. Completed-operation replay retains its original mutation receipt while reporting current projection status. Relevant implementation: [$HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/facet_vault.py:815]($HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/facet_vault.py:815), [$HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/facet_vault.py:1140]($HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/facet_vault.py:1140), [$HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/managed_workspace.py:1457]($HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/managed_workspace.py:1457) and [$HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/managed_portability.py:66]($HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src/open_brain_engine/engine/managed_portability.py:66).

Registered facet metadata is separated from verified source content; generated indexes are excluded from authoritative managed evidence. Modified generated views block export. These conclusions include real producer/recovery/restore tests, not only pure projection helpers. No additional authority, persistence, publication-recovery or managed-custody blocker was confirmed in this bounded standard review.

## Verification record

R commands used cwd `$HOME/Projects/oss/open-brain-facets-stage-one-r` and `$REPO/.venv/bin/python -m pytest`, with explicit `PYTHONPATH=$HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/src:$HOME/Projects/oss/open-brain-facets-stage-one-r/packages/app/src`.

| Candidate | Independent R execution | Result |
|---|---|---|
| `8a0b0d4` | Engine facets, faults and Portable v6 suites | 72 passed in 12.96s |
| `ec4f057` | Acceptance/publication/MCP, excluding unavailable V/F22 | 80 passed, 3 failed, 9 deselected in 53.66s; all failures were R4 and later closed |
| `547919e` | Real V/recovery/stale-pending/no-op/MCP/F22 selection | 19 passed, 1 failed, 69 deselected in 15.95s; sole failure was R5 and later closed |
| `d338dfb` | CLI grammar/rename/update and Unicode selection | 13 passed, 23 deselected in 4.99s |
| `8ec412b` | End-to-end, Portable v6, facet publication and engine suites with `-k 'f22 or wrapper or wrapped or padded or whitespace or receipt_bound or page_admission or unicode or successor or inherited_route or frozen_route or reroute'` | 17 passed, 128 deselected in 13.60s |
| `abeab52` | Real legacy v1/v2/v3 imports, validated standalone-v4 refusal and v5 empty-facet restore | 5 passed in 2.39s |
| `abeab52` | Standalone real MCP adversarial request/receipt/history reproduction described under R3 | Both oversize mutations refused before effects; 100 events delivered within every wrapped page cap |

The five final historical cases were `$HOME/Projects/oss/open-brain-facets-stage-one-r/packages/app/tests/unit/engine/test_source_tasks.py::test_schema_seven_imports_legacy_portable_without_changing_evidence` (three versions), the same file's `test_standalone_v4_import_refusal_uses_valid_v4_fixture`, and `$HOME/Projects/oss/open-brain-facets-stage-one-r/packages/engine/tests/contract/test_portable_brain_v6.py::test_legacy_v5_restore_keeps_empty_facets_and_is_not_reinterpreted_as_v6`.

Additional independent checks: real cursor expiration after 16 minutes; physical root relocation invalidating an old cursor; current C0 fixture hash; original C0 ancestry; v6 catalog digest; clean worktree and `git diff --check`. The production source in `abeab52` is unchanged from `8ec412b`; the intervening addition is T's transport error-preservation tests and documentation.

Separately attributed evidence inspected:

- P report `/tmp/facets-stage-one/P-report.md`: 106 passing real publication/manual/patch/legacy checks after the lineage repair (43 publication plus 63 regressions).
- X report `/tmp/facets-stage-one/X-report.md`: 189 passing Portable v6, real V and historical v3/v5 checks after the shared route fix. The broad count is not substituted for the final independent older-format executions above.
- T report `/tmp/facets-stage-one/T-report.md`: 12 passing transport regressions. Six exercise real engine/stdio requests; six inject closed adapter errors and prove only transport code preservation.
- Coordinator: 123 combined real acceptance/fault/MCP/migration/Portable checks passed in 56s on `8ec412b`. Full `make verify` on `abeab52136a668fd851a6d4a7c72aa8fa67baedb` remains active at `/tmp/facets-stage-one/full-candidate3.log`.

## Remaining gate

No code fix is required by this review. Record the coordinator's exact-candidate full gate receipt before marking overall release verification complete or executing the final catalog-freeze annotation. If production or catalog bytes change after the reviewed candidate, assess that delta rather than carrying this recommendation forward automatically.

## Coordinator completion receipt

Candidate: `abeab52136a668fd851a6d4a7c72aa8fa67baedb`. Command: `make verify`, exit 0, in
`$HOME/Projects/oss/open-brain-facets-verify-1`.
make verify passed: Ruff, mypy (431 files), 3975 Python tests with 5 filesystem skips, four package builds, 291 plugin tests, 259 desktop frontend tests, 1 desktop packaging test and 26 Rust tests.
Private run log: `/tmp/facets-stage-one/full-candidate3.log`.
The five skips are filesystem limitations: non-UTF-8 filenames (one) and distinct NFC/NFD filenames (four).
They were confirmed separately with 14 passing filesystem tests and 5 skips.
Full branch `git diff --check bd368ec..HEAD` and `actionlint .github/workflows/ci.yml` passed.

This completion receipt supersedes the pending coordinator-gate statements retained in the review history.
The post-verification delta is documentation and the catalog module's docstring; catalog bytes and runtime
behavior are unchanged. Post-annotation conformance passed 100 checks; full Ruff/mypy, actionlint and whitespace checks passed.
Native executable/Homebrew packaging, live GUI acceptance and provider qualification were not invoked
by this change. Existing desktop regression checks above remain part of the repository gate.
All implementation commits remain local.

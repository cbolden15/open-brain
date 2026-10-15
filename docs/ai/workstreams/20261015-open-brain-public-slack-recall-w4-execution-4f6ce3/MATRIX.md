# W4 synthetic acceptance matrix

Candidate worktree: `/Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w4-20261015-6d407b`.
Prepared baseline: `20f6c40`. This ledger maps the implementation plan's entire acceptance matrix to executable synthetic evidence. W3's preserved acceptance receipt records 7,474 Python passes at `b36dfed44f063b5b1d98d4f0130e1a460a945147`; these historical receipts do not substitute for fresh W4 checks.

All fixture data is synthetic. The journey uses real `SlackSourceClient`, `PriorityRuntime`, collector admission, source routing, engine sharing, durable consent and a real scoped MCP stdio subprocess. Only Slack HTTP responses are replaced. Unit driver cases intentionally supply protocol responses to isolate budget and malformed-response behavior. They are identified separately below.

## Executable evidence index

The short suite IDs below refer to these absolute paths. Each matrix cell names the exact test function within the suite; parameters are shown where they define the oracle.

| ID | Test file |
|---|---|
| J | `/Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w4-20261015-6d407b/packages/app/tests/integration/services/test_slack_decision_recall.py` |
| S | `/Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w4-20261015-6d407b/packages/app/tests/integration/engine/test_slack_sharing.py` |
| P | `/Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w4-20261015-6d407b/packages/app/tests/integration/engine/test_slack_sharing_proof.py` |
| R | `/Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w4-20261015-6d407b/packages/app/tests/integration/engine/test_slack_sharing_races.py` |
| V | `/Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w4-20261015-6d407b/packages/app/tests/integration/engine/test_slack_sharing_surfaces.py` |
| C | `/Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w4-20261015-6d407b/packages/collector/tests/integration/test_slack_capture_acceptance.py` |
| L | `/Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w4-20261015-6d407b/packages/collector/tests/integration/test_live_capture.py` |
| H | `/Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w4-20261015-6d407b/packages/connectors/tests/contract/test_slack_live.py` |
| E | `/Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w4-20261015-6d407b/packages/app/tests/integration/engine/test_w2_mcp_eligibility.py` |
| M | `/Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w4-20261015-6d407b/packages/app/tests/integration/services/test_w2_mcp_stdio.py` |
| A | `/Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w4-20261015-6d407b/packages/app/tests/unit/test_scoped_agent_setup.py` |
| T | `/Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w4-20261015-6d407b/packages/app/tests/unit/test_session_authority.py` |
| D | `/Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w4-20261015-6d407b/packages/app/tests/unit/test_slack_recall_driver.py` |
| Q | `/Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w4-20261015-6d407b/packages/app/tests/unit/engine/test_slack_sharing_projection.py` |
| B12 | `/Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w4-20261015-6d407b/packages/app/tests/integration/engine/test_portability_v12.py` |
| B10 | `/Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w4-20261015-6d407b/packages/engine/tests/contract/test_portable_brain_v10.py` |
| B9 | `/Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w4-20261015-6d407b/packages/app/tests/unit/engine/test_portable_v9_historical.py` |
| B8 | `/Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w4-20261015-6d407b/packages/app/tests/unit/engine/test_portable_v8_restore.py` |
| B7 | `/Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w4-20261015-6d407b/packages/app/tests/integration/engine/test_portability_v7.py` |
| B6 | `/Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w4-20261015-6d407b/packages/app/tests/integration/engine/test_portability_v6.py` |
| B5 | `/Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w4-20261015-6d407b/packages/app/tests/integration/engine/test_portability_v5.py` |
| BM | `/Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w4-20261015-6d407b/packages/app/tests/integration/engine/test_managed_portability.py` |
| BI | `/Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w4-20261015-6d407b/packages/app/tests/integration/engine/test_portability.py` |
| G | `/Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w4-20261015-6d407b/packages/app/tests/unit/engine/test_slack_sharing_migration.py` |
| K | `/Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w4-20261015-6d407b/packages/app/tests/unit/test_catalog.py` |

## Scenario map

“Existing” means the assertion predates W4. “W4” identifies added acceptance coverage. These are code-evidence mappings; fresh command results are recorded separately below.

| Group | Scenario | Executable oracle | Coverage |
|---|---|---|---|
| Capture | Real connector parsing and collector admission | J `test_slack_local_recall_continuity_and_external_release_refusal`; real HTTP-shaped messages become exact locally projected text | Existing |
| Capture | Duplicate identity/import | C `test_work_privacy_supported_and_dual_selection_deduplicates`; J local continuity; one current source, no duplicate revision | Existing |
| Capture | Lost acknowledgement and restart | L `test_acknowledgement_follows_commit_and_lost_response_retry_is_idempotent`, `test_crash_reuses_staged_batch_without_provider_refetch_and_commits_after_ack`; S `test_slack_lost_acknowledgment_and_restart_resume_one_copy` | Existing |
| Capture | Routed source edit and new source unrouted | J local continuity; same logical source/route for edit, distinct unrouted source for correction | Existing |
| Capture | Partial scan | H `test_failed_or_partial_scan_cannot_reconcile_absence_and_keeps_checkpoint`, `test_tracked_old_thread_refresh_freezes_replies_and_reports_partial` | Existing |
| Capture | Rate limit | H `test_channel_discovery_and_rate_limit_are_bounded`; L `test_rate_limit_preserves_checkpoint_and_respects_retry_time` | Existing |
| Capture | Deletion only after complete coverage | H `test_complete_frozen_scan_reconciles_missing_message_and_reply`, `test_failed_or_partial_scan_cannot_reconcile_absence_and_keeps_checkpoint`, `test_moving_floor_does_not_prove_older_message_deleted` | Existing |
| Capture | No collector routing authority | J `test_w4_persistent_stdio_observation_refresh_and_separate_correction`: `sources.route` refuses with `source_invalid_operation`, source state unchanged before owner routing | W4 |
| Scope | Work space distinct from tier, Personal/unrelated Work decoys | J local continuity routes Personal into Work space and unrelated Work elsewhere; external results/guessed reads remain empty/not_found | Existing |
| Scope | Same-space local-only originals and other-provider copies | J local continuity; S `test_slack_work_copy_lifecycle_and_structured_selection`; V `test_slack_copy_policy_marker_does_not_authorize_an_ordinary_capture` | Existing |
| Scope | No policy and legacy/owner elevation | M `test_cli_mcp_policy_origin_covers_legacy_negotiated_and_guessed_reads` (explicit/omitted policy, owner elevation); E `test_implicit_mcp_filters_original_copy_and_canonical_before_search_and_reads` | Existing |
| Scope | Missing/revoked consent | T `test_external_policy_requires_current_durable_consent`, `test_external_policy_rejects_missing_consent_path`; R `test_consent_revocation_after_snapshot_prevents_mcp_output` | Existing |
| Scope | Wrong declared provider | S `test_slack_work_copy_lifecycle_and_structured_selection`; S `test_projection_drift_is_explicit_only_with_matching_read_authority` | Existing |
| Scope | Raw external read refused, approved copy requires current bindings | J local continuity; S `test_slack_stale_approval_hidden_from_search_read_and_history` (`edit`, `withdraw`, `route`, `revoke`); P receipt-corruption tests | Existing |
| Scope | Restricted metadata/IDs cannot affect ranking, cursor or expansion | V `test_damaged_v2_copy_withheld_before_ranking_paging_and_history`, `test_v2_canonical_source_expansion_requires_every_current_approval`; E entire eligibility suite | Existing |
| Scope | Message requests shell/network tool use | J `test_w3_real_stdio_invite_rationale_paraphrases_and_inert_hostile_evidence`: content stays untrusted, only scoped search/read calls issued | Existing synthetic driver; actual-model resistance unproven |
| Evidence | Unlabeled rationale and proposal versus stated decision | J W3 rationale fixture; exact fixture-owned claim kinds and quotes validated | Existing |
| Evidence | Multi-message context | J W3 rationale fixture and large-selection fixture; engine membership traversed via structured DTO | Existing |
| Evidence | Full nested fake envelope, fabricated IDs and foreign permalink | J W3 rationale fixture; D `test_nested_envelope_selection_and_original_ids_never_grant_authority`; only structured returned IDs authorize citations | Existing |
| Evidence | Unsafe URL | Q `test_unsafe_permalink_withheld_with_exact_reason_and_projected_text`; D `test_validator_rejects_unsupported_claims_quotes_ids_and_links` | Existing |
| Evidence | Unicode projection and exact quote integrity | J W3 rationale fixture checks NFC-returned quote and rejects decomposed mismatch; D UTF-8 page fixture | Existing |
| Evidence | Incomplete reads | D `test_incomplete_or_rebound_read_cannot_support_an_answer`, `test_protocol_errors_and_unavailable_reads_are_incomplete`, `test_incomplete_result_never_accepts_factual_claims` | Existing protocol-unit evidence |
| Evidence | Byte, read/page, query, result, record and total-call limits | D `test_64k_budget_counts_projected_utf8_envelope_bytes`, `test_exact_64k_envelope_and_sixteen_pages_fit_the_bounds`, `test_pagination_and_total_call_guards_terminate`, `test_variants_and_record_limits_do_not_silently_drop_candidates`, `test_search_overflow_or_partial_page_is_incomplete` | Existing protocol-unit evidence |
| Evidence | Withheld/stale, revoked or never-approved selection member | J W3 large-selection fixture gives `incomplete/unavailable_member` after edit; S lifecycle fixture marks never-approved/revoked members unavailable; D `test_only_consistent_complete_structured_selections_expand_authority` refuses unavailable members; no factual claim allowed for incomplete result | Existing composed engine/driver evidence |
| Evidence | One edited member re-approved without re-approving others | J `test_w3_real_stdio_large_selection_and_one_member_reapproval` (9): eight unchanged copies retained, only revised member replaced | Existing |
| Evidence | Selection larger than eight, through 24 | J same fixture (9,24): all members read and revalidated, `1 + 2 * count` calls | Existing |
| Evidence | Foreign selection ID/member count in approved text | S `test_selection_large_membership_is_structured_without_text_authority` (9,24); engine descriptor wins and missing members remain unavailable | Existing |
| Evidence | Complete reads retain unknown-Slack qualifier | J W3 rationale and W4 persistent-observation fixtures; validator requires exact qualifier | Existing + W4 |
| Changes | Edit/withdraw/reroute between preview and approval | P `test_v2_source_transition_after_preview_reserves_no_copy` (`edit`, `withdraw`, `route`): `revision_changed`, zero decisions/links | Existing |
| Changes | Search/read stale boundary | J W4 persistent fixture discovers original copy, imports observed edit, then old read refuses; R explicitly searches for the copy before each edit/withdraw/reroute/revoke barrier transition | Existing + W4 |
| Changes | Before/after release snapshot | R `test_source_mutation_respects_returning_read_snapshot` (`revoke`, `edit`, `withdraw`, `route`) × (`before`, `after`): pre-snapshot refuses; after-snapshot produced bytes survive; next read refuses. Reroute creates a different owner Work space and asserts destination plus route-version increment. | Existing edit/revoke; W4 withdraw/route |
| Changes | Actual read lease versus writer | R `test_real_reader_lease_serializes_revocation`: writer gets `LockBusyError` while snapshot lease held; retry succeeds afterward | Existing |
| Changes | Consent revoke between snapshot and output | R `test_consent_revocation_after_snapshot_prevents_mcp_output`: synchronized post-snapshot revoke yields `stale_policy` | Existing |
| Changes | Revoked approval, same and cross process | S lifecycle/stale tests; S `test_cross_process_v2_revocation_is_observed_by_existing_reader` uses CLI writer subprocess and existing reader; W4 persistent stdio observes owner-process revocation | Existing + W4 |
| Changes | Failed capture/link acknowledgement and exact retry | S `test_slack_lost_acknowledgment_and_restart_resume_one_copy` (`capture-before`, `capture-after`, `route-before`, `route-after`): pending invisible, restart returns exactly one copy/link | Existing |
| Changes | Historical reads | S stale-copy test includes `brain_history_list/show`; V damaged-copy test; E retained/mixed-source eligibility tests | Existing |
| Changes | Projection version drift and idempotent approved bytes | S `test_projection_drift_is_explicit_only_with_matching_read_authority`: `reapproval_required` only to authorized reader; Q `test_projection_idempotence_and_inert_nested_envelope` | Existing |
| Changes | Portable restore and tamper refusal | B12 `test_real_mixed_sharing_selection_restore_reopen_reexport_exact`; `test_rehashed_v12_tamper_refused_before_destination_write` (9 tamper kinds); exact current bytes/revocation retained | Existing |
| Changes | Frozen predecessor refusal and old-format imports | G migration/refusal; B12 older-version tamper refusal (7–11), genuine old archive import (11); B7 frozen v6 import/current sharing roundtrips; B6/B5/BM/BI historical restores; B8 frozen-v8 clean restore states; B9 frozen-v8-to-current import and v9/v10 interrupted restore checkpoints; B10 actual schema15 restore opens at schema17 | Existing |
| Changes | Reaction-only re-import keeps approval current | S `test_selection_one_member_repair_reaction_duplicate_and_separate_correction`; J W4 persistent fixture proves identical returned evidence through existing stdio child | Existing + W4 |
| Changes | Upstream edit before owner re-import | J W4 persistent fixture queues edit at Slack HTTP boundary, proves no fetch and identical quote/citation observation bytes, validates answer with unknown qualifier | W4 |
| Changes | After owner re-import stale copy withheld | Same fixture imports queued edit, guessed old read refuses, only new explicit approval releases edited quote | W4 |
| Changes | New-message correction needs selection/approval, does not edit original | Same fixture: new source initially unrouted/invisible, routed still invisible, separate approval releases it alongside existing copy; original source unchanged; explicit revoke replaces obsolete release | Existing engine-level; W4 persistent stdio |
| Client | Real stdio negotiation/tools/grants | M policy-origin test; J W3/W4 sessions initialize and discover tools, with no history grant | Existing + W4 |
| Client | Setup preservation; removal after deletion/revocation | A `test_scoped_setup_launches_policy_and_preserves_unrelated_settings`, `test_revoked_policy_rolls_back_and_removal_ignores_deleted_authority`; forwarding/settings/legacy setup suites in focused command | Existing |
| Client | Coexisting unowned/unscoped fragments | A `test_unowned_other_client_entry_requires_bound_acknowledgment`, `test_selected_project_nested_claude_mcp_is_detected` | Existing |
| Answer | Lexical paraphrases | J W3 rationale: invite-only/registration/support; D `test_three_lexical_variants_dedupe_same_immutable_identity` | Existing |
| Answer | Contradictory decisions, insufficient/ambiguous evidence | J W3 rationale includes proposal and explicit disagreement, requires unresolved answer; empty authorized results yield unknown | Existing deterministic fixture |
| Answer | Unknown owner/effective date; source versus observation time | J W3 rationale asserts absent owner/effective date and distinct timestamps; W4 unseen edit preserves observation metadata | Existing + W4 |
| Answer | Unknown Slack coverage | Every successful answer fixture requires selected-message/unknown-coverage qualifier; complete protocol pages make no upstream-completeness claim | Existing + W4 |
| Reporting | Protocol acceptance separate from actual-model quality | K `test_slack_recall_capability_separates_synthetic_acceptance_from_model_proof`; actual-client check remains `not_run` | W4 regression |

## Snapshot oracle and scope limits

The `after` barrier in R runs after the `read_snapshot` context exits, before the read returns its already-produced bytes. The engine's actual reader lease prevents a writer from committing while that context is open; the separate lease test verifies that exclusion. Together these tests prove the implemented boundary: mutation committed before snapshot acquisition refuses the read; mutation committed after snapshot byte production does not retract those bytes; subsequent reads refuse. They do not pretend to demonstrate a writer committing inside an active leased snapshot. Consent/policy has the distinct final-output revalidation boundary.

Reply-count/thread refresh is connector behavior, separate from the matrix's required reaction-only stability case. The new reaction test does not claim to cover reply-count-only imports; a positive `reply_count` requests the existing thread endpoint, which the history-only journey transport deliberately rejects. `latest_reply` alone does not trigger that expansion. Connector thread pagination/partial-coverage tests run separately.

The hostile-evidence fixture proves deterministic protocol/citation handling only. No actual client, model, provider, real Slack, pilot, service installation, publication or outbound messages ran. Actual-model citation quality and injection resistance remain unproven and must be recorded privately before the pilot.

## Verification receipts

All commands use the candidate worktree above as CWD. The common invocation is:

```sh
/usr/bin/time -p env UV_PROJECT_ENVIRONMENT=/tmp/obw4-env TMPDIR=/tmp/obw4 \
  PYTEST_XDIST_AUTO_NUM_WORKERS=2 CARGO_BUILD_JOBS=2 \
  uv run --frozen pytest -q -n 2 \
  -o tmp_path_retention_policy=failed -o tmp_path_retention_count=1 <selection>
```

| Check | Exact selection / evidence | Result and time |
|---|---|---|
| First changed run | J `::test_w4_persistent_stdio_observation_refresh_and_separate_correction` plus all R; log `/tmp/obw4-logs/w4-changed-tests.log` | 10 passed, 1 failed in 11.34 s; wall 12.34 s. Fixture supplied positive `reply_count`, which triggered unsupported `/api/conversations.replies` in its history-only HTTP double. Removed reply metadata to test the required reaction-only scenario. No runtime change. |
| Failed fixture preservation | Original `/private/tmp/obw4/pytest-of-calebbolden/pytest-0/popen-gw0/test_w4_persistent_stdio_obser0`; copy `/tmp/obw4-failed-first`; manifest `/tmp/obw4-failed-first/relocation-manifest.json` | 35 files, 1,225,377 bytes; manifest records original/copy paths, SHA-256 and sizes. Failed log unchanged. |
| Changed retry | J new W4 test plus K `::test_slack_recall_capability_separates_synthetic_acceptance_from_model_proof`; log `/tmp/obw4-logs/w4-changed-retry.log` | 2 passed in 5.83 s; wall 6.12 s. Broad run below additionally includes the later collector-route-refusal assertion. |
| Broad matrix | Exact selection below; log `/tmp/obw4-logs/w4-matrix-focused.log` | Exit 0: 641 passed in 366.43 s; wall 366.82 s. Before the final initial-search assertion in R; affected tests rerun below. |
| Search/snapshot and historical supplement | R; B8 `::test_actual_clean_restore_reexport_retains_authority_and_history`; B9 `::test_frozen_v8_archive_restores_to_fourteen_without_reinterpreting_old_bytes`, `::test_v9_and_current_hidden_restore_retries_without_promoting_interrupted_authority`; B10 `::test_real_schema15_empty_facet_restore_reopens_and_preserves_inherited_bytes`; log `/tmp/obw4-logs/w4-race-historical-final.log` | Exit 0: 29 passed in 28.00 s; wall 28.43 s. Includes explicit initial search; before final different-space reroute strengthening. |
| Final race behavior | All R with initial search, different-space reroute and route-version assertions; log `/tmp/obw4-logs/w4-races-final.log` | Exit 0: 10 passed in 9.63 s; wall 10.00 s. Subsequent changes only import formatting. |
| Initial lint | `uv run --frozen ruff check .`; log `/tmp/obw4-logs/w4-ruff.log` | Exit 1: three import-format/line-length errors in new test imports; wall 0.89 s. Fixed formatting only. |
| Final lint | `uv run --frozen ruff check .`; log `/tmp/obw4-logs/w4-ruff-final.log` | Exit 0: all checks passed; wall 0.10 s. |
| Full configured typecheck | `uv run --frozen mypy`; log `/tmp/obw4-logs/w4-mypy.log` | Exit 0: no issues in 673 source files; wall 12.21 s. |
| Whitespace and matrix references | `git diff --check`; deterministic source scan of all exact matrix test names | Exit 0; all 56 named functions exist. |

Broad matrix `<selection>`:

```text
packages/collector/tests/integration/test_slack_capture_acceptance.py
packages/collector/tests/integration/test_live_capture.py
packages/connectors/tests/contract/test_slack_live.py
packages/app/tests/integration/services/test_slack_decision_recall.py
packages/app/tests/integration/services/test_w2_mcp_stdio.py
packages/app/tests/integration/services/test_managed_cli_mcp.py
packages/app/tests/integration/engine/test_slack_sharing.py
packages/app/tests/integration/engine/test_slack_sharing_proof.py
packages/app/tests/integration/engine/test_slack_sharing_races.py
packages/app/tests/integration/engine/test_slack_sharing_surfaces.py
packages/app/tests/integration/engine/test_w2_mcp_eligibility.py
packages/app/tests/integration/engine/test_slack_development_evidence.py
packages/app/tests/integration/engine/test_portability_v12.py
packages/app/tests/integration/engine/test_portability_v7.py
packages/app/tests/integration/engine/test_portability_v6.py
packages/app/tests/integration/engine/test_portability_v5.py
packages/app/tests/integration/engine/test_portability.py
packages/app/tests/integration/engine/test_managed_portability.py
packages/app/tests/unit/test_slack_recall_driver.py
packages/app/tests/unit/test_slack_recall_contract_fixtures.py
packages/app/tests/unit/engine/test_slack_sharing_projection.py
packages/app/tests/unit/engine/test_slack_sharing_migration.py
packages/app/tests/unit/test_session_authority.py
packages/app/tests/unit/test_scoped_agent_setup.py
packages/app/tests/unit/test_agent_setup.py
packages/app/tests/unit/test_w2_setup_forwarding.py
packages/app/tests/unit/test_w2_settings_feasibility.py
packages/app/tests/unit/test_catalog.py
```

Final test files are frozen. SHA-256 values after import formatting:

| Suite | SHA-256 |
|---|---|
| R | `608d98f89437c8b89750a4d10908e49f6cf8aab57dcb4505cd2c0df244e7c9a7` |
| J | `1285c6887ac7f79e07c717035f1382429f4b212c604b22b2c1f2c4f22168ec29` |
| K | `555999ae6f01370339a51928ca95ec377772ac87bdefe2fb1161269fa0df824b` |

No unresolved implementation blocker was found. The test schedule is released to the coordinator/reviewer. Full verification/native gates and exact frozen candidate identity are coordinator-owned receipts. This worker runs no heavy gates and makes no commits.

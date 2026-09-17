# Scoped privacy and recovery correction

STATUS: BLOCKED. Privacy and the documented current recovery reproductions are corrected, but complete recovery acceptance remains blocked by a legacy standalone authority gap. This is a partial local correction, not a completed candidate. Independent review, coordinator full make verify and integration remain pending. Attempt 11 of 12; no nested workers or additional model calls.

Authority: coordinator ASSIGNMENT.json and FIX-PROMPT.md for correction-93ba1c; review evidence REVIEW.md, INDEPENDENT-REVIEW.md and REPRODUCTION.json in coordinator commit 8040897b5b9ccc1735062ffc3719a634daf2e751. Read only the five specified parent synthetic repro scripts from recovery-review-20260917T151055Z-081799. Runner and Git state matched the assignment before editing.

Starting branch: ob-new-user/m1-search. Exact starting HEAD: 0228b899526324d42f3faacc1f6bc40d2c5eeb47. Implementation and final report commit identities will be recorded at local handoff.

## Design and evidence

Inference validates each retained revision's ordered capture membership and its ancestors against durable capture privacy. Unsupported, absent or malformed membership fails closed. Traversing retained ancestry prevents an owner frontmatter edit from silently removing an inherited restriction. Both prepare and release use selection, while representative metadata and all DTO/schema/Portable fields remain unchanged. Existing revision, consent, exclusion and redaction bindings remain active.

Recovery first validates original journal, manifest, caller, revision/body/digest and frozen preimage bindings. Verified owner edit/link lineage cancels obsolete authority. Observed path movement cancels the old path without rebinding it; cancellation is persisted before observation returns because later observations and owner actions replace observation rows. Same-revision owner acceptance or explicit materialization can prove that the requested bytes already established the base. Completion still checks the original target's current bytes. A later owner edit, even a return to the old preimage, cancels consumed authority rather than writing again. Existing accepted-edit, exclusion and deactivation cancellation remains. Standalone materialization shares the transition checks; explicit setup/reattachment retains its existing exclusion semantics.

Initial new-test baseline: 51 failed, 4 passed, 91 deselected in 16.50s. Eight link cells initially had an incorrect quote; corrected synthetic quote and reran all 12 accepted-link cells: 12 failed, 119 deselected in 4.47s, all at recovery rather than fixture construction. Restricted-secondary privacy reached the fake callback; restricted-primary control rejected it. No live provider or credentials were used.

First implementation workspace/inference check: 146 passed in 46.41s. Extra lifecycle regressions then exposed ephemeral move evidence and consumed-preimage revival: 3 failed, 1 passed, 131 deselected in 1.50s. After correction, edge/matrix/prior-supersession checks: 92 passed, 43 deselected in 35.46s.

Portable/malformed-membership focused check: 9 passed, 1 failed, 150 deselected in 3.96s. This caught overbroad exclusion handling for explicit reattachment; restored existing behavior by limiting exclusion-based cancellation to refresh children. Next Portable check: 5 passed, 1 failed, 135 deselected in 2.42s; this was a fixture expectation that a local note toggle removes an imported portable-set exclusion. The test now asserts preservation of the portable exclusion; imported write-authority rejection is checked on eligible states.

## Coverage map

- Privacy supported APIs: test_retained_public_job_privacy_gates_provider_callback covers ordinary primary A plus public-job B, owner routing/approval, explicit refresh and consent, primary restriction control and eligible mixed-source release. Restricted cases assert zero callback invocations.
- Fail-closed membership: test_accepted_unverifiable_membership_fails_closed covers missing, empty, unsupported and missing-capture references through supported observation/acceptance. test_accepted_edit_cannot_drop_retained_restricted_member checks ancestry retention. test_corrupt_retained_privacy_is_rejected_at_release is explicitly labelled injected-corruption evidence, separate from the supported-API reproductions.
- Release: test_retained_membership_release_revalidates_supported_refresh checks intervening restricted publication/refresh and terminal request cancellation. Existing policy-change, consent, revision and redaction regressions remain in scoped checks.
- Recovery matrix: test_durable_transition_settlement_matrix covers materialization, identical acceptance, observed rename and accepted edit/link transitions across preparation, target-write and promotion, parent-first/leaf-first ordering, explicit recovery/actual reopen, repeated original-ID replay, stable histories/write counts, unaffected siblings and fresh requests. There are 44 applicable cells. Identical acceptance before target write is inapplicable to the reported same-revision transition because disk still has the prior bytes; accepting those bytes creates a new owner revision, covered by accepted-edit cases.
- Standalone: test_standalone_materialization_same_revision_acceptance covers target-write/promotion interruption, actual reopen and repeated original-ID replay without another revision/write.
- Durable cancellation edges: test_observed_move_settlement_survives_observation_replacement covers subsequent missing observation, exclusion and sibling acceptance. test_completed_intervening_write_does_not_revive_consumed_preimage protects a newer owner reversion.
- Integrity: existing test_pending_refresh_revalidates_state_before_any_write, test_superseded_refresh_does_not_disguise_journal_corruption, preimage/atomic-replacement race checks and inactive/exclusion restore cases remain. No mismatches are blanket-caught.
- Portable: test_settled_retained_history_portable_round_trip covers refreshed, resolved, accepted-edit-cancelled, rename-cancelled, excluded-cancelled and inactive-cancelled export/validation/import/reattachment. It compares revision bodies, IDs/parents/kinds/digests, representative privacy/provenance, ordered source membership and accepted head; inactive remains unattached. Eligible imported states reject refresh for absent write authority. Portable-set exclusions remain effective. Existing link/history round-trip tests also remain.

## Verification boundary

Required scoped pytest, Ruff, mypy, actionlint, diff check and rendered handoff validation are recorded below. No full suite, make verify, native/plugin/Rust build, benchmark, dependency refresh, network product call, service, live Brain, push, PR or integration. Full verification remains coordinator-owned.

Confirmed surprises above are recorded here for coordinator capture; no gotcha registry edits. The existing external-editor check-to-rename race remains unchanged. This scoped matrix does not claim exhaustive arbitrary transition composition or certify a live provider.

## Additional verification and concerns

The first required scoped run returned 220 passed and one new-test accessor-name error in 58.95s. The subsequent run returned 220 passed and one failure in 59.10s: the existing active_exclusions API returns invalid_policy for imported portable_set exclusions. This is an existing out-of-scope listing defect in managed_policy.py / the exclusion DTO, not lost exclusion enforcement. The owned Portable test verifies the actual retained set with read-only journal inspection and verifies that refresh skips the excluded note. No product policy/DTO changes were made. Coordinator should capture and separately scope this concern.

A later scoped run passed all 223 tests in 61.64s; after lint-only corrections, 223 passed in 64.04s. Ruff initially reported 32 import/line/style diagnostics, then two remaining diagnostics, subsequently corrected. Mypy initially reported one new-fixture argument mismatch (BrainEngine versus EngineTaskSet); the callback test now uses the public engine.tasks property. The affected callback controls passed: 4 passed, 17 deselected in 1.87s. A following scoped run passed 223 tests in 63.57s, Ruff passed, mypy passed over 297 source files, and actionlint/diff checks passed.

The matrix was then strengthened to require a real new publication after settlement, explicit conflict resolution where appropriate, and original-ID replay after that new publication. That focused run passed all 44 cells, 97 deselected in 25.72s. The final required command sequence below verifies these final test assertions too.

Standalone coverage additionally includes test_standalone_link_materialization_identical_acceptance at target-write and promotion boundaries with no pending refresh parent, same accepted revision, two actual reopens, duplicate original-ID receipts and unchanged content/mtime and revision count. The preparation cell cannot represent identical acceptance of already-written new bytes because the write has not happened yet; owner edits before writing remain covered by the existing supersession matrix.

Only supported capture references with verifiable retained privacy are cloud eligible. Unknown reference kinds are intentionally rejected. All provider effects were synthetic; on the corrected candidate restricted cases made zero callback invocations. No live provider, credentials, account state or private Brain content was accessed.

## Exact final verification commands

Environment: existing Python 3.14 .venv, UV_NO_SYNC=1 and UV_OFFLINE=1; no install/sync/download.

```sh
UV_NO_SYNC=1 UV_OFFLINE=1 .venv/bin/python -m pytest -q packages/app/tests/unit/test_m1_bridge_search.py packages/app/tests/integration/engine/test_managed_workspace.py packages/app/tests/integration/engine/test_managed_portability.py packages/app/tests/integration/engine/test_managed_inference.py packages/app/tests/integration/services/test_managed_cli_mcp.py packages/app/tests/unit/test_plugin_bridge.py packages/app/tests/integration/engine/test_reconciliation.py packages/app/tests/integration/engine/test_review_search.py packages/app/tests/integration/engine/test_m1_source_resolver.py packages/app/tests/integration/services/test_m1_public_search.py packages/engine/tests/contract/test_portable_brain_v3.py
.venv/bin/ruff check .
UV_NO_SYNC=1 UV_OFFLINE=1 .venv/bin/mypy
actionlint .github/workflows/ci.yml
git diff --check
node /Users/calebbolden/.codex/skills/workflow-governance/scripts/validate-handoff.mjs /Users/calebbolden/Projects/oss/open-brain-public-worktrees/m1-search/docs/ai/workstreams/20260917-open-brain-public-privacy-recovery-correction-93ba1c-reports/correction/20260917-open-brain-public-privacy-recovery-047eba/HANDOFF.json
```

Final outcomes and implementation commit identity are recorded below after execution. Final report commit SHA is necessarily external to its own committed content; the terminal response records that exact SHA, and `git log -1 --format=%H -- <this REPORT.md>` resolves it without altering the report. The handoff head pins the tested implementation commit; the final HEAD is its documentation-only child.

## Owned paths

- packages/engine/src/open_brain_engine/engine/managed_workspace.py
- packages/engine/src/open_brain_engine/engine/managed_inference.py
- packages/app/tests/integration/engine/test_managed_workspace.py
- packages/app/tests/integration/engine/test_managed_inference.py
- docs/ai/workstreams/20260917-open-brain-public-privacy-recovery-correction-93ba1c-reports/correction/20260917-open-brain-public-privacy-recovery-047eba/STATE.md
- docs/ai/workstreams/20260917-open-brain-public-privacy-recovery-correction-93ba1c-reports/correction/20260917-open-brain-public-privacy-recovery-047eba/HANDOFF.md
- docs/ai/workstreams/20260917-open-brain-public-privacy-recovery-correction-93ba1c-reports/correction/20260917-open-brain-public-privacy-recovery-047eba/HANDOFF.json
- docs/ai/workstreams/20260917-open-brain-public-privacy-recovery-correction-93ba1c-reports/correction/20260917-open-brain-public-privacy-recovery-047eba/REPORT.md

No other tracked files changed. No schema, DTO, public format, storage primitive, dependency, plugin or unrelated cleanup change. Exactly one workstream was generated with the installed workflow-governance helper; handoff Markdown is rendered from its JSON using the installed helper.

Next action: coordinator decision on the legacy-authority contract gap, then independent review using reserved attempt 12 and coordinator-exclusive full make verify. Do not merge, integrate, release or freeze contracts based solely on this scoped return. Both global blockers remain subject to parent review and full verification.

## Scoped return

- Base: `0228b899526324d42f3faacc1f6bc40d2c5eeb47`.
- Implementation_SHA / tested code HEAD: `c2ee045f1fec029418c665a49318b740283aab8a`.
- Final required pytest: **228 passed in 69.81s (0:01:09)**, exit 0.
- Ruff: **All checks passed**, exit 0.
- Mypy: **Success, no issues in 297 source files**, exit 0.
- actionlint `.github/workflows/ci.yml`: exit 0.
- `git diff --check`: exit 0; repeated after final report generation.
- HANDOFF.json validation: performed with installed workflow-governance helper before the documentation commit; matching Markdown rendered from the same JSON.
- Changed tracked files: four scoped code/test paths and the four generated report artifacts listed above.
- Final_HEAD: the documentation-only child of `c2ee045f1fec029418c665a49318b740283aab8a` containing this report. Its exact SHA is provided in the terminal response, avoiding an impossible self-referential commit hash in committed content.
- Scoped concerns: independent review/full verification still pending; imported portable-set exclusion listing issue requires coordinator triage outside this allowlist. Existing check-to-rename filesystem race unchanged.
- Skipped by assignment: full suite/make verify, native/plugin/Rust builds, benchmarks, independent review, integration/release actions. No checks were skipped or marked xfail to obtain a pass.
- Next action: reserved independent review, then exclusive coordinator verification. Global blockers are not marked closed by this worker.

## Final standalone integrity extension

Before the report commit, a source audit identified three standalone relaxation cases that could mask a corrupt journal. Added isolated negative tests for damaged preimage, damaged target after acceptance, and damaged target with a non-move observation: baseline **3 failed, 141 deselected in 1.72s**. Standalone successor cancellation now verifies retained preimage bytes and requires its unchanged target. Unlike refresh manifests, legacy standalone caller hashes do not bind the target: new observed moves therefore settle while observe still has the old path, rather than using any current observation as evidence of a move. A recovery-only legacy standalone move without that witness remains fail-closed; no original target is invented or rebound. The shared transition/integrity subset passed **51 tests, 114 deselected in 25.55s**. The full scoped command then passed **226 tests in 68.38s**, followed by Ruff, mypy (297 files), actionlint and diff checks.

The absent-preimage initial-setup edge was also reproduced: baseline **1 failed, 144 deselected in 0.37s**. Setup can complete earlier in the recovery pass than an interrupted standalone write. The existing setup journal and its completion time prove whether setup was still pending at preparation; this retains absence authority without accepting a corrupted null preimage on an already-established revision. The focused initial-setup and corruption tests passed **4 tests, 141 deselected in 1.25s**. Added an explicit corrupted-null-preimage control too. Final full verification includes these additions.

The helper's first handoff rendering invocation incorrectly passed a filename as JSON text; it was corrected to the documented stdin interface. JSON validation passed and the committed Markdown is rendered from the same JSON. Preliminary lint diagnostics included one overlap with an earlier test run finishing; the final required verification sequence is serial.

Residual authority limit: older standalone operations contain only a caller hash, rather than a refresh manifest binding the target. If an older already-moved standalone operation has lost its prior-path witness, this correction rejects that unverifiable target instead of masking corruption. Current supported observations durably settle the move before the witness can be replaced. Independent review should assess this conservative legacy boundary alongside the existing imported exclusion-listing concern; no missing authority is manufactured.

## Blocking design gap and final handoff

**BLOCKED, not DONE.** Existing standalone materialization caller hashes cover kind, note, operation ID and workspace, but not the frozen target path. The older observation API overwrote the managed note path and retained only the new observation; later observations/acceptance can erase even that witness. An old, already-moved standalone request and a corrupted target binding cannot reliably be distinguished using those retained fields. Silently cancelling every such mismatch would weaken the required corruption detection; reconstructing or rebinding a target would invent authority. Current observations settle cancellation while the old path is still known, and refresh manifests independently bind their targets, but this cannot retroactively create a missing legacy witness.

This limit is source-confirmed from the original standalone request digest and observation storage flow; it is not claimed as a separately executed parent reproduction. The new standalone integrity regressions demonstrate why relaxing all path/preimage mismatches is unsafe. Resolving the legacy case requires an explicit contract choice (for example, an owner reauthorization or a distinct legacy cancellation policy) beyond this assignment's unchanged authority/error contracts. No schema, DTO, public format or history rewrite was attempted. The worker stops here rather than claiming complete lifecycle settlement.

Final code commits are `df60657d1dff07a697795a4c91a43c1f28de9bad` and `c2ee045f1fec029418c665a49318b740283aab8a`; implementation_SHA is `c2ee045f1fec029418c665a49318b740283aab8a`. Final scoped verification after the initial-setup and null-preimage control additions: **228 passed in 69.81s**, Ruff clean, mypy **297 source files**, actionlint and diff checks exit 0. Handoff JSON validates and Markdown is rendered from it. The final documentation-only commit SHA is in the terminal response because a commit cannot embed its own hash.

Keep independent review (attempt 12), coordinator full make verify and integration pending. The coordinator should resolve this design gap before declaring either global blocker closed or spending the final review attempt on integration approval. The existing imported portable_set listing issue is an additional out-of-scope concern, not the primary stop reason.

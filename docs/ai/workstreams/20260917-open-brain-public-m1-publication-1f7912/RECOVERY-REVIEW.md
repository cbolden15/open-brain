**CHANGES_REQUIRED**

Candidate: `32b3b030beda9553d5e823e3bd72f79f8401ab4d`, branch `ob-new-user/m1-search`.
Reviewed checkout: `/Users/calebbolden/Projects/oss/open-brain-public-worktrees/m1-search`.
Independent checkpoint-C review, reserved attempt12. Product/coordinator files were read-only; all review fixtures and artifacts are in the assigned private run directory. No dispatch, installation, real Brain access, provider calls, or native build occurred.

Two actionable findings, ordered by impact:

1. **P2 — Reject missing materialized-revision evidence before permitting legacy abandonment.**

   Location: `packages/engine/src/open_brain_engine/engine/managed_recovery.py:297-317` (particularly lines 297–298 and 313–317).

   Trigger: In an otherwise eligible schema-6 legacy standalone materialize request, the selected note's non-null `materialized_revision_id` references a nonexistent revision. The expected and accepted revisions remain valid, and `write_base_sha256` still equals the operation's expected preimage.

   `_preview()` validates only the expected and accepted revisions. The equal-write-base branch accepts the transition without validating the materialized revision. The current-schema opener does not run the upgrade-only foreign-key check. Consequently, inspection advertises eligibility and abandonment commits an audit decision and cancellation despite the missing referenced record. The real CLI returns exit 0 with a successful abandonment response, while `PRAGMA foreign_key_check` still reports the broken reference. This violates the contract's restriction to a target-evidence gap and refusal of missing foreign records; the recovery action consumes the original request in hard-corrupt state. The synthetic note file remained unchanged.

   Reproduction: Create the synthetic legacy fixture, use a standalone SQLite connection with foreign keys disabled to set `managed_notes.materialized_revision_id='revision_missing'`, inspect, and abandon with the returned digest. Private tests `test_missing_materialized_revision_refuses_legacy_abandonment` and `test_cli_refuses_missing_materialized_revision` reproduce the incorrect engine and CLI acceptance. Their safety assertions fail: decision count is 1 instead of 0; CLI exit is 0 instead of 78.

   Fix guidance: Validate the selected note's non-null materialized revision reference and its retained digest relationship before declaring eligibility, including the equal-preimage branch. Apply the same validation during transactional revalidation so schema-5 and schema-6 recovery reject this corruption without a decision or cancellation. Add this case to hard-corruption coverage.

2. **P2 — Preserve committed-upgrade reporting when store construction fails after migration.**

   Location: `packages/engine/src/open_brain_engine/engine/managed_recovery.py:565-566`, with failure projection at line 619. Supporting boundary: `packages/engine/src/open_brain_engine/storage/sqlite.py:226-244`.

   Trigger: A valid schema-5 abandonment passes preflight. `_LocalStore(...)` commits migration 6 during its guarded opener, but a subsequent storage-opening step fails, such as `_restrict_existing_file()` raising `OSError` after the migration's prepare callback.

   The `upgraded` flag is assigned only after the constructor returns. The post-commit exception therefore produces `ManagedRecoveryFailure(..., schema_upgraded=False)`, which the app forwards unchanged. In the real CLI reproduction the command exits 78 with `schema_upgraded: false`, although direct inspection confirms `PRAGMA user_version=6`; no decision was inserted and the original operation remains prepared. This incorrectly tells the operator that no upgrade committed even though schema-5 runtimes will now refuse the database. Existing post-migration validation/cleanup tests exercise later boundaries and miss this constructor interval.

   Reproduction: Start with the synthetic schema-5 legacy fixture and inject an `OSError` in `storage.sqlite._restrict_existing_file` only during abandonment. Private tests `test_post_migration_storage_failure_reports_committed_upgrade` and `test_cli_reports_upgrade_on_late_failure[storage_after_prepare]` both confirm durable version 6 and fail the expected `schema_upgraded is True` assertion. A separate post-migration root-revalidation control passes, isolating the unhandled interval.

   Fix guidance: Determine whether migration committed independently of successful store construction. Preserve that fact through opener failures, using guarded durable-state inspection under the writer lease where appropriate, and propagate it to the CLI. Cover failure after migration commit but before `open_local_database()` returns; do not merely set the flag before an upgrade that could still roll back.

**Checks and evidence**

Review-only reproductions are retained in `test_review_repros.py` beside this report. Across the two invocations, four safety assertions failed, confirming the two findings; one late-root-validation control passed. They use only repository synthetic fixture helpers and bounded fault injection. To reproduce all five cases from the candidate checkout:

```sh
RUN=/Users/calebbolden/.codex/workflow-runs/open-brain-wave1-20260917T055433Z-05fb82/checkpoint-c-review-be10e0
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider -q --tb=short \
  --basetemp="$RUN/recheck-tmp" "$RUN/test_review_repros.py"
```

Existing targeted checks used the same bytecode/cache restrictions and private `--basetemp` directories:

- **13 passed (3.79s):** all four observation process-exit boundaries; atomic path swap with multiple pending leaves and unaffected sibling; all three abandonment process-exit boundaries; historical audit replay after owner edit; schema-5/6 stale-preview refusal; peer death after journal cleanup; accepted edit retaining restricted source membership.
- **11 passed (1.83s):** populated frozen schema-5 upgrade/current reopen; interrupted migration; schema-6 old-runtime and invalid/newer refusal; real CLI live-peer refusal; read-only schema-5 preview followed by upgrade; journal-only reserved/dispatching inference cleanup; detached Portable-v2 export/import/reattachment; retained public/job privacy provider-callback gate variants.
- `git diff --check a67f65d6c9931843df89edc2e9ce46b5b67726c8 32b3b030beda9553d5e823e3bd72f79f8401ab4d` passed. HEAD remained the candidate and `git status --porcelain=v1` was empty.

Source review covered the contract and C scope, A/B-to-candidate recovery/schema/app diffs, the retained-source correction since `0228b899526324d42f3faacc1f6bc40d2c5eeb47`, and directly relevant callers/tests. Checked versioned authority creation/validation, transaction ownership, legacy classification, preview digests, audit replay, session admission and stale-marker handling, inference cleanup, Portable authority omission/fresh reattachment bindings, and operator instructions. Migrations 1–5 and their checksum values are unchanged. Desktop connection and native proof both call the exact schema-6/session-1 validator.

No additional concrete defects were found in those reviewed paths. This does not promote prior implementation reports into independent evidence.

**Unverified areas and handoff**

Per assignment, no full `make verify`, aggregate heavy suite, Rust/frontend suite, native build/proof, packaging audit, or Homebrew smoke was run. Desktop compatibility was reviewed in source; executable native validation remains the parent's responsibility. The four separately generated old-code fixtures were not regenerated or rerun; the targeted migration checks above used the checked-in frozen schema-5 fixture. No real-provider or real-data behavior was tested. Parent owns independent confirmation, corrections, and final verification. This review is complete; integration is not approved by this verdict.

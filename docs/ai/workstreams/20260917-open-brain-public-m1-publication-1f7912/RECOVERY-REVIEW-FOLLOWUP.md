**PASS** — candidate `8176a195cfbdb17ab86056e6f3ed78ed7e446744`.

This follow-up covers only the four-file correction delta from `32b3b030beda9553d5e823e3bd72f79f8401ab4d`. It complements the original review. Both original P2 findings are resolved. **No remaining or new actionable findings were found in this delta.**

**R1 — Resolved: hard-corrupt retained evidence is refused before mutation.**

Locations: `packages/engine/src/open_brain_engine/engine/managed_recovery.py:148-154` and `:303-311`.

The recovery classifier now checks foreign-key integrity, and selected-note validation resolves the materialized revision, validates its body digest, and compares that digest with the note's recorded materialized digest. The same checks run in preflight and transactional revalidation.

Independent adapted reproductions obtained a valid digest before injecting corruption, then required refusal rather than the old buggy eligibility. For both schema 5 and schema 6, missing materialized revisions, mismatched materialized digests, and missing parent revisions now cause `operation_replay_mismatch` during preview and apply. Complete SQLite dumps and schema versions remain unchanged; no audit, cancellation, or upgrade occurs, and synthetic note bytes remain intact. Real CLI preview/apply checks for both schemas also return exit 78 with the bounded mismatch code and `schema_upgraded: false`, without database mutation.

**R2 — Resolved: committed upgrades survive later opener failures.**

Locations: `packages/engine/src/open_brain_engine/engine/local_schema.py:374-377`, `:403-421`, and `packages/engine/src/open_brain_engine/engine/managed_recovery.py:620-621`.

The local schema boundary records a successful upgrade immediately after SQLite COMMIT, before busy-timeout restoration and remaining storage setup. Later failures carry `LocalSchemaUpgradeCommittedError`, which recovery translates to `schema_upgraded: true`. Current-schema early returns and fresh initialization do not set the upgrade marker.

The original independent post-prepare permissions repro and real CLI repro now pass unchanged. Additional independent checks confirm:

- Post-prepare permissions failures and immediate post-commit cleanup failures report true for schema-5 upgrades, with durable schema 6, no decision, and the original operation still prepared.
- The same failures on an already-current schema report false.
- An actual SQLite authorization denial of migration COMMIT rolls back to an identical schema-5 dump and reports false. The injector is installed after backfill validation to avoid SQLite's cached initial read-COMMIT bypassing authorization.
- Fresh database creation followed by an opener failure is not mislabeled as an upgrade.
- The new exception remains a `SchemaError`; existing app contention detection still recognizes a wrapped `DatabaseBusyError` through its exception context. The original post-migration root-revalidation control also passes.

**Checks run**

Independent reproductions are retained in `test_followup.py` beside this report. Final run: **18 passed in 1.91s**.

```sh
RUN=/Users/calebbolden/.codex/workflow-runs/open-brain-wave1-20260917T055433Z-05fb82/checkpoint-c-review-followup-4fa219
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider -q --tb=short \
  --basetemp="$RUN/recheck-tmp" "$RUN/test_followup.py"
```

Three targeted existing cases also passed in 0.53s: `test_preview_is_read_only_and_abandonment_is_durable` for schemas 5 and 6, and `test_replay_uses_historical_audit_after_later_owner_edit`. These check that the additional integrity gate preserves valid abandonment and historical replay.

Reviewed all four changed files and relevant exception consumers. The migration catalog and shared storage implementation are unchanged. `git diff --check 32b3b030beda9553d5e823e3bd72f79f8401ab4d 8176a195cfbdb17ab86056e6f3ed78ed7e446744` passed. HEAD matches the new candidate; repository status is clean. No repository/coordinator files were changed.

**Limitations**

This is a correction-only PASS, not a repeat of the original broad review or final release verification. No aggregate suite, full `make verify`, native build, provider call, real-data operation, installation, or dispatch was performed. Parent owns final verification on this exact candidate. The reported parent test/lint results were not substituted for the independent evidence above.

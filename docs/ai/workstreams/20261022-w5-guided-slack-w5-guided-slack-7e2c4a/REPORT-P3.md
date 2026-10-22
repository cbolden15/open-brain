# P3 implementer report

Status: DONE_WITH_CONCERNS. Commits on `1a65720`: `16be97a` policy helper, `f97cb6c` destination/route + halted cancel, `5efdc31` consent/selection/release tests, `b389418` policy/setup tests, `7342cb6` crash tests, `8a28bb1` interactive flow, `4f7f492` edited-member repair.

New: app `services/source_guide_release.py`, `services/scoped_policy_authoring.py`; tests `packages/app/tests/unit/test_scoped_policy_authoring.py`, `packages/app/tests/integration/services/test_source_guide_release.py`, `test_source_guide_release_recovery.py`. Modified: `source_guide_flow.py`, `source_guide.py`, `source_guide_interactive.py`, `test_source_guide_recovery.py`, architecture boundaries.

Verification claimed: `pytest -q -n 2 -p no:cacheprovider packages/collector packages/app/tests/unit packages/app/tests/integration tests/security` 5119 passed, 8 skipped (51m42s); lint, typecheck (806 files), `git diff --check main` clean. Policy helper unit tests were written just after the code, not before.

Halted exit: explicit `cancel` from `halted` is allowed (journal-only, keeps receipts and the unresolved operation, no authority call, blocks further actions); other actions refuse `receipt_unresolved` and point to cancel. P2 test updated.

Self-reported P0 deviations:
1. Existing space counts as dedicated only if empty when chosen; checked with a read-only SQL query because no owner service lists a space's contents.
2. Operation IDs, `decided_at`, space delivery ID generated once per intent and reused on confirmation; create-destination and consent requests also carry `brain_id`, `issuer_epoch`.
3. Release-preview requires completed consent and selection; approve re-reads the preview via `SharingTasks.inspect` and checks expiry/current head.
4. First-attempt typed authority refusal recorded refused; anything else unknown; any refused replay halts; consent file conflicts and foreign state halt immediately.
5. Setup-preview requires confirmation; halt-only after unknown outcome.
6. Journal stays `active` after setup-apply for later member repair.
7. JSON summary `count` = members with a current captured copy; closed shape has no separate completeness field.

Open risks: no cross-journal race test; destination dedication not rechecked after choice; resumed interactive re-pick of an already-approved message creates a second (explicitly approved) copy; policy helper imports private `session_authority._read_policy`; status shows source health only, not release progress.

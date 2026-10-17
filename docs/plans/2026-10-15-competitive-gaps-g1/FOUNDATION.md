# Isolated lifecycle journal foundation

Worktree: `/Users/calebbolden/Projects/oss/open-brain-public/.worktrees/competitive-gap-reconciliation`. Implementation starts from `9af2303`. This records the isolated milestone at **17/12/12**. The subsequent engine integration uses **18/13/13**, documented in `/Users/calebbolden/Projects/oss/open-brain-public/.worktrees/competitive-gap-reconciliation/docs/portable-brain-v13.md`.

## Implemented component

The engine now has immutable internal lifecycle request and group/segment bindings, an append-only SQLite evidence journal, and a validating snapshot/replay path. These are internal components, not public importer or undo commands.

The journal reserves an exact request before staging, verifies body/envelope custody hashes, and publishes complete group membership in one caller-owned transaction. Its additional eligibility predicate excludes pending segments from both current and historical reads. A committed replacement preserves old admitted history. Capture/session retraction excludes selected evidence and retains future-import barriers without retiring an unrelated newer group revision. A stale session membership snapshot refuses atomically.

Exact completed replay returns the retained receipt without appending membership or restoring eligibility. Explicit halt preserves prior completed groups; a newly approved operation can retry an abandoned revision with fresh capture identities. Old pending captures remain hidden and cannot retract the new admitted group. A changed issuer cannot progress old unfinished work, but can explicitly halt it under the caller's current admission.

Evidence export refuses unfinished operations. Restore validates canonical encoding, the SQL-schema digest, content hashes, event-chain order, request binding and valid state transitions in a disposable database before writing an empty destination. Terminal halted custody is retained without becoming admitted history. Restoring and exporting again preserves exact component evidence bytes and retraction state.

## Persistence boundary

The four internal tables are `lifecycle_journal_identity`, `lifecycle_journal_requests`, `lifecycle_journal_payloads` and `lifecycle_journal_events`. Updates/deletes are refused by SQL triggers. The journal checks the exact schema shape; reads and writes require an explicit caller transaction. Install refuses any existing schema objects or nonzero SQLite version/application metadata. It cannot be installed over an existing Brain database.

The DDL digest and canonical request/manifest fixtures freeze this **isolated journal component**. They are not migration18's final SQL checksum or the full Portable13 catalog. `export_evidence` is a component snapshot, not a Brain archive. It neither exports nor activates provider profiles, embedding consent, runtime grants or model state.

Canonical component JSON is limited to16MiB and depth64; group manifests have at most128 distinct segments. These are internal evidence bounds, not the planned64MiB normalized importer acceptance. Payload admission, resource quotas, complete provider normalization and production-size behavior remain integration work. A generic canonical request payload supplies a binding, not semantic approval or authorization.

State projections are rebuilt from immutable evidence on journal reads. This provides a small reference implementation for synthetic replay/restore verification; production integration still needs the reviewed indexed projections and admission fences. Callers must not treat `permits_capture` as source existence, privacy or capability authorization. It is an additional staging/retraction condition to compose with ordinary engine policy.

## Verification

Focused command, from the worktree above:

```sh
UV_PROJECT_ENVIRONMENT=/tmp/open-brain-lifecycle-15036c-venv uv run --frozen pytest -q packages/engine/tests/test_capture_lifecycle_contracts.py packages/engine/tests/test_capture_lifecycle_journal.py
```

The development environment is disposable and not needed by installed users. The focused tests cover canonical golden fixtures, malformed input and alias mutation, exact/changed replay, caught-error rollback, commit/reopen and process-close recovery, complete group activation, session append/retraction races, newer-head preservation, halt/retry, issuer changes and corrupted or impossible restore evidence. Independent review corrections have dedicated regressions.

Full `make verify` results and independent review are recorded in the workstream at `/Users/calebbolden/Projects/oss/open-brain-public/.worktrees/competitive-gap-reconciliation/docs/ai/workstreams/20261015-open-brain-public-lifecycle-journal-foundation-15036c/`. No installed-client, model, private-source or two-platform acceptance is implied by these checks.

## Next integration slice

Connect this evidence authority to reviewed concrete schema18 projections, complete command-level intent admission and source materialization/activation under engine leases. Update schema/runtime recognition and the Portable13 catalog/restore together; prove old-reader refusal and staging exclusion across every read/egress surface before exposing commands. Then add the owner preview/commit/status/recover/halt interfaces. Both official export profiles and actual-model acceptance remain separate gates.

# M1 explicit vault refresh repair

STATUS: DONE_WITH_CONCERNS

Base: `33b38aa9305618b5db0b6d185791a11c21bc9840`. Verified implementation SHA: `8601e8dfbb0c0a990471a374d9f7d48568b662d5`.
Branch: `ob-new-user/m1-search`. Assignment evidence was read first and coordinator HEAD was verified as `7a2745f602186aaed049a393c338da7e7996809e`.
The final return supplies the exact final HEAD after the report-only commit; a committed document cannot contain its own commit hash. HANDOFF head pins the implementation, not a future reporting commit.

The initial branch/HEAD matched the assignment and the tree was clean. Work stayed in the assigned Mac worktree with synthetic fixtures and its installed `.venv`. Attempt 7 remains the shared attempt; no nested agents, model calls, network access, installations or live Brain data were used.

## Result and authority

Explicit refresh now appends a parent-linked canonical projection for eligible existing notes, retaining the note ID, registered/observed renamed path and previous revisions. Full canonical bytes retain ordered publication membership. Representative-capture privacy and provenance are copied as they exist today; no aggregate metadata is invented. Ordinary unchanged refresh records only its request, without another note revision or file write. A representative metadata-only change records an identical-body revision and advances materialized state without replacing the file. Changing raw source content alone does not publish a page. For reviewed pages, refresh checks the canonical payload against the durable publication digest.

Canonical lineage requires a matching local setup operation or a materialization demonstrably bound to a refresh manifest. Accepted owner edits, links and workspace-selected merges remain the accepted head on divergence. The upstream publication is retained as a branch and both versions are available for explicit conflict review. If an accepted link is not yet materialized, its accepted bytes are preserved as the workspace choice and the original disk preimage remains guarded. Conflict resolution changes state; a differing body still requires separate explicit materialization. Unaccepted disk edits remain untouched. Existing open conflicts are not advanced underneath a resolution. Missing files are skipped, and inactive or excluded existing notes are retained. Ambiguous imported lineage fails closed because Portable import does not import local write-operation authority.

Refresh stores its original child-operation IDs in the existing `refresh` operation's digest-protected body, using existing schema fields and revision kinds. Parent manifest, child operations and revisions commit together under the writer lease and one database transaction. Canonical inputs are snapshotted and reread before preparation commits. Replays process/report only the bound children; later publications require a fresh ID. The parent completes only after children settle. Recovery dispatches refresh manifests and leaf operations, preserving original authorization across reopen. Old materialized fields remain intact until the guarded write and promotion complete.

Before any physical write, the materializer checks accepted revision/body/digest, current registered path, caller, activity/conflict state, pending operation status, expected write base, and refresh-specific exclusions. Existing confined path/root checks, collision checks, expected-preimage replacement and final-byte verification remain. Unrelated explicit materialization keeps its existing exclusion semantics.

Quick capture now invokes capture and workspace status, then the existing graph refresh callback only for the matching vault. It cannot incidentally invoke workspace refresh. The existing success notice and catch/error handler remain in `main.ts`. The optional 16-line capture seam exists solely to test real invoked bridge operations without loading the Obsidian runtime. Only the existing CLI refresh paragraph was updated.

## Changed files

- `packages/engine/src/open_brain_engine/engine/managed_workspace.py`
- `packages/app/tests/integration/engine/test_managed_workspace.py`
- `packages/app/tests/unit/test_m1_bridge_search.py`
- `packages/obsidian-plugin/src/main.ts`
- `packages/obsidian-plugin/src/capture.ts`
- `packages/obsidian-plugin/tests/capture.test.ts`
- `docs/cli.md`
- This generated directory: `STATE.md`, `HANDOFF.md`, `HANDOFF.json`, `REPORT.md`.

No schemas, DTOs, Portable formats, filesystem primitives, reconciliation/search implementation, dependencies, lockfiles, master coverage or central gotcha files changed.

## Behavior-to-test evidence

All names below are in `packages/app/tests/integration/engine/test_managed_workspace.py` unless another path is given.

| Requirement | Passing evidence |
| --- | --- |
| Reproduced revision-2 failure, complete ordered membership, public privacy, representative metadata, old revision and parent links | `test_multi_source_bridge_and_desktop_service_refresh_preserve_provenance` in `packages/app/tests/unit/test_m1_bridge_search.py`; original revision-2 and canary assertions preserved, ancestry/metadata assertions added |
| Existing update, stable ID and observed rename, digest/head consistency, observations, unchanged/fresh vs reused IDs | `test_refresh_advances_renamed_note_once_and_binds_replay_to_original_publication` |
| New canonical notes still created, raw quick capture not published | Existing `test_explicit_refresh_adds_only_new_canonical_pages` |
| Preparation/write/promotion interruption, same session and reopen, no duplicate revision, later publication cannot reuse authority, old materialized state retained | Six cases of `test_refresh_interruption_recovers_original_children_exactly_once` |
| Accepted/unaccepted owner edits, unchanged upstream, open conflicts, workspace-selected merge, both versions and accepted owner head retained | Two cases of `test_refresh_retains_owner_edits_and_open_conflicts_until_explicit_resolution` |
| Accepted links, both already and not yet materialized, both explicit resolution choices, no implicit body write, reopen | Four cases of `test_refresh_preserves_accepted_links_and_resolves_without_implicit_write` |
| Missing/inactive/note-excluded/folder-excluded existing notes | Four cases of `test_refresh_preserves_ineligible_existing_notes` |
| Imported lineage fails closed | `test_refresh_fails_closed_without_imported_operation_lineage` and actual export/import in `test_refreshed_and_resolved_revisions_remain_portable_and_import_fails_closed` |
| Raw capture content is not publication; metadata-only revision does not replace/rewrite file | `test_refresh_metadata_only_uses_representative_capture_without_rewriting_body` (bytes, inode and modification time checked) |
| Stale revision/path, inactive/excluded pending note, preimage, replaced root and symlink | Seven cases of `test_pending_refresh_revalidates_state_before_any_write` |
| Real owner edit acceptance after preparation invalidates original write | `test_owner_acceptance_after_preparation_invalidates_original_write` |
| Mutation immediately before guarded replacement is preserved | `test_refresh_guard_catches_mutation_immediately_before_replacement` |
| Existing target collision, original conflict reported on replay | `test_refresh_new_note_collision_preserves_owner_file_and_reports_bound_conflict` |
| Canonical input mutation after snapshot rolls back all prepared revisions/operations | `test_refresh_revalidates_canonical_snapshot_and_rolls_back_preparation` |
| Conflict in one child allows other originally bound children to finish; replay after resolution reports original outcome | `test_refresh_conflict_does_not_prevent_other_bound_children_from_completing` |
| Unchanged storage primitive's residual external-editor race | `test_existing_check_to_rename_external_editor_race_remains_a_documented_limit` deliberately demonstrates the limitation |
| Capture invokes only capture/status bridge operations, appropriate graph callback, absent/other vault, capture/status/graph errors | Five new cases in `packages/obsidian-plugin/tests/capture.test.ts` |
| Existing export/reactivation, inference, CLI/MCP, bridge, reconciliation and public search behavior | Full assigned scoped Python suite below |

## Exact checks and results

Run sequentially with installed dependencies; no installs or synchronization. The final Python rerun followed the last engine/test edits. Plugin source was unchanged after its successful checks.

```sh
.venv/bin/python -m pytest -q packages/app/tests/unit/test_m1_bridge_search.py packages/app/tests/integration/engine/test_managed_workspace.py packages/app/tests/integration/engine/test_managed_portability.py packages/app/tests/integration/engine/test_managed_inference.py packages/app/tests/integration/services/test_managed_cli_mcp.py packages/app/tests/unit/test_plugin_bridge.py packages/app/tests/integration/engine/test_reconciliation.py packages/app/tests/integration/engine/test_review_search.py packages/app/tests/integration/engine/test_m1_source_resolver.py packages/app/tests/integration/services/test_m1_public_search.py
```

PASS: **103 passed**, final run 18.79 seconds, exit 0. Intermediate focused runs exposed and corrected test-fixture mistakes (folder exclusion kind and version-aware Portable validator import), a test import typing error and formatting findings; no failure assertion was removed, weakened or marked xfail.

| Exact command | Result |
| --- | --- |
| `.venv/bin/ruff check .` | PASS, exit 0 |
| `.venv/bin/mypy` | PASS, no issues in 297 files |
| `npm --prefix packages/obsidian-plugin test` | PASS, 5 files / 23 tests |
| `npm --prefix packages/obsidian-plugin run typecheck` | PASS, exit 0 |
| `npm --prefix packages/obsidian-plugin run build` | PASS, exit 0; package-local ignored output |
| `actionlint .github/workflows/ci.yml` | PASS, exit 0 |
| `git diff --check` | PASS, exit 0; repeated for final reports |
| `node /Users/calebbolden/.codex/skills/workflow-governance/scripts/validate-handoff.mjs /Users/calebbolden/Projects/oss/open-brain-public-worktrees/m1-search/docs/ai/workstreams/20260917-open-brain-public-vault-refresh-repair-f71da8-reports/m1-refresh/20260917-open-brain-public-vault-refresh-dcd7f6/HANDOFF.json` | PASS, valid true, exit 0; final packet pins the implementation SHA |

The installed `create-workstream.mjs` generated this workstream ID. `render-prompt.mjs handoff` rendered HANDOFF.md from matching JSON values, and `validate-handoff.mjs` checks the packet before committing. Reports contain result summaries, not raw runtime logs or private content.

## Remaining gates, limitations and coordinator gotchas

- **Pending:** independent repair review and coordinator-owned full `make verify` under the one-heavy-job reservation. This return is DONE_WITH_CONCERNS, not product acceptance. Integration and contract freeze remain deferred and unauthorized in this task.
- **Explicitly skipped:** full Python suite, `make verify`, native/Rust builds, native audit/integration, indexing, benchmarks, network models, live-data/provider/GUI testing, model downloads, installs, pushes, PRs, cherry-picks, merges and releases. Package-local plugin tests/typecheck/build were authorized and completed.
- **Residual external-editor race:** the unchanged primitive checks the destination digest, stages/fsyncs replacement bytes, then renames. An uncooperative editor writing after the digest check can still lose that write. The test demonstrates this precisely, while a separate test proves edits before the guarded replacement check are retained. This repair does not claim arbitrary concurrent-editor safety. A stronger guarantee requires a separately scoped storage-primitive task.
- **Lineage gotcha:** accepted edits reset the write base; digest equality alone is insufficient. Portable imports omit local operation records, so imported refresh deliberately fails closed instead of guessing canonical ancestry. Export/import and explicit reattachment remain covered.
- **Conflict gotcha:** a divergent refresh retains the accepted owner head and a parent-linked upstream branch. Existing conflict records can represent this without a schema/DTO change. Resolution validates the branch's bound refresh operation before choosing it; accepted but unmaterialized link bytes are not mislabeled as already materialized.
- **Retry gotcha:** a reused conflict request continues reporting its original conflict outcome even after an explicit resolution. A new ID is necessary for any new publication. Stale path/root/revision authorizations fail closed rather than silently rebinding.
- No known unhandled in-scope verification gate or sandbox blocker. The external-editor race is the expressly accepted scope boundary, and full verification/review remain pending. Coordinator should record these gotchas centrally; the central registry was read-only here.

Next action: independently review the scoped implementation and run the reserved full verification before deciding integration. No push or product acceptance is authorized by this return.

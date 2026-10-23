# W5 P1 re-anchor record

Date: 2026-10-22. Branch `feat/w5-guided-slack-workflow` from `main` at `e865230`.

The P0 packet was written against `377d238` on the W4 worktree. That commit is present in this
repository but is not an ancestor of `e865230`. Every P0 absolute path maps to the same relative
path in this worktree.

## Compatibility coordinates

| Coordinate | P0 baseline | Current `e865230` | Evidence |
|---|---|---|---|
| State schema | 17 | 21 | `packages/engine/src/open_brain_engine/engine/local_schema.py:54` `PHASE1_STATE_SCHEMA_VERSION` |
| Runtime session | 12 | 16 | `packages/app/src/open_brain/services/local_runtime_session.py:37` `RUNTIME_SESSION_VERSION` |
| Portable metadata | 12 | 16 | `packages/app/src/open_brain/services/catalog.py:184` `portable_metadata` |

"No change to schema/runtime/Portable" now means no change to 21/16/16. The guide capability
handshake reports `{"schema":21,"runtime":16,"portable":16}` instead of the P0 example's 17/12/12.

## Anchor table

`git diff 377d238 e865230` reports no change under `packages/collector/src` or
`packages/connectors/src`. All collector and connector anchors in HEALTH.md and IDENTITY.md
resolve to the same lines with unchanged behavior. Only these anchors moved:

| Old anchor (377d238) | Current anchor | Behavior change |
|---|---|---|
| `engine/records.py:115` `RecordProjector.source` | `engine/records.py:116` | Yes, narrowly: `capture_visible(..., history=history)` now lets an owner history read see retained revisions. Non-history reads (`history=False`, the IDENTITY path) are unchanged. |
| `engine/records.py:177` source summary dict | `engine/records.py:183` | No. Same keys: `record_id`, `revision_id`, `source_id`, `record_type`. |
| `engine/records.py:464` `RecordTasks.read` | `engine/records.py:524` | No. Same signature `read(record_id, *, expected, history)`. |
| `engine/sources.py:311` `SourceTasks.route` | `engine/sources.py:317` | No for route. `_submit_revision_locked` now refuses reserved consumer namespaces; Slack observation namespaces are not reserved. |
| `engine/sharing_contracts.py:101,166,190` | unchanged | No. |

Verified unchanged (line and behavior): `live_capture.py:62,146,178,271,408,477,498,551,729,851,924`;
`custody.py:93,141,180,456,478`; `live_manager.py:105,322,466,529`; `capture_health.py:113,117,209,320`;
`live_storage.py:29,36,81,105,161`; `source_host.py:16`; `slack_auth.py:59,163,345`;
`source_registry.py:166`; `slack_live.py:174,545,806`. Every existing test file named in
ACCEPTANCE.md still exists.

## STOP-condition check

P1 needs none of the stop triggers:

- No new engine, Brain or Portable persisted format. The metadata reader only reads existing files.
- No change to account, custody or source formats. Existing validators (`LiveCaptureService._load`,
  `LiveCaptureService._pending`, `CustodyStore._load`, `slack_auth._metadata_account`) are reused
  against preloaded bytes instead of being extracted, so existing callers are untouched.
- No stronger fencing, no new distribution.
- No optional-collector import in the core. The app reaches the collector only as a child process.

## P1 decisions recorded during re-anchoring

- The P0 journal is P2 scope. Without a journal, `source guide status` has no selected source and
  returns `state_uninitialized` (closed code) without creating anything. The HealthV1 renderer and
  the metadata child call are exercised directly in P1 tests.
- `--guide-guard-fd` is added to the collector parser so the advertised `inherited-flock-v1`
  capability is real. Legacy callers that omit it are unchanged. The guard file itself belongs to
  the P2 journal directory.
- Account health reports registration only and never compares `credential_ref` (HEALTH.md). The
  existing `_metadata_account` validator still requires that key to be a string; the dropped
  namespace-equality check is a credential concern, re-proven by `SlackAuth` on any real use.
- No explicit canonical-path alias allowlist exists in current source. The reader follows the
  existing `PrivateJsonStore` traversal (`O_NOFOLLOW` from the filesystem anchor), so a symlinked
  ancestor such as macOS `/var` refuses as `unsafe_path`, matching existing collector behavior.

# Priority capture integration audit

Date: 2026-09-16. Reviewed candidate: `9f22849f448a8541965356e9b7e050e69495300e`.
Plan: [Complete the four priority capture sources](../plans/2026-09-16-priority-capture.md).

## Verdict

**READY for integration.** Independent review found no reproducible P0, P1 or P2 product-code
defects. Gmail, Google Drive, Claude Code and Codex passed their bounded live acceptance checks.
The Google grants intentionally revoked during acceptance were restored, then verified with
fresh-process reads and real refresh requests. This audit records local integration readiness;
it does not claim a public release, Mac mini deployment or public OAuth approval.

No application code changed during this final review. Candidate `9f22849` has the same application
code as `11d0fe4d4d3494fbb2029507006d1066051e02f0`, which passed the full project verification.
Google provider code is unchanged since `501876108648dd205960c0142818ee5181c108a8`.

## Requirement coverage

| Requirement | Status | Evidence |
|---|---|---|
| Separate Google read grants, OS credential storage, bounded OAuth/HTTP | COMPLETE | `google_sources_auth.py`, `live_storage.py`, `live_oauth.py`, `live_http.py`; auth contracts and real refresh/revocation checks |
| Selection and preview bound to one Brain and source | COMPLETE | `live_capture.py` generation/preimage validation; changed-selection and cross-Brain rejection tests |
| One implementation for CLI, desktop and scheduler | COMPLETE | `live_manager.py` dispatcher, `sources_cli.py`, desktop `runtime.rs` and `collector.rs`; native bridge and source-panel tests |
| Durable scheduling, acknowledged checkpoints, pause/resume and recovery | COMPLETE | `live_capture.py` and `runner.py`; crash/replay, stale-preview and pause-race tests; actual launchd receipts |
| Bounded Gmail label/date capture and history recovery | COMPLETE | `google_sources.py`; full/history-recovery contracts and real selected-label removal/restoration |
| Selected Drive revisions and availability changes | COMPLETE | `google_sources.py`; live body edit/trash/restoration, permission-error contracts and repeated loss-cycle probe |
| Selected-project agent hooks and separate capture opt-ins | COMPLETE | `agent_session_hooks.py` and `agent_session_live.py`; preview/preimage, filtering, quarantine and opt-out tests; both native clients |
| One current search result with historical export and provenance | COMPLETE | Capture revision identity and deduplication tests; seven Google transitions and two revisions per native client in verified exports |
| Public Google registration and production authorization acceptance | PARTIAL, release gate | Development-client consent passed; public verification and long-duration authorization are not established |

No in-scope implementation requirement is MISSING or DEVIATED. Slack and other unfinished
integrations remain deferred. Previously excluded Microsoft sources remain excluded.

## Live source matrix

Receipt names below identify private evidence retained by the coordinator. Provider contents,
account identities, exact local paths, credentials and native transcripts are intentionally absent
from this repository. Each source used a disposable Brain and bounded owner-authorized resources.

| Source | Candidate | Result | Private receipt |
|---|---|---|---|
| Gmail | `5018761` lifecycle; `9f22849` final edges | PASS: import/search, retry/replay, background pause/restart/resume, selected-label removal/restoration, grant revocation and equivalent reconnection; three transitions preserved in export | `google-live-acceptance-959650a7e4/receipt.json`; `google-edge-acceptance-368b3ccc60/receipt.json` |
| Google Drive | `5018761` lifecycle; `9f22849` final edges | PASS: import/search, retry/replay, background pause/restart/resume, upstream body edit, trash/restoration, grant revocation and equivalent reconnection; four transitions preserved in export | Same two Google receipts |
| Claude Code 2.1.273 | `11d0fe4`, documentation at `9f22849` | PASS: native fresh/resumed turns, automatic capture/search, failure retry/replay, actual background pause/restart/resume, replacement of current result, post-opt-out uncaptured turn, two-revision export | `claude-live-acceptance-6f1d0e3334/receipt.json` |
| Codex CLI 0.154.0 | `11d0fe4` verified candidate and source hashes | PASS: native fresh/resumed turns, automatic capture/search, failure retry/replay, actual background pause/restart/resume, replacement of current result, post-opt-out uncaptured turn, two-revision export | `codex-live-acceptance-4f6a3c0b2a/receipt.json` |

The live harnesses exercised `open-brain-collector sources` operations `connect`, `resources`,
`configure`, `preview`, `import`, `control`, `status`, `background-enable` and `background-disable`,
as applicable to each receipt. Fresh clients used `open-brain search`; exports used
`open-brain export <destination> --verify --json --data-dir <disposable-brain>`. Exact invocations
with private selections are retained alongside the receipts. Revocation also exercised the real
scheduled `LiveCaptureService.sync_due()` path: both sources returned `google_auth_required`,
and capture count and checkpoints stayed unchanged. No background service was installed for that
edge check; earlier receipts establish actual service lifecycle behavior.

Cleanup removed test hooks, services and Keychain entries. Test sources are disabled. The owner's
two Google connections remain restored, with zero configured collection sources. The synthetic
Drive document is in recoverable Trash; an unsent synthetic Gmail draft remains in its dedicated
test label. Acceptance did not enable normal project or mailbox collection.

## Verification

The final coordinator check at `9f22849` ran:

```sh
.venv/bin/python -m pytest \
  packages/connectors/tests/contract/test_google_sources_auth.py \
  packages/connectors/tests/contract/test_google_sources.py \
  packages/collector/tests/integration/test_live_capture.py \
  packages/collector/tests/integration/test_live_manager.py \
  -q -p no:cacheprovider
```

Result: **28 passed in 4.07 seconds**.

Independent reviewer checks on that candidate:

| Command | Result |
|---|---|
| `uv run pytest -q packages/connectors/tests/contract/test_agent_session_hooks.py packages/connectors/tests/contract/test_agent_session_live.py` | 23 passed |
| `npm test -- --run src/SourceCapturePanel.test.tsx` in `packages/desktop` | 4 passed |
| `cargo test --manifest-path packages/desktop/src-tauri/Cargo.toml collector::tests --quiet` | 3 passed, 14 filtered |
| Real-engine probe: available r1, missing, restored unchanged r1, missing again | All four transitions captured; only the current state was searchable |

The repeated-loss probe refuted a suspected reused-revision defect. It did not require a code fix.
The independent review report is retained as `priority_final_integration-report.md` with source
locations and requirement-by-requirement reasoning.

Prior `make verify` at `11d0fe4`: **1510 Python passed, 5 platform skips; 18 plugin, 13 desktop,
1 wrapper and 17 Rust tests passed**. Ruff and mypy passed. The final documentation checkpoint
passed `git diff --check` and `actionlint .github/workflows/ci.yml`. The full suite was not
repeated for documentation-only changes.

## Evidence limits and next gates

1. Public Google registration, required scope verification and long-duration authorization remain
   release work. Development-client setup is accurately labeled as such.
2. Live provider acceptance used the shared manager through CLI/collector control. Native desktop
   UI and bridge checks passed separately; an exact-candidate desktop real-account connection/import
   receipt remains a separate release check if required.
3. Live Drive unavailable-state proof covers trash/restoration and account-grant revocation.
   Per-file 403 permission loss is covered by automated tests, not a live ACL change.
4. Native client acceptance covers the named versions with transcript capture enabled. Extractive
   summaries, malformed records and secret quarantine have automated coverage; future client-format
   changes require renewed native acceptance.
5. Mac mini branch integration and deployment remain separate work. This audit does not enable
   recurring collection for the owner's normal resources or projects.

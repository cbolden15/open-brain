# W5 P1 brief: read-only foundation

Repo root (worktree): `/Users/calebbolden/Projects/oss/open-brain-public/.worktrees/w5-guided-slack`
Branch: `feat/w5-guided-slack-workflow` (from `main` at `e865230`). Never push, never switch branches, never touch another worktree.

## Sources of truth (read in this order)

1. `CLAUDE.md` in the repo root and `docs/engineering/gotchas/README.md`.
2. `docs/plans/2026-10-15-guided-slack-workflow-w5.md` (sections 6 and 7, P1 row).
3. P0 packet `docs/plans/2026-10-15-guided-slack-workflow-w5-p0/`: `HEALTH.md`, `INTERFACE.md`, `IDENTITY.md`, `ACCEPTANCE.md`. `RECOVERY.md` is P2/P3 context only.

The P0 packet was written against an older worktree and baseline (`377d238`, schema/runtime/Portable 17/12/12). Its absolute paths point at `/Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w4-20261015-6d407b`; map every one to the same relative path in this worktree. Current `main` is at schema/runtime/Portable 21/16/16 (owner retraction, consumer intake, local embedding landed since).

## Step 0: re-anchor P0 (do first, before any code)

Verify every file/symbol/line anchor that P1 depends on still exists on current source. Write `docs/plans/2026-10-15-guided-slack-workflow-w5-p0/REANCHOR-P1.md`: one table row per moved/changed anchor (old anchor, current anchor, behavior change yes/no) and the current compatibility coordinates. "No change to schema/runtime/Portable" now means no change to 21/16/16.

STOP and report (do not implement) if re-anchoring shows P1 needs any of: a new engine/Brain/Portable persisted format, a change to existing account/custody/source formats, stronger fencing, a new distribution, or an optional-collector import into the core.

## Deliverable (P1 only)

Per the W5 plan P1 row and the P0 packet:

1. Collector-owned non-bootstrapping metadata reader `read_source_metadata(...) -> HealthV1` in `packages/collector/src/open_brain_collector/source_metadata.py`, plus the early `guide-metadata` collector CLI dispatch exactly as frozen in INTERFACE.md (before manager construction, control IPC or engine bootstrap).
2. App-side optional collector adapter: finite argv allowlist, executable capability/trust handshake, bounded stdout/stderr framing, deadlines, strict JSON, no optional imports in the core closure.
3. Resource inventory read (bounded pages, returned rows only; stored aliases never identity) and exact captured-record resolution per IDENTITY.md.
4. `open-brain source guide status COMMON [--json]` wired in `local_entrypoints` before root selection/bootstrap, rendering HealthV1 (human + versioned JSON). `slack` and `advance` may be registered to refuse with a clear "not implemented in this build" result; do not implement their write phases.
5. Safe display of untrusted names/bodies per the ACCEPTANCE display row.

Tests (TDD, write failing first), per the ACCEPTANCE evidence map rows for P1:
- NEW `packages/app/tests/unit/test_source_guide_adapter.py`
- NEW `packages/collector/tests/integration/test_source_metadata.py` (zero-mutation snapshot before/after; patch prohibited constructors/HTTP/credentials/bootstrap/locks to raise)
- Health truthfulness fixtures (unknown is null, never synthetic zero/complete)
- NEW `packages/app/tests/unit/test_source_guide_display.py`
- Architecture/boundary inventories that must register new modules (see existing `tests/security/test_architecture_boundaries.py` precedent).

## Write scope

`packages/collector/src/open_brain_collector/source_metadata.py` (new), the collector CLI dispatch file, new app modules under `packages/app/src/open_brain/services/` for the guide adapter/status/display, `local_entrypoints.py` registration, their tests, inventory tests that must list new modules, `docs/plans/2026-10-15-guided-slack-workflow-w5-p0/REANCHOR-P1.md`, and this workstream directory. Do not change existing persisted formats, existing `source` actions/outputs, or engine admission.

## Verification

Focused: the new test files plus any existing suites you touch, then `make lint` and `make typecheck`. Then `uv run --frozen pytest -q -n 2 packages/collector packages/app/tests/unit tests/security`. Do not run native builds. A full `make verify` is the coordinator's gate, not yours, but report if focused runs reveal regressions elsewhere.

## Commit policy

Commit locally on the branch at green checkpoints with conventional messages (`feat(guide): ...`, `test(guide): ...`). Write each message to a file and use `git commit -F <file>`. End each message with:

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

## Stop condition and output contract

Stop when P1 is implemented and focused verification is green, or on a STOP condition above, or after two identical failures. Final report (under 400 words): status (done|blocked), commits (hash + subject), files changed, exact commands run and pass/fail counts, deviations from P0 with reasons, open risks for P2.

## Sensitive data

Synthetic fixtures only. Never read or use a real Brain, real Slack workspace, credentials, `~/.dev-secrets.env`, or private hostnames. Never call WebFetch.

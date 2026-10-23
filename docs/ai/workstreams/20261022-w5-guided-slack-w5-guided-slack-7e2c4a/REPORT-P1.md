# P1 implementer report

Status: DONE_WITH_CONCERNS. Commits `4fec58f` (noncreating collector source metadata reader) and `18ee80c` (optional collector adapter and read-only guide status) on `feat/w5-guided-slack-workflow`, base `main` `e865230`.

Files: `docs/plans/2026-10-15-guided-slack-workflow-w5-p0/REANCHOR-P1.md`; collector `source_metadata.py` (new), `sources_cli.py` (early `guide-metadata`, `--guide-guard-fd`), `cli.py` (`--guide-capabilities-json`); app `services/source_guide.py`, `source_guide_adapter.py`, `source_guide_display.py` (new), `local_entrypoints.py` (pre-parser `source guide` dispatch); tests `packages/collector/tests/integration/test_source_metadata.py`, `packages/app/tests/unit/test_source_guide_{adapter,display,status}.py`, `tests/security/test_architecture_boundaries.py`.

Verification claimed: new files pass; `pytest -q -n 2 packages/collector packages/app/tests/unit tests/security` 3089 passed, 3 skipped; `make lint`, `make typecheck` (794 files), `git diff --check` pass. No native builds.

Self-reported concerns:
1. Status always returns `state_uninitialized` in this build because source selection comes from the P2 journal; an existing journal refuses `journal_incompatible`. Real-collector render path is tested via direct `guide_status(paths, selected_source_id=...)`.
2. Existing validators reused via a read-only stand-in (`LiveCaptureService._load`/`_pending`, preloaded `CustodyStore._load`) rather than extracted, leaving `live_capture.py`/`custody.py` unchanged.
3. Added collector `--guide-guard-fd` (flock on inherited fd) so the advertised `inherited-flock-v1` guard exists; legacy callers unchanged.
4. `slack`/`advance` refuse with `operation_failed` (exit 78); closed code list has no not-implemented code.
5. Handshake advertises 21/16/16; collector hardcodes runtime/Portable 16 because constants live in the app; a real-collector parity test catches drift.
6. No path-alias allowlist: symlinked ancestors (e.g. macOS `/var`) refuse `unsafe_path`, same as existing `PrivateJsonStore`.

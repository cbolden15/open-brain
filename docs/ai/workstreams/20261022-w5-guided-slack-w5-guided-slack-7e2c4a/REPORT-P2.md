# P2 implementer report

Status: DONE_WITH_CONCERNS. Commits after `c9bad9c`: `7325703` journal v1 + CAS lock + lifetime guard; `06f8b39` adapter configure/preview/import, `--selection-file`, failure-code mapping, Portable parity test; `f029132` advance coordinator, interactive `slack`, journal-backed status; `5452223` crash/lost-ack/concurrency/guard recovery tests; `65f0d71` whitespace.

New: app `services/source_guide_journal.py`, `source_guide_flow.py`, `source_guide_interactive.py`; tests `packages/app/tests/unit/test_source_guide_journal.py`, `packages/app/tests/integration/services/test_source_guide.py`, `test_source_guide_recovery.py`, `source_guide_support.py`. Modified: `source_guide.py`, `source_guide_adapter.py`, architecture boundaries, adapter/status tests.

Verification claimed: `pytest -q -n 2 -p no:cacheprovider packages/collector packages/app/tests/unit packages/app/tests/integration/services tests/security` 3840 passed, 3 skipped; lint, typecheck (801 files), `git diff --check main` clean.

Not covered: custody quota refusal (256 items) only mapped in a unit test, no integration reaching the real quota.

Self-reported P0 deviations:
1. `choose` re-reads inventory (journal cannot hold inventories): one extra `conversations.list` per choose.
2. `preview` with an existing pending page refuses `inspection_required` and returns its ID; never calls preview to discover it.
3. Halt-only reconcile (configure, preview) records a refused reconcile with no call and no confirmation.
4. Unsupported client context at preflight maps to `unscoped_client`.
5. Collector failure codes mapped to closed guide codes but not stored in journal evidence.
6. Channel names may contain C1/bidi (escaped on display); collector rejects C0.
7. Lock/guard files are `<journal>.lock` / `<journal>.guard`; `collector_binding` is SHA-256 over Brain, state and live paths; `start` creates revision 1.

Carry-forwards: run_cli status end-to-end done; Portable parity done; macOS alias allowlist exactly `/var`,`/tmp`,`/etc` -> `/private/*` done; validator extraction not done (P2 did not touch `_load`/`_pending`).

Open risks for P3: pre-spawn crash covered only by SIGKILL test (supervisor PID recorded before spawn); settling a stale executing marker writes a new revision so callers must re-read before reconcile; each `needs_confirmation` adds a prepared operation, consuming the 1000-op / 256 KiB journal bounds in long sessions.

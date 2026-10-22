# W5 P2 brief: foreground capture guide and journal

Repo root (worktree): `/Users/calebbolden/Projects/oss/open-brain-public/.worktrees/w5-guided-slack`
Branch: `feat/w5-guided-slack-workflow`. P1 is committed on it (see `git log main..HEAD`). Never push, never switch branches, never touch another worktree.

## Sources of truth

1. Repo `CLAUDE.md`, `docs/engineering/gotchas/README.md`.
2. `docs/plans/2026-10-15-guided-slack-workflow-w5.md` sections 6–7 (P2 row, "Resume and bounded execution", "Owner checkpoints").
3. P0 packet `docs/plans/2026-10-15-guided-slack-workflow-w5-p0/`: `RECOVERY.md` (transition table + Journal v1), `INTERFACE.md`, `IDENTITY.md`, `ACCEPTANCE.md`, plus `REANCHOR-P1.md`. Old absolute paths in P0 map to the same relative path here. Current coordinates are schema/runtime/Portable 21/16/16 and must not change.
4. The P1 code you build on: `packages/app/src/open_brain/services/source_guide*.py`, collector `source_metadata.py`, `sources_cli.py`.

## Deliverable (P2 only)

Implement these RECOVERY.md transitions and nothing from P3: start (journal create), preflight / accounts / resources / choose / inspect / acknowledge-inspection (journal-only), configure, preview, import, reconcile (for these transitions only), cancel. Specifically:

1. App-local workflow journal v1 exactly as RECOVERY.md "Journal v1" freezes it: private 0700/0600, owned regular files, atomic writes, symlink rejection, 256 KiB bound, exact canonical request preimages and digests persisted before each authority call, no message bodies/credentials/tokens. Journal is not a second truth store.
2. Adapter allowlist extended to the configure, preview and import operations, with the app-side `--selection-file` locator (the collector side already has `_guide_selection_file`).
3. `open-brain source guide slack` (interactive, TTY-only, rejects `--json`) and `open-brain source guide advance --decision-file FILE --json` (one named action, never prompts, never advances a second write, never fetches/previews/imports a next page) for the transitions above. Bounds from the W5 plan: one child at a time; ≤5 resource pages per inventory advance; one preview page per preview advance; exactly one inspected, confirmed preview per import advance; ≤5 capture pages per interactive invocation with a distinct import checkpoint each; ≤1000 inventory receipt refs. Hitting a bound returns partial progress plus explicit continuation.
4. Lost-acknowledgement/crash handling per the table: exact-request replay where eligible, HALT-ONLY where the table says so, no new operation ID before reconciling, no compensating writes, cancellation retains completed work.
5. `source guide status` now reads the journal for the selected source and renders a real HealthV1 (P1 always returned `state_uninitialized`).

## Carry-forward requirements from the P1 review (must be done in P2)

- Add an end-to-end test of `source guide status` rendering a real HealthV1 through `run_cli`, not only `guide_status(...)` directly.
- Bind the Portable coordinate (hardcoded 16 in `source_guide_adapter.py:~56` and collector `source_metadata.py:~48`) to an app constant or add a parity test against the app/engine constant, so a Portable bump is caught.
- Path aliases: either add an explicit allowlist for macOS `/var`, `/tmp`, `/etc` → `/private/*` (only those exact OS aliases, never arbitrary user symlinks) or document the `unsafe_path` refusal for explicit `--data-dir`/state paths under those aliases in the operator guide draft. Prefer the allowlist if it stays small; tests use real temp dirs on macOS.
- Prefer extracting pure validators over the P1 duck-typed stand-in for `LiveCaptureService._load`/`_pending` where P2 touches them, without changing existing callers' receipt semantics. If extraction would touch `live_capture.py`/`custody.py` behavior, keep the stand-in and say so.

## Tests (TDD, failing first)

Per the ACCEPTANCE evidence map rows for P2: NEW `packages/app/tests/integration/services/test_source_guide.py` (real collector/intake/engine components, only the Slack HTTP boundary mocked: exact fetch counts, preview/import checkpoints, JSON mode never fetches next page, no scheduling/service enable, 5-page cutoff, quota refusal retained, Work fixed before import, Personal/unknown refused, `source_reset_required` on changed generation/options, names/pagination/rename/malicious strings, duplicate labels) and NEW `packages/app/tests/integration/services/test_source_guide_recovery.py` (crash/lost-ack before authority, after commit before result, after result before journal, for each P2 write; byte-exact replay; altered-intent conflict; two-process same/different journal; supervisor SIGKILL while collector child holds inherited guard incl. spawn-before-PID-record crash; missing/forged/unsafe guard; expired preview). Register new modules in architecture inventories.

## Write scope

App `services/source_guide*.py` and new guide modules under `packages/app/src/open_brain/services/`, `local_entrypoints.py` guide dispatch, collector `sources_cli.py` guide paths and `source_metadata.py`, new pure-validator extraction only if it preserves existing behavior, their tests, architecture inventory tests, P0 packet notes (add `REANCHOR-P2.md` only if new anchor drift is found), and this workstream directory. Do not change existing persisted formats, existing `source` actions/outputs, engine admission, or implement any P3 transition.

STOP and report instead of implementing if P2 requires a new engine/Brain/Portable format, a changed account/custody/source format, stronger fencing, or an optional-collector import into the core.

## Verification

Touched/new test files first, then `uv run --frozen pytest -q -n 2 -p no:cacheprovider packages/collector packages/app/tests/unit packages/app/tests/integration/services tests/security`, `make lint`, `make typecheck`, `git diff --check`. No native builds, no full `make verify` (coordinator gate).

## Commit policy

Local commits on the branch at green checkpoints, conventional messages written to a file and committed with `git commit -F`, ending with:

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

## Stop condition and output contract

Stop when P2 is implemented and focused verification is green, on a STOP condition, or after two identical failures. Final report (under 450 words): status, commits, files changed, exact commands and pass/fail counts, P0 deviations with reasons, carry-forward items done/not done, open risks for P3.

## Sensitive data

Synthetic fixtures only. No real Brain, Slack workspace, credentials, `~/.dev-secrets.env`, or private hostnames. Never call WebFetch. Search with `rg`.

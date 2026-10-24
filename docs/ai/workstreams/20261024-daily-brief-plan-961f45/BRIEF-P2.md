# Daily brief P2 brief: log store, composer, owner CLI, MCP tools and grants (plan Tasks 6–13)

Repo root (worktree): `$WORKTREE`
Branch: `feat/daily-brief` (Package 1, Tasks 1–5, is committed and reviewed ahead of you). Never push, never switch branches, never touch another worktree. Do not `cd` to the main checkout.

## Sources of truth (in order)

1. Repo `CLAUDE.md`, `packages/app/CLAUDE.md`, `docs/engineering/gotchas/README.md` (TESTING-013/014/015/016).
2. Spec `docs/superpowers/specs/2026-10-24-daily-brief-design.md` (revision 2, amended).
3. Plan `docs/superpowers/plans/2026-10-24-daily-brief.md`: Global Constraints, Reconciled interfaces, and **Package 2, Tasks 6–13 only** (Task 14 does not exist). Follow each step exactly, in order, test first. The "Reconciled interfaces" section wins over any sentence elsewhere in the plan that disagrees.
4. The real Package 1 code on the branch: `services/brief_contracts.py`, `services/brief_authority.py`, `engine/source_freshness.py`. Import from them; never redefine their names. `BRIEF_GRANTS` is defined in `brief_contracts.py`; `services/brief.py` re-exports it.

If the plan conflicts with the real code, the real code and spec win: make the smallest change that satisfies the spec, and record the deviation (task, step, what and why) in your report. If a plan step would change a persisted format, the schema, Portable, or the runtime-session coordinate (they stay 23/16/18), extend `owner_grants`, serve a brief from the log on the `brief`/`brain_brief` path, or needs anything outside the write scope, STOP and report.

## Write scope

Exactly the files named in Tasks 6–13: `packages/app/src/open_brain/services/brief_log.py`, `packages/app/src/open_brain/services/brief.py`, the follow-up-style insertion points in `services/local_entrypoints.py`, `services/local_mcp.py`, `services/mcp_protocol.py`, `services/agent_setup.py`, `services/plugin_bridge.py` and any adapter module Task 11 names, `tests/security/test_architecture_boundaries.py` (allow-list entries for `brief_log.py` and `brief.py` only), the catalog test the plan names in Task 10, and the test files the tasks create or extend. Nothing from Task 15 onward; no reference docs, no `CLAUDE.md`, no `docs/daily-brief.md`.

## Verification (yours)

Each task's own test commands as written, then `uv run --frozen pytest -q -n 4 -p no:cacheprovider packages/app/tests/unit tests/security`, `make lint`, `make typecheck`, `git diff --check`. Do not run `make verify`, `make native-audit` or `make homebrew-smoke` in this package.

## Commit policy

Local commits at each plan commit step, conventional messages written to a file and committed with `git commit -F`, ending with:

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>

Leave the tree clean.

## Output contract

Write the full report to `$WORKTREE/.superpowers/sdd/P2-report.md`: status (DONE, DONE_WITH_CONCERNS, NEEDS_CONTEXT or BLOCKED), commits (hash + subject), files changed, exact commands with pass/fail counts, every deviation from the plan, and anything Package 3 must know (the final CLI parser shape, MCP tool names and argument schemas, the grant flag names, the log file layout, what `brief last` prints). Reply with only: status, commit range, one-line test summary, concerns.

## Sensitive data

Synthetic fixtures only. No real Brain, Slack, credentials, provider keys, `~/.dev-secrets.env`, real client config or private hostnames. Never call WebFetch. Search with `rg`.

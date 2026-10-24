# Daily brief P1 brief: contracts, session authority and the engine `source_freshness` task (plan Tasks 1–5)

Repo root (worktree): `/Users/calebbolden/Projects/oss/open-brain-public/.worktrees/daily-brief`
Branch: `feat/daily-brief` (base `f21dfb9`: follow-ups work from #119 plus the daily brief spec and plan). Never push, never switch branches, never touch another worktree. Do not `cd` to the main checkout.

## Sources of truth (in order)

1. Repo `CLAUDE.md`, `packages/app/CLAUDE.md`, `docs/engineering/gotchas/README.md` (TESTING-013/014/015/016).
2. Spec `docs/superpowers/specs/2026-10-24-daily-brief-design.md` (revision 2, amended).
3. Plan `docs/superpowers/plans/2026-10-24-daily-brief.md`: Global Constraints, Reconciled interfaces, and **Package 1, Tasks 1–5 only**. Follow each step exactly, in order, test first. The "Reconciled interfaces" section wins over any sentence elsewhere in the plan that disagrees.

If the plan conflicts with the real code, the real code and spec win: make the smallest change that satisfies the spec, and record the deviation (task, step, what and why) in your report. If a plan step would change a persisted format, the schema, Portable, or the runtime-session coordinate (they stay 23/16/18), or needs anything outside the write scope, STOP and report.

## Write scope

Exactly the files named in Tasks 1–5: `packages/app/src/open_brain/services/brief_contracts.py`, `packages/app/src/open_brain/services/brief_authority.py`, `packages/engine/src/open_brain_engine/engine/source_freshness.py`, the engine wiring and capability registration the plan names in Task 4, `tests/security/test_architecture_boundaries.py` (allow-list entries for the two new service files), and the test files the tasks create. Nothing from Task 6 onward.

## Verification (yours)

Each task's own test commands as written, then `uv run --frozen pytest -q -n 4 -p no:cacheprovider packages/engine packages/app/tests/unit tests/security`, `make lint`, `make typecheck`, `git diff --check`. Do not run `make verify`, `make native-audit` or `make homebrew-smoke` in this package.

## Commit policy

Local commits at each plan commit step, conventional messages written to a file and committed with `git commit -F`, ending with:

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>

Leave the tree clean.

## Output contract

Write the full report to `/Users/calebbolden/Projects/oss/open-brain-public/.worktrees/daily-brief/.superpowers/sdd/P1-report.md`: status (DONE, DONE_WITH_CONCERNS, NEEDS_CONTEXT or BLOCKED), commits (hash + subject), files changed, exact commands with pass/fail counts, every deviation from the plan, and anything Package 2 must know (final signatures of `brief_session_authority`, `visibility_key`, `SourceFreshnessTasks.freshness`, `FamilyFreshness`, `FreshnessReport`, the capability registration). Reply with only: status, commit range, one-line test summary, concerns.

## Sensitive data

Synthetic fixtures only. No real Brain, Slack, credentials, provider keys, `~/.dev-secrets.env`, real client config or private hostnames. Never call WebFetch. Search with `rg`.

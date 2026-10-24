# Daily brief P3 brief: acceptance, documentation and the gate (plan Tasks 15–18)

Repo root (worktree): `$WORKTREE`
Branch: `feat/daily-brief` (Packages 1 and 2, Tasks 1–13, are committed and reviewed ahead of you). Never push, never switch branches, never touch another worktree. Do not `cd` to the main checkout.

## Sources of truth (in order)

1. Repo `CLAUDE.md`, `packages/app/CLAUDE.md`, `docs/engineering/gotchas/README.md` (TESTING-013/014/015/016, BUILD-020).
2. Spec `docs/superpowers/specs/2026-10-24-daily-brief-design.md` (revision 2, amended) and the evaluation `docs/audits/2026-10-14-work-laptop-product-evaluation.md` section 5.
3. Plan `docs/superpowers/plans/2026-10-24-daily-brief.md`: Global Constraints, Reconciled interfaces, and **Package 3, Tasks 15–18 only**. Follow each step exactly, in order, test first. The "Reconciled interfaces" section wins over any sentence elsewhere in the plan that disagrees.
4. The real Package 1 and 2 code on the branch (`services/brief*.py`, `engine/source_freshness.py`, the CLI, MCP and grant wiring) and the P2 report at `.superpowers/sdd/P2-report.md` for the final CLI and MCP shapes. The acceptance tests drive the real surfaces; where the plan's expected strings differ from what the code prints, the code and spec win and you record the deviation.

If the plan conflicts with the real code, the real code and spec win: make the smallest change that satisfies the spec, and record the deviation (task, step, what and why) in your report. If a plan step would change product code beyond what Tasks 15–18 name, weaken a compatibility check, or needs anything outside the write scope, STOP and report. A real defect in Package 1 or 2 code found by the acceptance tests is reported, not fixed here, unless the fix is one line and you record it.

## Write scope

Exactly the files named in Tasks 15–18: `packages/app/tests/integration/services/test_daily_brief_acceptance.py`, `docs/daily-brief.md`, the reference docs, both `CLAUDE.md` files, the documentation gate test, `tests/security/test_architecture_boundaries.py` (verification only; the four brief files should already be present), `docs/engineering/gotchas/README.md` only if Task 18 finds a new gotcha, and `docs/ai/workstreams/20261024-daily-brief-plan-961f45/LEDGER-EXECUTION.md` for the Task 18 gate record.

## Verification (yours)

Each task's own test commands as written. Task 18 is the frozen-tree gate: commit everything first, confirm `git status --short` is empty, then run `make verify`, `make native-audit`, `make homebrew-smoke`, the brief test set under `umask 022` and the acceptance and docs tests under `umask 077` with `--basetemp` under `/private/tmp`, `git diff --check`, `actionlint .github/workflows/ci.yml`. No checkout writes during `make verify` (TESTING-013). If the only failure is a known flake (TESTING-014, or the Google Calendar drip-feed deadline test), rerun that test alone and report both results. Save gate logs under `$SCRATCHPAD/brief-gate/` and name them in the report.

## Commit policy

Local commits at each plan commit step, conventional messages written to a file and committed with `git commit -F`, ending with:

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>

Leave the tree clean. Never push.

## Output contract

Write the full report to `$WORKTREE/.superpowers/sdd/P3-report.md`: status (DONE, DONE_WITH_CONCERNS, NEEDS_CONTEXT or BLOCKED), commits (hash + subject), files changed, exact gate commands with pass/fail counts and log paths, every deviation from the plan, and any Package 1 or 2 defect found. Reply with only: status, commit range, one-line gate summary, concerns.

## Sensitive data

Synthetic fixtures only. No real Brain, Slack, credentials, provider keys, `~/.dev-secrets.env`, real client config or private hostnames. Never call WebFetch. Search with `rg`.

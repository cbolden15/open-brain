# Follow-ups P1 brief: schema 23 and coordinates (plan Tasks 1–4)

Repo root (worktree): `/Users/calebbolden/Projects/oss/open-brain-public/.worktrees/followups`
Branch: `feat/followups` (base: spec + plan commits on top of main `9f50bd0`). Never push, never switch branches, never touch another worktree.

## Sources of truth (in order)

1. Repo `CLAUDE.md`, `packages/app/CLAUDE.md`, `docs/engineering/gotchas/README.md` (TESTING-011/013/014/015).
2. Spec `docs/superpowers/specs/2026-10-23-commitments-open-questions-design.md`.
3. Plan `docs/superpowers/plans/2026-10-23-followups.md`: Global Constraints, File Structure, and **Tasks 1–4 only**. Follow each step exactly, in order, test first.

If the plan conflicts with the real code, the real code and spec win: make the smallest change that satisfies the spec, and record the deviation (task, step, what and why) in your report. If a plan step would weaken a compatibility check, change a persisted format beyond schema 23 / Portable 17-at-schema-23 as the plan describes, or needs anything outside the write scope, STOP and report.

## Write scope

Exactly the files named in Tasks 1–4. Nothing from Task 5 onward. Portable stays 17 in this package (plan Task 4 note on `_expected_schema=23`).

## Verification (yours)

Each task's own test commands as written, then `uv run --frozen pytest -q -n 4 -p no:cacheprovider packages/engine packages/app/tests/unit tests/security`, `make lint`, `make typecheck`, `git diff --check`. Plan Task 4 Part B ends with `make verify`: run it once on a committed, clean tree with no other writes to the checkout during the run (TESTING-013); if the only failures are the two known plugin readiness flakes (TESTING-014) say so with the test names. Do not run `make native-audit` or `make homebrew-smoke`.

## Commit policy

Local commits at each plan commit step, conventional messages written to a file and committed with `git commit -F`, ending with:

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

Leave the tree clean.

## Output contract

Final report under 400 words: status, commits (hash + subject), files changed, exact commands with pass/fail counts, the Task 1 runtime-session decision and evidence, every deviation from the plan, and anything the next package must know.

## Sensitive data

Synthetic fixtures only. No real Brain, Slack, credentials, provider keys, `~/.dev-secrets.env`, real client config or private hostnames. Never call WebFetch. Search with `rg`.

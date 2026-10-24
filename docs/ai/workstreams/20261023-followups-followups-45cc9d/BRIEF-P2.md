# Follow-ups P2 brief: engine follow-up tasks (plan Tasks 5–9)

Repo root (worktree): `/Users/calebbolden/Projects/oss/open-brain-public/.worktrees/followups`
Branch: `feat/followups`. P1 (Tasks 1–4) and its review fix are committed. Never push, never switch branches, never touch another worktree.

## Sources of truth (in order)

1. Repo `CLAUDE.md`, `packages/app/CLAUDE.md`, `docs/engineering/gotchas/README.md` (TESTING-011/013/014/015).
2. Spec `docs/superpowers/specs/2026-10-23-commitments-open-questions-design.md`.
3. Plan `docs/superpowers/plans/2026-10-23-followups.md`: Global Constraints, File Structure, and **Tasks 5–9 only** (`FollowupTasks.propose`, `add_evidence`, `decide`, `list`/`show`, engine wiring). Follow each step exactly, in order, test first.
4. The P1 code as committed (`followup_schema.py`, `followup_contracts.py`, the 22→23 migration). Read it before Task 5; its signatures are what you consume.

If the plan conflicts with the real code, the real code and spec win: make the smallest change that satisfies the spec, and record the deviation (task, step, what and why) in your report. If a plan step would weaken a compatibility check, change a persisted format, or needs anything outside the write scope, STOP and report.

## Known corrections from P1 review

- Text limits now match the DDL: `project` and `owner_name` max 128, `ticket_url` max 2048, `ticket_key` 200. Anywhere Tasks 5–9 restate 200/2000 for those fields, use 128/2048.
- Second-revision fixtures use `engine.sources.submit_revision(SourceRevisionSubmission(...))` as in `packages/app/tests/unit/engine/test_source_tasks.py:455-490`.

## Write scope

Exactly the files named in Tasks 5–9 (including `followup_visibility.py` created in Task 5). Nothing from Task 10 onward. Portable stays 17 in this package; do not add the Portable 18 sidecar. Do not edit `docs/ai/workstreams/**` (coordinator-owned).

## Verification (yours)

Each task's own test commands as written, then `uv run --frozen pytest -q -n 4 -p no:cacheprovider packages/engine packages/app/tests/unit tests/security`, `make lint`, `make typecheck`, `git diff --check`. Then run `make verify` once on a committed, clean tree with no other writes to the checkout during the run (TESTING-013). Known unrelated flakes under load: the two TESTING-014 plugin readiness tests and `packages/connectors/tests/contract/test_google_calendar_http.py::test_drip_feed_has_total_wall_deadline_and_child_is_reaped`. If only those fail, rerun the failing test alone and report both results. Do not run `make native-audit` or `make homebrew-smoke`.

## Commit policy

Local commits at each plan commit step, conventional messages written to a file and committed with `git commit -F`, ending with:

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

Stage files by path. Leave the tree clean apart from coordinator files you were told not to touch.

## Output contract

Final report under 400 words: status, commits (hash + subject), files changed, exact commands with pass/fail counts, every deviation from the plan, and anything the next package (Tasks 10–13: scope tests, retraction/edited-source, Portable 18) must know.

## Sensitive data

Synthetic fixtures only. No real Brain, Slack, credentials, provider keys, `~/.dev-secrets.env`, real client config or private hostnames. Never call WebFetch. Search with `rg`.

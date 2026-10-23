# Follow-ups P3 brief: scope, retraction, edited sources, Portable 18 (plan Tasks 10–13)

Repo root (worktree): `/Users/calebbolden/Projects/oss/open-brain-public/.worktrees/followups`
Branch: `feat/followups`. P1 (Tasks 1–4) and P2 (Tasks 5–9) and their review fixes are committed. Never push, never switch branches, never touch another worktree.

## Sources of truth (in order)

1. Repo `CLAUDE.md`, `packages/app/CLAUDE.md`, `docs/engineering/gotchas/README.md` (TESTING-011/013/014/015).
2. Spec `docs/superpowers/specs/2026-10-23-commitments-open-questions-design.md`, especially the edited-source rule and retraction rule.
3. Plan `docs/superpowers/plans/2026-10-23-followups.md`: Global Constraints, File Structure, and **Tasks 10–13 only**. Follow each step exactly, in order, test first.
4. The P1/P2 code as committed (`followup_schema.py`, `followup_contracts.py`, `followups.py`, `followup_visibility.py`). Read it before Task 10.

If the plan conflicts with the real code, the real code and spec win: make the smallest change that satisfies the spec, and record the deviation in your report. If a plan step would weaken a compatibility check, change a persisted format beyond Portable 18 as the plan describes, or needs anything outside the write scope, STOP and report.

## Carried forward from P2 review (must be closed in Task 11)

- `show` sends non-owner sessions through `_owner_projection`, which reads the cited revision with `history=True`. After a source edit that can return old revision text to a client or wrongly withhold the summary. `visible_to` calls `projector.source(old_anchor, history=False)`, so items citing an edited source may be wrongly hidden. Task 11's `project_item` must replace both. Visibility after an edit is judged against the head (readable, same tier); quote still present in head -> status `moved` with new position; otherwise `source_changed` with no text. Old revision text is never returned to a non-owner session.
- Add a non-owner `show` test on an edited source (both `moved` and `source_changed`) and a non-owner `list` test on the same.
- Evidence rows are inserted after their item row (foreign keys ON).
- `EvidenceRef` enforces `capture_id == revision_id` snapshot; Portable 18 restore must keep that invariant.
- Text limits: `project`/`owner_name` 128, `ticket_url` 2048, `ticket_key` 200.

## Write scope

Exactly the files named in Tasks 10–13 (including Portable 18 export/restore, the `history/followups/followups-v1.json` sidecar and `docs/portable-brain-v18.md`). Nothing from Task 14 onward (no app service, CLI, MCP). Do not edit `docs/ai/workstreams/**`.

After Task 13, Portable is 18 and coordinates are 23/16/18. Follow the plan for which prose coordinates Task 13 owns.

## Verification (yours)

Each task's own test commands as written, then `uv run --frozen pytest -q -n 4 -p no:cacheprovider packages/engine packages/app/tests/unit tests/security`, `make lint`, `make typecheck`, `git diff --check`. Then `make verify` once on a committed, clean tree with no other writes to the checkout during the run (TESTING-013). Before starting it, check `uptime`; if load average is above 6, wait (poll at most every 5 minutes, up to 30 minutes) and note the load you ran at. Known unrelated load flakes: TESTING-014 plugin readiness tests (incl. t07-ui "does not rewrite or reveal the canvas after unload"), `tests/release/test_harness_acceptance.py` subprocess timeouts, `test_pytest_timing.py[xdist]`, and `test_google_calendar_http.py::test_drip_feed_has_total_wall_deadline_and_child_is_reaped`. If only those fail, rerun them alone and report both results; do not loop on full verify more than twice. Do not run `make native-audit` or `make homebrew-smoke`.

## Commit policy

Local commits at each plan commit step, conventional messages written to a file and committed with `git commit -F`, ending with:

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

Stage files by path. Leave the tree clean apart from coordinator files.

## Output contract

Final report under 400 words: status, commits (hash + subject), files changed, exact commands with pass/fail counts, every deviation, how the carried-forward edited-source items were closed (test names), and anything Tasks 14–18 (app service, CLI, MCP, grants) must know.

## Sensitive data

Synthetic fixtures only. No real Brain, Slack, credentials, provider keys, `~/.dev-secrets.env`, real client config or private hostnames. Never export a real Brain from this branch. Never call WebFetch. Search with `rg`.

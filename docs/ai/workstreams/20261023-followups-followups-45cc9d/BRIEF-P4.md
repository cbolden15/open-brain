# Follow-ups P4 brief: app service, CLI, MCP, grants (plan Tasks 14–18)

Repo root (worktree): `/Users/calebbolden/Projects/oss/open-brain-public/.worktrees/followups`
Branch: `feat/followups`. P1–P3 (Tasks 1–13) and their review fixes are committed; coordinates are 23/16/18. Never push, never switch branches, never touch another worktree.

## Sources of truth (in order)

1. Repo `CLAUDE.md`, `packages/app/CLAUDE.md`, `docs/engineering/gotchas/README.md` (TESTING-011/013/014/015).
2. Spec `docs/superpowers/specs/2026-10-23-commitments-open-questions-design.md`, especially grants, the Work-only default and the error table.
3. Plan `docs/superpowers/plans/2026-10-23-followups.md`: Global Constraints, File Structure, and **Tasks 14–18 only** (shared service with closed wire shapes and `followup_session_authority` Work-only clamp, owner CLI, MCP `brain_followup_*`, `open-brain mcp --allow-followup-*`, `agent setup` grants across CLI, plugin bridge and instructions). Follow each step exactly, in order, test first.
4. The engine code as committed (`followups.py`, `followup_visibility.py`, `followup_contracts.py`). Read it before Task 14; its signatures and limits win over plan literals.

If the plan conflicts with the real code, the real code and spec win: make the smallest change that satisfies the spec, and record the deviation in your report. If a plan step would weaken a compatibility check, change a persisted format, add a listener/daemon/dependency to the core, or needs anything outside the write scope, STOP and report.

## Known corrections (apply wherever Tasks 14–18 restate them)

- Field limits must equal the engine contracts: `project` and `owner_name` 128, `ticket_key` 200, `ticket_url` 2048, summary 280. The plan's `MAX_FOLLOWUP_TEXT_CHARACTERS = 200` (plan lines ~6006, ~6355) is used for project/owner_name and must be 128 for those; ticket_key stays 200. MCP schema `followup_text` (plan ~7610) and `owner_name`/`project` `maxLength: 200` (plan ~7758, ~7761) become 128 where they describe project/owner_name; keep a separate 200 limit for `ticket_key`. Prefer importing the engine constants over new literals. Add boundary tests at the app/MCP layer for 128/129 and 2048/2049.
- `source_changed` evidence returns `quote=None` for every session, owner included. `moved` and `current` carry head text only.
- Status and head positions are computed on every read; do not cache them in wire shapes.
- Restored decision rows have no stored receipt: replaying an old operation returns the item's current state. Do not promise byte-identical receipts across restore in docs or tests.
- Every MCP session is Work-only by default, including an unscoped `open-brain mcp`; Personal/Public only when the launcher policy allows that tier. All three grants (`followup-read`, `followup-propose`, `followup-decide`) are off by default. The owner CLI has full authority.
- Patterns to follow: `services/review_publication.py` (shared CLI/MCP service), `relationships.py` (CAS + decision log), existing `--allow-review-*` wiring.

## Write scope

Exactly the files named in Tasks 14–18 (app service, CLI, MCP server/tools, mcp launcher flags, agent setup across CLI, plugin bridge and instructions, plus their tests). Engine files only if a task names them. Nothing from Task 19 onward (no acceptance module, no `docs/followups.md`, no reference-docs gate). Do not edit `docs/ai/workstreams/**`.

## Verification (yours)

Each task's own test commands as written, then `uv run --frozen pytest -q -n 4 -p no:cacheprovider packages/engine packages/app/tests/unit tests/security`, `make lint`, `make typecheck`, `make plugin-test` (Task 18 touches the plugin bridge), `git diff --check`. Then `make verify` once on a committed, clean tree with no other writes to the checkout during the run (TESTING-013). Before starting it, check `uptime`; if load average is above 6, wait (poll at most every 5 minutes, up to 30 minutes) and note the load. Known unrelated load flakes: TESTING-014 plugin readiness tests (incl. t07-ui "does not rewrite or reveal the canvas after unload"), `tests/release/test_harness_acceptance.py` subprocess timeouts, `test_pytest_timing.py[xdist]`, `test_google_calendar_http.py::test_drip_feed_has_total_wall_deadline_and_child_is_reaped`. If only those fail, rerun them alone and report both results; at most two full verify runs. If `plugin-test` stops verify, run `make desktop-test` separately. Do not run `make native-audit` or `make homebrew-smoke`.

## Commit policy

Local commits at each plan commit step, conventional messages written to a file and committed with `git commit -F`, ending with:

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

Stage files by path. Leave the tree clean apart from coordinator files.

## Output contract

Final report under 400 words: status, commits (hash + subject), files changed, exact commands with pass/fail counts, every deviation, the grant and Work-only tests by name, and anything Tasks 19–21 (acceptance, user docs, reference-docs gate) must know.

## Sensitive data

Synthetic fixtures and synthetic client profiles only. No real Brain, Slack, credentials, provider keys, `~/.dev-secrets.env`, real client config (never touch real `~/.claude`, `~/.codex` or other agent config; use temp homes) or private hostnames. Never call WebFetch. Search with `rg`.

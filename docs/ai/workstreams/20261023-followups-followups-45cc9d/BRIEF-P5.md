# Follow-ups P5 brief: acceptance, user docs, reference-docs gate (plan Tasks 19–21)

Repo root (worktree): `/Users/calebbolden/Projects/oss/open-brain-public/.worktrees/followups`
Branch: `feat/followups`. Tasks 1–18 and their review fixes are committed; coordinates are 23/16/18. Never push, never switch branches, never touch another worktree. Task 22 (frozen-tree gate) is run by the coordinator, not you.

## Sources of truth (in order)

1. Repo `CLAUDE.md`, `packages/app/CLAUDE.md`, `docs/engineering/gotchas/README.md` (TESTING-011/013/014/015).
2. Spec `docs/superpowers/specs/2026-10-23-commitments-open-questions-design.md`.
3. Plan `docs/superpowers/plans/2026-10-23-followups.md`: Global Constraints, File Structure, and **Tasks 19–21 only** (end-to-end evaluation acceptance module, `docs/followups.md`, reference docs + project instructions + documentation gate). Follow each step exactly, in order, test first.
4. The committed code. Where the plan's prose or examples disagree with the code, the code wins and docs must describe the code.

If the plan conflicts with the real code, make the smallest change that satisfies the spec and record the deviation. If a plan step would change product code beyond a doc-gate fix, weaken a check, or needs anything outside the write scope, STOP and report.

## Facts the docs and acceptance test must reflect (from P1–P4)

- Limits: summary 280; `project` and `owner_name` 128; `ticket_key` 200; `ticket_url` 2048; 10,000 items; 50 evidence rows per item; MCP page limit 100; `expected_version` starts at 1.
- Evidence status is `current`, `moved`, `source_changed` or `retracted`. `source_changed` returns no quote text for any session, owner included; the CLI prints "(quote withheld)". Old revision text is never returned. Status and head positions are computed on every read.
- Items with retracted evidence are hidden from clients; owner sees `evidence_retracted` with summary withheld.
- Default list hides snoozed items until their date, and closed items unless new evidence arrived after the decision.
- Every MCP session is Work-only by default, including unscoped `open-brain mcp`; Personal/Public only via a launcher policy that allows that tier, and a launcher-policy session gets follow-up tools only if that policy grants the follow-up capabilities. Grants `followup-read`, `followup-propose`, `followup-decide` are independent and off by default; scoped agent setup refuses them. The owner CLI has full authority.
- Replay with the same `operation_id` returns the original receipt, also after the cited source was edited. Restored decision rows have no stored receipt: replaying an old operation after a Portable restore returns the item's current state.
- Portable 18 adds `history/followups/followups-v1.json`; Portable 17 input restores with no follow-ups. `docs/portable-brain-v18.md` already links to `docs/followups.md`, which Task 20 creates.
- MCP list responses are capped at 250 KB; when rows are dropped the response sets `truncated=True` and `next_cursor` resumes after the last returned row. Refusals use only the closed follow-up error codes plus `unsupported_capability` and `operation_pending`. Decision rows in `show` responses (MCP and CLI) carry no principal/actor field; never document an internal principal ID.

## Required additions from the P4 review

- Task 19 must include an end-to-end launcher-policy test using `--session-policy`: (a) the session's follow-up capabilities are the policy's capabilities intersected with the `--allow-followup-*` flags; (b) a Personal item becomes visible only when the policy allows the Personal tier, and stays hidden otherwise.
- External-provider sessions get no follow-up tools (`local_callbacks` requires `OWNER_LOCAL` egress, `local_entrypoints.py` ~2893). Docs must not promise MCP follow-up access for those sessions; the egress-eligible-items rule is tested at the engine layer only. Say so plainly in `docs/followups.md`.

## Write scope

Exactly the files named in Tasks 19–21 (acceptance test module, `docs/followups.md`, the reference docs and project-instruction files the plan names, and the documentation gate/test data). Follow the repo humanizer rule for user docs: plain sentences, sentence-case headings, no promotional wording. Do not edit `docs/ai/workstreams/**`.

## Verification (yours)

Each task's own test commands as written, then `uv run --frozen pytest -q -n 4 -p no:cacheprovider packages/engine packages/app/tests/unit tests/security`, `uv run --frozen pytest -q -p no:cacheprovider tests -k documentation`, `make lint`, `make typecheck`, `git diff --check`. Then `make verify` once on a committed, clean tree with no other writes to the checkout during the run (TESTING-013). Before starting it, check `uptime`; if load average is above 6, wait (poll at most every 5 minutes, up to 30 minutes) and note the load. Known unrelated load flakes: TESTING-014 plugin readiness tests (incl. t07-ui "does not rewrite or reveal the canvas after unload"), `tests/release/test_harness_acceptance.py` subprocess timeouts, `test_pytest_timing.py[xdist]`, `test_google_calendar_http.py::test_drip_feed_has_total_wall_deadline_and_child_is_reaped`. If only those fail, rerun them alone and report both results; at most two full runs. Do not run `make native-audit` or `make homebrew-smoke`.

## Commit policy

Local commits at each plan commit step, conventional messages written to a file and committed with `git commit -F`, ending with:

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

Stage files by path. Leave the tree clean apart from coordinator files.

## Output contract

Final report under 400 words: status, commits (hash + subject), files changed, exact commands with pass/fail counts, every deviation, and anything the coordinator's Task 22 frozen-tree gate must know.

## Sensitive data

Synthetic fixtures only. Docs and examples must use invented names, IDs and URLs (e.g. `example.com`), never real people, hostnames, Slack workspaces or Brain content. No credentials, `~/.dev-secrets.env`, or real agent config. Never call WebFetch. Search with `rg`.

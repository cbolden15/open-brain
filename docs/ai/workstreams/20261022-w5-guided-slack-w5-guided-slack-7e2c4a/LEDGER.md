# Dispatch ledger

Milestone: W5 P1–P4 to PR-ready (no push). Caps: 6 active, 12 total.

| # | Child | Package | Model | Status |
|---|---|---|---|---|
| 1 | task-tdd-implementer | P1 read-only foundation (+ P0 re-anchor) | opus | done: `4fec58f`, `18ee80c`; 3089 focused pass; 6 concerns |
| 2 | task-spec-compliance-reviewer | P1 review | opus | PASS_WITH_FIXES: 1 Important (closed DTO validation), 3 Minor; carry-forwards in BRIEF-P2 |
| 1b | (child 1 resumed, not a new child) | P1 review fixes | opus | done: `c9bad9c`; 3118 focused pass |
| 3 | task-tdd-implementer | P2 capture guide + journal | opus | done: `7325703`..`65f0d71` (5 commits); 3840 focused pass; 7 deviations, quota integration gap |
| 4 | task-spec-compliance-reviewer | P2 review | opus | PASS_WITH_FIXES: 1 Important (refused import != non-commit), 4 Minor; see REVIEW-P2.md |
| 3b | (child 3 resumed, not a new child) | P2 review fixes | opus | done: `1a65720`; 3847 focused pass |
| 5 | task-tdd-implementer | P3 release + scoped setup | opus | done: `16be97a`..`4f7f492` (7 commits); 5119 focused pass |
| 6 | task-spec-compliance-reviewer | P3 review (security-weighted) | opus | PASS_WITH_FIXES: 2 Important (dedication recheck, no raw SQL), 4 Minor; see REVIEW-P3.md |
| 5b | (child 5 resumed, not a new child) | P3 review fixes | opus | done: `0816dd4`, `6edd7c0`; v1 refuses existing spaces; new engine `InboxSpaceTasks.space_capture_ids`; full focused 7831 pass / 19 fail under 97% disk + concurrent other-session tests, all 19 pass in isolation; adapter unit 103/103 x3 by coordinator |
| 7 | task-tdd-implementer | P4 deferred items + docs | opus | done: `3b0c714`..`a094bce` (5 commits); guide files 196/196 on default temp root (151 failures were self-inflicted world-writable `--basetemp`); item 7 not done |

| 8 | task-spec-compliance-reviewer | final whole-branch review (read-only) | opus | READY_WITH_FIXES: 1 Important (cancel after state-root restore), 4 Minor |
| 7b | (child 7 resumed, not a new child) | final review fixes | opus | done: `905a2cc`, `24d08a8`, `558e691`; guide files 199/199 |

Final gates: coordinator, on a frozen committed tree.

- Gate 1 on `2569d96`: Python 9079 passed / 0 failed; Obsidian plugin 701/702 (t07-ui `vi.waitFor` 1000 ms default hit at 1008 ms under load; 702/702 on 3 isolated reruns; plugin untouched by W5; gotcha TESTING-014); desktop stage not reached.
- Gate 2: rerun after final-review fixes (this commit is its head).

Concurrency note: a separate Codex session created `.worktrees/owner-egress-policy` and `.worktrees/search-projection-drift` (base `dd4c31d`, external-provider egress overlay changes) on 2026-10-22. Overlaps W5 P3 egress surface; second-to-merge must rebase and re-review.

Planned: P1 review, P2 impl, P2 review, P3 impl, P3 review, P4 impl, final review (8 total); 4 reserved for remediation.

Baseline: `make verify` on `e865230` in this worktree, 2026-10-22: 8745 passed, 9 skipped, 26 errors, all in `tests/release/test_harness_acceptance.py` with `source_changed_during_run`. Cause: coordinator wrote workstream files into the checkout during the run; that suite refuses a changing tree. Plugin/desktop stages did not run. Final gate must run on a frozen, committed tree with no concurrent writers. Confirmed: same file on the clean main checkout at `e865230` passed 32/32.

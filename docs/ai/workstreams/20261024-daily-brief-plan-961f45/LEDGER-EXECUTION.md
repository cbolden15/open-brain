# Ledger: daily brief execution

Milestone: execute `docs/superpowers/plans/2026-10-24-daily-brief.md` (17 tasks) to PR-ready on `feat/daily-brief`, no push. Base `f21dfb9`.
Caps: 6 active, 12 total. New milestone ledger; the planning milestone (3 Fable authors) is counted in `LEDGER.md`.
Planned: one task-tdd-implementer and one task-spec-compliance-reviewer per package (6); 6 reserved for remediation and the final whole-branch review. Review fixes go back to the same implementer (resumed, not a new child).

| # | Child | Package | Model | Status |
|---|---|---|---|---|
| 1 | task-tdd-implementer | P1 Tasks 1–5 | opus | DONE_WITH_CONCERNS; f85e7f9..f02d6d7 (5 commits); 36 package tests, broad suite 5838 passed; deviation: `sharing_eligible` applied to available heads only so unavailable captures are counted; ruff/mypy fixes to plan test code; commits carry the Opus trailer |
| 2 | task-spec-compliance-reviewer | P1 | opus | APPROVED_WITH_FIXES; spec met; Important I1 (unavailable heads skipped the historical fence and sharing approval/revocation checks for non-owners) sent to child 1 (resumed); fixed in 7ecdb5f (2 new tests, 228 covering tests pass); re-review RESOLVED. P1 complete at 7ecdb5f |
| 3 | task-tdd-implementer | P2 Tasks 6–13 | opus | DONE_WITH_CONCERNS; a51f63f..0e5032e (8 commits); unit+security 3131 passed, package files 382 passed; concerns: `brief_ack` adds `brief-read` for ack-only sessions, family-ref bound widened to 1–2200 non-control chars, Task 10–13 commits rebuilt to drop unrelated ruff reformatting, Opus trailer |
| 4 | task-spec-compliance-reviewer | P2 | opus | APPROVED_WITH_FIXES; I1 due items starved past the 500 scan; I2 ack-only sessions (capability lift, unreadable key, fingerprint leak); I3 client space IDs evicting owner key files; sent to child 3 (resumed) |

Coordinator decisions on P2 findings that touched the plan (recorded here and in the spec, owner may override): I2 → `brain_brief_ack` requires both `brief-read` and `brief-ack`; ack without read is `unsupported_capability`; the grant flags stay independent. I3 → a selected space ID the session cannot see or that does not exist is `invalid_request` on every surface, before the visibility key is computed.

Minor findings carried to the final review (from `.superpowers/sdd/P1-review.md`): visibility-key tests do not cover every key dimension one at a time; `source_freshness` scan cost is unbounded by capture count; P1 commits carry the Opus trailer (six commits); `source_freshness` imports the private `_managed_copy_identity` from `sharing.py` (a public alias could follow); no external-provider test over an unavailable copy.

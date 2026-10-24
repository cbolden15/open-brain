# Dispatch ledger

Milestone: execute `docs/superpowers/plans/2026-10-23-followups.md` (22 tasks) to PR-ready, no push.
Caps: 6 active, 12 total. This is a new milestone ledger; the planning milestone (5 section authors + 1 integrator, Fable) was counted separately.

Packages (sequential): P1 Tasks 1–4, P2 5–9, P3 10–13, P4 14–18, P5 19–21. Task 22 (frozen-tree gate) is run by the coordinator.
Planned: one task-tdd-implementer and one task-spec-compliance-reviewer per package (10); 2 reserved for remediation. Review fixes go back to the same implementer (resumed, not a new child).

| # | Child | Package | Model | Status |
|---|---|---|---|---|
| 1 | task-tdd-implementer | P1 (Tasks 1–4) | opus | done with concerns: 5 commits ede0e22..421da63; verify 1 unrelated timing flake |
| 2 | task-spec-compliance-reviewer | P1 | opus | APPROVED_WITH_FIXES; fixes applied by child 1 (resumed) in a3786da |
| 3 | task-tdd-implementer | P2 (Tasks 5–9) | opus | done with concerns: 5 commits ab544a0..7c91f2f; verify blocked by load flakes (harness acceptance timeouts, t07-ui TESTING-014) |
| 4 | task-spec-compliance-reviewer | P2 | opus | APPROVED_WITH_FIXES; findings 1–4 sent to child 3 (resumed); finding 5 carried into BRIEF-P3 |
| 5 | task-tdd-implementer | P3 (Tasks 10–13) | opus | done with concerns: 4 commits 3402824..fd6f244; Portable 18; verify python/build green, plugin TESTING-014 flake passed on rerun |
| 6 | task-spec-compliance-reviewer | P3 | opus | APPROVED_WITH_FIXES; findings 1–5 sent to child 5 (resumed); fixed in 7974940, 3bb9f3e, 34e5e00 |
| 7 | task-tdd-implementer | P4 (Tasks 14–18) | opus | done with concerns: 5 commits 5dd7113..027d993; verify 1 load flake (pytest_timing xdist), rest green |
| 8 | task-spec-compliance-reviewer | P4 | opus | APPROVED_WITH_FIXES; findings 1–3 sent to child 7 (resumed), fixed in 95abd72; 4–5 carried into BRIEF-P5 |
| 9 | task-tdd-implementer | P5 (Tasks 19–21) | opus | done with concerns: 3 commits b5ac63e..1e1cce2; verify fails only on pre-existing consumer-intake lock race (unchanged files; 60/60 isolated) |
| 10 | task-spec-compliance-reviewer | P5 | opus | APPROVED_WITH_FIXES; 4 minor doc fixes applied by coordinator in Task 22 |

# Dispatch ledger

Milestone: execute `docs/superpowers/plans/2026-10-23-followups.md` (22 tasks) to PR-ready, no push.
Caps: 6 active, 12 total. This is a new milestone ledger; the planning milestone (5 section authors + 1 integrator, Fable) was counted separately.

Packages (sequential): P1 Tasks 1–4, P2 5–9, P3 10–13, P4 14–18, P5 19–21. Task 22 (frozen-tree gate) is run by the coordinator.
Planned: one task-tdd-implementer and one task-spec-compliance-reviewer per package (10); 2 reserved for remediation. Review fixes go back to the same implementer (resumed, not a new child).

| # | Child | Package | Model | Status |
|---|---|---|---|---|
| 1 | task-tdd-implementer | P1 (Tasks 1–4) | opus | done with concerns: 5 commits ede0e22..421da63; verify 1 unrelated timing flake |
| 2 | task-spec-compliance-reviewer | P1 | opus | APPROVED_WITH_FIXES; fixes applied by child 1 (resumed) in a3786da |

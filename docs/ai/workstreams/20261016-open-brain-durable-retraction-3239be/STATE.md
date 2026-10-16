# Workstream State

- ID: `20261016-open-brain-durable-retraction-3239be`
- Repo root: /Users/calebbolden/Projects/oss/open-brain-public
- Remote identity SHA-256 fingerprint: `51c3141e8dd7ccbb712aa075e0b4c373f3894d9e9b386c6db8f4301edc6bdfab`
- Worktree: /Users/calebbolden/Projects/oss/open-brain-public/.worktrees/competitive-gap-reconciliation
- Branch: feat/competitive-gap-reconciliation
- Objective: Implement durable owner retraction preview commit status with versioned approval receipts and Portable14 compatibility
- Created date: 2026-10-16
- Milestone: complete owner preview/page/commit/status, same-transaction approval receipts, schema19/runtime14/Portable14 and predecessor compatibility.
- Scope: public owner retraction only; no provider profiles, egress, live Brain operations, push or deploy.
- Stop: synthetic crash/replay/staleness/restore and CLI tests; make verify; native-audit/homebrew-smoke if native coordinates change; diff-check/actionlint.
- Ledger: active 2/3 and total 2/6 reserved for disjoint schema/runtime and Portable14 implementation. Root owns owner task/contracts and CLI. Independent review reserved for integration.
- Authority: current Git c68f7f5 and OWNER-RETRACTION.md; prior workstream is historical, not this milestone state.
- Schema/runtime worker complete: 158 focused Python tests,700 plugin tests,5 Rust runtime tests pass; frozen migrations1-18 preserved.
- Ledger: active2/3 (Portable finishing, independent review); total3/6 reserved. Root owns integration and CLI checks. No source freeze yet.

- Integration complete: durable owner preview/page/commit/status, atomic approval receipts, marker-only current visibility, schema19/runtime14/Portable14 export/restore and CLI/native journey coverage.
- Ledger: active0/3; total3/6. Schema/runtime and Portable workers and independent review finished. Review findings resolved: exact canonical receipt type checks, retained-projection export normalization, and existing-only CLI startup skips recovery and metadata publication.
- Focused verification:59 tests pass in38.84s; full Ruff and mypy709 source files pass; source-installed owner retraction journey passes; diff-check/actionlint pass.
- Candidate freeze next: local scoped commit, full make verify, then native-audit/homebrew-smoke, then test-tiers-audit. Logs and source hash manifest remain outside repository.
- Acceptance limitation: production-size indexed latency and both-platform CI remain separate; no real Brain or live provider operations performed.

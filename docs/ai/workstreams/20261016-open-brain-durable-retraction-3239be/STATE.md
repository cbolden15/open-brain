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
- Full verification attempt1 stopped after release documentation coordinate assertion (18 vs19):174 passed,1 failed before interruption. Updated documentation checker, feature matrix and mutation fixture; no engine behavior changed. Tier audit passes with7795 tests, disjoint complete coverage.
- Full verification attempt2 stopped after current sharing export assertions expected13 rather than14:605 passed,4 failed before interruption. Audited all remaining Python13/18 assertions; updated current export/migration admission assertions while preserving frozen13 recipes and intentional predecessor states.
- Cutover regression verification:319 tests pass in140.29s across sharing, facet publication/acceptance/faults, Portable10 inherited/current composition, historical recovery and current migration/catalog assertions. Freeze revised candidate for full/native gates.
- Full attempt3 stopped after2373 passed,5 skips and6 failures: new CLI architecture inventory entry missing, recovery closure rejected Portable14 baseline, and unpinned Portable13 relationship producer. Added exact14 recovery validator/catalog and capture replay baseline support, pinned13 producers and extended tests to14.
- Recovery/cutover verification:215 tests pass in30.42s including exact Portable8-14 closure validation, guarded capture and replay, relationship export and foreground module inventory. Ruff passes. Revised source candidate ready for full/native gates.
- Full attempt4 completed Python:7797 passed,5 skips,13 failures in911.15s. All failures were stale expected catalog command count or collector current Portable13 assertions; actual workflows succeeded. Updated98 command inventory (explicit retract preview/commit/status) and collector expectations14. No further runtime failures observed.
- Collector/catalog regression check:39 tests pass in7.52s. Revised frozen candidate requires full make verify and native gates.

## Verified milestone

- Status: complete; tested source commit `46e378a3deffaecb0c347858617516548dc4a147`. Ledger active0/3, total3/6, all workers finished.
- Owner CLI now connects durable preview/page/commit/status to the real engine. Marker and approval receipt commit atomically; exact retries and status survive restart and Portable14 restore. Existing-only read commands skip startup recovery and metadata publication.
- Current coordinates: schema19/runtime14/Portable14. Frozen predecessor migration statements and Portable validators remain preserved; recovery closures and capture replay admit14 through its exact validator/catalog.
- Full `make verify` ran on the frozen candidate: lint and mypy709 files passed; Python7807 passed,5 filesystem skips,3 environment failures (two subprocess storage_critical refusals and one harness timeout). All three passed unchanged on serial rerun in8.31s, covering all7810 Python cases successfully. The initial aggregate make command exited nonzero; do not describe that invocation as passing.
- Remaining verify components `make build plugin-test desktop-test` passed: package artifacts built;700 plugin,286 desktop UI,30 desktop Python,26 Rust tests pass. Worker caps: Python4, Vitest1. No assertions or timing budgets weakened.
- `make native-audit homebrew-smoke` passed on macOS arm64, including installed owner retraction preview/page/commit/status/replay/search exclusion/verified export, plus capture/search, doctor, Markdown, MCP, graph and Obsidian plugin. Temporary formula preserved the existing product.
- `make test-tiers-audit` passed:7815 collected,5101 fast and2714 integration, disjoint complete union. Full Ruff, mypy, diff-check and actionlint passed.
- Source invariance: all2023 tracked/untracked Git-visible inputs unchanged through verification; logs remain outside the repository. Only this STATE and HANDOFF are updated after gates.
- Next: review owner retraction usage. Production-size indexed latency acceptance and Linux native CI remain open; G1 overall is not complete. No live Brain/provider operation, push or deployment occurred.

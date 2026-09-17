# Workstream State

Historical planning receipt, not current execution state. Public references use `.` for the repository checkout and repository-relative paths; machine-specific paths were removed before publication. On another branch or machine, ground current Git state and create a fresh workstream rather than restoring this packet as authority.

- ID: `20260916-open-brain-public-new-user-functionality-plan-60f7da`
- Repo root: .
- Remote identity SHA-256 fingerprint: `0aaeca2a6d1b421eaa7439b39b3228c836f6c7c55026bac150b0aeda757566cf`
- Worktree: .
- Branch: main
- Objective: Plan all new-user functionality findings except backup and restore with tiered delegated design and independent review
- Created date: 2026-09-16

## Planning milestone

Status: planning complete; implementation has not started.

Baseline HEAD: `5cf081aa3d591e14b89245db290d4a186ff8156a`. Existing untracked assessment is preserved at `docs/audits/2026-09-16-new-user-functional-assessment.md`.

Scope: every critical and non-critical finding in the assessment except new backup/restore work. F3 Portable restore, scheduled backups/recovery orchestration, and new version-restore/revert operations are deferred. Readable history, corrections, source availability and existing export compatibility remain in scope.

Accepted decision: the user permits an optional local embedding model download. Baseline capture/search remains offline; optional runtime dependencies stay separate from the minimal core.

Outputs: an implementation master plan, a complete finding-to-task-to-acceptance map, a separate backup/restore follow-up, independent review evidence, and a validated handoff. No application implementation, real-data changes, push, deployment, release, or account setup is authorized by this planning milestone.

Worker reservations: one economy coverage mapper, one strong architectural designer, and one mid-tier delivery designer. Max three active native workers. Coordinator owns integrated plan, shared contracts and this state. Workers have disjoint scratch-report paths and cannot dispatch children.

Stop condition: coverage reconciled, architecture choices explicit, independent review completed or its limits clearly reported, and plan/handoff artifacts verified. Implementation requires the next instruction.

## Draft milestone

All three worker reports completed and were read. Master plan, 17-entry active coverage map and separate F3 follow-up are written. The plan contains 23 work packages and 17 acceptance gates; none is marked implemented.

Independent review: installed doc-review runner, forced coherence/feasibility/security with --third-party deny. Budget override recorded before launch: maximum 15 total attempts, including three completed planning workers and the runner's fixed maximum twelve attempts; nominal total ten. This preserves the installed verifier topology and bounded retries. No further worker expansion is authorized.

## Verified planning handoff

The coverage validator passed: all 17 active findings map to 23 tasks and 17 acceptance gates, with only F3 excluded. Baseline HEAD is unchanged; no tracked product files changed. Product tests were not rerun for this planning-only milestone. Whitespace, artifact existence and reference checks passed.

Independent review consumed three first-run attempts and four retry-run attempts, plus the three design workers, for ten actual attempts. Coherence timed out once and completed on one retry with independent refutation. The only retained P3 was corrected to require at least 201 search results consistently. First-run feasibility/security findings failed exact-evidence lookup; their absence is not security or migration certification. Reports preserve both runs and their limitations.

The user asked about implementation across concurrent sessions. The master plan now specifies one coordinator, two or three root implementation sessions in separate task worktrees, pinned contracts, exclusive file ownership, separate workstream handoffs and serial integration. This section and explicit T03 security proof obligations were coordinator additions after independent review. No implementation sessions or worktrees were launched.

All deliverables are untracked local files. Commit or distribute the approved plan before creating worker worktrees; untracked files do not carry into a new worktree. Existing assessment remains preserved. HANDOFF.json validates, and HANDOFF.md is rendered from the governance template. Next action: review the master plan and obtain an implementation instruction for M1.

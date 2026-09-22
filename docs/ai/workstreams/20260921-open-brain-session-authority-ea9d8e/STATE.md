# Workstream State

- ID: `20260921-open-brain-session-authority-ea9d8e`
- Repo root: /Users/calebbolden/Projects/oss/open-brain-public
- Remote identity SHA-256 fingerprint: `0aaeca2a6d1b421eaa7439b39b3228c836f6c7c55026bac150b0aeda757566cf`
- Worktree: /Users/calebbolden/Projects/oss/open-brain-public
- Branch: goal/open-brain-session-authority
- Objective: Complete Open Brain session-authority enforcement from goal issue 81
- Created date: 2026-09-21

## Milestone 1

- Objective: Require and propagate one session authority across MCP/plugin adapters, derive scoped exposure from the safe operation matrix, and pass local gates.
- Allowed scope: app authority/session adapters, their tests, authority-plan status, and this workstream record.
- Stop condition: targeted authority regressions and `make verify`, `make contributor-check`, `git diff --check`, and `actionlint .github/workflows/ci.yml` pass on one candidate commit.
- Baseline: `make verify` passed with 3,312 Python tests, 5 skipped filesystem tests, 291 Obsidian tests, 259 desktop web tests, 1 desktop Python test, and 26 Rust tests. `git diff --check` and `actionlint .github/workflows/ci.yml` passed.
- Child ledger: 3 total read-only workers dispatched; 0 active; all completed without writes. The
  final independent security review approved the corrected candidate with no remaining findings.

## Decisions

- Use `EffectiveAuthority` as the sole T03 grant/owner source; reject the prior independent `grants` and `owner` constructor state.
- Derive runtime discovery and dispatch from injected implementations, authority policy, and scoped-safe operation metadata. Preserve static no-session catalog registration as metadata only.
- Treat the desktop plugin as an explicit trusted owner-authority entrypoint. Scoped plugin capture must use an injected bounded sink and never the owner capture task.
- Bind destination submission to an immutable authority-bearing capability and require object
  identity at the MCP adapter boundary. Recheck requested tiers before invoking the capability.
- Require `PERSONAL` tier authority anywhere the compatibility capture sink can submit its fixed
  personal classification.
- Gate every plugin stdio operation before special-case handlers, credential discovery, Brain
  initialization, or task access. Preserve only handshake and catalog as scoped public operations.

## Verification progress

- Focused authority regression set: 151 passed.
- Static verification: Ruff passed and mypy reported no issues in five changed service modules.
- Independent review: no findings after remediation of three reproduced authority bypasses.
- Final contributor gate and hosted CI remain before merge.

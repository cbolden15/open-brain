# Goal contract

Profile: `full`
Type: `code/infra`
Parent program: `cbolden15/agent-config#41`
Launch state: filing authorized; implementation not launched.

## 1. Outcome

Autonomously implement the Open Brain Phase 3 appliance control plane in
`<repo-root>` from the independently
reviewed plan at
`<repo-root>/docs/plans/phase-3-appliance-control-plane.md`
(SHA-256 `e842eac8a5a933d20405bc84bde0ecf87474c7d4317018f32ac6ef95ef0263b7`)
until the complete acceptance contract below is verified. Use Codex for the
coordinator and every delegated agent. Do not stop merely because a wave, PR,
or first attempt finishes. The goal is complete only when every completion
criterion holds and the exact tested and independently reviewed source is
merged to the public repository's `main` branch. Keep parent goal
`cbolden15/agent-config#41` open for Phase 4 and later work.

## 2. Grounding

Work in these repositories only:

- Product repository and working directory:
  `<repo-root>`
- Goal tracking repository: `cbolden15/agent-config`, limited to the child
  goal issue and sanitized progress on parent `cbolden15/agent-config#41`

Read these project-truth files before changing anything:

- `<repo-root>/CLAUDE.md`
- `<repo-root>/docs/v0-product-contract.md`
- `<repo-root>/docs/plans/option-c-architecture.md`
- `<repo-root>/docs/architecture/proposed-v0-system-architecture.md`
- `<repo-root>/docs/architecture.md`
- `<repo-root>/docs/plans/phase-2-deepen-modules-in-place.md`
- `<repo-root>/docs/plans/phase-3-appliance-control-plane.md`
- `<repo-root>/docs/ai/workstreams/20260901-open-brain-public-phase3-planning-3c6a30/GROUNDING.md`
- `<repo-root>/docs/ai/workstreams/20260901-open-brain-public-phase3-planning-3c6a30/P3-REVIEW-01.md`
- `<repo-root>/docs/ai/workstreams/20260901-open-brain-public-phase3-planning-3c6a30/P3-REREVIEW-01.md`
- `<repo-root>/docs/ai/workstreams/20260901-open-brain-public-phase3-planning-3c6a30/P3-FINAL-REVIEW-01.md`

Planning began from freshly fetched, clean `main` at
`d93c6dae2a22ef028390f30c990b27968229178e`, equal to `origin/main` at that
checkpoint. Baseline `make verify` passed Ruff, strict MyPy over 439 source
files, 2,981 tests, wheel and sdist builds, and artifact policy. Five Codex
sessions participated in planning: one mapper, one planner, and three fresh
read-only reviewers. The final review verdict is `READY` with P0/P1/P2/P3
counts `0/0/0/0`; no non-Codex agent participated.

Before implementation:

1. Run `git fetch --prune origin`; inspect `git status --short --branch`,
   `git worktree list`, `git remote -v`, `git log --oneline --decorate -10`,
   and local-versus-`origin/main` ancestry.
2. Inspect open PRs and required checks in `vora-technology/open-brain`, then
   reread `cbolden15/agent-config#41` and completed Phase 2 child
   `cbolden15/agent-config#51`. PR `vora-technology/open-brain#1` was an
   unrelated Dependabot PR at planning time and must remain untouched.
3. Confirm the reviewed plan exists at the absolute path above and matches
   its recorded SHA-256. Any technical change to it requires a new fresh,
   read-only Codex review before implementation.
4. Preserve unrelated dirty files, user-owned changes, and all existing
   worktrees. If the tree is not clean or current work overlaps, isolate the
   goal in a new worktree rather than rewriting user state.
5. Re-run `make verify` on the exact implementation baseline. If product
   contracts or architecture authority changed after the planning SHA,
   re-ground, revise the plan, and obtain a new `READY` review first.

Current architecture facts to re-verify include: default scripts still use
Phase 1 entrypoints; current operations, scheduler, backup, doctor, and
release lifecycle modules are classified `legacy`; shipping application code
must not import them; the existing HTTP server does not yet route owner UI
POSTs; and the current writer lease is not a process-lifetime daemon-authority
mechanism.

Do not query a Brain connector, Brain wiki, live Brain root, private
predecessor, or production system for additional grounding. Repository files,
synthetic fixtures, Git history, and public GitHub metadata are the complete
authority for this goal.

## 3. Mandatory path and gates

### Gate 0: establish the execution baseline

1. Start from a clean, freshly fetched `origin/main` in a branch named
   `goal/open-brain-phase3`, using a separate worktree if another workstream
   owns the existing checkout.
2. Preserve the reviewed plan and review evidence. If they are not yet in
   Git, commit them as the first planning-only unit before code changes.
3. Record the baseline SHA, plan SHA-256, branch, worktree, open PRs, required
   checks, and `make verify` result in the goal workstream state.
4. Confirm there is no code implementation, package publication, deployment,
   live service operation, or live-data access hidden in the baseline.

Every implementation wave begins at the previous clean checkpoint. It exits
only when the exact focused Pytest and Ruff blocks in the reviewed plan pass,
`uv run mypy` passes, `make verify` passes, its behavior gate passes, and its
failure gate is clear. Write regression tests before fixes. Commit each wave
as a separate verified unit.

### Foundation and control-plane waves

1. `P3-W0`: freeze the app/engine seams, negative architecture contracts,
   appliance module names, and daemon-owned mutation path. Stop if a shipping
   path requires legacy operations code or Phase 4 packaging. Verify the exact
   W0 command block and clean-checkpoint exit in the reviewed plan.
2. `P3-W1`: implement idempotent app initialization, bounded preflight,
   owner-only generated credentials, engine maintenance reads, and the
   non-mutating offline/MCP read view. It must not rewrite credentials,
   mutate content on replay, recover state, or acquire writer authority.
3. `P3-W2`: implement the Unix-domain control socket, distinct lifetime
   daemon authority, engine-issued active authority, non-nested per-operation
   writer locks, internal durable scheduler, installed/module entrypoint
   cutover, read-only MCP path, and injected launchd/systemd adapters. Prove
   restart, stale-authority, retry, replay, and no-duplicate behavior.
4. `P3-W3`: implement owner UI parity, purpose-bound browser login, host-only
   session cookie, exact origin checks, CSRF, real `/api` versus `/share` POST
   routing, complete status/doctor, and bounded metadata-only history. Every
   unknown or cross-capability POST must fail closed.

### Recovery and endgame waves

1. `P3-W4`: implement bounded Markdown reconciliation, new non-legacy engine
   backup contracts and ports, immutable manifests, disposable restore and
   doctor, and distinct Portable export/import orchestration. Use synthetic
   roots only and preserve exact portable bytes and stable identities.
2. `P3-W5`: implement source-checkout upgrade and uninstall orchestration over
   an app-owned `ArtifactLifecyclePort`, with fake/disposable adapters only;
   add macOS launchd and Linux systemd adapter evidence. Preserve the Brain
   root by default and lock native artifacts, bundling, and clean-host proof
   to Phase 4.
3. `P3-W6`: run same-commit tests, static checks, source/built-artifact/history
   safety audits with one synthetic denylist, repository gotcha capture, and
   a fresh read-only Codex review. A material repair invalidates the verdict;
   rerun affected checks, full verification, and fresh review.

### Same-commit and merge gate

Run these checks against the exact candidate commit from
`<repo-root>`:

```bash
uv run pytest -q tests/security/test_release_audit.py tests/security/test_release_history_audit.py tests/security/test_no_network.py tests/security/test_provider_privacy.py tests/security/test_public_result_residue.py
uv run ruff check src/open_brain tests
uv run mypy
make verify
uv run python -m open_brain.dev.release_audit --root . --private-denylist "$PHASE3_SYNTHETIC_DENYLIST" --artifacts dist/*
uv run python -m open_brain.dev.public_history_audit --repository . --private-denylist "$PHASE3_SYNTHETIC_DENYLIST"
git diff --check
```

`PHASE3_SYNTHETIC_DENYLIST` must point to a temporary file containing only a
known fake canary created for this gate. Never put a real private value in the
file, command line, environment, logs, artifacts, commits, or issue comments.

After all local gates pass:

1. Dispatch a fresh read-only Codex reviewer against the exact candidate SHA.
   Require `READY` and P0/P1/P2 counts `0/0/0`.
2. Push only `goal/open-brain-phase3`; open a focused PR against public
   `main`; record the candidate SHA and evidence without private paths or
   values.
3. Require every repository check, including CI, release audit, and CodeQL if
   configured, to pass on the exact candidate.
4. Merge only that reviewed SHA after the required gates pass. Verify local
   `main`, `origin/main`, and GitHub `main` resolve to the merged SHA before
   closing the child goal.

Equivalent or stronger gate sequencing is allowed only when documented in a
decision record and independently accepted by a fresh read-only Codex
reviewer. No sequencing change may weaken a contract, test, boundary, or
same-commit requirement.

## 4. Authorized scope

> Authority is limited to the goal-specific scope recorded below.

The goal-specific authorized scope is:

- Source, tests, documentation, CI, and repository-local build metadata in
  `<repo-root>` that directly implement
  the reviewed Phase 3 plan.
- A feature branch and PR in `vora-technology/open-brain`, plus the child goal
  issue and sanitized parent progress in `cbolden15/agent-config`.
- Repository-local dependency installation, wheel/sdist builds, disposable
  source-checkout roots, synthetic fixtures, fake purpose-scoped credentials,
  injected host command runners, and ephemeral Linux/macOS CI identities.
- Local commits, branch push, PR creation/update, and merge after every
  required gate passes. No deployment authority is granted.
- Codex coordinator, implementer, mapper, and reviewer sessions dispatched
  through the Codex mechanism with explicit models. No other agent family is
  authorized.

## 5. Forbidden scope

> - Hetzner production is READ-ONLY: no deployments, migrations, writes, or
>   config changes, ever. Reading prod state is allowed.
> - No changes that risk unrelated projects on shared infrastructure.
> - No force-pushes, branch-protection bypasses, destructive history
>   rewrites, or silencing/weakening of required checks or tests.
> - No secrets, credentials, database URLs, or sensitive data in logs,
>   commits, or issue comments.
> - No sending of email or outbound messages of any kind. Email-draft goals
>   produce files only; this agent holds no send credentials.

Additional goal-specific prohibitions:

- Production is entirely out of scope, including read-only inspection. Do
  not deploy, operate, inspect, migrate, configure, or query production.
- Do not access, read, copy, mutate, back up, restore, migrate, or index live
  Brain data. Do not use Brain connectors, Brain wikis, live roots, Mac mini
  launch agents, scheduled jobs, or sync state.
- Do not access private predecessor source, roots, content, credentials,
  manifests, or operational state. Use public repository evidence and
  synthetic fixtures only.
- Do not enter Phase 4: no `packages/` split, native bundling, PyInstaller,
  Nuitka, native installers, signing, notarization, SDK, separate workers,
  clean-host artifact proof, or prior-release native-artifact upgrade proof.
- Do not publish packages, create releases or tags, upload to PyPI, alter
  package ownership, deploy services, or install/operate a real supervisor
  unit on a user or shared machine.

Further boundaries:

- Do not replace, purge, or destructively test against any non-disposable
  root. Restore, import, upgrade, and uninstall tests use empty disposable
  roots and injected adapters only.
- Shipping/default code must not import or reclassify legacy lifecycle,
  scheduler, backup, doctor, or release modules.
- Do not execute or close parent goal `cbolden15/agent-config#41`, Goal 24,
  Goal 39, or any other child. Close only this Phase 3 child after acceptance.
- Do not touch unrelated `vora-technology/open-brain#1` or unrelated user
  changes, branches, worktrees, services, repositories, or infrastructure.
- Do not dispatch Claude, Gemini, Qwen, broker, or other non-Codex agents.
  Do not send real network requests from product tests or use real provider,
  browser, intake, or owner credentials.

The sole goal-specific exception to the standing outbound-message
prohibition is the one acceptance-state iMessage required by section 9. The
authorized GitHub branch, PR, child issue, and sanitized parent status updates
are repository operations, not general outbound correspondence.

## 6. Execution rules

> - Never end a turn waiting for a go-ahead on in-scope work. "Ready to
>   continue" is not a pause condition — the only valid stops are the
>   pause-only-when list and goal completion. Checkpoint by writing state,
>   not by stopping. Ending a turn to await confirmation for in-scope work
>   is a contract violation.
> - Decision forks are not blocks. At a fork: choose the right-architecture
>   fix over the quick patch; scope is the goal's OUTCOME, not its file
>   list — if the right fix requires touching more code, do it; when two
>   options are defensible, take the more reversible one; write a decision
>   record (chosen, rejected, why) into the goal log for every fork.
> - Two identical failures require a changed strategy. After three
>   materially different failed strategies, record the evidence and park.
> - Progress heartbeat: check active operations at least every 20 minutes.
>   Three consecutive intervals with no verifiable state change (commit,
>   evidence file, decision record, passed gate) → park and notify. Work
>   efficiently; never run aimlessly.
> - Commit work as verified units; never combine unrelated changes. Use the
>   project's real merge gate, tests-first for regressions.
> - Dispatch bounded implementation/exploration/review to appropriate
>   cheaper-model subagents; the coordinator owns integration, tests, git
>   state, infrastructure changes, and final verification.
> - Never weaken constraints, isolation, tests, or acceptance criteria to
>   pass a gate.

Goal-specific execution rules:

- Use Codex only. Every delegated session must be recorded with task ID,
  role, explicit Codex model/effort, prompt path or digest, output path,
  status, and parent session in the workstream ledger.
- The coordinator owns the current plan, integration, local edits, tests,
  Git state, PR state, exact-SHA review, and acceptance reconciliation.
- Keep at most six Codex children active and twelve total. Dispatch read-only
  mapping and review independently; never let a reviewer edit the candidate.
- A review applies only to the exact SHA reviewed. Any material code, test,
  contract, or architecture change requires affected gates and a fresh final
  review.
- Maintain a repository workstream state, decision log, evidence index, and
  validated handoff. Do not use live Brain data as memory or evidence.

## 7. Data and rollback contract

- All execution data is synthetic and disposable. Preserve exact fixture
  Markdown bytes, stable IDs, timestamps where contractually significant,
  schema identity, and Portable V1 evidence across backup/restore/import
  tests.
- Credentials, indexes, sockets, locks, supervisor state, and live SQLite
  sidecars must be excluded from backup fixtures. Generate fresh fake
  credentials after disposable restore where the contract requires it.
- Before any destructive mutation of a disposable root, create its backup,
  verify restore into a second empty disposable root, and record checksums or
  fingerprints. Never designate a live or user-owned root as disposable.
- Production and live-Brain downtime is zero because neither may be touched.
  A source-checkout rehearsal exceeding twice the slowest prior successful
  rehearsal or failing a mandatory check rolls back to the preceding clean
  code checkpoint.
- Keep rollback commits, test evidence, synthetic backup manifests, and
  workstream evidence for at least seven days. User approval is required
  before deleting or decommissioning rollback assets; no cleanup of
  user-owned state is authorized by this goal.

Rollback code by reverting only the failing wave's clean commit or by fixing
forward from the preceding verified checkpoint. Never use force push,
destructive reset, branch-protection bypass, or deletion of unrelated work.

## 8. Completion criteria

Implementation and behavior:

- [ ] `P3-W0` through `P3-W6` each have a clean checkpoint with their focused
      Pytest/Ruff checks, strict MyPy, `make verify`, behavior gate, and
      failure gate recorded as passing.
- [ ] The source-checkout implementation proves `V0-INSTALL-02` through
      `V0-INSTALL-06` and `V0-OPS-01` through `V0-OPS-09`.
- [ ] It proves `V0-SURFACE-02`, `V0-SURFACE-03`, `V0-SURFACE-05`,
      `V0-DATA-05`, `V0-GATE-05`, `V0-GATE-10`, and `V0-GATE-14`.
- [ ] Recovery and upgrade behavior required by `V0-GATE-08` and
      `V0-GATE-09` passes in source checkout without claiming their
      artifact-defined Phase 4 proof.
- [ ] All Phase 2 privacy, portability, MCP, connector-empty,
      import-direction, stable-identity, exact-byte, and no-provider
      guarantees remain green.

Candidate and safety evidence:

- [ ] The exact candidate passes all focused checks, `uv run ruff check
      src/open_brain tests`, `uv run mypy`, `make verify`, and
      `git diff --check` from a clean tree.
- [ ] The same candidate's source, wheel, and sdist pass the release audit
      with one synthetic denylist; its reachable Git history passes the
      public-history audit with that same synthetic denylist.
- [ ] No shipping/default import reaches legacy lifecycle code, and no
      Phase 4 packaging, publishing, production, private-predecessor, real
      credential, or live-Brain dependency exists.
- [ ] macOS launchd and Linux systemd lifecycle adapters pass through
      injected source-checkout runners; no real supervisor unit is installed
      or operated.
- [ ] A fresh separate read-only Codex session reviews the exact candidate
      and returns `READY` with P0/P1/P2 counts `0/0/0`. Any repair after that
      verdict has a newer complete gate and review record.

Git and governance endgame:

- [ ] Required GitHub CI, release-audit, and security checks are green on the
      exact reviewed candidate; no required check was bypassed or weakened.
- [ ] The exact tested and independently reviewed source is merged to public
      `main`, and local `main`, `origin/main`, and GitHub `main` resolve to the
      merged SHA.
- [ ] Decision records exist for every material fork, including chosen and
      rejected options with evidence.
- [ ] The workstream handoff validates; the repository gotcha registry is
      updated for surprises. No Brain journal is accessed or written because
      live Brain access is forbidden for this goal.
- [ ] The child goal records sanitized final evidence, closes only after all
      criteria pass, and posts a sanitized completion pointer to parent
      `cbolden15/agent-config#41` without closing the parent.

## 9. Acceptance route

> Auto-merge is gated by: tests + CI green, plus an independent agent review
> in a separate session with read-only tools. Human review is NOT a gate —
> notify Caleb via iMessage (Mac mini bridge) after completion:
> done / parked / decision-needed.

For this goal, the independent agent must be a fresh Codex session that did
not implement or edit the candidate. Its report must identify the exact
candidate SHA, review the plan's contract traceability and every wave exit,
and return `READY` with P0/P1/P2 counts `0/0/0`. If the Mac mini bridge is
unavailable, report the same state in the active session and use the
available local completion notification; do not broaden messaging authority.

## 10. Pause-only-when

> Pause ONLY when:
> - The next action could affect production or another project.
> - Irreversible cleanup is required.
> - Billing or an external account must change.
> - A required credential is unavailable on all authorized machines.
> - Three materially different remediation strategies have failed and no
>   safe in-scope path remains.
>
> Otherwise, roll back safely when necessary, replan from observed evidence,
> and continue until every completion criterion is satisfied.

For this goal, any proposed production access, live Brain access, private
predecessor access, real supervisor operation, package publication, release
creation, or Phase 4 work is outside scope and is not a decision fork. Stop
before that action and record it as excluded or deferred. Ordinary local
edits, disposable synthetic tests, dependency installs, builds, commits,
feature-branch push, PR updates, and gated merge are already authorized and
are not pause conditions.

## 11. Runtime

- Coordinator: laptop Codex session in
  `<repo-root>`
- Agents: Codex-only sessions with explicit model and effort, dispatched by
  the coordinator and recorded in the workstream ledger
- Branch prefix: `goal/open-brain-phase3`
- Plan authority:
  `<repo-root>/docs/plans/phase-3-appliance-control-plane.md`
  at SHA-256
  `e842eac8a5a933d20405bc84bde0ecf87474c7d4317018f32ac6ef95ef0263b7`
- Planning workstream:
  `<repo-root>/docs/ai/workstreams/20260901-open-brain-public-phase3-planning-3c6a30`
- Implementation workstream: create a new repository-local governed
  workstream for the child goal; do not reuse planning task IDs
- Notifications: iMessage via the Mac mini bridge only for the required final
  state; if unavailable, active-session report plus local completion
  notification
- Launch state at issue creation: filed but not launched. Filing this child
  issue does not authorize beginning implementation.

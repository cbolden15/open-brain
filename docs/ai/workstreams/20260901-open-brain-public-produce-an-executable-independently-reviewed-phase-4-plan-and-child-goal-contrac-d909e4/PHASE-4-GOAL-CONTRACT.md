# Goal contract

Title: `Goal: Open Brain Phase 4 split, native artifacts, and full-stop cutover complete`

## 1. Outcome

Autonomously complete Open Brain Phase 4 until the monolith is physically
split into isolated engine, app, connector, and legacy distributions; the
supported macOS ARM64 and Linux x86_64 native artifacts pass the complete
clean-host lifecycle and recovery contract; one rehearsed full-stop production
cutover moves every shared surface to exactly one Open Brain owner; and a valid
day-0 stabilization baseline is recorded with every rollback asset retained.
Do not stop merely because a wave, package move, artifact build, PR, rehearsal,
production window, or first attempt finishes. The goal is complete only when
every completion criterion below holds.

This is the Phase 4 child of `cbolden15/agent-config#41` and is cross-linked to
production-cutover Goal `cbolden15/agent-config#24`. It supersedes their phased
transition order and rehearsal-derived downtime caps for Phase 4 only. It does
not weaken capability parity, backup, verified restore, privacy,
exactly-one-writer, rollback, retention, or day-0 evidence requirements.

Public package publication, package-name reservation, PyPI, billing, Trusted
Publishing, tags, GitHub releases, public release artifacts, predecessor
deletion, and irreversible cleanup remain outside this goal.

## 2. Grounding

- Profile: `full`.
- Type: `code/infra`.
- Primary repository: `vora-technology/open-brain` and its public working copy
  at `<repo-root>`.
- Goal-contract repository: `cbolden15/agent-config`.
- Parent contracts: issues `#41` and `#24`, including their comments and
  validated successor handoffs.
- Product truth: `<repo-root>/docs/v0-product-contract.md`.
- Architecture truth:
  `<repo-root>/docs/plans/option-c-architecture.md` and
  `<repo-root>/docs/architecture.md`.
- Phase 4 plan authority:
  `<repo-root>/docs/plans/phase-4-physical-split-native-artifacts-and-cutover.md`
  at SHA-256
  `7fe6e5d1e48b44fb4fba232661a8b01eeca019e4f35a43f1b2c162914905bfd2`.
- Predecessor implementation evidence:
  `<repo-root>/docs/plans/phase-2-deepen-modules-in-place.md`,
  `<repo-root>/docs/plans/phase-3-appliance-control-plane.md`, and their linked
  workstream state, decisions, evidence, and handoffs.
- Current baseline at contract drafting: clean public `main` and `origin/main`
  at `3b89a4ba4787a378e6040ff042bd117da881918d`; Phase 3 child goal `#62` is
  closed; Goals `#41`, `#24`, and `#39` are open.
- Current classification baseline: 224 runtime Python files, comprising 46
  engine, 34 app, 5 connector, 135 legacy, and 4 workspace files; temporary
  live architecture debt is empty. The repository also has 248 Python test
  files and 36 schema or fixture files that require exact Phase 4 ownership.
- Current package baseline: one Hatch project, one `open-brain` wheel/sdist,
  one source namespace, no native artifact, and no physical distribution
  isolation.
- Current CI baseline: Linux Python 3.12/3.13/3.14 verification, macOS ARM64
  source-checkout lifecycle coverage, public-artifact safety, and CodeQL.
- Current local artifact baseline: ARM64 macOS, `notarytool`, and a valid
  Developer ID Application identity are available. Notary credential readiness,
  the Linux native builder, and exact bundler versions remain P4-W0 checks.
- Before changing anything, re-fetch remotes and reinspect repository status,
  branches, worktrees, PRs, required checks, runner architectures, package and
  schema inventories, signing/notary readiness, Goals `#24/#39/#41`, private
  production workstream state, scoped helper identity, recovery assets,
  service/writer state, synchronization state, and stale executable references.
- Search durable work context and the project gotcha registry before making an
  architecture or production decision. Record whether the retrieved context
  was used.
- Preserve unrelated dirty files, branches, worktrees, local commits, private
  evidence, and user-owned changes. Never place private production material in
  the public repository, PR, CI logs, artifacts, or issue comments.

Treat every creation-time fact as a snapshot and reverify it directly.

## 3. Mandatory path and gates

### Gate 0: immutable launch baseline

1. Verify the exact reviewed plan checksum, current project instructions,
   repository state, goals, host/build capabilities, signing/notary status,
   required CI, scoped production authority, recovery state, and private
   handoff before implementation.
2. Create a new governed implementation workstream and branch from freshly
   fetched `origin/main`. Do not reuse planning task IDs or state files.
3. Run and record the baseline `make verify`, release audit, complete-history
   audit, package inventory, and current source ownership validator.
4. Execute `P4-W0` before moving any runtime source. Extend the existing
   package classification into the canonical move manifest, classify every
   test/fixture/schema/resource, add the manifest validator and acceptance
   harness, record the bounded current-monolith expected-red result, pin the
   toolchain, add draft-PR CI, and finish with the default repository gate
   green.
5. Open a draft PR after the P4-W0 checkpoint. P4-W0 requires only jobs with
   real subjects: root verification, manifest/harness self-tests, and current
   artifact safety. Add isolated-distribution jobs with their owning P4A wave
   and native build/smoke jobs in P4-W5. Do not use placeholder green jobs or
   require expected-red future artifacts. The then-applicable required CI must
   pass before every later independent acceptance review.

### Gate P4A: physical split and distribution isolation

1. Execute `P4-W1` through `P4-W4` in dependency order: workspace and engine,
   app and installed entry points, connectors and isolated worker, then legacy
   and workspace quarantine.
2. Use distinct `open_brain_engine`, `open_brain`, `open_brain_connectors`, and
   `open_brain_legacy` namespaces. Do not use a shared namespace or artifact
   filters as a substitute for physical ownership.
3. Keep mechanical moves behavior-neutral. A behavior repair requires a red
   regression, a separate verified commit, and updated evidence.
4. Build release artifacts with workspace sources disabled and install them
   into disposable environments that cannot import repository source or
   undeclared workspace dependencies.
5. Prove engine with no app, app with engine but no connector/legacy, connector
   through only published extension/engine values, and legacy absent from every
   shipping/default artifact.
6. Ship a versioned provisional connector interface, conformance kit, and
   isolated worker. Do not claim stable SDK compatibility until reference,
   event, and measurement proofs all pass.
7. Remove every old import path and temporary compatibility edge by the P4A
   exit. Private compatibility may remain only inside legacy.
8. Run root and isolated checks, release/history audits, and all required CI.
   Only after CI is green, dispatch a fresh read-only Codex reviewer against
   the exact P4A candidate and require `READY` with P0/P1/P2 `0/0/0`.

### Gate P4B: native artifacts and clean-host proof

1. Execute `P4-W5` through `P4-W7`. Bind the Phase 3 lifecycle port to a real
   native adapter and run the one-working-day PyInstaller 6 one-folder spike on
   native macOS ARM64 and Linux x86_64 builders.
2. Pin PyInstaller and `pyinstaller-hooks-contrib` together. Switch to Nuitka
   standalone only after the plan's bounded PyInstaller failure gate is met;
   use the same acceptance matrix for the fallback.
3. Build with Python 3.12 on each target operating system. Neither native
   artifact may require source checkout or system Python at runtime.
4. Produce one unpublished macOS ARM64 candidate that is Developer ID signed,
   notarized, and stapled, plus one checksummed Linux x86_64 archive.
5. Run clean-host install, init, daemon, status/doctor, capture, review,
   retrieval, backup, exact-byte disposable restore, Portable export/import,
   prior-schema upgrade, rollback, uninstall, and residue checks against the
   exact native artifacts. Explicitly re-prove `V0-GATE-07` with several sibling
   proposals from one capture plus CLI/UI approve, reject, and safe edit.
   Explicitly re-prove `V0-GATE-13` with space create/rename, later routing of
   an unassigned capture without identity change, and retrieval within one
   space and across spaces.
6. Bind wheels, sdists, native artifacts, supervisor files, checksums,
   supported versions, licenses/SBOM, and Portable schema compatibility to one
   unpublished release-candidate manifest.
7. Run source, complete-history, Python-artifact, unpacked-native-artifact,
   secret, license, CodeQL, host-matrix, and clean-install gates in CI.
8. Confirm no package upload, tag, release, or public download exists.
9. Only after every required CI job is green, dispatch a fresh read-only Codex
   reviewer against the exact source and artifact digests. Require `READY` with
   P0/P1/P2 `0/0/0`.

### Gate P4C: full-stop rehearsal and production cutover

The owner's 2026-09-01 decision supersedes Goal `#24` Gate 3's staged
per-surface transition, Goal `#24` Gate 4's one-at-a-time canaries and fixed
surface order, and the Goal `#24/#41` rehearsal-derived four-hour downtime
formulas. Phase 4 uses one full-stop transaction and has no elapsed-time
acceptance or rollback threshold.

1. Reverify complete Goal `#24` capability parity, executable-reference
   accounting, production bindings, privacy, scoped helper, service/writer
   inventory, synchronization state, and rollback authority.
2. Create fresh encrypted primary and replica recovery points. Verify both and
   complete disposable restores from both against the exact P4B artifact.
3. Bind source, artifacts, checksums, migrations, configuration, helper,
   recovery receipts, and starting owner map into one immutable transaction.
4. Execute `P4-W8`: rehearse the complete full-stop transaction on disposable
   state. Stop all predecessor writers together, prove zero remaining
   predecessor authority, run copy-only/idempotent migration and derived-state
   rebuilds, start Open Brain, verify every required surface and exactly one
   writer, then force and verify complete rollback separately.
5. Record duration but do not impose a cap. Verifiable progress and workflow
   heartbeat rules still apply. Any mandatory integrity, privacy, health,
   queue, ownership, migration, recovery, or synchronization failure triggers
   rollback.
6. Obtain a fresh independent read-only Codex review of the immutable rehearsal
   receipt. P4-W9 remains blocked until the receipt is accepted with P0/P1/P2
   `0/0/0` and every input remains unchanged.
7. Execute `P4-W9` in the authorized production window. Stop all inventoried
   predecessor services and writers together, prove zero writer authority,
   snapshot, migrate, rebuild approved derived state, install/start the exact
   P4B artifact, and verify all required surfaces before acceptance.
8. On any mandatory failure, stop Open Brain, restore the complete prior owner
   map and mutable state, and verify rollback with the same evidence suite.
9. Restore synchronization only after data integrity, privacy, queue health,
   writer ownership, recovery, and all Open Brain entry points pass. Keep the
   excluded peer non-authoritative.
10. Retain every disabled predecessor, backup, manifest, helper, and rollback
    asset. Deletion, decommissioning, helper removal, and irreversible cleanup
    are unauthorized.
11. Record and validate the machine-readable day-0 stabilization baseline and
    seven-day reset rules on the final unchanged production state.

### Final source, evidence, and goal gate

1. The exact P4A/P4B source must merge through the public repository's normal
   PR, required CI, CodeQL, artifact-safety, and independent-review path before
   P4C uses its artifacts.
2. Any material source, artifact, migration, configuration, helper, or recovery
   change after review invalidates affected evidence and requires repeated
   checks and a fresh review.
3. Close this child goal only after P4A, P4B, and P4C all pass, the day-0
   baseline validates, the workstream handoff validates, and sanitized final
   evidence is recorded on this issue and linked from Goals `#41` and `#24`.
4. Do not close Goals `#41`, `#24`, or `#39` unless their remaining independent
   completion criteria also pass.

Equivalent or stronger sequencing is allowed only when it preserves the
full-stop transaction, is recorded in a decision entry, and is accepted by a
fresh independent reviewer before the affected risky step.

## 4. Authorized scope

> Authority is limited to the goal-specific scope recorded below.

Goal-specific authority:

- Full development authority in the Open Brain repository for the package
  workspace, source moves, import rewrites, tests, fixtures, schemas, release
  tooling, build configuration, CI, documentation, and governed evidence
  required by P4A and P4B.
- Authority to install bounded project/build dependencies; build unpublished
  wheels, sdists, and native candidates; use disposable environments and clean
  hosts; submit the macOS candidate for existing-account notarization; and
  store sanitized CI artifacts and evidence.
- Authority to create the Phase 4 branch, commit verified units, push it, open
  and update one draft/ready PR, and merge the exact tested and independently
  reviewed P4A/P4B candidate after required checks pass.
- After P4A, P4B, and P4-W8 pass unchanged, all scoped Mac mini Brain
  production authority already granted by Goal `#24`: create/verify backups
  and restores, stop/start/disable/enable only inventoried Brain services,
  designate/revoke writer generations, run copy-only/idempotent migrations and
  derived-state rebuilds, install the exact reviewed Open Brain artifact, run
  benign canaries and health checks, restore synchronization through its gate,
  and automatically roll back through the scoped helper.
- Authority to update Goals `#24`, `#41`, and this child with sanitized status,
  decisions, and final evidence. The acceptance-route iMessage is the only
  outbound-message exception and contains no sensitive data.
- Standing user authorization continues until completion once the child is
  explicitly launched. In-scope wave boundaries, PR updates, builds,
  notarization, and the rehearsed production transaction are not later
  approval gates.

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

Goal-specific prohibitions:

- No PyPI/TestPyPI upload, package-name reservation, billing, account change,
  Trusted Publisher registration, tag, GitHub release, first public release,
  or public release download.
- No shared namespace, overlapping distribution ownership, source-path-masked
  isolation claim, indefinite compatibility shim, or include/exclude-only fake
  split.
- No stable public Connector SDK compatibility claim before reference, event,
  and measurement proofs pass the same conformance suite.
- No weakening of package isolation, artifact membership, privacy, redaction,
  review, path confinement, writer authority, backup/restore, migration,
  release-audit, or clean-host gates.
- No live production action before P4A, P4B, P4-W8, recovery, and independent
  receipt review pass on unchanged inputs.
- No phased/rolling/dual-write cutover. No Open Brain writer may start until all
  predecessor writer authority is stopped and directly verified at zero.
- No deletion, retirement, decommissioning, helper removal, backup removal,
  rollback-asset cleanup, destructive migration, or data purge.
- No private content, production topology, credentials, local absolute paths,
  private configuration, or raw operational output in the public repository,
  Git history, PR, CI logs, artifacts, or issue comments.
- No arbitrary root shell, root SSH, general interpreter sudo, `NOPASSWD: ALL`,
  new synchronization peer, authoritative excluded peer, unrelated Vora
  repository access, or unrelated service change.

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

- Use Codex-only implementation and review sessions with explicit model and
  effort recorded in one dispatch ledger. Generic runners that introduce a
  non-Codex participant do not satisfy this contract.
- One coordinator owns the milestone budget. Use at most six active and twelve
  normal child lineages. Record a concrete reason before any mandatory-review
  override.
- Use one exclusive implementation writer at a time. The move manifest,
  imports, package metadata, root lockfile, and shared tests overlap across
  distributions and are coordinator-owned integration surfaces.
- Do not repeat Phase 2's mapper fan-out after P4-W0. The canonical manifest
  and generated reports are the source for later movement and review.
- Open the draft PR at P4-W0. Required CI runs before P4A/P4B independent
  review, not after it. A later material repair invalidates the verdict.
- Workers do not push, merge, mutate Goals, sign/notarize, access production,
  or change shared state. The coordinator performs those actions at their
  explicit gates.
- Keep mechanical moves and behavior repairs in separate commits. A behavior
  repair starts with a reproducing regression.
- After each wave, verify observed state, update decisions/evidence/handoff,
  then select the next action from results rather than the original estimate.

## 7. Data and rollback contract

- Preserve all canonical Markdown and source bytes, provenance, review and
  publication state, identities, personal/work privacy boundaries, operational
  databases that are not declared rebuildable, Git refs, package artifacts,
  issue/PR history, configuration identities, service/writer inventories,
  recovery receipts, and unrelated user work.
- Derived indexes and caches may be rebuilt only from approved canonical and
  source data. Migration is copy-only or versioned/idempotent; never overwrite
  initialized Open Brain state wholesale.
- Before P4-W8 and again before P4-W9, create fresh encrypted primary and
  replica recovery points, verify both, perform disposable restores from both,
  and bind manifests/counts/digests/schemas/application reads to the exact
  source, artifact, configuration, helper, and migration identities.
- The default goal-template downtime formula and the Goal `#24/#41` formula
  are explicitly superseded. Phase 4 has no downtime cap and no elapsed-time
  rollback trigger. Duration remains measured evidence. Workflow heartbeat and
  no-progress rules still prevent an aimless or abandoned window.
- Any mandatory integrity, privacy, health, queue, ownership, migration,
  recovery, or synchronization failure triggers the rehearsed complete
  rollback. Rollback stops Open Brain, restores the prior mutable state,
  predecessor service map, writer generation, harness wiring, and
  synchronization disposition, then runs the same acceptance checks.
- Retain disabled predecessor services, source, helpers, manifests, backups,
  recovery points, and rollback receipts through the complete seven-day
  stabilization window and at least seven additional days. Deletion,
  retirement, decommissioning, and helper removal require explicit later
  approval.

## 8. Completion criteria

- [ ] Gate 0 records a fresh clean baseline, exact reviewed plan checksum,
      required checks, host/build/signing capabilities, Goals `#24/#39/#41`
      state, private recovery/cutover state, and a new validated implementation
      workstream.
- [ ] `P4-W0` assigns all runtime files, tests, fixtures, schemas, entry points,
      resources, and release tools one destination/API/artifact disposition;
      validator and harness self-tests pass; the current expected-red report is
      bounded; the default repository gate remains green.
- [ ] P4A creates the four distinct distributions and namespaces, removes old
      import paths, leaves zero temporary architecture debt, and passes the
      canonical manifest validator.
- [ ] Engine tests pass from an engine-wheel-only environment with app,
      connectors, legacy, workspace tools, and repository source unavailable.
- [ ] App first-value, daemon, status/doctor, backup/restore, Portable, upgrade,
      and uninstall tests pass from app+engine wheels with connectors, legacy,
      workspace tools, and repository source unavailable.
- [ ] Wheel-only app tests explicitly pass `V0-GATE-07` with sibling proposals
      and CLI/UI approve, reject, and safe edit, plus `V0-GATE-13` with space
      create/rename, later unassigned-capture routing without identity change,
      and retrieval within one space and across spaces.
- [ ] Connector conformance passes only through published extension/engine
      values and the isolated worker; the default app passes with the connector
      distribution absent; no stable SDK claim is made prematurely.
- [ ] Legacy/workspace code, private fixtures, and unselected optional cloud
      dependencies are absent from all default Python and native artifacts.
- [ ] P4A required CI is green before a fresh exact-candidate read-only Codex
      review returns `READY`, P0/P1/P2 `0/0/0`.
- [ ] PyInstaller or the accepted evidenced fallback builds macOS ARM64 and
      Linux x86_64 artifacts without source checkout or system Python.
- [ ] The macOS artifact is Developer ID signed, notarized, and stapled; Linux
      and macOS checksums, licenses/SBOM, supported versions, supervisor files,
      package versions, and Portable schema range bind to one unpublished
      release-candidate manifest.
- [ ] Clean-host install, init, daemon, capture, review, retrieval, backup,
      exact-byte disposable restore, Portable round trip, prior-schema upgrade,
      rollback, uninstall, setup-time measurement, and residue checks pass on
      every supported host family required by the plan.
- [ ] Native-artifact clean-host tests explicitly repeat `V0-GATE-07` and
      `V0-GATE-13` with the same sibling proposal, terminal review, stable space,
      later routing, and scoped/all-space retrieval identities as the wheel-only
      path.
- [ ] Source, complete history, wheels, sdists, unpacked native artifacts,
      secret, license, CodeQL, and artifact-policy checks pass on the exact P4B
      candidate; no package, tag, release, or public download exists.
- [ ] P4B required CI is green before a fresh exact-source/artifact read-only
      Codex review returns `READY`, P0/P1/P2 `0/0/0`.
- [ ] Fresh primary and replica backup/restore evidence binds to the exact P4B
      artifact and final pre-cutover state.
- [ ] P4-W8 completes one disposable full-stop cutover and a separately forced
      complete rollback with zero overlapping, missing, or duplicate writers;
      an independent receipt review returns `READY`, P0/P1/P2 `0/0/0`.
- [ ] P4-W9 direct production evidence reports zero active/loaded predecessor
      writers or services, all required Open Brain surfaces healthy, exactly
      one writer per shared surface, no stale lease, no undrained/malformed
      queue, and no privacy or integrity failure.
- [ ] Benign production checks pass for CLI, MCP from Claude Code and Codex,
      UI, each capture route, sibling proposal approve/reject/safe-edit outcomes,
      space create/rename and later route identity, scoped and all-space
      retrieval, every required scheduled flow, backup, disposable restore,
      migration replay, and rollback readiness.
- [ ] Synchronization is restored only after its safety gate and is healthy
      without making the excluded peer authoritative.
- [ ] A machine-readable day-0 baseline binds final source, artifacts,
      configuration, migrations, services, writers, recovery receipts, helper,
      synchronization, timestamps, and seven-day reset rules.
- [ ] Every predecessor, helper, backup, manifest, and rollback asset remains
      retained; no deletion, decommissioning, package publication, tag, release,
      billing, account change, or irreversible cleanup occurred.
- [ ] The exact P4A/P4B source is merged to public `main`; local and remote
      `main` equal the exact tested and independently reviewed SHA; required PR
      and post-merge checks are green.
- [ ] Decision records exist for every material fork. The gotcha registry and
      brain journal contain every material surprise. The implementation
      workstream handoff validates and names the stabilization next action.
- [ ] This child contains sanitized final evidence and closes only after every
      criterion passes. Goals `#41` and `#24` link the result and remain open or
      close strictly according to their remaining independent criteria.

## 9. Acceptance route

> Auto-merge is gated by: tests + CI green, plus an independent agent review
> in a separate session with read-only tools. Human review is NOT a gate —
> notify Caleb via iMessage (Mac mini bridge) after completion:
> done / parked / decision-needed.

For this child, every independent acceptance reviewer must be a fresh Codex
session and identify the exact source SHA, artifact digests or rehearsal
receipt, plan requirements, completion count, and P0/P1/P2 counts. CI must be
green before source/artifact review. If the Mac mini bridge is unavailable,
report the same state in the active session and use the available local
completion notification without broadening outbound-message authority.

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

Goal-specific interpretation: after this child is explicitly launched, the
scoped Open Brain production actions, notarization submission, branch/PR
updates, and full-stop cutover authorized above do not trigger a pause after
their mandatory gates pass. Production action before those gates, an effect on
another project, unavailable signing/notary or scoped-helper credentials,
billing/account changes, public release, and irreversible cleanup still require
a pause. Missing authority is not permission to weaken a gate or broaden access.

## 11. Runtime

- Coordinator: laptop Codex session in `<repo-root>` with unrestricted
  filesystem/network access, authenticated GitHub CLI, target-native builders,
  fingerprint-verified scoped production access, the cutover helper, private
  recovery/audit inputs, signing/notary capability, and notification bridge.
- Agents: Codex-only sessions with explicit model and effort, dispatched by the
  coordinator and recorded in one implementation ledger.
- Branch prefix: `goal/open-brain-phase4`.
- Plan authority:
  `<repo-root>/docs/plans/phase-4-physical-split-native-artifacts-and-cutover.md`
  at SHA-256
  `7fe6e5d1e48b44fb4fba232661a8b01eeca019e4f35a43f1b2c162914905bfd2`.
- Planning workstream:
  `<repo-root>/docs/ai/workstreams/20260901-open-brain-public-produce-an-executable-independently-reviewed-phase-4-plan-and-child-goal-contrac-d909e4`.
- Implementation workstream: create a new repository-local governed workstream
  after explicit launch; do not reuse planning state or task IDs.
- Notifications: iMessage via the Mac mini bridge only for required final or
  parked state; if unavailable, active-session report plus local completion
  notification.
- Launch state at issue creation: filed but not launched. Filing, commenting,
  or linking this child does not authorize beginning Phase 4 implementation or
  touching production.
- Stopping point: P4A, P4B, and P4C complete; exact source merged; day-0
  baseline valid; rollback assets retained; package publication, release, and
  irreversible cleanup untouched.

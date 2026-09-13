# Workstream State

- ID: `20260901-open-brain-public-execute-goal-63-through-phase-4-p4a-p4b-and-p4c-with-independently-reviewed-pack-8a3f9b`
- Repo root: <repo-root>
- Remote identity SHA-256 fingerprint: `cb5e9cd7ac71c16e5109717b4bc07f01aed1bbda2b18a99bfbc76f7bd98245bc`
- Worktree: <repo-root>
- Branch: goal/open-brain-phase4-p4c
- Objective: Complete Goal 63's public P4A/P4B source and CI work. By owner
  direction on 2026-09-04, macOS binary notarization/distribution,
  backup/restore operations, P4-W8, P4-W9, and private production cutover are
  outside this project's scope.
- Created date: 2026-09-01

## Current milestone

- Milestone: source/CI complete; macOS binary distribution deferred
- Status: complete under the owner-revised scope. P4A/P4B product source and CI
  are merged on `origin/main`; every commit on this branch after that merge is
  confined to bounded P4C workstream records. The signed pre-notary DMG remains
  unpublished and must not be represented as notarized or public-release-ready.
  Exact cancellation record `cafaf03468842560fbb8ab1e4405be1012ec27e8ca9020d46b7d55eb456eda71`
  supersedes the prior Apple submission authority. No Apple submission,
  backup, restore, P4-W8, P4-W9, deployment, or production action occurred.
  Final `make verify` passed Ruff, strict MyPy on 549 source files, all 3,249
  tests, six isolated package builds, and artifact policy.
- Allowed scope: historical preservation of source, signed candidate,
  cancellation, and bounded workstream evidence only. No further execution is
  authorized in this workstream.
- Stop condition: any backup, restore, backup-access repair, recovery snapshot,
  rehearsal, P4-W8, P4-W9, deployment, production cutover, service/content
  mutation, helper mutation or removal, predecessor deletion, irreversible
  cleanup, source/artifact mutation, publication, or rerun of Goal #24.
- Base: merged `origin/main` at
  `10769806361e7ed419b653534e1303b623d84673`
- Launch commit: reviewed planning commit cherry-picked as
  `68c34d723e8f0bbdc7a51d5c5070e63e994b335d`
- Branch: `goal/open-brain-phase4-p4c`
- Plan SHA-256:
  `7fe6e5d1e48b44fb4fba232661a8b01eeca019e4f35a43f1b2c162914905bfd2`
- Launch runtime goal thread: `01a05f1b-de55-7a41-bc01-673d277a1995`
- Resumed runtime goal thread: `01a05fc4-2219-7251-8798-a74a1a200911`

## Launch constraints

- Goal #24's earlier approved production apply is complete and must never be
  rerun. The owner scope correction removes the later full-stop transaction;
  no P4-W8 or P4-W9 transaction will be created.
- The public repository stores bounded result classes and opaque identities
  only. Private topology, recovery material, and operational output remain in
  the private governed state repository.
- Public package publication, tags, releases, predecessor deletion, and
  irreversible cleanup remain forbidden.
- No implementation writer child has been dispatched. One coordinator owns
  shared package metadata, the canonical manifest, imports, lockfile, and
  integration; review children remain read-only.

## Gate 0 observations

- Public `origin/main` matches the reviewed baseline and its latest CI,
  release-audit, and CodeQL runs are green.
- Protected `main` requires linear history plus `verify (3.12)`, `verify
  (3.13)`, `verify (3.14)`, and `public-artifacts`; force pushes and deletions
  are disabled.
- Goals #24, #39, #41, and #63 are open. Goal #63 had no comments at launch.
- Local host: ARM64 macOS, Python 3.12 available, `uv` available,
  `notarytool` available, and one valid Developer ID Application identity.
- Successful CI logs directly identify the current builders as macOS 14 ARM64
  and Ubuntu 24.04 x86_64.
- Notarization credentials were not ready on either checked authorized host.
  This is a P4B prerequisite, not permission to weaken or skip signing.
- Durable context lookup: no Phase 4-specific work-brain page was found;
  project gotchas and the Phase 3 decision/evidence logs were used.
- Latest private Goal #24 handoff is complete and safe to read. It reports the
  old production apply complete, the remote stage absent, transaction
  namespaces inactive, and immutable historical/recovery artifacts retained.
- Fresh private read-only checks proved exact scoped-helper authority and
  recorded current service, writer, synchronization, executable-reference,
  and recovery-readiness result classes. Recovery access needs remediation and
  re-proof before P4-W8. No private topology or raw output entered this repo.
- Private companion workstream:
  `20260901-open-brain-private-retain-private-goal-63-recovery-service-writer-synchronization-notarization-rehe-70e37e`.
- Baseline `make verify` passed Ruff, strict MyPy on 474 source files, all
  3,114 tests, wheel/sdist builds, and artifact policy.
- The explicit ownership suite passed 53 tests. Source/artifact release audit,
  reachable-history audit, artifact policy, and diff integrity passed.
- Current inventory: 224 runtime files (46 engine, 34 app, 5 connector, 135
  legacy, 4 workspace), 250 tracked Python files under `tests/`, 36 tracked
  schema/fixture files, and zero temporary live architecture debt.
- Launch notices are recorded on child Goal #63 and parent Goals #41 and #24.

## Next action

No action remains in this workstream. If public macOS binary distribution is
desired later, create a separate owner-authorized workstream with fresh Apple
authority and revalidate the preserved candidate before use.

## Owner scope amendment (2026-09-04)

- Backup and restore operations are not part of this project and are no longer
  a completion gate for this workstream.
- P4-W8, P4-W9, private rehearsal, and production cutover are out of scope.
- Apple notarization and public macOS binary distribution are deferred and are
  not a completion gate for the source/CI project scope.
- Existing product code, tests, plans, receipts, and installed helper versions
  are retained unchanged. This amendment does not request feature deletion or
  local helper cleanup.
- Historical sections below remain factual records but grant no execution
  authority.

## P4-W0 complete

- Canonical manifest schema 3 covers 555 subjects: all 224 runtime files and
  331 non-runtime subjects. The latter include all 253 current Python files
  under `tests/`, all 36 schema/fixture files, three entry points, package and
  test resources, release resources/tools, and three generated reports.
- Runtime ownership remains engine 46, app 34, connector 5, legacy 135, and
  workspace 4. No file below `src/open_brain` moved or changed.
- The validator emits stable `P4M001` through `P4M011` findings and rejects
  incomplete/stale subjects, duplicate/out-of-bound destinations, unresolved
  rewrites, forbidden dependency edges, unresolved old paths, shipping leaks,
  and release/schema mismatches.
- The reusable harness defines all six P4A/P4B acceptance contracts, isolated
  no-sources/no-index commands, source-path masking checks, and stable artifact
  finding codes. Synthetic missing, leaked, mismatched, unsafe, duplicate, and
  source-masked cases pass.
- The current monolith check is intentionally outside the default green gate.
  It exits 1 with exactly 11 bounded `P4E001` through `P4E005` findings matching
  the committed expected-red report.
- Toolchain records pin `uv 0.12.8`, Hatchling `1.32.0`, PyInstaller `6.22.2`,
  hooks `2026.7`, and fallback Nuitka `4.2`. Workspace activation remains
  correctly assigned to P4-W1.
- Dedicated real-subject `phase4-contracts` CI and widened public-artifact
  triggers are present. No future artifact has a placeholder-green job.
- Local verification: 68 focused tests, Ruff, strict MyPy on 481 files,
  manifest validation, all 3,129 repository tests, isolated wheel/sdist build,
  artifact policy, source/artifact and reachable-history audits, Gitleaks,
  lock check, diff integrity, and zero runtime-source movement all passed.
- Checkpoint commit:
  `ae144db9e461f9e369976c04b75467eb5c69a144`.
- Draft PR: `vora-technology/open-brain#6`.
- Required `phase4-contracts`, Python 3.12/3.13/3.14, public artifacts,
  macOS ARM64 lifecycle, and both CodeQL analyses passed on the exact commit.
- `phase4-contracts` was added to protected `main` as a required check.
- Sanitized Goal #63 checkpoint comment: `5502376856`.

## P4-W1 source checkpoint

- The root uv workspace has all four declared members. The engine distribution
  owns its distinct `open_brain_engine` namespace; app, connector, and legacy
  skeletons remain nonbuildable until their waves.
- All 46 engine runtime files and Portable resources are at their canonical
  manifest destinations. The engine has an explicit public API and no app,
  connector, legacy, third-party, or hidden workspace import edge.
- The manifest now covers 565 subjects: 224 runtime and 341 non-runtime.
  Package-local PEP 639 license files and the bounded Hatch sdist hook are
  classified, and generated movement evidence was refreshed.
- Exact artifact policy derives required membership from
  `docs/v0-package-classification.json`. The engine wheel contains the declared
  PEP 639 `LICENSE` and `NOTICE`; the sdist contains those files plus declared
  release resources and no `.gitignore` or undeclared member.
- The isolated engine contract installs only the engine wheel into one Python
  3.12 environment. A separate disposable test-runner environment executes all
  20 moved engine test modules from copied test inputs while repository source,
  app, connectors, and legacy remain unavailable.
- Remote CI at `2174dc2cfbc0e942c0842991650427b8a75ef91f` exposed Linux socket inode
  reuse in the restart readiness test. Commit
  `4ab916d27dacdccefdf1619120d3b323da5dc707` replaced inode observation with a
  bounded real status-protocol probe; 20 consecutive local restart cycles and
  all exact-head PR checks passed.
- A fresh review of `4ab916d27dacdccefdf1619120d3b323da5dc707` returned
  `NOT_READY`, P0/P1/P2 `0/0/4`. It found incomplete artifact membership,
  missing installed-wheel engine tests, stale wave evidence, and one incorrect
  manifest path in the review packet. The three repository findings are fixed
  at source checkpoint `a82485dd699ca7cd56c86d1904da7487fa1f87c1`; the next
  review packet names the sole canonical manifest correctly.
- Exact-head CI at evidence commit
  `974631e21e0a71a97b7269c69757e268687a5a63` then exposed uv's default Linux
  hardlink install mode: packaged Portable fixtures shared an inode with uv's
  cache and correctly failed the unique-regular-file invariant. Commit
  `181f9ae3438955d23dd39a155b7f23e9b93aa2f6` forces copied product installs.
  The 11 focused contracts passed in a metadata-free Linux Python 3.12
  container before the full local gate.
- Repaired candidate `7266e494f431ac84d9635b1a044bd15ce303c731` is pushed.
  CI run `33584116919`, public-artifacts run `33584116923`, and CodeQL run
  `33584115528` passed at that exact SHA, including Phase 4 contracts and
  Python 3.12, 3.13, and 3.14 verification.
- Fresh read-only child 4 reviewed the full P4-W1 range at `7266e494` and
  returned `NOT_READY`, P0/P1/P2 `0/0/1`. It independently resolved artifact
  membership, all moved engine tests under wheel isolation, and the canonical
  manifest path. Its sole P2 was that STATE, EVIDENCE, HANDOFF, and the
  dispatch ledger did not yet record the completed push, CI, and verdict; this
  evidence-only update is the bounded repair.
- Pinned local verification passed: 76 Phase 4 contracts, Ruff, strict MyPy on
  485 source files, canonical manifest validation, all 3,139 repository tests,
  isolated wheel/sdist build, exact artifact policy, source/artifact and
  reachable-history audits, Gitleaks over 59 commits, lock check, and diff
  integrity.
- Rebuilt artifact SHA-256 digests are
  `83e534799359e8ccb7ac8e90dfd5114564c0638428c5f50ca897a26f495cc682`
  for the wheel and
  `d62df80976117ff76ccbbb3a753b8765e7cb9f3c2404b273ef27e3a18e418c26`
  for the sdist.
- Evidence successor `aab6f1e0891d551a87a2968ef9fb0dae1a8e62e2` passed every
  exact-head required check. A fresh read-only rereview returned `READY`,
  P0/P1/P2 `0/0/0`; P4-W1 is complete.
- No package publication, tag, release, deployment, production access, private
  state access, or cutover action occurred.

## P4-W2 complete

- All 34 app runtime files, 28 existing app test modules, two supervisor
  resources, and one explicit wheel-gate test module are at their manifest
  destinations under `packages/app`.
- `open-brain` depends on exactly `open-brain-engine==0.1.0`. Installed scripts
  bind `open-brain` and `open-brain-mcp` to app-owned appliance entry points;
  HTTP remains an app-owned callable rather than a third installed script.
- Installed supervisor rendering loads package resources and emits no checkout
  `PYTHONPATH` or working directory. Source-checkout rendering remains explicit.
- The app isolation harness builds app and engine with workspace sources
  disabled, installs only those two wheels into the product environment, and
  runs 400 app tests through a separate locked test environment. Connector,
  legacy, workspace source, and private engine imports remain unavailable.
- Named wheel-only tests prove `V0-GATE-07` through independent CLI/UI sibling
  approve, reject, and safe edit, and `V0-GATE-13` through create, rename,
  later route with stable capture identity, and scoped/all-space retrieval.
- One policy verifies four Python artifact coordinates: app/engine crossed
  with wheel/sdist. Exact members derive from the canonical manifest; all four
  real archives pass.
- The phased root suite uses a test-only namespace overlay for unmoved
  `cli`, `integrations`, and `services` modules. Installed-wheel acceptance
  proves the overlay is absent from the app product; P4-W4 removes it.
- First-repair local gates passed: `make verify` with strict MyPy on 488 files, 3,148
  tests, and four artifact builds; uncached Ruff; `make phase4-contracts`
  with 79 tests;
  isolated app-wheel journeys on Python 3.12, 3.13, and 3.14; source/artifact
  and reachable-history audits; Gitleaks over 66 commits; lockfile and diff
  integrity.
- Child 6 reviewed exact candidate
  `85428b125ddd370cfa6a6c2be20f2aab4669bf7e` and returned `NOT_READY`,
  P0/P1/P2 `0/3/1`. It found unreachable installed supervisor mode, a
  hard-coded Python 3.12 isolation interpreter, incomplete app import
  enforcement, and stale completion evidence.
- Three focused regressions failed before repair. Source checkpoint
  `7d87c3968e15de5d98f7e509c8c8a31c4c5b500c` validates source-checkout
  detection, runs wheel isolation on the active matrix interpreter, rejects
  private/undeclared/unreviewed imports, and reports bounded failure stages.
- Exact-head CI run `33591952670`, public-artifacts run `33591952648`,
  and CodeQL run `33591951436` passed at `7d87c39`. PR head, remote branch,
  and local head matched; the draft PR was mergeable.
- Evidence checkpoint `579b45bc12247d34ed612798fc2a6021ccc1d30a`
  recorded the first review and repair. Child 7 reviewed that exact candidate
  and returned `NOT_READY`, P0/P1/P2 `0/1/0`. The sole P1 found that dynamic
  import checks could be bypassed through `builtins`, assignment aliases, and
  reflective access, while a shadowed local `importlib` object was falsely
  flagged.
- Focused regressions reproduced those bypasses and the false positive. A
  self-review added the related unresolved-capability escape inside the one
  reviewed dynamic-import file before the final repair.
- Source repair `e27ac3c3ad7daa4748b094490fdadab6e66a3773`
  tracks lexical import provenance, binds the one reviewed variable import to
  its exact artifact path and function, rejects all other capability uses,
  and leaves unrelated local objects clean.
- Final local gates passed: `make verify` with strict MyPy on 488 files, 3,149
  tests, and four artifact builds; uncached Ruff; `make phase4-contracts`
  with 80 tests; isolated app-wheel journeys on Python 3.12, 3.13, and 3.14;
  source/artifact and reachable-history audits; Gitleaks over 69 commits;
  lockfile and diff integrity.
- Exact-head CI run `33594652359`, public-artifacts run `33594652371`, and
  CodeQL run `33594650509` passed at `e27ac3c`. PR head, remote branch, and
  local head matched; the draft PR was mergeable.
- Evidence checkpoint `0a67a26210b79d0c91fa8953846560db34903034`
  recorded the second review and repair. Child 8 reviewed that exact candidate
  and returned `NOT_READY`, P0/P1/P2 `0/1/1`. The P1 found an importer escape
  through `sys.modules` and related runtime namespace access. The P2 corrected
  the installed app-suite evidence from 402 tests to its exact 400-test scope.
- One focused artifact regression failed before repair across `sys.modules`,
  `globals`, `locals`, `vars`, `eval`, `exec`, and an assigned alias. It also
  proved a shadowed local `globals` callable remains unrelated.
- Source repair `d8e2cb20f0268e16ebdd5b46053d5081dab7ac7c`
  rejects those reflective importer paths, preserves ordinary `sys` and
  `getattr` use, records the private engine target reached through
  `sys.modules`, and corrects the app-suite count to 400.
- Latest local gates passed: `make verify` with strict MyPy on 488 files,
  3,150 tests, and four artifact builds; uncached Ruff;
  `make phase4-contracts` with 81 tests; exact collection of 400 app tests;
  isolated app-wheel journeys on Python 3.12, 3.13, and 3.14; source/artifact
  and reachable-history audits; Gitleaks over 71 commits; lockfile and diff
  integrity.
- Exact-head CI run `33596668616`, public-artifacts run `33596668630`, and
  CodeQL run `33596666546` passed at `d8e2cb2`. PR head, remote branch, and
  local head matched; the draft PR was mergeable.
- Evidence checkpoint `e483973e7f8153b97aa5a44b049d787d08fd884f`
  recorded the third review and repair. Child 9 reviewed that exact candidate
  and returned `NOT_READY`, P0/P1/P2 `0/1/0`. The sole P1 found bounded
  reflection paths through `sys.__dict__`, `sys.__getattribute__`, and
  function `__globals__`, plus path-insensitive rebinding and false positives
  for loop, `with`, and exception targets.
- A focused artifact regression failed before repair on every positive and
  negative case. Self-review added bound function and `object.__getattribute__`
  paths before broad verification.
- Source repair `bd994a0288f8711f216e130c25c45f7a654eb90f`
  tracks provenance sets, joins branch outcomes, models lexical and compound
  bindings, recognizes namespace and function reflection, and keeps shadowed
  targets clean.
- Current local gates passed: `make verify` with strict MyPy on 488 files,
  3,151 tests, and four artifact builds; uncached Ruff;
  `make phase4-contracts` with 82 tests; the complete analyzer test file with
  11 tests on each of Python 3.12, 3.13, and 3.14; isolated app-wheel journeys
  on all three versions; source/artifact and reachable-history audits;
  Gitleaks over 73 commits; lockfile and diff integrity.
- Exact-head CI run `33600292963`, public-artifacts run `33600292877`, and
  CodeQL run `33600287985` passed at `bd994a0`. PR head, remote branch, and
  local head matched; the draft PR was mergeable.
- Evidence checkpoint `0cff2ea6077b2450febd07f456310aff1f6ffd25`
  recorded the fourth review and repair. Child 10 reviewed that exact
  candidate and returned `NOT_READY`, P0/P1/P2 `0/2/0`. The P1 findings were
  missing `ImportFrom` provenance for modeled module members and incorrect
  PEP 572 scope for assignment expressions inside comprehensions.
- Focused wheel regressions failed before repair for aliases of
  `sys.__dict__`, `sys.__getattribute__`, and `builtins.object`, plus an
  enclosing-scope comprehension assignment and its shadow-negative case.
  Self-review added symmetric `importlib` and `builtins` aliases and normal
  walrus overwrite behavior.
- Source repair `e103255b2ab03c3312206383b71ce38fcde67b8e`
  derives `ImportFrom` provenance uniformly from modeled module members and
  binds comprehension assignment expressions to the nearest enclosing scope
  without changing ordinary assignment-expression semantics.
- Latest local gates passed: `make verify` with strict MyPy on 488 files,
  3,151 tests, and four artifact builds; uncached Ruff;
  `make phase4-contracts` with 82 tests; all 14 analyzer and app-wheel tests on
  each of Python 3.12, 3.13, and 3.14; source/artifact and reachable-history
  audits; Gitleaks over 75 commits; lockfile and diff integrity.
- Exact-head CI run `33602946392`, public-artifacts run `33602946514`, and
  CodeQL run `33602944303` passed at `e103255`. PR head, remote branch, and
  local head matched; the draft PR was mergeable.
- Evidence checkpoint `995bd781869901e772eee0c7fdfd0ab8132065d5`
  recorded the fifth review and repair. Child 11 reviewed that exact candidate
  and returned `NOT_READY`, P0/P1/P2 `0/2/0`. The P1 findings were equivalent
  loader and reflection spellings with lost authority, and an allow-listed
  dynamic-import argument whose name survived unsafe reassignment.
- Two focused artifact regressions failed before repair with zero findings.
  They cover `importlib.__init__`, `pkgutil.resolve_name`, aliased
  `importlib.util`, frame namespaces, function-type reflection, direct and
  branch parameter replacement, and a second-parameter substitution.
- Source repair `559690e14b9a1dd935566b54e97ec8f2b73f8d06`
  models those authorities by semantics, distinguishes pristine parameters
  from unknown values through control-flow joins, preserves safe
  `importlib.metadata` and `importlib.resources` use, and rejects internal
  package roots at the optional-provider runtime boundary.
- Latest local gates passed: `make verify` with strict MyPy on 488 files,
  3,157 tests, and four artifact builds; uncached Ruff;
  `make phase4-contracts` with 84 tests; exact collection of 404 app tests;
  all 16 analyzer and app-wheel tests on each of Python 3.12, 3.13, and 3.14;
  source/artifact and reachable-history audits; Gitleaks over 77 commits;
  lockfile and diff integrity.
- Exact-head CI run `33608085686`, public-artifacts run `33608085774`, and
  CodeQL run `33608082590` passed at `559690e`. PR head, remote branch, and
  local head matched; the draft PR was mergeable.
- Evidence checkpoint `ce909a525dbc66ec5d893892984f2814f7bb9e71`
  passed CI run `33609157287`, public-artifacts run `33609157406`, and CodeQL
  run `33609152877`. Child 12 began reviewing that candidate, then stopped
  without a verdict when the source architecture was superseded.
- Source repair `9ca31ba36c44e4e4a269e5c932fae27fa174831e`
  replaces arbitrary module strings with a closed `OptionalProvider` enum and
  immutable lazy-loader registry containing the one declared `openai` extra.
  Internal roots are unrepresentable through the typed API and rejected again
  at runtime. P4H009 now has no dynamic-import exception and is explicitly a
  finite architecture regression corpus rather than a Python sandbox.
- Latest local gates passed: `make verify` with strict MyPy on 488 files,
  3,157 tests, and four artifact builds; uncached Ruff;
  `make phase4-contracts` with 84 tests; exact collection of 404 app tests;
  all 16 analyzer and app-wheel tests on each of Python 3.12, 3.13, and 3.14;
  source/artifact and reachable-history audits; Gitleaks over 79 commits;
  lockfile and diff integrity.
- Exact-head CI run `33611279850`, public-artifacts run `33611279794`, and
  CodeQL run `33611274401` passed at `9ca31ba`. PR head, remote branch, and
  local head matched; the draft PR was mergeable.
- Evidence checkpoint `ee2f8c22ac1bd00fd6a3d2924071b8ff23a32238`
  passed CI run `33611917553`, public-artifacts run `33611917534`, and CodeQL
  run `33611914545`. Child 12 resumed against source `9ca31ba` and this
  docs-only evidence successor, then returned `NOT_READY`, P0/P1/P2 `0/0/1`.
  The P2 was a stale dynamic-import review in the canonical manifest that the
  normal architecture gate hid by filtering moved source records first.
- Source repair `30d49d31d35f86e26be3c0ac99b884a47d76b5f6`
  removes the stale generic-loader review, validates the canonical review
  inventory against every current `open_brain` source location, and adds a
  moved-source regression. The legacy source projection no longer rewrites
  review evidence.
- Latest local gates passed: `make verify` with strict MyPy on 488 files,
  3,158 tests, and four artifact builds; uncached Ruff;
  `make phase4-contracts` with 85 tests; exact collection of 404 app tests;
  all 16 analyzer and app-wheel tests on each of Python 3.12, 3.13, and 3.14;
  source/artifact and reachable-history audits; Gitleaks 8.30.1 over 81
  commits; lockfile and diff integrity.
- Exact-head CI run `33614647926`, public-artifacts run `33614647755`, and
  CodeQL run `33614643945` passed at `30d49d3`. PR head, remote branch, and
  local head matched; the draft PR remained open.
- Docs-only evidence checkpoint
  `e20be9debc6cd68cd443a3df00f6c2cd76041cb3` passed CI run `33615635189`,
  public-artifacts run `33615635198`, and CodeQL run `33615631031`.
- Child 12's same-lineage rereview was bound to source `30d49d3` and evidence
  `e20be9d`. It returned `READY`, P0/P1/P2 `0/0/0`, explicitly closed the
  prior P2, and declared P4-W2 ready for milestone closure.
- P4-W2 is complete. No P4-W3 implementation or review child was started; its
  reviewer budget resets at the next milestone boundary.
- Independent review is bound to the source SHA. A docs-only evidence
  successor does not invalidate an unchanged source review. Child budgets are
  scoped to and reset at each milestone.
- Rebuilt SHA-256 digests are app wheel
  `cfee79bfe16adc1dc09ffd930db3a49763028b44bd0eb8c85d3403ea6e17b43e`,
  app sdist
  `fc6b43252d5842fdd470236c07a4bbd9a96d1c55450e81c77e922d0d4e20f68d`,
  engine wheel
  `83e534799359e8ccb7ac8e90dfd5114564c0638428c5f50ca897a26f495cc682`,
  and engine sdist
  `a52e659e44d4dcfc882f76d998540a41d2abffa7886a83db365cb688c914084a`.
- No publication, tag, release, native build, deployment, private-state access,
  production access, or cutover action occurred.

## P4-W3 local source candidate

- All five connector-owned runtime files and their three owned tests are in the
  buildable `open-brain-connectors` package. Nine connector runtime records are
  moved; the workspace inventory is 232 runtime files and 355 Phase 4 subjects,
  including 260 tests.
- Published app extension values remain provisional v1. The connector package
  depends on exact app and engine versions; the app has no connector dependency
  and defaults to an empty connector profile.
- The bounded worker loads the explicitly enabled entry point only in its child,
  applies capability, budget, process, memory, time, output, network, receipt,
  and replay checks, and runs real YouTube reference conformance without
  production/private state.
- Latest local gates passed: `make phase4-contracts` with 87 tests; `make
  verify` with Ruff, strict MyPy on 500 files, 3,170 tests, and six artifact
  builds/policy; connector wheel acceptance with 3 tests on each of Python
  3.12, 3.13, and 3.14; source/artifact and reachable-history audits; Gitleaks
  8.30.1 over 83 commits; actionlint; lockfile and diff integrity.
- Three new durable gotchas record canonical child bootstrap identity, singular
  artifact coordinates, and parent-bound receipt budgets. No package
  publication, tag, release, native build, deployment, private-state access,
  production access, cutover, or P4-W4 work occurred.
- First exact-head CI run `33627007197` at `fe579e4` passed every existing CI
  job but failed all three new connector jobs during setup because of a
  one-character copied action-pin drift. Release audit run `33627007170`
  passed. The repair makes all four `setup-uv` pins identical, adds a regression,
  and passes focused checks, actionlint, `make phase4-contracts`, and `make
  verify` locally. `CI-002` records that actionlint cannot validate remote pins.
- Repair checkpoint `2c05dea` passed exact-head CI `33627525857`, Release audit
  `33627525864`, and CodeQL `33627520718`. All three connector-isolation jobs,
  all three full verify jobs, Phase 4 contracts, macOS appliance checks, and
  public artifacts are green. PR #6 is open, draft, mergeable, and bound to the
  same source SHA.
- Child 13 (`01a06202-4d7a-7331-a038-239a6e93a630`) returned `NOT_READY`,
  P0/P1/P2 `0/1/0`. `P4W3-001` found that the default app registry could load
  the installed connector entry point and call `run()` outside the bounded
  child.
- The repair separates the installed public metadata group from the injected
  internal compatibility group, rejects installed registry resolution, removes
  `run()` from the published entry-point object, and adds a wheel-only parent
  non-load regression. All 72 focused tests, 87 Phase 4 tests, and 3,171 full
  tests pass with Ruff, strict MyPy, six artifact builds/policy, the supported
  Python wheel matrix, release/history/Gitleaks audits, actionlint, lockfile,
  and diff integrity. Repair checkpoint `27344c6` passed exact-head CI
  `33629757827`, Release audit `33629757695`, and CodeQL `33629754431`.
- Child 13's same-lineage rereview returned `READY`, P0/P1/P2 `0/0/0`,
  reproduced the parent non-load proof, and explicitly closed `P4W3-001`.
  Reviewer budget is 0 active and 1 total; the P4-W3 lineage is closed.
- P4-W3 is complete. P4-W4 did not start. No package publication, tag, release,
  native build, deployment, production/private access, or cutover occurred.

## P4-W4 launch grounding

- Fresh fetch proved local and remote branch head
  `18dd32b719935d8e283b54bf2599b4981afe242a`; the worktree was clean and
  `origin/main` remained at the reviewed baseline.
- The recorded Phase 4 plan SHA-256 matched exactly. Goal #63 remained open and
  explicitly launched. Draft PR #6 remained mergeable, and all 12 current
  CI/Release/CodeQL checks passed at the exact starting head.
- The sole canonical manifest validated. Its unresolved P4-W4 inventory is
  exactly 135 legacy runtime files, four workspace runtime files, 160 legacy
  tests, two legacy fixtures, and one legacy test resource. No manifest
  ambiguity was observed.
- The focused pre-move manifest, rewrite, and architecture baseline passed 66
  tests. Durable context search returned no relevant Phase 4 record, so the
  canonical plan, manifest, project gotchas, and prior workstream decisions
  remain the operative context.
- Reviewer budget reset to zero active and zero total for this milestone. One
  coordinator retains exclusive write ownership; review remains read-only and
  starts only after exact-candidate CI is green.

## P4-W4 local movement and repair

- Governance launch is committed at `a94fed1`; the behavior-neutral canonical
  move is committed separately at `818caf6`.
- All 302 planned legacy and workspace paths are at their final locations. The
  old `src/open_brain` tree is absent, and all runtime and non-runtime movement
  states are `moved`.
- The deterministic rewrite now converts relative imports that cross a moved
  namespace. The workspace tools use the single `tools.open_brain_dev`
  identity, and strict MyPy no longer sees duplicate module names.
- The move exposed undeclared app and connector imports in the private legacy
  package. A red regression derived the actual `{app, connectors, engine}`
  edge set. Exact package metadata and the canonical graph now agree with that
  source-derived set while all shipping dependencies continue to point away
  from legacy.
- Repair checkpoint `678c61cab75f8b90ac64aa04ac8d06bac8ee220f`
  passes 233 focused tests, all 3,173 repository tests, `make
  phase4-contracts`, `make verify`, all source-free builds and artifact policy,
  and the 76-test isolation matrix on Python 3.12, 3.13, and 3.14.
- Source/artifact and reachable-history audits, Gitleaks 8.30.1 over 90
  commits, actionlint, lockfile integrity, and diff integrity pass. Exact-head
  CI `33642529540`, Release audit `33642529483`, and CodeQL `33642524132` are
  green at `7927162096330355520f2de756aa45b48ccb6493`; local, remote, and PR
  source match. Child 14 is reserved for the independent final review, and the
  reviewer budget is one active and one total.
- Child 14 returned `NOT_READY`, P0/P1/P2 `0/1/1`. P4-W5 remains blocked.
  The reviewer lineage is closed while repairs run and will be resumed only
  after a new exact candidate passes all local and remote gates.
- D-040 is superseded. The canonical graph, legacy package metadata, lockfile,
  source imports, and private wheel metadata now require only
  `open-brain-engine==0.1.0`. Thirty-four app/connector support modules needed
  only by predecessor execution are quarantined under
  `open_brain_legacy._compat`; no shipping artifact contains that namespace.
- The clean-room legacy contract installs only the engine and legacy wheels,
  imports every packaged legacy module, and proves `open_brain` and
  `open_brain_connectors` remain unavailable. The complete 2,147-test legacy
  suite also passes.
- `CLAUDE.md` names the canonical artifact-policy path. The reusable readiness
  preflight covers signing, notarization, macOS ARM64, Linux x86_64, disk
  capacity, and recovery access. Its public snapshot contains only booleans and
  validated opaque receipts and can be restored for P4-W5 through P4-W9 without
  rerunning any probe.
- Current local repair gates pass: 98 Phase 4/architecture tests, Ruff, strict
  MyPy on 536 files, manifest validation, all 3,182 repository tests, six
  source-free shipping artifacts, artifact policy, lockfile integrity, and
  diff integrity. Exact-commit audits and remote checks remain pending.
- Repair checkpoint `b86beacbc7005b0a7f2ceeb5c009f0a2849579a6`
  passed exact-head CI `33650699793`, Release audit `33650699895`, and CodeQL
  `33650693028`; local, remote, and PR source matched. Child 14's same-lineage
  rereview returned `NOT_READY`, P0/P1/P2 `0/1/1`. The prior `P4A-001` and
  `P4A-002` findings are closed; new `P4A-003` found one ambient `openai`
  import in `_compat`, and `P4A-004` proved that a readiness probe's
  `SystemExit` escaped with raw detail.
- Both new findings failed focused regressions before repair. Legacy wheel
  inspection now rejects every static or dynamic `_compat` import outside
  stdlib, engine, and legacy. Optional provider metadata accepts only an
  explicit injected loader, and the engine-plus-legacy clean room exercises
  that enabled path while the OpenAI SDK remains absent.
- Readiness observation now treats `SystemExit` as an unavailable probe and
  retains the same boolean-and-opaque-receipt projection. `KeyboardInterrupt`
  remains interruptible. The sensitive-canary regression covers both ordinary
  exceptions and CLI-style exits.
- Current second-repair gates pass: 10 focused tests, `make
  phase4-contracts` with 100 tests, Ruff, strict MyPy on 536 files, manifest
  validation, `make verify` with all 3,184 repository tests and six shipping
  artifacts, and the 100-test P4A matrix on Python 3.12, 3.13, and 3.14.
  Exact-commit audits and remote checks remain pending for the next candidate.
- Exact candidate `9098ff5e676a76ef1637f30dee99ff6508d46a30`
  passed source plus six-artifact release audit, reachable-history audit,
  Gitleaks over 93 commits, actionlint, lock, manifest, diff, and clean-tree
  checks. Private legacy SHA-256 values are
  `a10330f320439eb7fc240f663a59ed1422c44db204274c140de036e6aa47985c`
  for the wheel and
  `8ad90592be262c42d7312d2da0adbece4c5d0823d555d08fb4795fa2b977538e`
  for the sdist.
- Exact-head CI `33654478123`, Release audit `33654478071`, and CodeQL
  `33654473793` passed; all 12 PR #6 jobs passed and local, remote, and PR
  source identities matched.
- Child 14's second same-lineage rereview returned `READY`, P0/P1/P2
  `0/0/0`, at exact source `9098ff5`. It independently reproduced the focused
  regressions, built private artifacts with matching hashes, closed
  `P4A-001` through `P4A-004`, and found no new issue. The lineage is closed.
- P4-W4 and the aggregate P4A gate are complete. P4-W5 did not start. No
  publication, tag, release, native build, deployment, private-state access,
  production access, or cutover action occurred. The reusable readiness
  preflight is ready to run once at P4-W5 and reuse unchanged through P4-W9.

## P4-W5 independent review repair

- Child 15 (`01a0636c-3ef9-7823-ad08-d8dcdb5a48d9`) owns the sole P4-W5
  reviewer lineage. At exact source `28f1fa7` it returned `NOT_READY`,
  P0/P1/P2 `0/3/1`. At exact source `47aee48` it returned `NOT_READY`,
  P0/P1/P2 `0/4/0`, while accepting D-050 as a valid clarification.
- `P4W5-002`, `P4W5-003`, `P4W5-005`, and `P4W5-006` are closed.
  `P4W5-001`, `P4W5-004`, and `P4W5-007` remained open, and `P4W5-008`
  was new. The lineage is closed during repair and must be resumed after a new
  exact candidate passes all local and remote gates.
- Red-first tests reproduced all four remaining classes. The repaired
  supervisor protocol unloads and resumes KeepAlive jobs, inventory bootstrap
  trusts only an explicit current link, resource membership is exact and
  Git-derived, and PyInstaller builds from an isolated archive of the named
  commit with before/after source-tree digests.
- All six finding-specific tests and `make p4w5-focused` with 108 tests pass.
  No broad suite, native build, workflow, or readiness probe reran during the
  edit loop. Candidate preflight is the next gate.
- Candidate `e55d7488a60a98f2bf5f06cebc18f8fe485e169f` passed the complete
  local and exact-head remote ladder on the first attempt. Child 15's
  same-lineage rereview returned `NOT_READY`, P0/P1/P2 `0/2/0`, accepted
  D-050/D-051, and left only `P4W5-004` and `P4W5-008` open.
- Four focused cases were red: `.envrc`, `.environment`, a replacement ref,
  and a tracked archive exclusion. The repair denies every `.env*` component,
  rejects repository-local Git overrides, disables replacements and external
  configuration, and matches the extracted archive against the raw Git blob
  IDs and executable modes. The complete eight-case set and 115 focused tests
  pass. Candidate preflight is the next gate; no broad or native rerun occurred
  in this edit loop.

## P4-W5 complete

- Exact source `c7c4fad1b109ac7d7c55d55cdfa57b64a9c910db` passed one
  115-test candidate preflight, 119 Phase 4/security tests, all 3,221
  repository tests, source-free distribution policy, source/artifact/history
  audits, Gitleaks over 104 commits, and the local macOS ARM64 native matrix.
- Exact-head CI `33684763227`, Release audit `33684763223`, and CodeQL
  `33684759266` passed on attempt one. Literal Ubuntu 24.04 and macOS 14 ARM64
  native jobs passed with the artifact/member digests recorded in
  `EVIDENCE.md`; PR #6 had 14 of 14 green checks.
- Child 15's final same-lineage review returned `READY`, P0/P1/P2 `0/0/0`.
  D-050 and D-051 are accepted, `P4W5-001` through `P4W5-008` are closed,
  and no finding remains. The lineage is closed.
- The readiness snapshot still hashes to
  `753a1635aa2be81f3ebe6b3723dbc8e46a6d6aa46f1b39003b9d9c39da769d1b`;
  no probe reran. P4-W6 remains unstarted and blocked by false notarization
  readiness. False recovery readiness remains a later gate blocker.
- This D-031/D-048 closure changes evidence documents only. Accepted source,
  tests, policy, workflows, and native artifacts remain those reviewed at
  `c7c4fad`.

## P4-W6 complete

- Private notarization readiness is owner-validated. Bounded coordinator checks confirm one
  Developer ID Application identity and the required third-party license inputs without exposing
  private metadata.
- The source implementation now has separate P4-W6 build, signing, deterministic packaging,
  clean-host, manifest, SBOM, license, Make, and CI surfaces. P4-W5 native defaults and accepted
  evidence remain unchanged; no P4-W5 target or readiness probe reran.
- The black-box lifecycle harness passed once against a disposable package made from accepted
  native bytes. The implemented final assembler requires all 23 coordinates and both the bounded
  exact-signed macOS 14 runner limitation and a passed source-equivalent macOS 14 result.
- Initial source `b99e619` exposed one exact-build-only `uv` `.gitignore` marker. The bounded fix
  removes only the exact generated byte and rejects every other output residue.
- Source `69a9596` built the six exact Python artifacts. Apple accepted its macOS submission, but
  the parser correctly stopped before stapling on the previously unmodeled zero-issue `null`
  representation. Both accepted zero-issue shapes are now explicit and tested.
- Source `3439fe6` then produced six Python artifacts plus a signed, accepted, stapled, validated,
  Gatekeeper-approved DMG whose full artifact-only lifecycle passed in 19 seconds. It is superseded
  because the Make recipe echoed local absolute paths before the bounded result; command echo is
  now suppressed and covered.
- Source `dda22d7` passed fresh local Python and signed macOS artifact construction plus the
  path-silent macOS lifecycle. Its exact-head audit, Linux build, and source-equivalent macOS 14
  lifecycle passed, but all three Linux host jobs exposed GNU `cp` preserving `current` as a
  symlink. The controller now copies the validated enrolled directory explicitly; the downloaded
  exact archive passes the corrected full lifecycle in a local Ubuntu 24.04 x86_64 container.
- Source `25a785a` moved all three Linux hosts past that symlink defect but failed during
  prior-schema initialization. Diagnostic source `5e2bbb2` narrowed every native CI failure to
  `prior-schema-init`. Reproduction with a UID-1001 fixture proved that preservation of the host
  runner's ownership, rather than artifact or fixture content, caused the owner-only state check to
  fail. Fixture copying now preserves content and modes without preserving a foreign UID.
- The ownership regression passes the complete lifecycle on local Ubuntu 24.04 and Debian 13.
  Local Ubuntu 26.04 QEMU cannot extract the archive, while the required native Ubuntu 26.04 CI
  subject passes the exact lifecycle.
- Pre-commit verification is green: 28 focused P4-W6 tests, 10 release-policy tests, 147 Phase 4
  contracts, strict MyPy on 549 files, all 3,249 repository tests, six source-free artifacts,
  Actionlint, ShellCheck, Perl syntax, artifact policy, manifest validation, and diff integrity.
- Exact source `537bc4f1059ef4b4e8f0916702f38f4e531b13fe` passed CI
  `33714932363`, Release audit `33714932452`, and CodeQL `33714929770` on the first attempt. All
  20 PR checks are green, including Ubuntu 24.04, Ubuntu 26.04, Debian 13, and macOS 14 lifecycle
  evidence.
- The final exact-source DMG is Developer ID-signed with hardened runtime and secure timestamps,
  accepted by Apple with zero issues, stapled, validated, and Gatekeeper-approved. Its SHA-256 is
  `aa78303a1b1ac7b42215adada8d7932fe55114391292f622073b9aec825a95ac`; the exact signed
  macOS 26 lifecycle passed. The Linux archive SHA-256 is
  `aa84da386b70a8be826290d08342636de68ea275b47d9eaecbd74b456e5c72a2`.
- The assembled unpublished candidate contains exactly 23 coordinates and passes the standalone
  verifier. Child 18's same-lineage rereview returned `READY`, P0/P1/P2 `0/0/0`, closed
  `P4W6-AR-001`, and found no product issue. Child 17 returned no result before its timeout breaker.
- P4-W5 remains accepted at `c7c4fad1b109ac7d7c55d55cdfa57b64a9c910db`. No P4-W5 target or
  readiness probe reran, and the snapshot remains
  `753a1635aa2be81f3ebe6b3723dbc8e46a6d6aa46f1b39003b9d9c39da769d1b`.
- At P4-W6 closure, P4-W7 had not started. No artifact was published, tagged, released, deployed,
  or used for production/private-content access or cutover.

## P4-W7 exact-candidate audit and P4B complete

- The rendered P4-W7 contract limits this wave to audit, public-state checks,
  governed evidence, and one fresh read-only reviewer. It forbids product or
  artifact changes, push, merge, publication, deployment, and P4-W8.
- Current head `ab3f860e23abab2177341c9598011487eaf5ab2b` is a docs-only
  successor of accepted source
  `537bc4f1059ef4b4e8f0916702f38f4e531b13fe`. The P4-W6 handoff schema
  validates, and its accepted-source identity is reconciled through D-031 and
  D-053 rather than treated as the docs-only closure head.
- CI `33714932363`, Release audit `33714932452`, and CodeQL `33714929770`
  remain completed successfully at the exact accepted source. All 16 CI jobs
  and all 20 PR checks are green. The six retained CI bundles are unexpired.
- Fresh download and comparison proves that all six CI Python artifacts, the
  Linux archive and checksum, Linux build evidence, three Linux host records,
  and the macOS 14 source-equivalent record match the assembled candidate byte
  for byte where they are candidate coordinates.
- The standalone candidate verifier rehashed all 23 coordinates. Direct DMG
  integrity, signature, stapling, and Gatekeeper checks passed. Fresh unpacked
  audits reproduced the 111-member Linux and 144-member macOS membership and
  tree digests. Exact Git materialization reproduced source-tree digest
  `1fd73d629664e7284f2be4a9640be73982b5c7272b74ffaabcae0c5da74a2a11`.
- Fresh source-plus-six-artifact generic release audit, complete reachable
  history audit, Gitleaks 8.30.1 over 114 commits, artifact policy, SPDX,
  license, checksum, and residue checks passed. The earlier exact-candidate
  private-denylist audit remains valid because source and artifact bytes did
  not change; that private input is unavailable in this shell and was not
  reconstructed.
- Public state reports zero tags, releases, release assets, and deployments.
  All three expected PyPI project endpoints and 15 expected public GitHub
  package coordinates return not found. Six authenticated Actions artifacts
  remain CI evidence, not release assets or public package downloads.
- Children 19 through 21 failed at the shared response endpoint before review.
  After the explicit Codex CLI 0.153.0 update and a healthy Doctor check,
  child 22 completed the fresh review. Its initial P1 signature finding was
  isolated to the CLI's macOS read-only sandbox: the coordinator and the same
  reviewer lineage reproduced relative, absolute, and deep strict signature
  passes against unchanged DMG bytes outside that sandbox. The reviewer closed
  `P4W7-001`, completed the skipped live checks, and returned final `READY`,
  P0/P1/P2 `0/0/0`, with no file change or concern.
- Child 22 accepted D-054's CI rebuild boundary, retained private-denylist
  boundary, and direct publication-absence boundary. Every P4A/P4B completion
  criterion is supported. P4-W7 and aggregate P4B are complete.
- No artifact was rebuilt, re-signed, published, tagged, released, deployed,
  or used for production/private-state access or cutover. P4-W5 and readiness
  were not rerun. P4C and P4-W8 remain unstarted.

## P4-W8 launch and merge gate

- PR #6 was retitled to the completed P4A/P4B scope, marked ready, and
  squash-merged only after all 20 exact-head checks remained successful.
  Merged commit `10769806361e7ed419b653534e1303b623d84673` and reviewed
  head `7e269219b340335f8567ca7bc13f07b1ee9323f0` both resolve to tree
  `5c98b73fe854dd05406bf7992174cfbe20092eb9`.
- The remote P4A/P4B branch was deleted by the normal merge workflow. New
  local branch `goal/open-brain-phase4-p4c` starts exactly at
  `origin/main`; the prior local branch remains intact.
- Fresh post-merge queries found zero tags, GitHub releases, and deployments.
  The exact candidate verifier still returns `valid`, source `537bc4f`,
  and 23 artifacts. The plan, readiness snapshot, and P4-W5 prompt still hash
  to their recorded values.
- The rendered P4-W8 contract requires a new independently reviewed Goal #63
  transaction. Historical Goal #24 helper/recovery assets are static
  architecture references only; its completed production apply and cleanup
  remain permanently forbidden.
- P4-W8 has not yet touched live state. Children 23 and 24 were both closed
  without verdict after bounded CLI timeouts. Child 25 completed three passes
  through the distinct in-app runtime and returned final architecture `READY`,
  P0/P1/P2 `0/0/0`; its slot is released. Red-first private implementation is
  eligible, while live work and P4-W9 remain prohibited.

## P4-W8 pre-execution READY and fresh-inventory stop

- The private controller contains 18 source modules, seven schemas, and 20
  tests. Pytest, Ruff, strict MyPy, and all seven schema checks passed. Child
  28's fifth bounded pass closed IR-001 through IR-008 and returned final
  pre-execution `READY`, P0/P1/P2 `0/0/0`.
- The exact 23-coordinate P4B candidate was transferred owner-only and staged
  on the canonical macOS ARM64 host. All 16 stage coordinates validated; the
  accepted source and protected candidate bytes remained exact.
- Fresh read-only writer/service inventory stopped before the exact-profile
  probe. It found one unexpected loaded registration with a known Open Brain
  job-class identity. No process was running, but configuration discovery
  proved five roots with three overlaps against the governed production root
  set. An inactive loaded registration can still gain authority, so adding its
  digest to an allowlist or treating it as unrelated would weaken the closed
  writer-map guarantee.
- The bounded private blocker receipt hashes to
  `3af46ad50b9e61207e080ce203f887c744dc82fdc7043e955852cac5d036b9f8`.
  No backup-access repair, backup, restore, candidate launch, rehearsal,
  postflight, production service/content mutation, publication, deployment,
  irreversible cleanup, or P4-W9 action occurred.

## P4-W8 separate reconciliation result

- Exact retained provenance proves the loaded registration was deliberately
  installed as the canonical scheduled writer, stabilized to an empty queue,
  and restored in two prior governed writer windows. Process absence between
  calendar runs does not remove its authority. Removal or quarantine is
  rejected.
- The two configurations have seven unique roots and two separate writer
  leases. Three roots match exactly. The two divergent role pairs contain
  materially different inventories, so neither can stand in for the other.
- Helper v18's immutable full profile covers exactly five candidate-generation
  roots. A five-root encrypted snapshot cannot recover the complete seven-root
  generation, and one held lease cannot fence both writers.
- Independent reconciliation Child 1 returned `NOT_READY`, P0/P1/P2 `3/0/0`.
  The minimum correct architecture is a two-generation manifest, deterministic
  two-lease fence, exact seven-root encrypted backup/restore, generation-qualified
  disposable staging, seven-root isolation, and exact two-generation rollback.
- Continuing requires explicit authority to build and install a versioned
  seven-root helper successor and use it for one encrypted snapshot in each
  existing repository plus two disposable restores. Helper v18 remains an
  unchanged rollback asset. Service state, production content, publication,
  deployment, P4-W9, and irreversible cleanup remain excluded.

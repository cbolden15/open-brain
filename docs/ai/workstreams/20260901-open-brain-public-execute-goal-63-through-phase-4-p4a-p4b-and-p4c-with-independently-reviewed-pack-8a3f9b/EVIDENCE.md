# Phase 4 execution evidence

## Gate 0 launch

- Explicit user launch received on 2026-09-01.
- Runtime goal registered for Goal #63.
- Fresh fetch confirmed `origin/main` at
  `3b89a4ba4787a378e6040ff042bd117da881918d`.
- Exact reviewed plan SHA-256 confirmed as
  `7fe6e5d1e48b44fb4fba232661a8b01eeca019e4f35a43f1b2c162914905bfd2`.
- New branch `goal/open-brain-phase4` created from that base; reviewed planning
  commit cherry-picked as `68c34d723e8f0bbdc7a51d5c5070e63e994b335d`.
- New implementation workstream created; planning task IDs and state were not
  reused.
- Public required checks, current workflow hosts, open parent goals, local
  ARM64/Python/uv/signing tools, project gotchas, and latest private Goal #24
  handoff were inspected.
- No runtime source moved, no native artifact built, and no production or live
  Brain state was accessed during launch.

## Gate 0 baseline verification

- `make verify`: passed Ruff, strict MyPy on 474 source files, all 3,114 tests,
  isolated wheel/sdist builds, and artifact policy.
- Current source-ownership validator: 53 tests passed.
- Source plus wheel/sdist release audit: passed with one untracked synthetic
  denylist.
- Reachable-history audit: passed with the same denylist.
- Explicit artifact-policy rerun and `git diff --check`: passed.
- Inventory equality: all 224 tracked runtime Python files exactly match the
  canonical classification; owner counts are engine 46, app 34, connector 5,
  legacy 135, and workspace 4; temporary live debt is zero.
- Additional manifest subjects observed: 250 tracked Python files under
  `tests/` and 36 tracked schema/fixture files.
- Required public checks: strict linear `main`, `verify (3.12)`, `verify
  (3.13)`, `verify (3.14)`, and `public-artifacts`; force pushes and branch
  deletion disabled.
- Successful CI logs directly identify macOS 14 ARM64 and Ubuntu 24.04 x86_64
  builders.
- Local build host: ARM64, Python 3.12, `uv`, `notarytool`, and one valid
  Developer ID Application identity. A ready notary credential profile was not
  found on either checked authorized host.
- Private read-only preflight: exact scoped helper and host identity checks
  passed; arbitrary sudo and root SSH remained denied; service, writer,
  synchronization, executable-reference, and recovery-readiness inventories
  completed with zero mutations and no private output in this repository.
- Private recovery access is not currently ready and must be remediated and
  re-proved before P4-W8. The prior Goal #24 apply remains historical and will
  not be rerun.
- Launch status comments:
  `cbolden15/agent-config#63` comment `5502133708`, parent `#41` comment
  `5502134010`, and parent `#24` comment `5502134267`.

## P4-W0 local candidate

- Canonical schema: version 3 with Phase 4 schema version 1.
- Subject closure: 224 runtime plus 331 non-runtime equals 555 exact current
  subjects. Non-runtime kinds: 253 tests, 21 package resources, 21 fixtures,
  15 schemas, 10 release tools, 4 release resources, 3 generated reports, 3
  entry points, and 1 test resource.
- Generated evidence: exact move report, exact import-rewrite report, and one
  11-finding current-monolith expected-red report.
- Expected-red execution: exited 1 and exactly matched the committed bounded
  finding set (`P4E001` through `P4E005`). It is not a required green test.
- `make phase4-contracts`: 68 tests passed; Ruff passed; strict MyPy passed on
  481 source files; canonical validator returned zero findings.
- `make verify`: 3,129 tests passed; Ruff and strict MyPy passed; isolated
  wheel/sdist builds used Hatchling 1.32.0; artifact policy passed.
- Source plus built-artifact release audit, reachable-history audit, explicit
  artifact-policy rerun, Gitleaks over 51 commits, `uv lock --check`, and
  `git diff --check`: passed.
- Runtime movement assertion: no path below `src/open_brain` changed relative
  to the reviewed launch commit.
- Read-only child 1 implementation map: `READY`, P0/P1/P2 `0/2/2`. Both P1s
  were implemented: Phase 4 tools are under strict MyPy and public-artifact
  triggers cover the new manifest/tools/tests.
- Checkpoint commit: `ae144db9e461f9e369976c04b75467eb5c69a144`.
- Draft PR: `vora-technology/open-brain#6`.
- Exact-commit PR checks passed: protected `phase4-contracts`, `verify (3.12)`,
  `verify (3.13)`, `verify (3.14)`, `public-artifacts`, macOS ARM64 appliance
  lifecycle, CodeQL Actions analysis, and CodeQL Python analysis.
- Branch protection now requires `phase4-contracts` in addition to the prior
  four required checks.
- Sanitized Goal #63 checkpoint comment: `5502376856`.
- P4-W0 is complete. P4-W1 may begin; later gates remain blocked by their own
  source, artifact, review, recovery, and production preconditions.

## P4-W1 source checkpoint

- Workspace and engine movement completed in commits `c884d88`, `5f127c1`, and
  `2174dc2`. The four-member workspace is active, all 46 engine runtime files
  and Portable resources are moved, and the app/connector/legacy skeletons
  remain nonbuildable for their later waves.
- Remote Python 3.12, 3.13, and 3.14 jobs failed only because the source-checkout
  restart test waited for a changed Unix socket inode. Linux immediately reused
  that inode. Commit `4ab916d27dacdccefdf1619120d3b323da5dc707` now waits for
  a bounded status control-protocol response. Twenty consecutive local restart
  cycles passed, followed by all exact-head PR checks.
- Fresh read-only review of `4ab916d27dacdccefdf1619120d3b323da5dc707`
  returned `NOT_READY`, P0/P1/P2 `0/0/4`. The valid repository findings were:
  incomplete manifest-declared artifact membership, no execution of the moved
  engine tests from the installed wheel, and stale P4-W1 evidence. The fourth
  finding corrected the review packet from a nonexistent manifest path to
  `docs/v0-package-classification.json`; no second manifest was created.
- Red regressions proved the gaps: the acceptance test could not import an
  installed engine-test command, and artifact tests failed for absent manifest
  binding, accepted unclassified members, and incomplete derived membership.
- Source checkpoint `a82485dd699ca7cd56c86d1904da7487fa1f87c1` fixes all
  three repository findings. Exact artifact membership is derived from the
  canonical manifest. PEP 639 license files and declared release resources are
  packaged, and a bounded Hatch hook removes forced VCS exclusion-file residue
  from the sdist.
- The isolated engine gate now installs only the engine wheel into its product
  environment and executes all 20 moved engine test modules through a separate
  locked test-runner environment. Tests run from copied inputs with repository
  source and the app, connector, and legacy distributions unavailable.
- Exact-head CI at `974631e21e0a71a97b7269c69757e268687a5a63` failed only
  the installed-engine contract in `phase4-contracts` and Python 3.12, 3.13,
  and 3.14 verification. A clean Linux reproduction showed uv's default wheel
  install had hardlinked packaged Portable fixtures to its cache, so their
  link count correctly violated the unique-regular-file invariant.
- Commit `181f9ae3438955d23dd39a155b7f23e9b93aa2f6` makes the
  isolated product install use uv's copy mode. All 11 focused harness and
  engine-distribution tests passed in a metadata-free Linux Python 3.12
  container; no validator invariant was weakened.
- Candidate `7266e494f431ac84d9635b1a044bd15ce303c731` is pushed and
  clean. Exact-head CI run `33584116919`, public-artifacts run `33584116923`,
  and CodeQL run `33584115528` passed, including Phase 4 contracts and Python
  3.12, 3.13, and 3.14 verification.
- Fresh read-only child 4 reviewed all nine P4-W1 commits and 371 changed paths
  at `7266e494`. It returned `NOT_READY`, P0/P1/P2 `0/0/1`. Exact artifact
  membership, execution of all moved tests under wheel isolation, and the sole
  canonical manifest path are independently resolved. The only finding was
  stale completion evidence in STATE, EVIDENCE, HANDOFF, and the dispatch
  ledger; this bounded evidence-only update repairs that finding.
- Local Python 3.12 and uv 0.12.8 verification passed: 76 Phase 4 contracts,
  Ruff, strict MyPy on 485 files, manifest validation, 3,139 total tests,
  wheel/sdist build, exact artifact policy, source plus artifact release audit,
  reachable-history audit, Gitleaks over 59 commits, `uv lock --check`, and
  `git diff --check`.
- Artifact SHA-256 digests:
  wheel `83e534799359e8ccb7ac8e90dfd5114564c0638428c5f50ca897a26f495cc682`;
  sdist `d62df80976117ff76ccbbb3a753b8765e7cb9f3c2404b273ef27e3a18e418c26`.
- Evidence successor `aab6f1e0891d551a87a2968ef9fb0dae1a8e62e2` passed all
  exact-head checks. Its fresh rereview returned `READY`, P0/P1/P2 `0/0/0`;
  P4-W1 is complete.
- No publication, deployment, production, private-state, or cutover action
  occurred.

## P4-W2 complete

- Behavior repair `2f08a48aefe51eb755885d9f9ae01cf9c5fa5769` packages the
  launchd/systemd templates and adds an installed supervisor mode. Its red
  regression failed exactly for absent resources and rejected installed mode;
  the repaired supervisor suite passed before movement.
- Implementation checkpoint `2ed82c05ad2f5508a9a04b3c7eadeb86ed676cdf`
  moves all 34 app runtime files, 28 existing app test modules, and app
  resources to `packages/app`; adds one explicit two-test wheel gate module;
  and binds the installed CLI/MCP scripts.
- Canonical inventory is 224 runtime plus 348 non-runtime subjects. It includes
  257 test subjects, 34 moved app runtime records, and 38 moved app subjects.
- App metadata requires exactly `open-brain-engine==0.1.0`; wheel metadata,
  module origins, console scripts, supervisor resources, and connector/legacy
  absence are asserted from an installed product environment.
- A static wheel scan derives the allowed public engine modules from the
  canonical manifest and rejects app imports of private engine modules.
- The isolated app contract builds with `--no-sources`, installs only copied
  app/engine wheels into Python 3.12, and exposes that site-packages directory
  to a separate locked test runner. All 400 app tests pass without checkout
  fixtures, connector, legacy, or workspace source.
- Named wheel-only contracts pass `V0-GATE-07` with six sibling proposals and
  independent CLI/UI approve, reject, and safe edit. They pass `V0-GATE-13`
  with space create/rename, later route without capture-identity change, and
  scoped/all-space retrieval through CLI and UI.
- Artifact policy version 2 owns app and engine distribution/kind coordinates.
  The real app/engine wheels and sdists contain exactly canonical members and
  no forbidden or undeclared members.
- Candidate `85428b125ddd370cfa6a6c2be20f2aab4669bf7e` passed exact-head CI after
  one same-SHA retry. CI attempt 1 failed only the Python 3.12 app-isolation
  wrapper with generic `P4H007`; the concurrent dedicated Ubuntu/Python 3.12
  contract and a local isolated Python 3.12 full suite passed. Attempt 2
  passed every job.
- Child 6 reviewed `85428b1` and returned `NOT_READY`, P0/P1/P2 `0/3/1`.
  The P1 findings were installed supervisor source leakage, false-positive
  3.13/3.14 wheel coverage, and incomplete private/undeclared import checks.
  The P2 was this stale evidence packet.
- Three focused regressions reproduced all P1 findings before repair:
  installed factory rendering leaked `PYTHONPATH`; environment creation
  selected 3.12 while the active interpreter was 3.14; and the artifact scan
  missed private-child, undeclared, and unreviewed dynamic imports.
- Repair checkpoint `7d87c3968e15de5d98f7e509c8c8a31c4c5b500c` validates a
  source-checkout layout before adding supervisor paths, selects the active
  matrix interpreter, derives the complete engine module policy and declared
  app dependencies, allow-lists one exact dynamic import seam, and reports
  bounded app-isolation failure stages.
- Local verification passed: `make verify` (Ruff, strict MyPy on 488 files,
  3,148 tests, and all four artifact builds/policy); a separate uncached Ruff
  run; `make phase4-contracts` (79 tests, Ruff, MyPy, manifest); app-wheel
  journeys under isolated Python 3.12, 3.13, and 3.14; source+artifact release audit;
  reachable-history audit; Gitleaks 8.30.1 over 66 commits; `uv lock --check`;
  and `git diff --check`.
- Exact-head CI run `33591952670`, public-artifacts run `33591952648`, and
  CodeQL run `33591951436` passed at `7d87c39`, including interpreter-specific
  wheel execution on Python 3.12, 3.13, and 3.14.
- Artifact SHA-256 digests:
  app wheel `9b0f06e03e2bdfcfc690e011faa15773eb4efbf26844fff5c446bfc0d11ad84d`;
  app sdist `d6afc82e2cc3ec4d9bf53215129c39a64542a60cbe7831b95505b2660208917a`;
  engine wheel `83e534799359e8ccb7ac8e90dfd5114564c0638428c5f50ca897a26f495cc682`;
  engine sdist `a52e659e44d4dcfc882f76d998540a41d2abffa7886a83db365cb688c914084a`.
- Evidence checkpoint `579b45bc12247d34ed612798fc2a6021ccc1d30a`
  passed all exact-head checks before the second P4-W2 review.
- Child 7 reviewed `579b45b` and returned `NOT_READY`, P0/P1/P2 `0/1/0`.
  The sole P1 found dynamic-import bypasses through `builtins`, assignment
  aliases, and reflective access, plus a false positive for a local object
  named `importlib`.
- A focused artifact regression failed before repair on every reported case.
  Self-review extended it with an importer-capability escape inside the sole
  reviewed dynamic-import file.
- Repair checkpoint `e27ac3c3ad7daa4748b094490fdadab6e66a3773`
  follows lexical provenance for dynamic importer capabilities, records
  constant targets for private and undeclared dependency checks, rejects
  unresolved uses, and binds the only exception to its exact artifact path
  and function signature.
- Final local verification passed: `make verify` (Ruff, strict MyPy on 488
  files, 3,149 tests, and all four artifact builds/policy); uncached Ruff;
  `make phase4-contracts` (80 tests, Ruff, MyPy, manifest); isolated
  app-wheel journeys on Python 3.12, 3.13, and 3.14; source+artifact release
  audit; reachable-history audit; Gitleaks 8.30.1 over 69 commits; `uv lock
  --check`; and `git diff --check`.
- Exact-head CI run `33594652359`, public-artifacts run `33594652371`, and
  CodeQL run `33594650509` passed at `e27ac3c`, including interpreter-specific
  wheel execution on Python 3.12, 3.13, and 3.14.
- Rebuilt artifact SHA-256 digests were unchanged from the preceding repair.
- Evidence checkpoint `0a67a26210b79d0c91fa8953846560db34903034`
  passed all exact-head checks before the third P4-W2 review.
- Child 8 reviewed `0a67a26` and returned `NOT_READY`, P0/P1/P2 `0/1/1`.
  The P1 reproduced a private engine import through
  `sys.modules["builtins"]` and identified the same fail-open class through
  runtime namespace helpers. The P2 found that the isolated app evidence said
  402 tests although the exact package scope collects 400.
- A focused artifact regression failed before repair for `sys.modules`,
  `globals`, `locals`, `vars`, `eval`, `exec`, and an assigned alias. Its
  negative case proved a shadowed local `globals` callable remains clean.
- Repair checkpoint `d8e2cb20f0268e16ebdd5b46053d5081dab7ac7c`
  carries provenance through `sys.modules` and built-in namespace mappings,
  fails closed on dynamic evaluation capabilities, records constant private
  and forbidden targets, and corrects both app-suite claims to 400.
- Latest local verification passed: `make verify` (Ruff, strict MyPy on 488
  files, 3,150 tests, and all four artifact builds/policy); uncached Ruff;
  `make phase4-contracts` (81 tests, Ruff, MyPy, manifest); exact collection
  of 400 app tests; isolated app-wheel journeys on Python 3.12, 3.13, and
  3.14; source+artifact release audit; reachable-history audit; Gitleaks
  8.30.1 over 71 commits; `uv lock --check`; and `git diff --check`.
- Exact-head CI run `33596668616`, public-artifacts run `33596668630`, and
  CodeQL run `33596666546` passed at `d8e2cb2`, including interpreter-specific
  wheel execution on Python 3.12, 3.13, and 3.14.
- Rebuilt artifact SHA-256 digests remained unchanged.
- Evidence checkpoint `e483973e7f8153b97aa5a44b049d787d08fd884f`
  passed all exact-head checks before the fourth P4-W2 review.
- Child 9 reviewed `e483973` and returned `NOT_READY`, P0/P1/P2 `0/1/0`.
  The P1 reproduced import escapes through `sys.__dict__`,
  `sys.__getattribute__`, and function or lambda `__globals__`. It also found
  path-insensitive rebinding and false positives where loop, `with`, or
  exception targets shadowed the standard-library `sys` module.
- One focused artifact regression failed before repair on every positive and
  negative case, including a hidden `__globals__` import in the otherwise
  reviewed optional-provider file. Self-review added bound function and
  `object.__getattribute__` variants before broad verification.
- Repair checkpoint `bd994a0288f8711f216e130c25c45f7a654eb90f`
  uses conservative provenance sets, joins branch states, models function,
  class, loop, `with`, exception, match, and comprehension bindings, and
  recognizes namespace and bound `__getattribute__` reflection.
- Current local verification passed: `make verify` (Ruff, strict MyPy on 488
  files, 3,151 tests, and all four artifact builds/policy); uncached Ruff;
  `make phase4-contracts` (82 tests, Ruff, MyPy, manifest); all 11 analyzer
  tests independently on Python 3.12, 3.13, and 3.14; isolated app-wheel
  journeys on all three versions; source+artifact release audit;
  reachable-history audit; Gitleaks 8.30.1 over 73 commits; `uv lock --check`;
  and `git diff --check`.
- Exact-head CI run `33600292963`, public-artifacts run `33600292877`, and
  CodeQL run `33600287985` passed at `bd994a0`, including interpreter-specific
  wheel execution on Python 3.12, 3.13, and 3.14.
- Rebuilt artifact SHA-256 digests remained unchanged.
- Evidence checkpoint `0cff2ea6077b2450febd07f456310aff1f6ffd25`
  passed all exact-head checks before the fifth P4-W2 review.
- Child 10 reviewed `0cff2ea` and returned `NOT_READY`, P0/P1/P2 `0/2/0`.
  One P1 reproduced forbidden imports through aliases of `sys.__dict__`,
  `sys.__getattribute__`, and `builtins.object`. The other reproduced a PEP
  572 comprehension assignment that lost its enclosing-scope provenance.
- Focused wheel regressions failed before repair for each positive path and
  for the related shadow-negative case. Self-review added symmetric
  `importlib` and `builtins` aliases plus normal walrus overwrite behavior.
- Repair checkpoint `e103255b2ab03c3312206383b71ce38fcde67b8e`
  derives `ImportFrom` bindings from the same modeled module-member authority
  used for attribute access. Comprehension assignment expressions now update
  their nearest enclosing scope and are predeclared as function locals.
- Latest local verification passed: `make verify` (Ruff, strict MyPy on 488
  files, 3,151 tests, and all four artifact builds/policy); uncached Ruff;
  `make phase4-contracts` (82 tests, Ruff, MyPy, manifest); all 14 analyzer and
  app-wheel tests on Python 3.12, 3.13, and 3.14; source+artifact release
  audit; reachable-history audit; Gitleaks 8.30.1 over 75 commits; `uv lock
  --check`; and `git diff --check`.
- Exact-head CI run `33602946392`, public-artifacts run `33602946514`, and
  CodeQL run `33602944303` passed at `e103255`, including interpreter-specific
  wheel execution on Python 3.12, 3.13, and 3.14.
- Rebuilt artifact SHA-256 digests remained unchanged.
- Evidence checkpoint `995bd781869901e772eee0c7fdfd0ab8132065d5`
  passed CI run `33603709388`, public-artifacts run `33603709475`, and CodeQL
  run `33603706129` before the sixth P4-W2 review.
- Child 11 reviewed `995bd78` and returned `NOT_READY`, P0/P1/P2 `0/2/0`.
  One P1 reproduced unreviewed imports through `importlib.__init__`,
  `pkgutil.resolve_name`, aliased `importlib.util`, `sys._getframe`, and
  function-type reflection. The other showed that the reviewed helper could
  reassign `import_path` while retaining its approved call spelling.
- Both focused artifact regressions failed with zero findings before repair.
  Self-review added `from` aliases, colon-qualified resolver targets,
  `function.__class__`, direct and conditional replacement, and
  second-parameter substitution. Four app tests prove internal roots are
  rejected by metadata construction and the runtime loader.
- Repair checkpoint `559690e14b9a1dd935566b54e97ec8f2b73f8d06`
  normalizes equivalent module authorities, tracks frame and type reflection,
  separates pristine parameters from unknown values through control flow,
  preserves the app's safe importlib resource/metadata APIs, and blocks
  internal package roots at the optional-provider boundary.
- Latest local verification passed: `make verify` (Ruff, strict MyPy on 488
  files, 3,157 tests, and all four artifact builds/policy); uncached Ruff;
  `make phase4-contracts` (84 tests, Ruff, MyPy, manifest); exact collection
  of 404 app tests; all 16 analyzer and app-wheel tests on Python 3.12, 3.13,
  and 3.14; source+artifact release audit; reachable-history audit; Gitleaks
  8.30.1 over 77 commits; `uv lock --check`; and `git diff --check`.
- Exact-head CI run `33608085686`, public-artifacts run `33608085774`, and
  CodeQL run `33608082590` passed at `559690e`, including interpreter-specific
  wheel execution on Python 3.12, 3.13, and 3.14.
- Rebuilt SHA-256 digests are app wheel
  `812b0a64bbecb2f19b1713411f31bab69a5d52027dc7c3c871bc493ffc65f272`,
  app sdist
  `48a7c763b9b46c5832d11ddd20a3f6975bfd57f0f5db6a96e870aaea3543d080`,
  engine wheel
  `83e534799359e8ccb7ac8e90dfd5114564c0638428c5f50ca897a26f495cc682`,
  and engine sdist
  `a52e659e44d4dcfc882f76d998540a41d2abffa7886a83db365cb688c914084a`.
- Evidence checkpoint `ce909a525dbc66ec5d893892984f2814f7bb9e71`
  passed CI run `33609157287`, public-artifacts run `33609157406`, and CodeQL
  run `33609152877`. Child 12 started a final review and was stopped without a
  verdict after the candidate architecture was superseded.
- Repair checkpoint `9ca31ba36c44e4e4a269e5c932fae27fa174831e`
  removes the generic string-based loader and its sole P4H009 exception. A
  closed `OptionalProvider` enum selects an immutable lazy-loader registry
  containing only the declared `openai` extra. Typed construction makes
  internal roots unrepresentable, while the runtime loader rejects non-enum
  internal identifiers. P4H009 remains a finite adversarial architecture
  corpus rather than a malicious-code sandbox.
- Latest local verification passed: `make verify` (Ruff, strict MyPy on 488
  files, 3,157 tests, and all four artifact builds/policy); uncached Ruff;
  `make phase4-contracts` (84 tests, Ruff, MyPy, manifest); exact collection
  of 404 app tests; all 16 analyzer and app-wheel tests on Python 3.12, 3.13,
  and 3.14; source+artifact release audit; reachable-history audit; Gitleaks
  8.30.1 over 79 commits; `uv lock --check`; and `git diff --check`.
- Exact-head CI run `33611279850`, public-artifacts run `33611279794`, and
  CodeQL run `33611274401` passed at `9ca31ba`, including interpreter-specific
  wheel execution on Python 3.12, 3.13, and 3.14.
- Rebuilt SHA-256 digests are app wheel
  `cfee79bfe16adc1dc09ffd930db3a49763028b44bd0eb8c85d3403ea6e17b43e`,
  app sdist
  `fc6b43252d5842fdd470236c07a4bbd9a96d1c55450e81c77e922d0d4e20f68d`,
  engine wheel
  `83e534799359e8ccb7ac8e90dfd5114564c0638428c5f50ca897a26f495cc682`,
  and engine sdist
  `a52e659e44d4dcfc882f76d998540a41d2abffa7886a83db365cb688c914084a`.
- Evidence checkpoint `ee2f8c22ac1bd00fd6a3d2924071b8ff23a32238`
  passed CI run `33611917553`, public-artifacts run `33611917534`, and CodeQL
  run `33611914545`. Its six changed files were docs-only, so child 12 resumed
  against unchanged source `9ca31ba` with `ee2f8c2` as the evidence scope.
- Child 12 returned `NOT_READY`, P0/P1/P2 `0/0/1`. The canonical manifest
  retained the removed optional-loader review at `integrations/ports.py:917`.
  A direct full-inventory reproduction reported the stale review, while the
  normal architecture test passed because `_planned_source_classification`
  removed reviews for moved source records before stale validation.
- Repair checkpoint `30d49d31d35f86e26be3c0ac99b884a47d76b5f6`
  removes the stale review and makes the normal architecture gate compare the
  canonical review inventory with all current `open_brain` source locations.
  A moved-source regression proves stale and unreviewed records remain visible;
  the legacy source projection no longer filters review evidence.
- Latest local verification passed: `make verify` (Ruff, strict MyPy on 488
  files, 3,158 tests, and all four artifact builds/policy); uncached Ruff;
  `make phase4-contracts` (85 tests, Ruff, MyPy, manifest); exact collection
  of 404 app tests; all 16 analyzer and app-wheel tests on Python 3.12, 3.13,
  and 3.14; source+artifact release audit; reachable-history audit; Gitleaks
  8.30.1 over 81 commits; `uv lock --check`; and `git diff --check`.
- Exact-head CI run `33614647926`, public-artifacts run `33614647755`, and
  CodeQL run `33614643945` passed at `30d49d3`, including interpreter-specific
  wheel execution on Python 3.12, 3.13, and 3.14. Rebuilt artifact SHA-256
  digests remained unchanged.
- Docs-only evidence checkpoint
  `e20be9debc6cd68cd443a3df00f6c2cd76041cb3` passed CI run `33615635189`,
  public-artifacts run `33615635198`, and CodeQL run `33615631031`.
- Child 12's same-lineage rereview was bound to source `30d49d3` and evidence
  `e20be9d`. It returned `READY`, P0/P1/P2 `0/0/0`, confirmed the prior P2 is
  closed, and declared P4-W2 ready for milestone closure.
- P4-W2 is complete. P4-W3 has not started, and its reviewer budget resets at
  the milestone boundary.
- Independent review is source-SHA-bound. A docs-only evidence successor does
  not invalidate unchanged source review, and the child budget resets at each
  milestone boundary.
- No package publication, tag, release, native build, deployment, production
  access, private-state access, cutover action, or P4-W3 work occurred.

## P4-W3 local source candidate

- Starting from verified P4-W2 closure
  `c3480be28fa36b9dae2256ff6aee610044b86847`, all five canonical
  connector-owned runtime files and three connector-owned tests moved to the
  buildable `open-brain-connectors` distribution. The canonical inventory now
  contains 232 runtime files and 355 Phase 4 subjects, including 260 tests; all
  nine connector runtime records are moved.
- The connector wheel depends on exact app and engine versions and imports app
  values only from the published provisional extension modules. The app wheel
  has no connector dependency, its default profile discovers none, and an
  installed connector remains unloaded in the parent before and after child
  execution.
- The worker request binds protocol version, invocation, entry-point metadata,
  manifest, budget, and network mode. The child runs with an empty environment,
  direct socket APIs disabled, bounded process/time/memory/output resources,
  metadata-only responses, and process-group termination. Parent validation
  binds receipts to identity, manifest, budget, capture count, and replay.
- Actual reference conformance runs the YouTube connector twice against
  synthetic host-mediated media and a durable temporary checkpoint. The first
  run creates one capture; replay creates none.
- Local verification passed: `make phase4-contracts` with 87 tests, Ruff,
  strict MyPy on 500 files, and manifest validation; `make verify` with 3,170
  tests and all six artifact builds/policy; connector wheel-only acceptance on
  Python 3.12, 3.13, and 3.14 with 3 tests per version; and `actionlint`.
- Source plus built-artifact release audit, reachable-history audit, Gitleaks
  8.30.1 over 83 commits, `uv lock --check`, and `git diff --check` passed.
- Rebuilt SHA-256 digests are app wheel
  `e2ecef1d7a283f6af485fbb9a5a8f666a69935c68c96466a5a644807b2d256a1`,
  app sdist
  `1e10a663dabfde9015a6ffedeb319465213fc19d052f8b6b9d455c0eabbcd9f9`,
  connector wheel
  `0a43a8b80423a5b03168f9ae2e9d13ff037f683503f9ba716280bf911c0ad31b`,
  connector sdist
  `5fc3c220849544ac35d24d7caefb678d3da313bd57d2a62a3bbc79f1bc3d0473`,
  engine wheel
  `83e534799359e8ccb7ac8e90dfd5114564c0638428c5f50ca897a26f495cc682`,
  and engine sdist
  `199e0fc2adb264b0a0f9db50fd291e906b4d0a61cdb90e4eb55784716d000eca`.
- The branch-finish gotcha audit added canonical module-identity,
  artifact-coordinate, and parent-budget receipt entries. No other
  non-duplicate finding survived the evidence and registry checks.
- First exact-head CI run `33627007197` at source `fe579e4` passed all three
  full verify jobs, Phase 4 contracts, and the macOS appliance job. Release
  audit run `33627007170` passed. The three new connector-isolation jobs failed
  during job setup because their copied `setup-uv` commit pin differed by one
  character from every existing job; no repository step ran. A focused
  regression now requires all four pins to be present and identical. The
  corrected workflow, regression, `actionlint`, `make phase4-contracts`, and
  `make verify` all pass locally.
- Repair checkpoint `2c05dead068ef517ed365d100f3b6273c29eeba9`
  passed exact-head CI run `33627525857`, Release audit run `33627525864`, and
  CodeQL run `33627520718`. The run includes green connector-isolation jobs on
  Python 3.12, 3.13, and 3.14, three full verify jobs, Phase 4 contracts, the
  macOS appliance suite, public artifacts, source/artifact and history audits,
  and Gitleaks. PR #6, its remote branch, and local HEAD matched the reviewed
  source; the draft PR was mergeable.
- Child 13 (`01a06202-4d7a-7331-a038-239a6e93a630`) reviewed source `2c05dea`
  and returned `NOT_READY`, P0/P1/P2 `0/1/0`. `P4W3-001` proved that the
  default in-process registry could resolve and execute the installed public
  connector entry point, bypassing the worker boundary.
- A direct regression failed on the reviewed design, then passed after the
  installed public group became metadata-only, injected compatibility retained
  a distinct internal group, and the published entry-point object lost its
  direct `run()` method. The wheel-only contract now proves both
  `ConnectorRegistry.resolve()` and `ConnectorHost.run()` leave the connector
  module unloaded in the parent. All 72 focused connector/composition tests,
  `make phase4-contracts` with 87 tests, and `make verify` with Ruff, strict
  MyPy on 500 files, 3,171 tests, six artifact builds, and artifact policy pass.
  The wheel-only connector journey passes on Python 3.12, 3.13, and 3.14.
- Source/artifact and reachable-history audits, Gitleaks 8.30.1 over 85 commits,
  actionlint, `uv lock --check`, and `git diff --check` pass for the repair.
  Rebuilt SHA-256 digests are app wheel
  `19a4e217385abc42b38266f7ef8b27206c93995cfef3e036f762b68d0a1a4c49`,
  app sdist
  `4070a47cf3f22ae60ff90ccf96b7c3ddc8867470677146efc7bab94c2e0899ef`,
  connector wheel
  `2018c931cd372287a2828197ee937ce5a0de4c39f944a204040c8e84e4fb09eb`,
  connector sdist
  `0b27910054ff1f12c877728dcf6868b7de65ed11ba04e369db64d67c86a7d33a`,
  engine wheel
  `83e534799359e8ccb7ac8e90dfd5114564c0638428c5f50ca897a26f495cc682`,
  and engine sdist
  `199e0fc2adb264b0a0f9db50fd291e906b4d0a61cdb90e4eb55784716d000eca`.
  Repair checkpoint `27344c6cf83b8b74c11d6ec0c1c3075cbfa98e09`
  passed exact-head CI `33629757827`, Release audit `33629757695`, and CodeQL
  `33629754431`. All connector, full verify, Phase 4, macOS, artifact, and
  security jobs are green; PR #6, remote, and local source match.
- Child 13's same-lineage rereview returned `READY`, P0/P1/P2 `0/0/0`, at
  source `27344c6`. It reproduced the fresh-wheel parent non-load behavior,
  explicitly closed `P4W3-001`, found no new issues, and declared P4-W3 ready
  for milestone closure.
- P4-W3 is complete. P4-W4 has not started, and its reviewer budget resets at
  the next milestone boundary. No publication, tag, release, native build,
  deployment, production/private access, cutover action, or P4-W4 work occurred.
- No publication, tag, release, native build, deployment, production/private
  access, cutover action, or P4-W4 work occurred.

## P4-W4 local repair candidate

- Governance launch commit `a94fed1` and behavior-neutral movement commit
  `818caf6` remain separate from this repair candidate. The canonical move put
  all 302 planned legacy and workspace paths at their final locations and
  removed the old `src/open_brain` tree.
- Initial post-move collection exposed 35 cross-namespace import errors. The
  deterministic rewriter now converts relative imports that cross a moved
  package boundary. The full root suite subsequently exposed and closed 11
  stale path, module-identity, and dynamic-module assertions.
- A source-derived dependency regression failed with an undeclared connector
  edge: legacy imported `{app, connectors, engine}` while the manifest and
  metadata declared only app and engine. The canonical graph, exact package
  metadata, lockfile, and wheel metadata now agree on all three inward edges.
- Focused architecture, manifest, package-build, app, engine, and legacy
  verification passed 233 tests. The complete repository suite passed 3,173
  tests in 69.89 seconds. `make phase4-contracts` passed 89 tests, Ruff,
  strict MyPy on 500 files, and manifest validation. `make verify` passed
  Ruff, strict MyPy, the same 3,173 tests, all six shipping artifact builds,
  and artifact policy.
- Source-free private legacy build hashes are wheel
  `09ec2124ef01cd1730b2918d6c2bc0a9d33315068c022b2312dbed8fa73feac4`
  and sdist
  `ff9d17a1d090ad72815d59adbc371fd87db02872881a8586660647e03079b44a`.
  Shipping artifact hashes are app wheel
  `19a4e217385abc42b38266f7ef8b27206c93995cfef3e036f762b68d0a1a4c49`,
  app sdist
  `ea47d9afea6829c12ced114a46f6778738f57a0d037dcecb08d806d4b2b2d782`,
  connector wheel
  `2018c931cd372287a2828197ee937ce5a0de4c39f944a204040c8e84e4fb09eb`,
  connector sdist
  `0b27910054ff1f12c877728dcf6868b7de65ed11ba04e369db64d67c86a7d33a`,
  engine wheel
  `83e534799359e8ccb7ac8e90dfd5114564c0638428c5f50ca897a26f495cc682`,
  and engine sdist
  `199e0fc2adb264b0a0f9db50fd291e906b4d0a61cdb90e4eb55784716d000eca`.
- Uncached Ruff, strict MyPy, canonical manifest and import-rewrite checks,
  `uv lock --check`, and `git diff --check` pass.
- Repair checkpoint `678c61cab75f8b90ac64aa04ac8d06bac8ee220f`
  passed the focused source-free isolation matrix with 76 tests on each of
  Python 3.12, 3.13, and 3.14. Source plus all six shipping artifacts passed
  the release audit with an untracked synthetic denylist; reachable history
  passed with the same public-only fixture. Gitleaks 8.30.1 found no leaks
  across 90 commits, and actionlint passed.
- All local P4-W4 source, package, matrix, artifact, and audit gates are green.
  Exact-head CI run `33642529540`, Release audit `33642529483`, and CodeQL
  `33642524132` all passed at evidence commit
  `7927162096330355520f2de756aa45b48ccb6493`. Local, remote, and PR source
  identities match. The fresh independent review is now reserved as child 14;
  reviewer budget is one active and one total.
- Child 14 (`01a0628c-d723-73e0-96cc-f2b3d8ca3f63`) returned
  `NOT_READY`, P0/P1/P2 `0/1/1`, against exact source `7927162`.
  `P4A-001` proves D-040 broadened the approved engine-only legacy dependency
  into 42 app-importing files and eight connector-importing files, including
  distribution-private modules. `P4A-002` proves `CLAUDE.md` still names the
  deleted `src/open_brain/dev/artifact_policy.py` path. No other finding
  survived review. The same lineage is retained for rereview after both
  regressions and repairs pass exact-head CI.
- Both reviewer findings were reproduced before repair. The dependency
  regression observed `{app, connectors, engine}` instead of the approved
  `{engine}` graph, and the instruction regression found the deleted artifact
  policy path.
- The repaired legacy source has zero static app or connector imports. The
  canonical graph, validator, package metadata, lockfile, and private artifact
  metadata all declare only `open-brain-engine==0.1.0`. Thirty-four supporting
  modules are contained in the legacy-only `_compat` namespace and classified
  as private compatibility.
- A new clean-room contract builds engine and legacy with `--no-sources`,
  installs only those two wheels, imports every module in the legacy wheel, and
  proves the app and connector namespaces are unavailable. It passes, as do all
  2,147 legacy tests.
- The P4-W5 readiness preflight has seven passing tests. It invokes each of the
  six required read-only probes once, fails closed without propagating raw
  errors, emits only booleans and `rct_v1_` opaque receipts, validates restored
  snapshots, and reuses one snapshot through P4-W5, P4-W6, P4-W7, P4-W8, and
  P4-W9 without another probe call.
- Current local verification passes 98 Phase 4/architecture tests, Ruff,
  strict MyPy on 536 files, manifest validation, all 3,182 repository tests,
  six source-free shipping builds, artifact policy, `uv lock --check`, and
  `git diff --check`. Exact-commit audits and remote checks remain pending.
- Candidate `b86beacbc7005b0a7f2ceeb5c009f0a2849579a6` passed exact-head CI
  `33650699793`, Release audit `33650699895`, and CodeQL `33650693028`; every
  PR #6 job passed and local/remote source identities matched. Child 14's
  same-lineage rereview returned `NOT_READY`, P0/P1/P2 `0/1/1`. It explicitly
  closed `P4A-001` and `P4A-002`, then reported `P4A-003` for the sole external
  `_compat` import and `P4A-004` for uncaught `SystemExit` from a readiness
  probe.
- The new legacy wheel regression failed with `P4H009` on
  `_compat/open_brain/integrations/ports.py`. The readiness canary failed by
  propagating `SystemExit` and its synthetic sensitive detail. These were the
  expected red states before implementation.
- The repaired private compatibility metadata has no ambient provider loader.
  Its enabled path requires an injected callable; no callable returns the
  stable optional-dependency outcome. Wheel-level AST analysis rejects both
  static and dynamic import authority outside stdlib, engine, and legacy. The
  installed engine-plus-legacy-only contract imports every packaged module and
  exercises the enabled injected path without installing the OpenAI SDK.
- The readiness observer catches `SystemExit` alongside ordinary exceptions,
  fails closed, and derives the same opaque unavailable receipt without
  retaining exception text. `KeyboardInterrupt` is intentionally not caught.
  Both synthetic failure classes pass the boolean-and-receipt-only canary.
- Post-repair verification passed 10 focused tests; `make phase4-contracts`
  with 100 tests, Ruff, strict MyPy on 536 files, and manifest validation;
  `make verify` with Ruff, strict MyPy, 3,184 tests, six source-free shipping
  artifacts, and artifact policy; and 100 P4A tests on each of Python 3.12,
  3.13, and 3.14. Exact-commit audits and remote checks remain pending.
- At exact source `9098ff5e676a76ef1637f30dee99ff6508d46a30`,
  artifact policy, source plus six-shipping-artifact release audit,
  reachable-history audit with the same synthetic denylist, Gitleaks 8.30.1
  over 93 commits, actionlint, `uv lock --check`, manifest validation, `git
  diff --check`, and clean-tree verification passed.
- Exact shipping SHA-256 values are app wheel
  `19a4e217385abc42b38266f7ef8b27206c93995cfef3e036f762b68d0a1a4c49`,
  app sdist
  `964e55220df93e956c5aab0a67097d1fb4340df3d75c1f40fbf1e2f6aaa33aea`,
  connector wheel
  `2018c931cd372287a2828197ee937ce5a0de4c39f944a204040c8e84e4fb09eb`,
  connector sdist
  `0b27910054ff1f12c877728dcf6868b7de65ed11ba04e369db64d67c86a7d33a`,
  engine wheel
  `83e534799359e8ccb7ac8e90dfd5114564c0638428c5f50ca897a26f495cc682`,
  and engine sdist
  `199e0fc2adb264b0a0f9db50fd291e906b4d0a61cdb90e4eb55784716d000eca`.
  Private legacy SHA-256 values are wheel
  `a10330f320439eb7fc240f663a59ed1422c44db204274c140de036e6aa47985c`
  and sdist
  `8ad90592be262c42d7312d2da0adbece4c5d0823d555d08fb4795fa2b977538e`.
- Exact-head CI `33654478123`, Release audit `33654478071`, and CodeQL
  `33654473793` passed. All 12 PR #6 jobs are green; local HEAD and the remote
  branch matched the exact reviewed source.
- Child 14's second same-lineage rereview returned `READY`, P0/P1/P2
  `0/0/0`. It independently reproduced the ten focused tests, found zero
  disallowed or unresolved `_compat` imports, verified `SystemExit` fails
  closed while `KeyboardInterrupt` remains effective, rebuilt the private
  artifacts with matching hashes, and explicitly closed `P4A-001` through
  `P4A-004` with no new finding.
- P4-W4 and P4A are complete. No private readiness implementation or state was
  accessed; only the public preflight contract was reviewed. No publication,
  tag, release, native build, deployment, production/private access, or
  cutover action occurred, and P4-W5 remains intentionally unstarted.

## P4-W5 implementation candidate

- Live grounding matched branch `goal/open-brain-phase4` at local, remote, and
  PR head `418fcd5530ee9a7fb2eaae6764d4f7ddffc46a97`. PR #6 remained open,
  draft, mergeable, and green on its inherited 12 checks. Goal #63 remained
  open. The worktree contained only the P4-W5 implementation described here.
- The one-shot snapshot SHA-256 remains
  `753a1635aa2be81f3ebe6b3723dbc8e46a6d6aa46f1b39003b9d9c39da769d1b`.
  Strict restore and exact serialization passed for P4-W5 through P4-W9. Its
  results remain signing true, macOS ARM64 true, disk true, Linux x86_64 false,
  notarization false, recovery false, and aggregate false. No readiness probe
  was called again.
- Static records explain Linux readiness without a probe: the inherited CI
  jobs used `ubuntu-latest`, while `release/phase4-toolchain.json` requires
  literal `ubuntu-24.04` for Linux x86_64. The new real native job uses that
  exact image. The macOS job uses GitHub's `macos-14` ARM64 YAML label, which
  resolves to the recorded `macos-14-arm64` image; the build also validates
  runtime architecture.
- Red-first checks rejected an unavailable native module, rejected
  `--artifact-kind=native-onedir`, found stale managed residue after uninstall,
  rejected Python-style frozen child commands, and found no real native CI
  subjects. Focused repairs passed 7 adapter and CLI contracts, 3 native
  entrypoint/worker contracts, 2 build/audit contracts, and the updated policy
  and CI contracts.
- One disposable, non-acceptance macOS diagnostic built with Python 3.12,
  PyInstaller 6.22.2, and hooks 2026.7. It audited 142 members, four confined
  symlinks, required supervisor resources, and tree digest
  `b862f373eb98977d5691b6e33949c670a467425e4292ee2b96ddbd1c594fb88b`.
  This digest is from uncommitted diagnostic source and cannot satisfy the
  frozen-candidate gate.
- Diagnostic smoke exposed and closed four harness/build defects: package
  relative imports from a PyInstaller entry script, noncanonical `/tmp` root
  identity, stale control-socket readiness on daemon restart, and an empty
  `PATH` hiding the required host supervisor. The passing strategy uses
  absolute package imports, a resolved root, successful control-response
  polling, and a temporary supervisor-only path containing no Python.
- The source candidate now includes a real manifest-bound native lifecycle
  adapter, direct frozen daemon and worker routing, a checked-in onedir spec,
  bounded build/member/runtime evidence, native activation and clean uninstall,
  the exact target-native jobs, and the single compositional preflight. The
  pinned dependency group is locked.
- Final `make p4w5-preflight` passed 91 focused tests, isolated pinned Python
  3.12 toolchain validation, actionlint, canonical manifest validation, Ruff,
  strict MyPy on 16 touched Python files, and `git diff --check`. Its earlier
  bounded failures exposed the invalid macOS image-name/YAML-label distinction,
  two Ruff findings, and four test-only MyPy attribute errors; each rerun
  followed a changed repair strategy. Source freeze, full verification, exact
  target-native evidence, and independent review remain pending.
- No signing, notarization, publication, release, deployment, service install,
  production/private-state access, recovery action, cutover, or P4-W6 work
  occurred.

## P4-W5 independent review repair candidate

- Exact-head CI `33667187568`, Release audit `33667187309`, and CodeQL
  `33667180599` passed at source
  `6c2c82de89b554cca8ec6c27b10a57959766c39e`. Child 15
  (`01a0636c-3ef9-7823-ad08-d8dcdb5a48d9`, Kierkegaard) opened the
  independent P4-W5 lineage and reported `P4W5-001` through `P4W5-007`.
- Repairs and one policy correction produced exact source
  `28f1fa7055a9194e433caea666a0c13bf2c126da`. CI `33672312445`, Release
  audit `33672312619`, and CodeQL `33672307671` passed. The same-lineage
  rereview returned `NOT_READY`, P0/P1/P2 `0/3/1`.
- Source `47aee48e248156816b863071b7df199513a7da43` added journaled
  stop-before-recovery ordering, separate daemon-restoration evidence,
  corrupt-enrolled cleanup, case-insensitive source suffix rejection, and
  removal of mutation-capable hidden smoke commands. Local Phase 4 contracts,
  all 3,211 repository tests, local macOS ARM64 native proof, artifact policy,
  and exact-head remote checks passed. CI `33675602201` became green after a
  failed-only retry of one timing-sensitive Python 3.12 test; Release audit
  `33675602206` and CodeQL `33675596478` also passed. Neither native job reran.
- The `47aee48` rereview returned `NOT_READY`, P0/P1/P2 `0/4/0`. It accepted
  D-050 as a gate-strengthening contract clarification, closed `P4W5-002`,
  `P4W5-003`, `P4W5-005`, and `P4W5-006`, and retained `P4W5-001`,
  `P4W5-004`, and `P4W5-007`. New finding `P4W5-008` showed that ignored
  working-tree files were outside the source-SHA check while PyInstaller read
  package trees directly.
- Six focused regressions reproduced the four remaining failure classes:
  launchd had no unload/resume boundary, first-run discovery enrolled unrelated
  valid candidates, an undeclared `api.token` passed below a resource tree,
  and no exact-Git source materialization existed. All six failed before the
  repair.
- The repair gives the lifecycle port explicit quiesce and resume operations.
  Launchd uses `bootout` before offline work and `bootstrap` plus `kickstart`
  afterward. The smoke shim models KeepAlive relaunch after process kill. The
  managed inventory starts empty or bootstraps only the explicit relative
  `current` candidate.
- Native builds now extract the named commit through `git archive`, digest the
  isolated source before and after PyInstaller, and put only that tree on the
  build import path. The artifact audit derives exact package-resource members
  from tracked source and rejects any extra file under those roots. Credential
  suffixes and `.env*` names are independently denied.
- The six red regressions now pass, and `make p4w5-focused` passes 108 tests.
  Candidate preflight, source freeze, full frozen-candidate verification, new
  target-native artifacts, exact-head CI, and the same-lineage rereview remain
  pending. The readiness snapshot remains unchanged and no probe was called.
- Repair candidate `e55d7488a60a98f2bf5f06cebc18f8fe485e169f` passed the
  final 108-test preflight, 112 Phase 4 tests, Ruff, MyPy on 543 files, all
  3,214 repository tests, six source-free distributions, artifact policy,
  source/artifact and reachable-history audits, Gitleaks 8.30.1 over 103
  commits, lock/workflow/diff checks, and one local macOS ARM64 build/smoke.
  Exact-head CI `33681462262`, Release audit `33681462105`, and CodeQL
  `33681457587` passed on their first attempt with all 14 PR checks green.
- At `e55d748`, Linux job `100418774889` on literal `ubuntu-24.04` recorded
  111 members, no symlinks, membership
  `121db9cdd0622785771b77456327499a82661b4fa9689d0f8e2e4e924647649a`,
  and tree `c6040acd919d0afa1b0a740159dc9cea9256fb1f0e0ecd749fefdb2efdde3e9c`.
  macOS CI job `100418774890` recorded 139 members, four symlinks, membership
  `6e5118ad6a106df150abac5834b13bef5805be0bb956dc2339e88b30ee86bafb`,
  and tree `f40dcadbde03825516a5ccba74170287e02dea68b43ea70820a767a9b11f6567`.
  The separate local macOS subject recorded 142 members, four symlinks,
  membership
  `064cebd3dbc51a27f80c63a047eea73e54e6b581075308533f7e59d98ae17265`,
  tree `e196ff397cb873a4d6fcc6d202afd4a875d732d498fb3430817639643328af17`,
  and source-tree
  `da2a5df04bc36cbcbfe20ecde4eeb9b02e5d566013c217674fa5fc2f0330af81`.
- Child 15's next same-lineage rereview returned `NOT_READY`, P0/P1/P2
  `0/2/0`. It accepted D-050 and D-051, closed `P4W5-001`, `P4W5-002`,
  `P4W5-003`, `P4W5-005`, `P4W5-006`, and `P4W5-007`, and retained only
  `P4W5-004` and `P4W5-008`. `.envrc` and `.environment` still passed in a
  runtime tree. `git archive` still accepted replacement refs and could differ
  from the raw named tree through attribute rules.
- The final focused repair rejects every case-folded component beginning with
  `.env`. Git materialization disables replacement objects, rejects replacement
  refs and repository-local attributes, neutralizes global/system attributes,
  and compares every extracted file mode and blob object ID with raw
  `git --no-replace-objects ls-tree` output. Four new cases were red before
  repair: `.envrc`, `.environment`, a replacement ref, and tracked
  `export-ignore`. The complete eight-case set and all 115 focused P4-W5 tests
  now pass. A new preflight, source freeze, frozen-candidate gates, native
  artifacts, exact-head checks, and same-lineage verdict remain pending.

## P4-W5 accepted source and artifact closure

- The accepted source candidate is
  `c7c4fad1b109ac7d7c55d55cdfa57b64a9c910db`. Its one candidate preflight
  passed 115 focused tests, pinned Python 3.12 validation, actionlint, manifest
  validation, Ruff, MyPy on 21 touched files, and diff integrity.
- The frozen local ladder passed 119 Phase 4/security tests, Ruff, MyPy on 543
  files, all 3,221 repository tests, six source-free distributions, artifact
  policy, source/artifact and reachable-history audits, Gitleaks 8.30.1 over
  104 commits, lock/workflow/diff checks, and a clean worktree.
- The separate local macOS ARM64 build materialized and verified source-tree
  digest `2befe27e25b7feaf67e3efea8dea95d15c888f194c087903eb43488bc25e1c7f`.
  Its 142-member, four-symlink artifact has membership
  `064cebd3dbc51a27f80c63a047eea73e54e6b581075308533f7e59d98ae17265`
  and tree `f08140c6b12c8ea0a96309880eb30a49547cd300ec2615fcf6183839f87ece76`.
- Exact-head CI `33684763227`, Release audit `33684763223`, and CodeQL
  `33684759266` passed on their first attempt. PR #6 reported 14 of 14 checks
  green. Linux job `100429455057` ran on literal `ubuntu-24.04`; its 111-member,
  zero-symlink artifact has membership
  `121db9cdd0622785771b77456327499a82661b4fa9689d0f8e2e4e924647649a`
  and tree `79defd2c7cea266d050036b6467b02bc348f795b3370aa3dbed4acfeec85768b`.
  macOS job `100429455034` ran on `macos-14-arm64`; its 139-member,
  four-symlink artifact has membership
  `6e5118ad6a106df150abac5834b13bef5805be0bb956dc2339e88b30ee86bafb`
  and tree `7f1ec77566f76119419d9d5177026a3879f7c0f34a6da5b78ff91cbfe0bd9e90`.
- Every local and CI native runtime result passed: activation, KeepAlive-aware
  quiescence, owner-confirmed corrupt rollback and daemon restoration, a second
  active-daemon upgrade, public-control Portable export/import, backup/restore,
  connector isolation, uninstall, clean residue, and no source checkout or
  system Python.
- Child 15's final same-lineage review returned `READY`, P0/P1/P2 `0/0/0`,
  against the exact source and all three artifact subjects. It accepted D-050
  and D-051, explicitly closed `P4W5-001` through `P4W5-008`, and opened no
  finding. The lineage is closed.
- The immutable readiness snapshot remains byte-for-byte unchanged at SHA-256
  `753a1635aa2be81f3ebe6b3723dbc8e46a6d6aa46f1b39003b9d9c39da769d1b`.
  No readiness probe reran. Its Linux false result remains the recorded
  `ubuntu-latest` versus required `ubuntu-24.04` mismatch; the exact target job
  above supplies P4-W5 acceptance without rewriting that one-shot evidence.
- P4-W5 is complete. D-031 and D-048 bind acceptance to source `c7c4fad` and
  the artifact/member digests above. This evidence closure changes only the
  workstream evidence documents and does not alter accepted source, tests,
  policy, workflows, or artifacts.
- P4-W6 has not started. Notarization readiness remains false and blocks its
  signed/notarized candidate gate. Recovery readiness also remains false and
  blocks the later recovery/rehearsal gates. No signing, notarization,
  publication, tag, release, deployment, production/private-state mutation,
  recovery action, or cutover occurred.

## P4-W6 implementation candidate

- The owner completed the private credential remediation outside public state. A bounded local
  check confirms exactly one usable Developer ID Application identity, and the native-build
  environment can resolve the required CPython and PyInstaller license files. No account, team,
  certificate subject, profile selector, password, or raw notary output entered the repository or
  public evidence.
- D-052 separates release assembly from the accepted P4-W5 path. P4-W5 defaults remain
  `candidate_native-p4w5` and `P4-W5`. No P4-W5 build, smoke target, readiness probe, publication,
  tag, release, deployment, production access, or cutover ran during this implementation pass.
- Red-first P4-W6 contracts initially failed because the release modules and CI/Make targets did
  not exist. The implemented layer signs nested macOS code inside-out with hardened runtime and
  secure timestamps, creates and signs a DMG, requires accepted zero-issue notarization, staples
  and assesses the DMG, and packages Linux as a deterministic checksummed tarball.
- The artifact-only clean-host harness covers install, prior-schema migration, supervised daemon
  health, exact backup/restore bytes, V0-GATE-07, V0-GATE-13, Portable round trip, forced rollback,
  successful upgrade, uninstall, and residue with no source checkout or system Python. A
  disposable rehearsal using already accepted P4-W5 native bytes and only P4-W6 packaging/test
  surfaces passed the complete harness on macOS in about 20 seconds; it did not rebuild or mutate
  P4-W5 evidence.
- The final assembler closes 23 exact coordinates, emits bounded macOS 14 unavailable-runner
  evidence alongside a separate source-equivalent result, writes SPDX 2.3 and license evidence,
  rejects untracked output residue, and revalidates all hashes, checksums, native-build evidence,
  notarization evidence, clean-host records, supported hosts, Portable range, and unpublished
  publication state.
- Initial source candidate `b99e6194d6e4f28cf7b3294b6cd1c63ef734fbac` exposed one bounded
  exact-build failure after all three Python distributions built: `uv build --out-dir` creates an
  exact one-byte `*` `.gitignore` beside its outputs. The inventory continued to reject all
  residue; assembly now removes only that exact generated marker and has a focused fail-closed
  regression for any differing bytes.
- Corrected source `69a9596031d96dfd51f9db3464f13c893709373c` built all six exact Python
  artifacts. Its macOS submission was accepted by Apple, but the bounded parser stopped before
  stapling because a zero-issue notary log represented `issues` as JSON `null` instead of `[]`.
  The parser now accepts only those two zero-issue representations; null, empty-list, nonempty,
  and invalid-shape behavior is covered without recording the submission ID or raw output.
- Source `3439fe68e402be26a87f3774df103c2acfbc2601` built all six exact Python
  artifacts and a Developer ID-signed macOS ARM64 DMG. Apple accepted it with zero issues; the
  ticket stapled and validated, Gatekeeper assessment passed, and the exact DMG passed the complete
  artifact-only lifecycle in 19 seconds. Its SHA-256 was
  `be0a845f2f25c70ae3df0f08e9e949d84ce2e87a0ebffa5e7ab18789b7002fda`.
- That candidate is superseded rather than promoted: the local Make invocation echoed its absolute
  input paths before the bounded harness result. The recipe is now silent and a static contract
  prevents path-bearing command echo. Exact source binding requires fresh artifacts after this
  source-only hygiene repair.
- Source `dda22d761f2019b3ccff82462d72203e25a39851` produced fresh exact Python artifacts
  and a signed, accepted, stapled, validated DMG at SHA-256
  `294698fdfcb029cd97e7d34e75ced47d3792ce8ed45c1c4f21e96b9778321c03`; its path-silent
  macOS lifecycle passed. Exact-head Release audit `33713364009`, Linux native build, Python
  artifact build, and the source-equivalent macOS 14 build/lifecycle passed.
- CI run `33713363991` then reproduced one Linux-only harness defect on all three hosts: GNU
  `cp -R` preserved the managed `current` symlink where macOS `ditto` copied its target. The
  controller now validates and copies the explicit enrolled candidate directory. The downloaded
  exact Linux archive passed the corrected full lifecycle inside a local Ubuntu 24.04 x86_64
  container. Fresh exact-head CI remains required; no failed-only job was rerun.
- Source `25a785a3bee982e136e3fd5189ffd87057ac5a4b` advanced all three exact-head Linux
  jobs past artifact installation, then failed inside the broad prior-schema stage. Exact-head CI
  `33713739975` still passed the Python and Linux builds, macOS 14 source-equivalent lifecycle,
  Phase 4 contracts, and broad Python matrix; Release audit `33713739976` passed.
- Diagnostic source `5e2bbb25da8673803f4abdd8b3c4b8d3bfc0473f` split that stage into bounded
  operations. CI `33714301547` reproduced `prior-schema-init` on Ubuntu 24.04, Ubuntu 26.04, and
  Debian 13. A local Linux reproduction with the fixture tree forced to host-runner UID 1001
  failed at the same operation: GNU `cp -p` preserved that foreign owner inside the root
  container, and the product correctly rejected its owner-only initialization files.
- The fixture copy now uses `cp -RP` into a newly created private destination. This preserves
  bytes, modes, and symlinks while making the lifecycle user the owner. The synthetic foreign-owner
  regression passes the complete artifact-only lifecycle on Ubuntu 24.04 and Debian 13. Local
  Ubuntu 26.04 QEMU extraction is unavailable because its tar/filesystem combination returns
  `Function not implemented`; native exact-head CI remains the acceptance subject for that host.
- Current pre-commit gates pass: `make p4w6-preflight` with 28 focused tests and pinned native
  configuration; the 10-test release-policy suite; `make phase4-contracts` with 147 tests, Ruff,
  strict MyPy on 549 files, and manifest validation; and `make verify` with all 3,249 repository
  tests plus all six source-free distribution builds and artifact policy. Actionlint, ShellCheck,
  Perl syntax, diff integrity, and the immutable readiness hash also pass.
- The immutable snapshot remains exactly
  `753a1635aa2be81f3ebe6b3723dbc8e46a6d6aa46f1b39003b9d9c39da769d1b`. Exact-source native media,
  notarization, the CI host matrix, final manifest, exact-head audits, and independent review remain
  pending until this source candidate is committed.

## P4-W6 accepted source and unpublished candidate closure

- The accepted source is `537bc4f1059ef4b4e8f0916702f38f4e531b13fe`. Its exact-head CI
  `33714932363`, Release audit `33714932452`, and CodeQL `33714929770` passed on attempt one. PR
  #6 reports all 20 checks green, including all three Linux clean-host jobs, the macOS 14
  source-equivalent lifecycle, Python/native builds, Phase 4 contracts, and Python 3.12 through
  3.14 verification.
- Final local gates passed 28 focused P4-W6 tests, 147 Phase 4/security tests, Ruff, strict MyPy on
  549 files, all 3,249 repository tests, six source-free Python builds, artifact policy, the
  source-plus-six-artifact release audit, reachable-history audit, and Gitleaks 8.30.1 over 112
  commits. Local HEAD, remote HEAD, and the clean worktree matched the accepted source.
- Exact Python artifacts matched their CI copies byte for byte. Their SHA-256 values are
  `34780ccd7daa3c54f0ba7a306b275dd2e06e16703793073d43a76a87aa23df38` (app wheel),
  `cb8b0fc5fd01006474714175bb57249d189fea3b0994e7e4604a1972ccddec7e` (app sdist),
  `3f9fc140331cd042466f4f7962f2a38459cb36f4ead14a9c1133850205fffb19` (connector wheel),
  `6184805b26f930c55c870f6f9612207321b488adb25f550e74405b3a86edc012` (connector sdist),
  `65568fe6ed12381e7311dbf72b9c8f300d33a9fca81f5ce350f2f1f99c931be1` (engine wheel), and
  `c663273e6b54e17b2ec92afc857f7af15d05f9b588b95db84dd3de1c83755ff9` (engine sdist).
- The Linux x86_64 archive SHA-256 is
  `aa84da386b70a8be826290d08342636de68ea275b47d9eaecbd74b456e5c72a2`; its checksum-file
  SHA-256 is `f6c79a2d7f54ef45f7f94d598069c10a431e10c91bea8a4b2b2d9736ebb6b707`. The native tree has
  111 members, zero symlinks, membership
  `121db9cdd0622785771b77456327499a82661b4fa9689d0f8e2e4e924647649a`, tree
  `e3abd69ad931f1e20915a4caece3ce6b370cf91d4e9fba57047f1ca96dd8731c`, and source-tree
  `1fd73d629664e7284f2be4a9640be73982b5c7272b74ffaabcae0c5da74a2a11`.
- The macOS ARM64 DMG SHA-256 is
  `aa78303a1b1ac7b42215adada8d7932fe55114391292f622073b9aec825a95ac`; its checksum-file
  SHA-256 is `75ab75e46ca0b8a7eb54351aca927dcdf627db00189bf6e0ff7825c8e00af22d`. Its signed native tree
  has 144 members, four symlinks, membership
  `2b5ddbe0986f8ac4d9a427a950a02db75e9f22cf1a681e3f89768942fb26c344`, tree
  `26ea9d2c5f26eda35e4957255227a8b6470ee6bbf69403df5b58c32a7d30f169`, and the same exact
  source-tree digest as Linux.
- Fifty-four nested code subjects were signed inside-out with hardened runtime and secure
  timestamps. Apple accepted the submission with zero issues; opaque receipt
  `rct_v1_bac00e280d273df7c2fc0aa9293f6039ec46203215e08d87e6e2e7f07447fbf5` is stapled and
  validated, Gatekeeper assessment passed, and no account, team, certificate, profile, submission
  ID, or raw notary output entered public evidence.
- Ubuntu 24.04, Ubuntu 26.04, and Debian 13 passed the exact Linux lifecycle with two-second setup
  receipts. The macOS 14 source-equivalent artifact passed in two seconds. The exact signed DMG
  passed on macOS 26 in four seconds, while the separate macOS 14 record carries only the
  plan-authorized `exact-signed-candidate-runner-unavailable` blocker.
- The release candidate contains exactly 23 coordinates. Its standalone verifier rechecked every
  artifact hash, checksum, build receipt, host role, Portable schema range, SPDX 2.3 document,
  license binding, and the empty tag/package/release sets with publication status `unpublished`.
- An exploratory audit over-scoped the text scanner to the native tar and produced the expected
  size-limit findings; a following command initially masked that exit status. The final gate used
  fail-fast shell behavior and the intended source-plus-six-Python-artifact scope, while dedicated
  native and release-candidate validators covered binary media.
- Child 17 (`01a0658a-9e01-71f3-98fa-6753cacc2c4e`) returned no verdict before two 60-second
  waits triggered the timeout breaker. Fresh child 18 (`01a0658f-0169-7d41-8084-093c4f90d7f7`)
  initially returned `NOT_READY`, P0/P1/P2 `0/1/0`, only because a coordinator interruption left
  four review checks unfinished. Its bounded same-lineage rereview independently completed those
  checks, closed `P4W6-AR-001`, and returned `READY`, P0/P1/P2 `0/0/0`, with no finding.
- P4-W5 remains unchanged at accepted source
  `c7c4fad1b109ac7d7c55d55cdfa57b64a9c910db`. The readiness snapshot remains byte-for-byte
  unchanged at SHA-256
  `753a1635aa2be81f3ebe6b3723dbc8e46a6d6aa46f1b39003b9d9c39da769d1b`; no readiness probe
  or P4-W5 target reran. At P4-W6 closure, P4-W7 was unstarted, and no publication, tag,
  release, deployment, production/private-content access, or cutover had occurred.

## P4-W7 exact-candidate audit

- `P4-W7-AUDIT.md` maps the exact-candidate and aggregate P4A/P4B gates to
  direct evidence. Child 22's final `READY`, P0/P1/P2 `0/0/0`, closes the
  fresh-review gate and accepts P4-W7 plus aggregate P4B.
- Current head `ab3f860e23abab2177341c9598011487eaf5ab2b` differs from
  accepted source `537bc4f1059ef4b4e8f0916702f38f4e531b13fe` only in the
  governed workstream records and gotcha registry allowed by D-031 and D-053.
  The P4-W6 handoff packet validates at 1,936 serialized bytes.
- All exact-source run and job records remain green: CI `33714932363` has 16
  successful jobs, Release audit `33714932452` has the successful artifact,
  source, complete-history, and Gitleaks steps, and CodeQL `33714929770` has
  successful Python and Actions analyses. PR #6 reports 20 successful checks.
- The six unexpired CI bundles were downloaded fresh. Their six Python
  artifacts, Linux media and checksum, Linux native evidence, three Linux host
  receipts, and macOS 14 source-equivalent receipt match the final candidate
  exactly. GitHub reports SHA-256 digests for all six bundles.
- The standalone release verifier returned `valid` with 23 artifacts bound to
  source `537bc4f`. The manifest SHA-256 is
  `e5891a2dab80034199be0102d39fc6ee5ff84499d1fb10c7f3d27e7f7e337ab5`.
  A before/after aggregate over every candidate file remained
  `82c5ee005ed008e6681a401e598ba485650c68def3d6982d504d158d92645a90`.
- Both checksum files pass. DMG integrity, strict signature, staple, and
  Gatekeeper checks pass. Fresh unpacked audits reproduce Linux membership
  `121db9cdd0622785771b77456327499a82661b4fa9689d0f8e2e4e924647649a`
  and tree `e3abd69ad931f1e20915a4caece3ce6b370cf91d4e9fba57047f1ca96dd8731c`,
  plus macOS membership
  `2b5ddbe0986f8ac4d9a427a950a02db75e9f22cf1a681e3f89768942fb26c344`
  and tree `26ea9d2c5f26eda35e4957255227a8b6470ee6bbf69403df5b58c32a7d30f169`.
- Exact Git materialization reproduces source-tree digest
  `1fd73d629664e7284f2be4a9640be73982b5c7272b74ffaabcae0c5da74a2a11`.
  Fresh generic source-plus-six-artifact and complete-history audits pass;
  Gitleaks 8.30.1 scans all 114 currently reachable commits with no finding.
  Artifact policy and candidate license, SPDX, checksum, and residue checks
  pass. The accepted private-denylist result remains the unchanged
  exact-candidate evidence; its private input was not available or recreated.
- Fresh public-state checks report zero tags, releases, release assets, and
  deployments. The three expected PyPI projects and 15 expected public GitHub
  package coordinates are absent. Authenticated Actions artifacts remain
  retained CI evidence only. No publish, push, merge, deployment,
  production/private-state access, cutover, or P4-W8 action occurred.
- Child 22 accepted all three D-054 adjudications: exact-source CI plus direct
  final signed-transform checks, reuse of the unchanged accepted private
  denylist alongside fresh generic/Gitleaks scans, and direct public absence
  checks despite package-list API scope.
- Review children 19 and 20 failed through the Codex CLI before execution when
  both WebSocket and HTTPS transports returned HTTP 404. The second attempt
  changed from explicit Sol to the documented GPT-5.6 family selector before
  the two-identical-failure breaker closed that path. Child 21 then used the
  separate in-app subagent channel and hit the same shared endpoint failure
  before execution.
- Codex CLI 0.153.0 restored execution. Child 22 initially returned
  `NOT_READY`, P0/P1/P2 `0/1/0`, after the CLI's macOS read-only sandbox made
  `codesign --verify --strict` misreport unchanged DMG bytes as modified. The
  exact relative, absolute, and deep strict commands passed immediately in the
  coordinator shell and then in the same reviewer lineage under the
  workspace-write sandbox with its no-write contract intact. The SHA-256 and
  worktree remained unchanged, so child 22 closed `P4W7-001` as a
  sandbox-induced false positive.
- The same reviewer completed live exact-run and publication-absence checks,
  then returned final `READY`, P0/P1/P2 `0/0/0`, with no file change or
  concern. P4-W7 and P4B are complete. Source, candidate, P4-W5, the readiness
  snapshot, and public release state remain unchanged; P4-W8 has not started.

## P4-W8 launch evidence

- PR #6 exact head `7e269219b340335f8567ca7bc13f07b1ee9323f0`
  remained mergeable and clean with 20 successful checks, including every
  protected required check. It was marked ready and squash-merged at
  `10769806361e7ed419b653534e1303b623d84673`.
- The reviewed head and merged commit both have Git tree
  `5c98b73fe854dd05406bf7992174cfbe20092eb9`. Fresh fetch confirmed
  `origin/main` at the merge commit and deletion of the remote P4 branch.
- Post-merge GitHub queries returned no tags, releases, or deployments.
  Candidate verification returned `{"artifact_count":23,"source_sha":
  "537bc4f1059ef4b4e8f0916702f38f4e531b13fe","status":"valid"}`.
- The plan remains
  `7fe6e5d1e48b44fb4fba232661a8b01eeca019e4f35a43f1b2c162914905bfd2`;
  the immutable readiness snapshot remains
  `753a1635aa2be81f3ebe6b3723dbc8e46a6d6aa46f1b39003b9d9c39da769d1b`;
  and the P4-W5 phase prompt remains
  `71760e927bb24a85f7df51e1002f7a262b5adf2edb1fd152fd17370fc7038256`.
- `P4-W8-PHASE-PROMPT.md` was rendered through the workflow-governance
  phase template. The private companion holds separate sensitive phase and
  architecture-review prompts. Child 23 is reserved before dispatch.
- No package, tag, release, deployment, candidate rebuild, private/live
  inventory, backup, restore, rehearsal, production mutation, or P4-W9 action
  occurred during launch.

## P4-W8 architecture-review blocker

- Child 23 remained in recursive static inspection for about 190 seconds and
  returned no closed report before interruption. It changed no file and had no
  live authority; no finding or approval is inferred.
- Child 24 used a materially narrower prompt that prohibited broad search,
  codegraph, Git history, web, and live commands and limited inspection to
  named source regions. It also returned no closed report within its bounded
  wait and changed no file.
- The milestone now records two consecutive review timeouts, zero active
  children, and two total children. D-058 stops implementation and live P4-W8
  work until a functioning independent review path returns a closed verdict.
- Source, candidate, P4-W5, readiness, production, recovery, and disposable
  state remain unchanged. P4-W9 was not started.

## P4-W8 architecture review initial verdict

- The distinct in-app Child 25 completed the bounded static review and changed
  no file. Its initial verdict is `NOT_READY`, P0/P1/P2 `0/7/2`.
- The reviewer rejects direct adaptation of Goal #24's production-oriented
  controller and recommends a clean capability-isolated disposable engine,
  complete owner descriptors plus an fsynced hash-chain rollback journal, one
  closed exact stage manifest, and a verifier that imports no producer code.
- Two feasibility gates precede implementation: an OS-enforced execution
  boundary with no production-root authority and an online-consistency
  primitive that binds both fresh backups to one immutable generation without
  stopping production services.
- The remaining design closures are transaction-unique descriptor/inode/path
  safety, exact native/config/migration/rebuild bindings, a complete required
  surface and writer matrix, marker-bound cleanup, and exact bounded
  production before/after identity.
- `P4W8-AR-001` records only that Child 25 was not given the exact nested
  public prompt path. The prompt exists in this workstream and will be supplied
  by absolute path and digest on same-lineage rereview.
- No implementation or live inventory, backup, restore, staging, service, or
  rehearsal action began. P4-W9 remains prohibited.

## P4-W8 architecture adjudication evidence

- The exact public phase prompt was supplied for same-lineage rereview at
  SHA-256
  `92c93fdf2a6176c8c5db805070a79ba0ebe2e109111cefc07dadfd17394228e7`.
  The private adjudication and bounded feasibility receipt have SHA-256 values
  `78f6242fda1f948c3d50f186c600caed32c5265930425f01fe7363f95a9a456e`
  and `0386cf4b7b0f8eff240f9041b18eab5e72b51c502c295fdd63caa80f7e5ab966`.
- A default-deny macOS sandbox ran the unchanged signed candidate and confined
  child lifecycle. Public-repository reads, an unrelated sibling read, and real
  `launchctl` execution were denied. The probe touched disposable temporary
  state only and returned `passed`.
- The adjudication selects one lease-held source generation across both helper
  backups while production services remain loaded. It closes exact owner and
  rollback-journal schemas, transaction namespace and inode rules, the full
  stage coordinate set, a standalone FD-only verifier, all ten required
  surfaces, marker-bound cleanup, and the bounded production before/after
  digest set.
- Child 25 is reserved on its existing lineage for the mandatory rereview. No
  P4-W8 controller implementation, production inventory, backup, restore,
  service action, rehearsal, publication, deployment, or P4-W9 action has run.

## P4-W8 architecture second verdict

- Child 25 verified all three supplied hashes, changed no file, and returned
  `NOT_READY`, P0/P1/P2 `0/2/0` on the same lineage.
- AR-001 and AR-004 through AR-009 are closed. The accepted closures cover the
  exact prompt, one-generation dual-backup fence, descriptor/inode namespace,
  no-fallback stage, independent verifier, ten-surface matrix, cleanup, and
  production noninterference contracts.
- AR-002 remains until runtime proof covers external same-user signals,
  pre-existing loopback listeners, non-transaction Unix sockets, every bounded
  production root, and all non-allowlisted host service controls. AR-003
  remains until every mutation uses durable write-ahead `planned` and
  `applied` records with crash-point reconciliation.
- The reviewer slot is released. No implementation or live rehearsal action
  has begun, and P4-W9 remains prohibited.

## P4-W8 retained-finding repairs

- The expanded OS-isolation probe passed against all five bound production
  roots and 11 present host service controls. It denied external same-user
  signaling, a pre-existing host loopback listener, a non-transaction Unix
  socket, production-root reads and write-opens, and every service executable.
- The same profile allowed only one exact transaction TCP port, one
  transaction-path Unix socket, same-sandbox child lifecycle, transaction
  writes, and the unchanged signed candidate. The receipt and probe SHA-256
  values are
  `760a55ccd0c1b4f639a5df7ef83f77b55aaf5f8b045b83d12f09af39b33f2841`
  and `4c8bb414ae8ff270d3b05e3184bfed7fb823dd012f8a1e86bd278cbb5bfa8b9c`.
- The amended journal contract now persists and fsyncs a complete `planned`
  intent before every forward or inverse mutation, records `applied` only after
  exact revalidation, and requires rollback-only reconciliation plus crash
  injection at every append, fsync, mutation, and validation boundary.
- Child 25 is reserved on the same lineage to adjudicate only AR-002 and AR-003
  and reconfirm the seven prior closures. Implementation and live rehearsal
  remain stopped.

## P4-W8 architecture READY

- Child 25 verified the amended adjudication, expanded probe source, and V2
  receipt hashes and returned final `READY`, P0/P1/P2 `0/0/0`, without changing
  a file.
- AR-002 and AR-003 are closed. AR-001 and AR-004 through AR-009 remain closed;
  no architecture finding or concern remains before red-first implementation.
- The permission is implementation-only. Populated exact hashes, all 20
  red-first tests, supported lint/type checks, the isolation and crash matrix,
  and an independent pre-execution review must pass before any live inventory,
  helper, backup, restore, service, or rehearsal operation.
- Product source, the 23-coordinate P4B candidate, P4-W5, readiness, production
  ownership, and release/publication state remain unchanged. P4-W9 has not
  started.

## P4-W8 implementation, pre-execution review, and inventory blocker

- The private implementation contains 18 source modules, seven schemas, and
  exactly 20 tests. Pytest passed all 20 tests; Ruff, strict MyPy on the 36
  implementation/test files, and all seven schema metaschema checks passed.
- Child 28 iteratively reviewed the populated implementation. Its final pass
  closed IR-001 through IR-008 and returned `READY`, P0/P1/P2 `0/0/0`, with no
  file change. This authorized owner-only staging and fresh live metadata, not
  a waiver of the fresh-machine stop conditions.
- Owner-only transfer and staging on the canonical macOS ARM64 host preserved
  the accepted source and exact candidate. The remote stage validated all 16
  coordinates before any backup, restore, candidate launch, or rehearsal.
- Fresh read-only inventory found one unexpected inactive but loaded Open Brain
  registration with a known job-class identity. Bounded configuration
  discovery returned five roots, three overlapping the governed production
  roots. This prevents proof of a closed writer map even though no process was
  present at observation time.
- The inventory stop receipt has SHA-256
  `3af46ad50b9e61207e080ce203f887c744dc82fdc7043e955852cac5d036b9f8`.
  Exact-profile and preflight did not complete; backup-access repair, dual
  backup, restore, rehearsal, postflight, and final receipt review did not
  start. Production services, ownership, and content were not changed.

## P4-W8 separate reconciliation evidence

- Retained provenance and fresh read-only metadata agree: the extra loaded
  registration is the intentional scheduled canonical writer. It previously
  completed stabilized empty-queue runs and was deliberately restored in two
  governed writer windows. It is not stale merely because no process exists
  between scheduled runs.
- Role-by-role comparison proves two configuration generations with seven
  unique roots and two separate writer leases. Three roots are identical; the
  divergent capture and state roots have materially different inventories.
- Immutable helper v18's full profile is generated from and restricted to the
  candidate configuration's five roots. It cannot create a complete encrypted
  recovery point for seven roots.
- Independent Child 1 returned `NOT_READY`, P0/P1/P2 `3/0/0`. Findings require
  complete two-generation writer/root/lease inventory, seven-root dual recovery,
  and seven-root rehearsal/isolation/rollback evidence. No implementation or
  remote P4-W8 action is eligible before bounded helper authority and a fresh
  independent pre-execution review.
- No service, manifest, production content, helper, backup, restore, candidate,
  publication, deployment, P4-W9, P4-W5, or readiness state changed.
## Owner-revised scope closure

- Owner scope closure on 2026-09-04 removed Apple notarization and public
  macOS binary distribution from the current source/CI completion boundary.
  Private cancellation record SHA-256
  `cafaf03468842560fbb8ab1e4405be1012ec27e8ca9020d46b7d55eb456eda71`
  binds the prior authority, launcher, script, transaction, and pre-notary DMG.
  The signed DMG remains unpublished and unnotarized. No Apple submission,
  backup, restore, P4-W8, P4-W9, deployment, or production action occurred.
- Final `make verify` passed Ruff, strict MyPy on 549 source files, all 3,249
  tests in 176.31 seconds, six isolated wheel/sdist builds, and artifact policy.

# Phase 4 execution decisions

## D-001: carry the reviewed planning commit onto a fresh implementation branch

- Chosen: create `goal/open-brain-phase4` directly from freshly fetched
  `origin/main`, then cherry-pick the exact reviewed planning commit.
- Rejected: implement on `phase4-planning`, because the goal requires a new
  implementation branch from the fresh public base.
- Rejected: merge a separate planning PR first, because one Phase 4 draft PR
  can carry the reviewed plan and implementation without changing the base
  requirement or adding an unnecessary review cycle.
- Why: this preserves the exact reviewed plan, starts implementation from the
  required base, and keeps one coordinator-owned writer surface.

## D-002: treat Goal #24's earlier apply as immutable history

- Chosen: retain and reference the prior Goal #24 receipts, but build a new,
  separately bound Phase 4 rehearsal and full-stop transaction.
- Rejected: rerun or repurpose the old production apply.
- Why: the latest private handoff explicitly says the earlier apply and stage
  cleanup are complete and must never be rerun. Goal #63 authorizes a later
  transaction only after new P4A, P4B, recovery, and P4-W8 gates pass.

## D-003: use project-local durable context when context-mode is unavailable

- Chosen: search the project gotcha registry, Phase 3 decisions/evidence, and
  the work-brain filesystem for relevant Phase 4 context.
- Rejected: infer prior decisions without a durable-context search.
- Why: no `ctx_search` capability is installed in this runtime, and no Phase
  4-specific work-brain page was found. Project-local verified records remain
  available and authoritative for implementation behavior.

## D-004: classify all 250 tracked Python files under tests

- Chosen: treat all 250 currently tracked Python files under `tests/` as
  manifest subjects, including root `tests/__init__.py` and
  `tests/conftest.py`.
- Rejected: preserve the planning snapshot's count of 248 by omitting the two
  root files.
- Why: the goal says every test must have one owner and disposition, and every
  creation-time count must be reverified. The broader current inventory is the
  stronger and mechanically complete boundary.

## D-005: continue P4A preparation while preserving the notarization blocker

- Chosen: record unavailable notarization credentials as a mandatory P4B
  prerequisite while continuing reversible source-only P4-W0/P4A work.
- Rejected: weaken the signed/notarized macOS requirement or claim readiness
  from the presence of `notarytool` and a local Developer ID identity alone.
- Why: neither checked authorized host has a ready standard notary credential
  profile. No current source milestone consumes that credential, and the goal
  remains able to make meaningful progress before P4B signing.

## D-006: record the exact workspace in P4-W0 and activate it in P4-W1

- Chosen: bind the four members, shared lockfile, backend, and no-sources build
  command in the P4-W0 toolchain record and validator; keep the current root
  project active until P4-W1 creates the four member skeletons atomically.
- Rejected: create empty member projects in P4-W0, which would pull P4-W1 work
  across the reviewed checkpoint.
- Rejected: add workspace members that do not exist, which would make the root
  toolchain unusable and the green P4-W0 gate dishonest.
- Why: P4-W0 records and tests the exact decision; P4-W1 owns physical
  activation. The expected-red report explicitly records inactive membership.

## D-007: one runtime map plus one non-runtime subject map

- Chosen: enrich each existing runtime `files` record in place and add one
  `phase4.subjects` map for every non-runtime subject.
- Rejected: a second runtime ownership list, which could drift from the
  architecture validator.
- Rejected: separate hand-maintained maps for tests, fixtures, schemas,
  resources, entry points, and release tools, which would fragment destination
  uniqueness and make whole-repository closure harder to prove.
- Why: all 555 current subjects share one validator and one destination index,
  while existing runtime import policy keeps its canonical owner fields.

## D-008: pin current verified bundlers without installing them in P4-W0

- Chosen: bind current primary-index versions in the Phase 4 toolchain record
  and install them only when P4-W5 creates real native build subjects.
- Rejected: add native bundlers to the root development environment now,
  before any native adapter/spec exists.
- Why: P4-W0 must make the decision reproducible, while P4-W5 owns build/smoke
  execution. This avoids a placeholder toolchain and unnecessary root weight.

## D-009: keep stable source identities and track movement separately

- Chosen: retain each manifest key as the stable source identity, add an
  explicit `movement_state`, and change `current_path` to the target only after
  that distribution's isolated artifact contract passes.
- Rejected: re-key every record to its destination during each wave, which
  would erase the stable source-to-target identity and create large unrelated
  rewrites in later waves.
- Why: the plan requires the engine contract to pass before manifest state is
  changed to moved. A stable identity plus current and target paths represents
  that order directly and lets the validator reject copied, stale, or
  prematurely claimed files.

## D-010: keep the Phase 7 capture parity scenario with legacy parity

- Chosen: correct `test_capture_scenarios.py` from engine to legacy ownership
  and leave it at its source path until P4-W4.
- Rejected: make the engine test tree import app parity and consume a
  legacy-owned scenario resource.
- Why: collection after the mechanical move proved the test exercises the
  Phase 7 parity harness and its paired legacy fixture, not an isolated engine
  contract. Keeping both in the same later wave preserves the dependency graph
  and prevents a false engine-isolation claim.

## D-011: retain shared test factories in the workspace

- Chosen: keep `tests/unit/storage/_factories.py` as a workspace-owned shared
  test helper and keep `tests/integration/migrate/_synthetic.py` with the
  legacy migration wave.
- Rejected: place either helper in the engine test tree while app, operations,
  parity, or migration tests still import it from the shared test namespace.
- Why: full-suite collection proved the storage factory has cross-distribution
  consumers and the synthetic note helper serves migration tests. Their
  original engine assignments would make unrelated test suites depend on the
  engine test package and misstate ownership.

## D-012: bind installed app scripts only after P4-W2 acceptance

- Chosen: keep all app entry-point subjects `planned`, remove console scripts
  from the nonbuildable P4-W1 app skeleton, and retain source-checkout coverage
  through the app-owned callables and module entry point.
- Rejected: leave dormant `[project.scripts]` declarations in the app skeleton
  and mark them moved before an isolated app wheel exists.
- Why: P4-W2 owns installed entry points. Moving their manifest state during
  P4-W1 would contradict D-009 and claim artifact behavior that has not passed
  acceptance.

## D-013: derive exact artifact membership from the canonical manifest

- Chosen: make `docs/v0-package-classification.json` select every required
  engine wheel and sdist member, with only bounded archive-layout rewrites for
  PEP 639 license destinations. Reject every non-generated member that the
  manifest does not declare.
- Rejected: maintain a second exhaustive allowlist in the artifact policy or
  rely only on required and forbidden patterns.
- Why: the first P4-W1 rereview proved that pattern checks could miss absent
  license files and an undeclared `.gitignore`. One canonical selection plus
  exact archive comparison prevents that drift.

## D-014: keep engine product and test-runner environments separate

- Chosen: install only the engine wheel in one disposable Python 3.12
  environment, install locked pytest/jsonschema tooling in another, copy the
  moved engine tests and bounded shared fixtures into a temporary test root,
  and expose only the installed engine site-packages to the runner.
- Rejected: execute the moved tests through the editable root environment or
  install test tooling into the engine product environment.
- Why: P4-W1 must prove the full engine test subset with repository source,
  app, connectors, legacy, and workspace package sources unavailable.

## D-015: keep one canonical move manifest

- Chosen: correct every review packet and coordination reference to
  `docs/v0-package-classification.json`.
- Rejected: create the nonexistent `release/phase4-move-manifest.json` named in
  one temporary review packet.
- Why: the approved plan and validator already designate the documentation
  manifest as the sole ownership and movement authority.

## D-016: copy isolated product installs out of uv's cache

- Chosen: pass `--link-mode copy` when installing built product wheels into
  disposable isolation environments.
- Rejected: uv's default Linux hardlink mode, because installed Portable
  fixtures then share inodes with uv's cache and violate the product's
  unique-regular-file safety invariant.
- Why: exact-head Linux CI exposed the filesystem-dependent behavior. A clean
  Linux container passes the complete focused contract with copied installs,
  without weakening validation or adding repository sources to the product
  environment.

## D-017: test the app wheel through separate product and test environments

- Chosen: install only app and engine wheels in the product environment. Keep
  pytest and locked development requirements in a separate runner environment,
  copy app test inputs, and expose only product site-packages to those tests.
- Rejected: run app tests through the editable workspace or install test tools
  into the product environment.
- Why: P4-W2 must prove app behavior and entry points without connector,
  legacy, workspace source, or undeclared dependencies. The split environment
  keeps test tooling from becoming product evidence.

## D-018: key Python artifact policy by distribution and kind

- Chosen: use one versioned policy with four coordinates: app wheel, app sdist,
  engine wheel, and engine sdist. Derive exact members for each coordinate from
  the canonical manifest.
- Rejected: run two unrelated policy files or keep global wheel/sdist
  uniqueness, which would reject the expected second wheel and sdist.
- Why: one release identity owns both distributions. Coordinate-level
  uniqueness detects real duplicates while preserving one auditable command
  and one membership authority.

## D-019: keep phased namespace coexistence in the root test harness only

- Chosen: while unmoved modules remain under `src/open_brain`, extend the
  `cli`, `integrations`, and `services` search paths only from root
  `tests/conftest.py`. The isolated app test copy has no source tree, so the
  extension is a no-op there.
- Rejected: add `pkgutil.extend_path` or checkout paths to shipping app code,
  and rejected moving P4-W3/P4-W4 files early merely to satisfy collection.
- Why: regular moved subpackages hide unmoved peer modules during the phased
  root suite. A test-only bridge keeps every wave green without giving the app
  artifact an undeclared workspace dependency. P4-W4 removes the bridge.

## D-020: derive app-to-engine import authority from the canonical manifest

- Chosen: parse app wheel sources and allow only engine modules whose manifest
  records are public.
- Rejected: maintain another hard-coded module allowlist or widen the engine
  facade solely to satisfy existing app imports.
- Why: API status already belongs to the canonical manifest. Reusing it makes
  private imports fail at the artifact boundary and prevents policy drift.

## D-021: validate source layout before rendering checkout supervisor paths

- Chosen: derive a checkout root only when the running module resolves to the
  exact declared `src/open_brain/services/appliance_lifecycle.py` layout.
- Rejected: infer source mode from a fixed number of `__file__` parents.
- Why: an installed wheel has the same module depth under `site-packages`.
  Parent counting fabricated a nonexistent `src` directory and made the
  packaged installed mode unreachable.

## D-022: bind artifact isolation to the active matrix interpreter

- Chosen: create product and test environments with the major/minor version of
  the interpreter running each acceptance job. Keep an explicit version
  parameter for deterministic focused tests.
- Rejected: hard-code Python 3.12 inside the harness while labeling outer CI
  jobs as Python 3.13 and 3.14.
- Why: each matrix leg must execute the installed artifacts on its own
  interpreter, not merely run workspace tests before delegating isolation back
  to Python 3.12.

## D-023: enforce the app import boundary from artifact metadata and the manifest

- Chosen: derive declared external roots from app wheel metadata, derive all
  public and private engine modules from the canonical manifest, resolve
  imported submodule aliases, reject forbidden/undeclared roots, and permit one
  exact reviewed dynamic-import signature.
- Rejected: inspect only `ImportFrom.module`, trust lazy imports to runtime
  coverage, or maintain another engine API list.
- Why: a public parent can expose a private child syntactically, and a lazy
  workspace dependency may never execute during a passing journey. The wheel
  itself must prove both boundaries.

## D-024: keep isolation failure evidence bounded but stage-specific

- Chosen: report the allow-listed harness stage when an installed-app
  subprocess fails, without including commands, paths, stdout, or stderr.
- Rejected: collapse every failure into generic `P4H007` or expose raw
  subprocess output.
- Why: the first exact-head Python 3.12 run failed transiently inside the
  installed-app harness. A bounded stage distinguishes setup, install, tests,
  CLI, and product-contract failures without leaking local details.

## D-025: treat dynamic importer capabilities as reviewed artifact events

- Chosen: track `importlib`, `builtins`, their importer functions, assignment
  aliases, and reflective lookups by lexical provenance. Record both capability
  bindings and calls, then allow only the exact path and function signature of
  the existing optional-provider seam.
- Rejected: match callable spellings globally, allow unresolved aliases, or
  classify every local object named `importlib` as the standard-library module.
- Why: name-only scanning both missed real import paths and rejected unrelated
  code. Capability events fail closed when an importer escapes while preserving
  one narrow, reviewable exception.

## D-026: fail closed on runtime namespace importer acquisition

- Chosen: recognize `sys.modules` and built-in namespace mappings as importer
  provenance, and reject `globals`, `locals`, `vars`, `eval`, and `exec` as
  unreviewed artifact capabilities.
- Rejected: enumerate only direct imports or attempt to interpret dynamic code
  strings inside the acceptance harness.
- Why: Python exposes the same importer through runtime registries and dynamic
  evaluation. The app has no authorized use for those capabilities, so a
  bounded fail-closed policy is simpler and safer than partial interpretation.

## D-027: join provenance across control flow and lexical binding scopes

- Chosen: represent each binding as a set of possible capabilities, merge
  branch and loop outcomes, predeclare function-local names, and model compound
  targets plus nested scope visibility.
- Rejected: let AST traversal order overwrite provenance or treat every shared
  identifier as the module it once referenced.
- Why: a dead or alternate branch must not erase a possible importer path, but
  a loop, context-manager, or exception target must shadow that path inside its
  own body. Conservative joins enforce both halves of the boundary.

## D-028: derive imported members and comprehension bindings from Python semantics

- Chosen: resolve `ImportFrom` aliases through the same module-member model as
  attribute access, and bind comprehension assignment expressions in the
  nearest enclosing non-comprehension scope.
- Rejected: maintain separate per-syntax alias lists or discard all names when
  a comprehension scope exits.
- Why: equivalent member access must carry equivalent provenance regardless of
  syntax. PEP 572 deliberately gives walrus targets different scope from
  comprehension iteration targets, so the analyzer must preserve that split.

## D-029: review semantic authority and value provenance, not surface spelling

- Chosen: normalize equivalent loader modules, model frame and function-type
  reflection, distinguish pristine function parameters from unknown or
  reassigned values across control flow, and deny internal roots at the
  optional-provider runtime boundary.
- Rejected: add isolated string matches for each bypass or continue approving a
  dynamic call solely because its argument identifier has a reviewed name.
- Why: Python exposes the same authority through multiple import and reflection
  spellings, while a stable identifier says nothing about the value it holds.
  Semantic provenance closes both classes without rejecting the app's safe
  `importlib.metadata` and `importlib.resources` APIs.

## D-030: close optional loading instead of sandboxing Python

- Chosen: replace arbitrary module strings with a closed `OptionalProvider`
  enum and immutable lazy-loader registry. The only current provider is the
  declared `openai` extra. Remove every app dynamic-import exception and keep
  P4H009 bounded to its finite adversarial architecture corpus.
- Rejected: continue expanding a general-purpose Python capability analyzer or
  approve a generic loader through increasingly detailed data-flow rules.
- Why: package isolation is an architecture property, not hostile-code
  containment. Closing the authority removes the escape hatch directly and is
  smaller, reviewable, and enforceable at runtime. This supersedes D-029's
  temporary generic-loader provenance repair.

## D-031: bind review to source and reset dispatch budgets by milestone

- Chosen: an independent verdict names the reviewed source SHA. A docs-only
  evidence successor does not invalidate that verdict when source and tests
  are unchanged. Child lineage budgets reset after each verified milestone.
- Rejected: force another source review after every evidence-only commit or
  carry an exhausted lineage budget into the next milestone.
- Why: source review and evidence history have different identities. Keeping
  those identities explicit prevents an infinite evidence-review loop while
  preserving an auditable gate for each milestone.

## D-032: validate review evidence against current source locations

- Chosen: keep one canonical dynamic-import review inventory and validate it
  against every current `open_brain` source location, including manifest
  records whose movement state is `moved`. Legacy source projections may
  select code for debt checks but may not rewrite review evidence.
- Rejected: filter review entries to whichever source subtree a particular
  architecture test happens to scan.
- Why: filtering hid the removed generic-loader exception after its source
  moved into the app distribution. Review evidence must become stale when the
  reviewed source site disappears, regardless of package movement.

## D-033: publish only the demonstrated provisional connector seam

- Chosen: move the five connector-owned runtime files into the independent
  `open-brain-connectors` distribution. Publish only the app extension values
  named by `open_brain.extensions.connectors.__all__` and the versioned worker
  protocol. Keep the app free of a connector dependency and mark compatibility
  provisional v1.
- Rejected: expose app composition or local-store objects, retain the connector
  inside the app wheel, or claim a stable SDK before all proof categories pass.
- Why: the reference connector already demonstrates a narrow capture-only
  contract. Packaging that exact seam preserves dependency direction without
  inventing a broader public surface.

## D-034: run reference conformance in a bounded child

- Chosen: discover entry-point metadata in the parent, load connector code only
  in a fixed isolated child, disable direct socket APIs, apply process and
  output limits, and execute the real YouTube reference connector twice with a
  synthetic host-mediated media capability and durable temporary checkpoint.
- Rejected: import connector code in the app process, grant direct network or
  secret access, or substitute a stub that does not exercise the reference
  connector and replay path.
- Why: P4-W3 needs actual reference and replay evidence without adding a vendor
  integration or exposing production/private state.

## D-035: bind worker output to canonical types, budgets, and replay

- Chosen: use a separate child bootstrap so protocol classes retain their
  canonical module identity. Reconstruct every frozen request and receipt at
  trust boundaries, compare both run counts with the parent-issued budget,
  require replay to create no capture, and accept only metadata-only output.
- Rejected: execute the protocol module directly with `python -m`, trust a
  frozen instance because its type matches, or treat schema-valid child counts
  as host budget evidence.
- Why: direct module execution creates duplicate class identities, while schema
  validation alone does not prove request-bound resource compliance.

## D-036: key connector artifacts and stability evidence separately

- Chosen: use the singular artifact-policy coordinate `connector` so it maps
  exactly to `connector-wheel` and `connector-sdist`. Record reference, event,
  and measurement conformance as three separate stability prerequisites.
- Rejected: use the distribution directory's plural spelling as the manifest
  label or add event/measurement integrations solely to label v1 stable.
- Why: artifact membership is keyed by canonical disposition labels, and a
  passing reference connector does not establish the two deferred proof classes.

## D-037: separate installed metadata from injected in-process compatibility

- Chosen: reserve `open_brain.connectors.v1` for installed metadata and the
  isolated worker. Keep explicitly injected legacy/test connectors on the
  distinct `open_brain.internal_connectors.v1` group. Installed registry
  sources may discover metadata but cannot resolve or execute, and the
  published entry-point object exposes conformance without `run()`.
- Rejected: let the default `ConnectorHost` resolve the same installed entry
  point as the worker or rely only on the connector object's missing methods
  after importing its module in the parent.
- Why: metadata discovery and code execution are separate authorities. Sharing
  one loadable registry path allowed callers to bypass every child-process
  limit despite the worker itself being bounded.

## D-038: start P4-W4 from the canonical inventory with a fresh review budget

- Chosen: bind P4-W4 to clean source `18dd32b`, move only the manifest-owned
  legacy/workspace paths, keep one coordinator as the exclusive writer, and
  reset the milestone child ledger to zero active and zero total. Defer the
  independent review lineage until exact-candidate CI is green.
- Rejected: infer move ownership from the current directory tree, reuse the
  closed P4-W3 reviewer budget, or dispatch overlapping implementation writers.
- Why: 302 paths move across shared import, test, package, and manifest
  surfaces. The canonical inventory prevents ownership drift, while one writer
  preserves atomic integration and the fresh budget keeps P4-W4 review lineage
  independent from P4-W3.

## D-039: give workspace tooling one canonical module identity

- Chosen: import moved release tooling as `tools.open_brain_dev` with only the
  repository root on the import path. Keep `tools/open_brain_dev` outside every
  built distribution.
- Rejected: add both the repository root and `tools` to `PYTHONPATH` or MyPy's
  search path, or retain the checkout-only bare `open_brain_dev` identity.
- Why: the dual search path loaded each tool as both `phase4.*` and
  `tools.phase4.*`; strict MyPy rejected the duplicate module identities. One
  root and one qualified namespace work from checkout without creating a
  second import identity or a packaged workspace dependency.

## D-040: make the private legacy package the truthful outer compatibility layer

- Chosen: declare exact app, connector, and engine dependencies from
  `open-brain-legacy`, and record the same one-way edges in the canonical
  runtime graph. Add a regression that derives cross-distribution imports from
  the legacy source and requires exact agreement with both the graph and wheel
  metadata.
- Rejected: preserve an engine-only metadata claim while the root workspace
  masks app and connector imports, or duplicate current app and source-specific
  connector implementations inside the private package.
- Why: the physical move exposed 42 legacy files that import the current app
  and eight that import connector code. Legacy is the outermost private
  compatibility package, so these declared inward edges cannot make a shipping
  artifact depend on legacy. Truthful metadata removes the hidden source-path
  dependency while retaining predecessor behavior for private compatibility.

## D-041: restore the reviewed legacy dependency boundary

- Chosen: treat D-040 as a failed hypothesis after independent finding
  `P4A-001`. Restore `legacy -> engine`, remove every legacy runtime import of
  app-private and connector modules, and keep the predecessor behavior that
  cannot use a current published interface inside the legacy-only `_compat`
  namespace. That snapshot consumes engine interfaces and is excluded from
  every shipping artifact.
- Rejected: amend the governing plan during its execution, declare broad
  private implementation dependencies, move app or source-specific connector
  behavior into engine, or make the validator bless the observed source graph.
- Why: a dependency can point away from shipping code and still violate the
  reviewed architecture. The private legacy boundary is the designated home
  for predecessor compatibility, while engine remains app-independent and the
  app and connector distributions retain sole ownership of current behavior.

## D-042: use one read-only readiness preflight from P4-W5 through P4-W9

- Chosen: before P4-W5, add one reusable preflight covering signing,
  notarization, macOS ARM64, Linux x86_64, disk capacity, and recovery access.
  Its public result contains only booleans and opaque receipt identifiers; all
  probes are injected read-only capabilities. The serialized snapshot validates
  on restore and is reused unchanged through `P4-W5` to `P4-W9` without calling
  a probe again.
- Rejected: rediscover readiness independently in each wave, emit host paths or
  credential details, or let the preflight sign, notarize, recover, deploy, or
  mutate a host.
- Why: one stable preflight prevents gate drift across P4B and P4C while
  keeping private topology, credentials, and raw operational output outside the
  public repository.

## D-043: inject private compatibility providers instead of importing ambient SDKs

- Chosen: retain provider-neutral legacy compatibility metadata, but require
  its enabled provider loader to be injected by the caller. Enforce the
  private wheel with AST inspection that rejects static and dynamic `_compat`
  imports outside stdlib, engine, and legacy, then exercise the enabled
  injected path in the engine-plus-legacy clean room.
- Rejected: keep an undeclared ambient `openai` import, add an OpenAI extra to
  the private legacy distribution, or delete the provider-neutral metadata
  without proving enabled compatibility behavior.
- Why: the current app owns optional SDK packaging and loading. An explicit
  callable preserves legacy compatibility behavior without creating a hidden
  package edge or weakening the approved `legacy -> engine` boundary.

## D-044: map CLI-style readiness exits to opaque unavailability

- Chosen: catch `SystemExit` explicitly at each readiness probe boundary and
  map it to the same deterministic unavailable receipt as ordinary probe
  exceptions. Keep `KeyboardInterrupt` outside the catch boundary.
- Rejected: catch all `BaseException`, allow CLI helpers to terminate the
  aggregate preflight, or expose their exit text in public evidence.
- Why: a read-only probe may wrap a CLI-style helper that raises `SystemExit`.
  The preflight still must finish with booleans and opaque receipts, while an
  operator interrupt must remain effective.

## D-045: use one compositional P4-W5 preflight and a three-rung test ladder

- Chosen: make `make p4w5-preflight` the only local candidate gate before a
  source freeze. It composes focused P4-W5 tests, pinned Python 3.12 bundler
  validation, workflow and manifest checks, touched-code Ruff and MyPy, and
  `git diff --check`. Small edits run only their focused tests. Full Phase 4,
  repository, native, artifact, and review gates run once after the candidate
  is frozen.
- Rejected: run `make verify` after each edit, duplicate the full verifier in a
  P4-W5 target, or let a passing preflight replace any frozen-candidate gate.
- Why: one narrow gate catches local integration failures without weakening or
  repeatedly paying for the final matrix.

## D-046: preserve readiness evidence and add literal target-native jobs

- Chosen: retain the one-shot readiness snapshot byte for byte through P4-W9.
  Diagnose its Linux false result from the recorded image mismatch only, then
  add a real P4-W5 Linux job on literal `ubuntu-24.04` and a macOS ARM64 job on
  GitHub's `macos-14` YAML label. The latter resolves to the recorded
  `macos-14-arm64` runner image and the build fails if runtime architecture is
  not ARM64.
- Rejected: rerun any readiness probe, reinterpret `ubuntu-latest` as the
  required Linux image, or count emulation as target-native acceptance.
- Why: the private adapter required `ubuntu-24.04`, while the inherited CI jobs
  used `ubuntu-latest`. Static source and the pinned toolchain record explain
  the false result without mutating the snapshot. Notarization and recovery
  remain later blockers and do not prevent the P4-W5 bundler spike.

## D-047: bind the onedir artifact to a manifest and one managed activation link

D-050 clarifies the cleanup rule below for an enrolled candidate whose manifest
has become corrupt.

- Chosen: build one PyInstaller onedir tree whose directory name matches its
  lifecycle candidate ID. Bind its version, platform, Portable range, complete
  tree digest, and executable in `open-brain-native.json`. Activate it through
  the relative `current -> candidates/<id>` link. Uninstall validates every
  managed candidate before removing all managed candidate trees and leaves
  unrelated owner state untouched.
- Rejected: one-file bundling, absolute activation links, unchecked recursive
  deletion, source-checkout launch commands, or a frozen worker that invokes
  `python -m`.
- Why: PyInstaller onedir relies on confined relative symlinks. The same shape
  supports atomic activation and rollback, direct supervisor launch, frozen
  child routing, complete membership audit, and clean managed residue.

## D-048: apply the existing source-and-evidence review binding to P4-W5

- Chosen: freeze one named source candidate before full verification. Bind
  review to that source SHA, both target-native artifact digests, membership
  digests, and host evidence. Any later closure commit may change only this
  workstream's evidence documents and must name the source candidate it closes.
- Rejected: include product, build, policy, test, or artifact changes in an
  evidence closure, or require a new source review solely because bounded
  evidence was recorded after CI.
- Why: the governing plan already requires source SHA plus artifact and host
  binding, and accepted D-031 already defines docs-only evidence successors.
  This is an application of the reviewed contract, not a new exception or
  clarification.

## D-049: bind the opaque readiness receipt to one exact Gitleaks fingerprint

- Chosen: preserve the immutable readiness snapshot and add the single
  `generic-api-key` fingerprint from its introducing commit to
  `.gitleaksignore`. Make changes to that file trigger the full Release audit
  and enforce the exact fingerprint in the P4-W5 contract.
- Rejected: alter or regenerate the snapshot, suppress the generic detector by
  path or rule, or skip Gitleaks for Phase 4 evidence.
- Why: the reported value is a schema-validated opaque receipt, not a
  credential. An exact commit/path/rule/line exception keeps every other
  secret finding fatal while preserving the one-shot snapshot byte for byte.

## D-050: quiesce native upgrades and bind cleanup to trusted enrollment

- Chosen: after compatibility succeeds, journal and stop the active supervisor
  before offline backup, restore, and migrations. A failure after quiescence
  rolls back the candidate when required, restarts the prior daemon, and
  records rollback and daemon-restoration states separately. The native
  adapter keeps a canonical inventory of candidates enrolled while their
  manifests are valid. Uninstall validates healthy enrolled trees, quarantines
  every enrolled tree, and uses the platform's non-symlink-following removal;
  a corrupt enrolled tree remains removable while unregistered state is left
  alone. Native Portable proof uses the public daemon recovery protocol, and
  corrupt-candidate rollback runs through an owner-confirmed upgrade request.
- Rejected: acquire offline recovery authority while the daemon is active,
  ship mutation-capable hidden smoke commands, invoke rollback directly from
  the build harness, manually delete a failed candidate, trust every directory
  found under `candidates/`, or follow symlinks during cleanup.
- Why: the daemon owns mutation authority until it is stopped, while cleanup
  still has to work after the exact corruption that triggered rollback. The
  inventory supplies the pre-corruption ownership fact without granting
  deletion authority over unrelated paths. This clarification cannot satisfy
  P4-W5 until the independent reviewer accepts its exact source candidate.

## D-051: isolate native source and make supervisor quiescence explicit

- Chosen: build each native subject from a temporary `git archive` of the
  named source SHA. Disable replacement objects, reject replacement refs and
  repository-local attributes, neutralize external Git configuration, and
  require each extracted mode and blob ID to match the raw no-replace Git
  tree. Digest that tree before and after PyInstaller and derive allowed
  package resources from tracked files. Give the supervisor lifecycle separate
  quiesce and resume operations. Launchd implements them with `bootout` and
  `bootstrap` plus `kickstart`; systemd uses stop and start. A missing
  ownership inventory may trust only the candidate named by the existing
  relative `current` link.
- Rejected: build from the working tree while ignoring ignored files, allow
  every member below a broad resource directory, treat launchd process
  termination as service quiescence, or enroll every valid-looking candidate
  found during first-run discovery.
- Why: the artifact must be a function of one Git tree, and a KeepAlive job
  must be unloaded before recovery can own the mutation lease. Exact resource
  and ownership sets prevent untracked input or unrelated install state from
  becoming deletion authority.

## D-052: separate release assembly from the frozen native-build contract

- Chosen: add a P4-W6 release-candidate module over bounded, parameterized
  exact-source primitives whose P4-W5 defaults remain unchanged. Package
  macOS ARM64 as a Developer ID Application-signed DMG. Sign every nested
  Mach-O target inside-out with hardened runtime and a secure timestamp,
  regenerate the native tree manifest after signing, sign the DMG, require an
  accepted notarization log with zero issues, then staple and validate before
  computing its final checksum. Package Linux x86_64 as a deterministic
  checksummed tarball. Exercise unpacked artifacts through an artifact-only
  black-box clean-host harness, and bind Python distributions, native media,
  checksums, supervisor resources, SPDX/license evidence, supported hosts,
  Portable compatibility, and bounded host results in one unpublished
  release-candidate manifest.
- Rejected: put Developer ID behavior in the PyInstaller specification, use
  `codesign --deep` as the signing operation, notarize a ZIP that cannot be
  stapled, require a Developer ID Installer certificate that is not currently
  available, place credentials in CI, rebuild from a dirty working tree, or
  infer minimum-macOS compatibility from the newer signing host. The exact
  signed DMG must either run on macOS 14 or carry the plan-authorized bounded
  unavailable-runner record while a source-equivalent macOS 14 candidate runs
  separately.
- Why: release assembly changes artifact bytes and has private authority that
  does not belong in the accepted P4-W5 bundler contract. The separate layer
  keeps exact-source construction reusable, makes secret-bearing effects
  coordinator-only, preserves deterministic Linux media, and leaves one
  auditable manifest as the closure boundary. Child 16 produced no verdict
  before two consecutive waits timed out and was closed under the milestone
  breaker, so D-052 still requires the final exact-source/artifact review.

## D-053: bind P4-W6 to one exact unpublished candidate

- Chosen: accept source `537bc4f1059ef4b4e8f0916702f38f4e531b13fe` only with the exact six
  Python artifacts, Linux archive, signed/notarized/stapled macOS DMG, five executed host records,
  bounded macOS 14 unavailable-runner record, native-build evidence, checksums, SPDX/license
  evidence, and 23-coordinate unpublished manifest reviewed by child 18. Treat a later docs-only
  closure as an evidence successor under D-031; it does not change the reviewed source, tests,
  policy, workflows, or artifact bytes.
- Rejected: accept a superseded artifact, infer notarization from submission success alone, weaken
  the macOS 14 requirement, publish from P4-W6, rerun P4-W5 or the readiness probes, or make a
  source/test/tool change after the exact-source review.
- Why: signing and assembly transform bytes after the native build, so source identity alone is
  insufficient. The closed manifest and independent artifact review bind every transformed byte
  and compatibility claim while preserving the immutable P4-W5 and readiness evidence.

## D-054: audit the frozen P4-W6 candidate without creating a new artifact identity

- Chosen: use exact-source CI `33714932363`, Release audit `33714932452`,
  CodeQL `33714929770`, and their retained outputs as P4-W7's CI rebuild
  evidence. Re-download and compare those outputs with the frozen 23-coordinate
  candidate, then directly revalidate the final signed DMG and unpacked native
  trees. Require the P4-W7 reviewer to accept or reject this boundary
  explicitly.
- Rejected: rerun P4-W5, rerun the readiness probes, rebuild or re-sign the
  accepted P4-W6 candidate, create a new source identity from docs-only closure
  commits, place signing authority in hosted CI, or publish an artifact merely
  to make it reviewable.
- Why: the exact run already rebuilt the six Python distributions, Linux
  archive, and macOS source-equivalent native subject from accepted source
  `537bc4f`. The final DMG is a timestamped, coordinator-only signing and
  notarization transform under D-052, so byte-identical CI reproduction would
  require a new signed artifact and would invalidate the candidate being
  audited. Direct hash, signature, stapling, Gatekeeper, unpacked-tree, host,
  and manifest checks preserve the intended independent evidence boundary.

## D-055: block P4-W7 when no fresh reviewer can execute

- Chosen: preserve the completed exact-candidate audit and stop P4-W7 as
  blocked after two Codex CLI dispatches and one separate in-app subagent
  dispatch all failed at the shared response endpoint before execution.
- Rejected: make a third CLI retry, substitute the coordinator's own judgment,
  reuse child 18's P4-W6 verdict as a fresh P4-W7 verdict, lower the required
  `READY` threshold, use a non-Codex reviewer, or continue to P4-W8.
- Why: the reviewed Phase 4 contract requires a fresh exact-source/artifact
  Codex review after the P4-W7 gates are green. A transport failure provides
  neither approval nor a finding. Stopping retains the verified audit without
  converting unavailable independent judgment into false acceptance.

## D-056: distinguish sandboxed signature failures from artifact failures

- Chosen: resume P4-W7 only after the explicit Codex CLI 0.153.0 update and a
  healthy Doctor check. When child 22's macOS read-only sandbox reported the
  exact DMG signature as modified, preserve the frozen bytes, reproduce the
  identical strict checks in the coordinator shell, and require the same
  reviewer lineage to adjudicate the conflicting machine evidence. Permit a
  workspace-write sandbox for that bounded rereview while retaining an
  explicit no-write contract and before/after hash plus worktree checks.
- Rejected: rebuild or re-sign the candidate, accept the first sandboxed error
  without reproduction, discard the reviewer, self-certify P4B, or weaken the
  final `READY`, P0/P1/P2 `0/0/0` threshold.
- Why: unchanged DMG bytes passed relative, absolute, and deep strict
  signature verification outside the read-only sandbox and in the same
  reviewer lineage after the sandbox changed. Child 22 therefore closed
  `P4W7-001` as a sandbox-induced false positive, accepted every D-054
  adjudication and P4A/P4B criterion, and returned final `READY`, P0/P1/P2
  `0/0/0` without changing a file.

## D-057: merge the exact P4A/P4B tree before a new disposable transaction

- Chosen: merge PR #6 through protected `main` after all 20 checks passed,
  verify the squash commit's tree equals the exact reviewed branch-head tree,
  and start P4-W8 on a new branch from that merge. Build a new Goal #63
  transaction in private governed state, gated first by a read-only
  architecture review. Reuse the approved helper and recovery semantics only
  through explicit new hashes, manifests, tests, and disposable namespaces.
- Rejected: rehearse from the unmerged feature branch, treat squash ancestry
  as the identity proof, modify or rebuild the frozen P4B candidate, invoke
  the historical Goal #24 controller directly, reuse its completed production
  apply, or let P4-W8 touch production writer/service ownership.
- Why: Goal #63 requires the exact reviewed P4A/P4B content to pass protected
  merge before P4C consumes it. Tree equality proves the squash preserved that
  content. A separate reviewed transaction prevents historical production
  authority or stale topology from leaking into the disposable rehearsal.

## D-058: stop before implementation after two architecture-review timeouts

- Chosen: close Child 23 after its broad static review returned no report, then
  make one materially narrower attempt limited to named files and regions.
  Close Child 24 without verdict when it also exceeded the bounded wait, set
  consecutive timeouts to two, and stop before controller implementation or
  live P4-W8 work.
- Rejected: infer approval from partial reviewer exploration, dispatch a third
  equivalent child, self-certify the architecture, weaken the requirement for
  reviewed control code, or begin inventory/backup/rehearsal while the review
  gate is unresolved.
- Why: neither child returned a closed report, finding, or verdict. The shared
  governance breaker requires a changed execution path after two consecutive
  timeouts and preserves the no-production/no-live boundary while review
  availability is restored.

## D-059: use the distinct in-app runtime after closing the CLI path

- Chosen: preserve both timeout records and keep the Codex CLI review path
  closed, then reserve one bounded read-only reviewer on the separately
  exposed in-app subagent runtime using Child 24's fixed named-region prompt.
- Rejected: a third Codex CLI dispatch, a broader prompt, implementation before
  review, or any live inventory, backup, restore, service, or rehearsal action.
- Why: runtime capability discovery exposed an independent execution mechanism
  after D-058. This materially changes the failed path while preserving the
  same no-write/no-live contract and shared P4-W8 child budget.

## D-060: isolate the disposable engine and fence one online backup generation

- Chosen: keep all production discovery, scoped-helper calls, and before/after
  comparison in a non-mutating host-side coordinator; run the rehearsal engine
  and every candidate descendant under a deny-by-default macOS sandbox that
  receives only transaction-owned descriptors and cannot execute real service
  controls. Keep production services loaded while the recovery process holds
  the complete closed set of writer leases across both fresh backups and proves
  the source generation unchanged before, between, and after them.
- Rejected: direct reuse of the Goal #24 production controller, an unrestricted
  same-user process, stopping any production service, two independently sampled
  live backups, or provisioning a macOS VM by consuming the host's remaining
  disk margin or deleting unrelated data.
- Why: the signed candidate and a same-sandbox child ran inside the restricted
  boundary while public-repository reads, sibling reads, and real `launchctl`
  execution were denied. A macOS VM remains the ideal isolation option, but no
  manager or image is installed and current free space cannot safely absorb
  one. The existing writer protocol provides a non-mutating generation fence:
  exclusive acquisition of every inventoried lease completes prior writes and
  prevents conforming writers from entering while both helper snapshots run.

## D-061: exact-endpoint Seatbelt and write-ahead rollback

- Chosen: restrict the sandbox to one transaction TCP port, transaction-path
  Unix sockets, and same-sandbox descendants; deny every other endpoint,
  outside-process signal, production root, and non-allowlisted service tool.
  Journal a durable `planned` record before every forward or rollback mutation
  and a matching `applied` record after identity/state revalidation.
- Rejected: broad loopback access, post-mutation-only journaling, treating tests
  as a substitute for runtime denial, or adding a broker/VM after the narrower
  Seatbelt policy proved enforceable.
- Why: the expanded probe denied reads and write-opens across all five bound
  production roots, an outside same-user signal, a pre-existing loopback
  listener, a non-transaction Unix socket, and all 11 present host service
  tools while preserving exact transaction TCP/Unix, child lifecycle, write,
  and signed-candidate behavior. Write-ahead intent closes the crash window
  between a mutating syscall and its prior post-transition record.

## D-062: accept the reviewed P4-W8 architecture before implementation

- Chosen: accept Child 25's same-lineage `READY`, P0/P1/P2 `0/0/0`, only as
  authority to begin red-first private implementation. Preserve the separate
  no-live gate until generated configuration, migration, rebuild, controller,
  verifier, and sandbox bytes are hash-closed, tested, and independently
  reviewed.
- Rejected: treating architecture READY as rehearsal authority, skipping the
  20 red-first tests, weakening the expanded isolation probe or crash matrix,
  or beginning inventory, helper, backup, restore, or service work early.
- Why: the reviewer explicitly closed AR-002 and AR-003, reconfirmed all seven
  prior closures, and retained no finding. Its stated permission ends at
  implementation and preserves the final pre-execution review boundary.

## D-063: stop P4-W8 on overlapping unexpected writer authority

- Chosen: preserve the fresh-machine inventory failure as a precise blocker
  and stop before exact-profile, preflight, backup, restore, candidate launch,
  or rehearsal. Require the unexpected loaded registration to be reconciled
  outside P4-W8 under separate owner authority, then begin a fresh transaction
  and rerun the complete inventory.
- Rejected: add the unexpected manifest digest to a static allowlist, classify
  the registration as unrelated because its process is currently absent,
  unload or disable it within P4-W8, or continue using a partial writer map.
- Why: bounded configuration discovery proves that three of its five roots
  overlap governed production roots. A loaded registration can regain writer
  authority even while inactive. Continuing would violate the exact closed
  writer-map requirement, while unloading it would violate P4-W8's immutable
  production-service boundary.

## D-064: retain the canonical writer and require seven-root recovery

- Chosen: classify the loaded registration as the intentional transitional
  canonical writer, preserve it unchanged, and keep P4-W8 stopped until a
  versioned helper successor can recover the deduplicated seven-root union
  under both writer leases. Require generation-qualified staging, seven-root
  isolation and postflight evidence, exact two-generation rollback, and fresh
  independent review.
- Rejected: quarantine the registration, infer staleness from an idle scheduled
  process, add only its manifest to the static inventory, treat five-root
  helper snapshots as complete, or copy divergent roots into covered roots.
- Why: retained provenance proves deliberate production ownership. Fresh
  comparison proves two divergent role pairs and two independent leases.
  Removing the writer changes production service state; ignoring its roots or
  lease invalidates recoverability and zero-overlap; copying them mutates
  production content. A narrowly expanded immutable backup helper is the
  smallest architecture that preserves all existing service and content state.

## D-065: remove backup and private cutover from project scope

- Chosen: accept the owner's 2026-09-04 scope correction. Stop all backup,
  restore, recovery-repair, P4-W8, P4-W9, rehearsal, and private cutover work.
  Retain the historical plans and evidence, and finish only the target-compatible
  P4-W6/P4-W7 release-candidate path.
- Rejected: continue the seven-root workflow because the earlier plan required
  it, delete existing backup-related product code or evidence, or uninstall a
  verified helper as incidental cleanup.
- Why: current owner authority outranks the earlier Phase 4 plan. The public
  release-candidate work and the owner's private backup/cutover operation are
  separate concerns; the latter is not part of this project's scope.

## D-066: stop local Apple-ID authentication retries

- Chosen: treat the repeated HTTP 401 response as an external Apple account
  authentication blocker. Preserve the signed candidate, collect at most one
  bounded verbose request identifier for support, and require successful
  Keychain profile validation before any Apple submission.
- Rejected: another unchanged credential retry, bypassing validation, changing
  the signed DMG, weakening the notarization gate, or resuming unrelated
  backup/cutover work.
- Why: the owner confirmed a fresh app-specific password, exact Apple Account,
  active membership, and accepted agreements. Validation failed both with and
  without Team ID, while the signed candidate independently proves the same
  TeamIdentifier. The failure occurs before upload and requires Apple account
  support for the owner's requested Apple-ID authentication method.

## D-067: close P4 as source/CI complete and defer macOS distribution

- Chosen: accept the owner's explicit `skip notarization` instruction. Close
  the current project scope with P4A/P4B source and CI complete, preserve the
  signed DMG as unpublished and unnotarized, and move any future public macOS
  binary distribution into a separate owner-authorized workstream.
- Rejected: continue Apple credential troubleshooting, claim the DMG is
  notarized or public-release-ready, publish it, resume P4-W8/P4-W9, delete
  historical evidence, or remove installed helper versions as cleanup.
- Why: notarization is required for the deferred public binary distribution,
  not for continued open-source source development. An exact cancellation
  record supersedes the earlier submission authority while preserving every
  reviewed byte and the failed authentication history.

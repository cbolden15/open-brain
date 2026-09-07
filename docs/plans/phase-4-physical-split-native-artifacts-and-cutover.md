# Historical Phase 4 implementation plan: physical split, native artifacts, and full-stop cutover

- Status: planning candidate; implementation has not started
- Date: 2026-09-01
- Baseline commit: `3b89a4ba4787a378e6040ff042bd117da881918d`
- Planning branch: `phase4-planning`
- Product authority: [`../v0-product-contract.md`](../v0-product-contract.md)
- Architecture authority: [`option-c-architecture.md`](option-c-architecture.md)
- Current runtime architecture: [`../architecture.md`](../architecture.md)
- Phase 3 evidence: [`phase-3-appliance-control-plane.md`](phase-3-appliance-control-plane.md)
- Parent goals: `cbolden15/agent-config#41` and `cbolden15/agent-config#24`
- Estimate: 2 to 3 weeks for P4A and P4B, followed by one owner-gated production window

Product-scope note: the package split and artifact evidence remain valid foundations. Native
appliance lifecycle and cutover requirements belong to Secure Node or legacy replacement work, not
the default five-minute Open Brain product. Current authority is
[`../product-family.md`](../product-family.md).

## Objective

Complete Phase 4 through three ordered gates:

1. `P4A`, physically split the monolith into isolated engine, app, connector,
   and legacy distributions with enforceable package interfaces.
2. `P4B`, build and verify signed or checksummed native artifacts on the
   supported host matrix without requiring source checkout or system Python.
3. `P4C`, perform one full-stop production cutover to the exact P4B artifact,
   verify the day-0 baseline, and retain every rollback asset.

Phase 4 does not publish a package, create a tag or GitHub release, reserve a
package name, change billing or external accounts, delete a predecessor, or
remove rollback evidence. Public release remains Phase 5.

## Verified baseline

The executor must reverify these facts before changing source:

- Public `main` is clean at the Phase 3 completion commit named above.
- Phase 3 provides one appliance daemon, app-owned scheduling, generated local
  credentials, source-checkout lifecycle ports, backup and disposable restore,
  Portable export/import, upgrade, uninstall, and macOS/Linux CI coverage.
- The current build is one Hatch project and one `open-brain` wheel/sdist under
  `src/open_brain`.
- `docs/v0-package-classification.json` classifies 224 runtime Python files:
  46 engine, 34 app, 5 connector, 135 legacy, and 4 workspace. Temporary live
  architecture debt is empty.
- The repository has 248 tracked Python test files and 36 tracked schema or
  fixture files. They do not yet have one complete Phase 4 destination map.
- The default app path is provider-none and connector-empty. The retained
  YouTube proof is the only implemented connector category.
- A local ARM64 macOS builder, `notarytool`, and at least one valid Developer
  ID Application identity are available. Notary credentials and the Linux
  native builder must be checked without exposing secret values.
- PyInstaller and Nuitka are not project dependencies yet. Their exact versions
  and the matching PyInstaller hooks version remain P4-W0 decisions.

The baseline `make verify` result, current GitHub required checks, runner
architectures, signing/notary status, and private cutover authority are runtime
facts. Record them again in the implementation workstream rather than relying
on this planning snapshot.

## Authority and supersession

This plan refines the approved Phase 4 architecture and records the owner's
2026-09-01 sequencing decision.

For Phase 4 only, this plan and its child goal supersede these older clauses:

- Goal `#24` Gate 3's per-surface transition sequence and per-surface rollback
  rehearsal become one full-stop transaction in which all predecessor writers
  stop before any Open Brain writer starts.
- Goal `#24` Gate 4's one-at-a-time production canaries and fixed eight-surface
  transition order become one rehearsed full-stop production transaction.
- Goal `#24` and Goal `#41` downtime formulas, including the rehearsal-derived
  four-hour cap, do not apply. Duration is measured but is not an acceptance or
  rollback threshold.

The supersession changes sequencing and downtime only. The following remain
mandatory and cannot be weakened:

- complete predecessor capability parity and executable-reference accounting;
- fresh encrypted recovery points and verified disposable restores;
- copy-only or versioned idempotent migration with before/after evidence;
- personal/work privacy boundaries and zero unauthorized egress;
- zero overlapping writers, followed by exactly one authorized Open Brain
  writer for each shared surface;
- automatic rollback on any mandatory integrity, privacy, health, queue,
  ownership, migration, or recovery failure;
- retention of predecessor services, helpers, backups, manifests, and rollback
  assets through stabilization and the later approval boundary;
- a machine-readable day-0 baseline and seven-day reset rules.

When another clause conflicts, this plan governs Phase 4 sequencing. The
stricter data, privacy, verification, rollback, and retention requirement wins.

## Architecture decisions

### Workspace and namespaces

Use one uv workspace for development coordination and one shared lockfile. The
workspace is not isolation evidence. Every shipping distribution must also be
built with workspace sources disabled and installed into a disposable
environment that cannot import repository source.

The target ownership is:

```text
packages/
  engine/       distribution: open-brain-engine       namespace: open_brain_engine
  app/          distribution: open-brain              namespace: open_brain
  connectors/   distribution: open-brain-connectors   namespace: open_brain_connectors
  legacy/       distribution: open-brain-legacy       namespace: open_brain_legacy
tools/          workspace-only verification and release tooling
tests/          cross-distribution product, contract, and artifact tests
```

Do not use a shared PEP 420 namespace, duplicate ownership of `open_brain`, or
wheel include/exclude rules as a substitute for the physical split. Remove old
import paths by the P4A exit. A private compatibility wrapper belongs in legacy
and cannot enter the app dependency graph or default artifact.

### Dependency direction

The allowed distribution graph is:

```text
app -> engine
connectors -> engine + the app's published extension values
legacy -> published engine migration/Portable interfaces
workspace tools -> any built artifact for verification only
engine -> standard library and declared engine dependencies only
```

No shipping distribution imports legacy or workspace tools. Engine cannot
import app or connectors. Connector implementations cannot import one another,
app composition, the Brain root, local stores, or raw database handles.

### Release and schema identity

Add one machine-readable release compatibility record under `release/`. It is
the authority for the unpublished candidate version, supported Python and host
families, engine/app compatibility, connector API status, and Portable Brain
schema range. Package metadata, native manifests, checksums, supervisor
templates, doctor output, and Portable exports must agree with that record.

Development may use workspace sources. Release checks must build with
`uv build --no-sources`, install only the produced artifacts, and fail on an
undeclared dependency or source-tree import.

### Connector boundary

Phase 4 creates a versioned provisional connector interface, conformance kit,
isolated worker runtime, and separately buildable connector distribution. It
does not claim a stable public SDK compatibility policy. That claim remains
blocked until reference, event, and measurement connectors all pass the same
conformance suite and shared behavior is extracted from evidence.

The worker boundary must enforce manifest identity, explicit enablement,
capture versus action authority, time and output budgets, approved network and
secret capabilities, metadata-only bounded logs, checkpoint receipts, process
termination, and replay behavior. The app continues to work with the connector
distribution physically absent.

### Native artifacts

Use Python 3.12 as the native build interpreter while retaining source and
wheel verification on Python 3.12 through 3.14. Build on the target operating
system because the native bundler is not a cross-compiler.

PyInstaller 6 one-folder mode is the first candidate. P4-W0 pins PyInstaller
and `pyinstaller-hooks-contrib` together. P4-W5 has one working day to close
the recorded spike matrix. Switch to Nuitka standalone only when a reproducible
failure remains after the bounded diagnosis attempts and the same acceptance
matrix is ready for the fallback.

The macOS result is ARM64, signed with Developer ID, notarized, and stapled.
The Linux result is x86_64 and distributed as a checksummed archive. Neither
requires system Python. Signing and notarization evidence stores identities,
digests, timestamps, and result classes only, never credentials or raw account
material.

### Production transition

P4C is one full-stop transaction. There is no dual write, rolling canary, or
surface-by-surface ownership transition. The transaction has these states:

```text
preflight -> recoverable -> predecessors-stopped -> migrated -> open-brain-started
          -> verified -> accepted
          -> rollback-started -> predecessors-restored -> rollback-verified
```

The transition may spend as long as required while verifiable progress
continues. A mandatory check failure triggers rollback; elapsed time alone does
not. No predecessor, backup, helper, manifest, or rollback artifact is deleted.

## Wave map

| Gate | Wave | Outcome |
|---|---|---|
| P4A | `P4-W0` | Exact move manifest, acceptance harness, toolchain decisions, and draft CI |
| P4A | `P4-W1` | Isolated engine distribution |
| P4A | `P4-W2` | Isolated app distribution and installed entry points |
| P4A | `P4-W3` | Connector distribution, provisional interface, and isolated worker |
| P4A | `P4-W4` | Legacy/workspace quarantine, old-path removal, P4A review |
| P4B | `P4-W5` | Native bundler spike and real artifact lifecycle adapter |
| P4B | `P4-W6` | Signed/checksummed clean-host artifact and lifecycle matrix |
| P4B | `P4-W7` | Exact-candidate audit, CI, and P4B review |
| P4C | `P4-W8` | Full-stop disposable rehearsal, rollback receipt, and pre-cutover review |
| P4C | `P4-W9` | Owner-gated production cutover and day-0 baseline |

Every wave starts from the previous clean checkpoint. Implementation workers
do not commit or push. The coordinator owns integration, focused checks, full
checks, checkpoint commits, draft-PR updates, and evidence. Use one exclusive
writer at a time because package and import changes overlap.

## P4-W0: move manifest and acceptance harness

P4-W0 must finish before a source file changes location.

### Move-manifest requirements

Extend `docs/v0-package-classification.json` as the one canonical inventory.
Do not create a second hand-maintained ownership list. For every runtime file,
test, fixture, schema, entry point, generated resource, and release tool, record:

- current repository-relative path;
- target distribution and repository-relative destination;
- runtime namespace and import rewrite, if any;
- API status: public, distribution-private, test-only, or workspace-only;
- artifact disposition for engine wheel/sdist, app wheel/sdist/native,
  connector wheel/sdist, legacy-only, or excluded;
- test and fixture owner;
- compatibility or deletion disposition for the old import path;
- resource-loading and package-data requirements.

Add a validator under `tools/phase4/` with unit tests. It must fail on:

- an unclassified, stale, duplicated, or missing path;
- two sources targeting one destination;
- a destination outside its declared distribution;
- an unowned test, fixture, schema, entry point, or package resource;
- an import rewrite with no destination symbol;
- a forbidden distribution edge or a new dependency cycle;
- an old import path with no explicit removal or legacy disposition;
- a shipping artifact entry that includes legacy, workspace, private, or
  unselected optional code;
- version or Portable schema range disagreement.

Generate human-readable move and import reports from the canonical JSON. The
generated reports are evidence, not a second source of truth.

### Acceptance-harness requirements

Create `tests/phase4/` and reusable helpers that build artifacts into temporary
directories and install them into new isolated environments. The harness must
contain these independently runnable contracts:

1. Engine isolation: install only the engine wheel, import its declared public
   API, run the engine unit/integration subset, and prove app, connectors,
   legacy, and repository source are unavailable.
2. App isolation: install app and engine wheels only, initialize a disposable
   root, run the no-provider first-value journey, create and rename a space,
   accept an unassigned capture and route it later without changing its
   identity, retrieve within that space and across spaces, create several
   sibling proposals from one capture, approve one, reject one, safely edit one
   through both CLI and UI representations, then run daemon/status/doctor,
   backup, restore, Portable export/import, and uninstall. Prove connectors and
   legacy are unavailable throughout.
3. Connector isolation: install connector, app, and engine public artifacts,
   run the reference conformance proof through the isolated worker, and reject
   app composition, local-store, Brain-root, action-authority, and unbounded
   capability access.
4. Artifact membership: inspect every wheel, sdist, and unpacked native
   artifact for required and forbidden members, duplicate files, unsafe paths,
   private residue, and undeclared optional dependencies.
5. Identity compatibility: compare package metadata, native manifest, doctor,
   Portable export, schema fixtures, and compatibility record.
6. Clean-host lifecycle: install, init, start, status, capture, review,
   retrieve, back up, restore, upgrade, stop, uninstall, and inspect residue
   without source checkout or system Python.

The checker must produce stable finding codes and bounded metadata-only output.
P4-W0 records the current monolith's expected-red finding set. Those checks run
outside the default green gate until their owning distribution exists. Unit
tests must prove the harness detects synthetic missing, leaked, mismatched, and
source-path-masked cases. No deliberately failing test is committed to the
default suite.

### Toolchain and CI requirements

P4-W0 also records and tests:

- uv workspace membership and one shared lockfile;
- the build backend for each distribution;
- exact native build Python, PyInstaller, hooks, and fallback versions;
- ARM64 macOS and x86_64 Linux runner identities;
- sanitized Developer ID and notary readiness status;
- release-audit, complete-history audit, artifact-policy, license, and SBOM
  commands for each artifact;
- draft-PR CI jobs for root checks, manifest/harness self-tests, and the current
  public-artifact safety checks. These P4-W0 jobs have real green subjects.

Add each isolated-distribution CI job with the P4A wave that creates its built
artifact. Add macOS and Linux native build/smoke jobs in P4-W5, when the native
adapter and bundler configuration exist. Do not create placeholder green jobs,
make expected-red future work a required check, or block P4-W0 on an artifact
that does not exist yet.

Open the draft PR after the P4-W0 checkpoint. At that point the required jobs
are root verification, manifest/harness self-tests, and current artifact
safety. Each later wave adds and passes the jobs whose real subject it creates.
CI precedes independent review at every later gate. A review of a commit that
has not passed its then-applicable required matrix is not an acceptance verdict.

### P4-W0 checks

```bash
uv run pytest -q tests/phase4/test_move_manifest.py tests/phase4/test_acceptance_harness.py tests/security/test_architecture_imports.py
uv run ruff check tools/phase4 tests/phase4 tests/security/test_architecture_imports.py
uv run mypy
make verify
git diff --check
```

P4-W0 exits with a clean checkpoint, one exact manifest, one bounded expected-
red report, green validator/harness self-tests, pinned toolchain decisions, and
draft CI running. No runtime source has moved and no Phase 4 behavior is claimed.

## Gate P4A: physical split and distribution isolation

### P4-W1: workspace and engine

- Create the root virtual workspace plus the four member project skeletons.
- Move engine-owned files and Portable schemas/conformance data to
  `open-brain-engine` using the manifest and a deterministic import rewrite.
- Publish one explicit engine API and remove engine imports of app, connector,
  legacy, and workspace modules.
- Build with workspace sources disabled, install into a fresh environment,
  and run the engine contract before updating the manifest state to moved.
- Keep the root suite green. Separate any behavior repair from the mechanical
  move and require a red regression first.

### P4-W2: app and entry points

- Move app-owned files to the `open-brain` distribution and depend on the exact
  compatible engine release identity.
- Bind `open-brain` and `open-brain-mcp` only to app-owned installed entry
  points. Preserve the Phase 3 daemon-only mutation and read-only MCP topology.
- Move native supervisor templates and app package data under one declared
  resource-loading contract.
- Prove the app journey from built wheels with connectors and legacy absent.
- Reject an app artifact that imports an engine private module or obtains an
  undeclared workspace dependency.

### P4-W3: connectors and isolated worker

- Move the five connector-owned runtime files and their tests/resources to the
  connector distribution.
- Extract only the already-demonstrated extension values and conformance rules.
  Mark the compatibility status provisional.
- Add the isolated worker and host protocol with explicit capabilities,
  budgets, metadata-only receipts, termination, and replay tests.
- Keep the default app connector-empty. Entry-point discovery must return no
  connector when the distribution is absent and only explicitly enabled names
  when it is installed.
- Do not add event or measurement vendor integrations merely to call the API
  stable. Record those proof categories as the stability prerequisite.

### P4-W4: legacy, workspace tools, and P4A reconciliation

- Move every legacy-owned file to `open-brain-legacy` and every workspace file
  to root tooling. Legacy is not a dependency or packaged member of engine,
  app, connectors, or native artifacts.
- Remove the old monolith tree and every old import path. Private compatibility
  may exist only behind the legacy distribution boundary.
- Reclassify tests and resources to their final locations, regenerate the
  manifest reports, and require zero unresolved movement or dependency finding.
- Run root verification, isolated builds/installs, installed product journeys,
  release/history audits, and the draft-PR matrix.
- After CI is green, obtain a fresh read-only review of the exact P4A candidate.
  Any material repair invalidates the verdict.

P4A exits with independently buildable engine, app, and connector Python
artifacts, a physically quarantined legacy distribution, no old import path,
zero hidden source-path dependency, zero temporary architecture debt, green
required CI, and a `READY` review with P0/P1/P2 `0/0/0`.

## Gate P4B: native artifacts and clean-host proof

### P4-W5: bundler spike and native lifecycle adapter

- Bind the Phase 3 `ArtifactLifecyclePort` to one real native artifact adapter.
- Run PyInstaller one-folder builds on native macOS ARM64 and Linux x86_64.
- Verify package/resource discovery, daemon restart, child-process environment,
  supervisor templates, backup/restore, Portable operations, upgrade,
  uninstall, and clean residue.
- Record build inputs, tool versions, artifact members, digests, runtime logs,
  and failures as bounded evidence.
- If the one-working-day PyInstaller gate fails after changed strategies,
  preserve the evidence and run Nuitka standalone against the same matrix. Do
  not weaken the matrix or support boundary to make either tool pass.

### P4-W6: signed/checksummed clean-host matrix

- Produce one unpublished macOS ARM64 candidate and one unpublished Linux
  x86_64 candidate from the exact source commit.
- Sign, notarize, and staple macOS. Produce checksums for both host artifacts.
- Install on disposable clean hosts without repository source or system
  Python and measure the provisional 15-minute setup target.
- Run the full lifecycle acceptance contract, including exact-byte backup and
  disposable restore, doctor, Portable round trip, prior-schema upgrade,
  rollback, uninstall, and residue scan. Re-prove `V0-GATE-07` with several
  sibling proposals from one capture and CLI/UI approve, reject, and safe-edit
  outcomes. Re-prove `V0-GATE-13` with space creation and rename, later routing
  of an unassigned capture without identity change, and retrieval within one
  space and across spaces.
- Test Linux on the accepted baseline and compatibility hosts. Test macOS on
  the minimum supported major version or record a concrete unavailable-runner
  blocker rather than inferring compatibility from a newer host.
- Generate one unpublished release-candidate manifest binding wheels, sdists,
  native artifacts, supervisor files, checksums, SBOM/license evidence,
  supported versions, and schema compatibility.

### P4-W7: exact artifact candidate

- Rebuild every artifact from the exact candidate in CI.
- Run root, isolated-distribution, clean-host, release-audit, complete-history,
  CodeQL, secret, license, and artifact-residue gates.
- Confirm no package, tag, release, or public download was created.
- Obtain a fresh read-only review after every required CI job is green. The
  reviewer binds the source SHA, artifact digests, manifest, host evidence,
  and every P4A/P4B completion criterion.

P4B exits with one exact, independently reviewed, publishable but unpublished
artifact set. P4B does not authorize production cutover by itself.

## Gate P4C: owner-gated full-stop cutover

Private topology, credentials, content, and raw operational output remain in
the private governed workstream. Public evidence contains bounded result
classes and opaque identities only.

### P4-W8: disposable rehearsal and rollback proof

- Reverify Goal `#24` capability parity, executable references, production
  bindings, scoped helper identity, and current writer/service inventory.
- Create fresh encrypted primary and replica recovery points. Verify both and
  complete disposable restores from both before rehearsing.
- Bind the exact P4B source, wheels, native artifact, checksums, migration
  adapters, configuration, helper, backup receipts, and starting owner map.
- Rehearse one full-stop transaction on disposable state. Stop all predecessor
  writers together, prove zero remaining authority, run copy-only/idempotent
  migration and derived-state rebuilds, start Open Brain, verify all required
  surfaces, then separately force rollback and verify the entire prior owner
  map returns without duplicate or missing writers.
- Duration is recorded but has no cap. Lack of verifiable state change is still
  governed by the workflow heartbeat and failure rules.
- Obtain an independent read-only review of the immutable rehearsal receipt.
  The reviewer must accept the rollback proof and every preserved safety gate
  before P4-W9 becomes eligible.

### P4-W9: production transaction and day-0 baseline

Run only after P4A, P4B, and P4-W8 remain valid on unchanged inputs:

1. Confirm fresh recoverability, artifact identity, configuration identity,
   migration identity, helper identity, health, and rollback readiness.
2. Stop every inventoried predecessor writer and service together. Prove zero
   loaded or active predecessor writer authority before starting Open Brain.
3. Snapshot the stopped state, run copy-only/idempotent migration, and rebuild
   only approved derived state.
4. Install and start the exact P4B native artifact as the sole owner.
5. Verify identity, schema, counts, digests, canonical/source bytes, privacy,
   queues, CLI, MCP, UI, capture, review, retrieval, scheduled work, backup,
   restore, and exactly-one-writer ownership.
6. On any mandatory failure, stop Open Brain, restore the complete prior owner
   map and state from the rehearsed assets, and verify rollback with the same
   integrity, privacy, health, queue, and ownership checks.
7. After successful verification, restore synchronization only through its
   existing safety gate. Keep the excluded peer non-authoritative.
8. Record the machine-readable day-0 baseline and seven-day reset rules on the
   final unchanged state.

Disabled predecessors and every rollback asset remain available. No deletion,
decommissioning, helper removal, repository publication, or public release is
part of P4-W9.

P4C exits only when the live transaction is accepted, every required Open Brain
surface is healthy, no predecessor is active or writable, exactly one writer
owns each shared surface, synchronization is safe, the day-0 baseline validates,
and rollback remains executable.

## Verification matrix

| Boundary | Required evidence |
|---|---|
| Manifest | Complete source/test/resource inventory, unique destinations, allowed graph, zero unresolved old paths |
| Engine | Wheel-only install and tests with app/connectors/legacy/source absent |
| App | Wheel-only first-value and lifecycle journey with connectors/legacy/source absent |
| Connectors | Wheel-only conformance through isolated worker and bounded capabilities |
| Legacy | No shipping dependency or artifact member; private compatibility stays quarantined |
| Python artifacts | Independent wheel/sdist build, install, metadata, license, private-residue, and member checks |
| Native artifacts | Target-native build, no system Python, signing/checksum, install, lifecycle, upgrade, recovery, uninstall, residue |
| Compatibility | One release identity and Portable schema range across all artifacts and exports |
| Production | Fresh recoverability, all writers stopped, migration, exactly-one-writer, integrity, privacy, health, rollback, day-0 |

## Contract traceability

| Requirement | Gate | Principal evidence |
|---|---|---|
| Engine/app/connector/legacy ownership rules | P4A | Move manifest, import graph, isolated builds and installs |
| Connector absence and extension boundary | P4A | App-without-connectors and worker conformance tests |
| `FOUNDATION-PORTABLE-*` | P4A/P4B | Package/resource placement, exact export/import, shared schema range |
| `V0-INSTALL-01` construction only | P4B | Native artifacts for supported host families; publication remains Phase 5 |
| `V0-GATE-01` | P4B | Clean-host setup timing without source or system Python |
| `V0-GATE-07` | P4A/P4B | Wheel-only and native CLI/UI sibling approve, reject, and safe-edit behavior |
| `V0-GATE-08` | P4B/P4C | Artifact backup, disposable restore, exact bytes, doctor, live recoverability |
| `V0-GATE-09` | P4B | Prior-schema artifact upgrade with recovery and rollback evidence |
| `V0-GATE-12` | P4B | Bound Python/native artifacts, supervisor files, checksums, metadata |
| `V0-GATE-13` | P4A/P4B | Wheel-only and native space create/rename, later route identity, scoped/all-space retrieval |
| Goal `#24` parity and writer ownership | P4C | Full-stop receipt, capability checks, zero overlap, exactly one writer |
| Goal `#24` day-0 handoff | P4C | Validated final baseline and reset rules |

## Stop and rollback rules

1. Stop on manifest ambiguity. No move begins while a file, test, fixture,
   resource, import, or artifact disposition is unresolved.
2. Stop on semantic drift. A package move that changes behavior is separated,
   reproduced with a red regression, and verified before movement continues.
3. Stop on hidden dependency. Any isolated environment that imports repository
   source, an undeclared package, legacy, or workspace tooling invalidates the
   gate.
4. Stop on artifact mismatch. A version, schema range, resource, digest,
   signature, checksum, or manifest disagreement invalidates the complete P4B
   candidate.
5. Stop before P4C if source, artifacts, configuration, migrations, helper,
   backups, restore receipts, or rehearsal evidence changed after review.
6. Roll back production on any mandatory integrity, privacy, health, queue,
   ownership, migration, recovery, or synchronization failure. There is no
   elapsed-time rollback trigger.
7. Never delete or decommission a predecessor or rollback asset in Phase 4.
8. Two identical failures require a changed strategy. Three materially
   different failed strategies with no safe route produce a parked outcome.

## Definition of done

Phase 4 is complete only when one bound evidence chain proves:

- P4-W0 through P4-W9 completed in order with clean checkpoints;
- the canonical move manifest and acceptance harness are complete and green;
- engine, app, and connector artifacts build and test in real isolation;
- legacy and workspace code are absent from every shipping/default artifact;
- old import paths and temporary compatibility debt are zero;
- the connector interface and worker are versioned, bounded, provisional, and
  make no unsupported stable-SDK claim;
- macOS ARM64 and Linux x86_64 native candidates pass the clean-host lifecycle,
  recovery, upgrade, uninstall, and residue matrix;
- wheel-only and native paths preserve sibling proposal approve/reject/edit and
  space create/rename/later-route/retrieval identity behavior required by
  `V0-GATE-07` and `V0-GATE-13`;
- required CI passes before each exact-candidate independent review;
- the full-stop rehearsal and rollback receipt are independently accepted;
- the exact reviewed artifact completes the production transaction with all
  required surfaces healthy and exactly one writer per shared surface;
- the day-0 baseline validates and rollback assets remain available;
- no package, tag, GitHub release, public release, predecessor deletion, or
  irreversible cleanup occurred.

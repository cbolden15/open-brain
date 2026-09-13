# Historical Phase 3 implementation plan: appliance control plane

- Status: Independently reviewed and ready for goal contracting; implementation not started
- Date: 2026-09-01
- Baseline commit: `d93c6dae2a22ef028390f30c990b27968229178e`
- Planning branch at grounding: `phase3-planning`
- Plan path: `<repo-root>/docs/plans/phase-3-appliance-control-plane.md`
- Product authority: `<repo-root>/docs/v0-product-contract.md`
- Architecture authority: `<repo-root>/docs/plans/option-c-architecture.md`
- Runtime target architecture: `<repo-root>/docs/architecture/proposed-v0-system-architecture.md`
- Current implemented boundary: `<repo-root>/docs/architecture.md`
- Predecessor plan: `<repo-root>/docs/plans/phase-2-deepen-modules-in-place.md`
- Grounding note: `<repo-root>/docs/ai/workstreams/20260901-open-brain-public-phase3-planning-3c6a30/GROUNDING.md`
- First independent review: `NEEDS_FIX`, P0/P1/P2 `1/4/0`; every finding is addressed in this revision

Product-scope note: the implemented appliance control plane is preserved as Secure Node precursor
and regression evidence. It no longer defines the default Open Brain journey. Current authority is
[`../product-family.md`](../product-family.md).

## Objective

Implement the approved v0 Phase 3 appliance control plane in dependency-ordered waves inside the current monolith, without starting Phase 4 package splitting, native artifact work, publishing, production access, or live Brain access.

Phase 3 is complete only when the source checkout proves the application-owned control plane behavior for:

- `V0-INSTALL-02` through `V0-INSTALL-06`
- `V0-OPS-01` through `V0-OPS-09`
- `V0-SURFACE-02`, `V0-SURFACE-03`, `V0-SURFACE-05`
- `V0-DATA-05`
- `V0-GATE-05`, `V0-GATE-10`, `V0-GATE-14`
- the complete source-checkout recovery behavior required by `V0-GATE-08`; its release-artifact proof remains Phase 4

Phase 3 also must implement the recovery, upgrade, and installation orchestration surfaces needed for `V0-GATE-08`, `V0-GATE-09`, and `V0-INSTALL-01`, but it defers their native-artifact and clean-host proof to Phase 4. The behavior, interfaces, receipts, and failure handling do not defer.

## Definition of complete

The execution branch may exit Phase 3 only when all of the following are true in one verified commit:

1. One app-owned daemon exists as the only canonical writer path for the default profile.
2. The CLI becomes a short-lived control client for mutating and lifecycle operations; it no longer mutates the default profile by opening full engine writer tasks directly.
3. Loopback HTTP and the local UI share one app state surface, with generated local credential bootstrap, browser session auth, strict origin checks, and CSRF protection for browser mutations.
4. Status, doctor, bounded run history, backup, disposable restore, Portable export/import, upgrade, uninstall, and direct Markdown reconciliation work through app-owned orchestration over engine-owned maintenance tasks.
5. Phase 2 import, privacy, portability, no-provider, connector-empty, and MCP read-only boundaries still pass.
6. No wave requires Phase 4 package splitting, native artifact bundling, signing, notarization, publishing, or live/private Brain data.

## Authority and explicit Phase 3 / Phase 4 boundary

Precedence for execution:

1. Runtime and user constraints in this plan.
2. `<repo-root>/CLAUDE.md`
3. `<repo-root>/docs/ai/workstreams/20260901-open-brain-public-phase3-planning-3c6a30/GROUNDING.md`
4. `<repo-root>/docs/v0-product-contract.md`
5. `<repo-root>/docs/plans/option-c-architecture.md`
6. `<repo-root>/docs/architecture/proposed-v0-system-architecture.md`
7. `<repo-root>/docs/architecture.md`
8. `<repo-root>/docs/plans/phase-2-deepen-modules-in-place.md`
9. Current source, tests, package classification, CI, and `pyproject.toml` at `d93c6dae2a22ef028390f30c990b27968229178e`

Phase 3 scope:

- app-owned initialization, daemon lifetime, control transport, internal scheduler, local auth, status/doctor, bounded run history, backup/restore/export/import orchestration, upgrade, uninstall, and direct-edit reconciliation
- engine-owned maintenance and reconciliation task surfaces required by the app
- source-checkout and CI evidence only, using disposable roots and fake credentials

Phase 4 remains deferred:

- `packages/` split and isolated distributions
- PyInstaller or Nuitka native artifacts
- checksums, signing, notarization, installer packaging, clean-host artifact installation
- release publication
- final clean-artifact proofs for `V0-INSTALL-01`, `V0-GATE-01`, `V0-GATE-08`, `V0-GATE-09`, and `V0-GATE-12`

Evidence boundary:

- Phase 3 must prove lifecycle and recovery behavior through the public app in source checkout and CI.
- Phase 4 repeats those same behaviors against native artifacts and supported clean hosts.
- No Phase 3 wave may claim the artifact-defined recovery gate, native artifact success, release-host setup time, or public distribution evidence.

## Architecture choices

Selected option: do it properly

- Add a new app-owned control plane in `src/open_brain/services/` and keep `phase1_*` modules as compatibility surfaces until the new path is proven.
- Add narrow engine-owned maintenance and reconciliation tasks; do not import legacy `operations` or `release` modules into shipping app paths.
- Use one supervised daemon that serves loopback HTTP and the local UI, owns the internal scheduler, and is the only process allowed to invoke canonical-write orchestration for the default profile.
- Use a root-confined Unix domain control socket for CLI control and mutation requests on macOS and Linux. This avoids network exposure for CLI control, keeps writer authority inside the daemon, and avoids placing credentials in CLI arguments or URLs.
- Add a distinct daemon-authority lease scope. The daemon holds that scope for its lifetime and supplies an engine-issued, active authority capability to mutating task composition. Existing per-operation `shared-writer` leases remain a different discriminator, so mutations do not nest or self-conflict.
- Cut over `open-brain` and `python -m open_brain` to the appliance entrypoint in the same wave that claims daemon-only mutation. Remove the standalone public HTTP writer path; MCP remains a separate read-only process over a non-mutating engine read view.

Rejected alternatives:

1. Promote the existing `src/open_brain/operations/`, `src/open_brain/release/`, and 30-job scheduler into the public appliance.
   Rejected because those paths are classified `legacy`, hard-code `JOB-001` through `JOB-030`, and violate the approved one-daemon topology.
2. Keep the CLI as a direct engine writer while adding a daemon only for background schedules.
   Rejected because `V0-OPS-03`, `V0-GATE-05`, and the target architecture require the daemon to own canonical-write authority.
3. Use bearer-only HTTP for both browser and CLI, with no browser session or CSRF model.
   Rejected because the approved local HTTP security model requires a generated credential plus browser session, strict origin checks, and CSRF protection for browser mutations.
4. Treat backup as the same thing as Portable export.
   Rejected because backup preserves recoverable operational instance state, while Portable export is the documented cross-deployment data contract that excludes credentials, indexes, supervisor state, and live SQLite copies.

## Target module, dependency, and process topology

### Target source surfaces

App-owned Phase 3 surfaces:

- `src/open_brain/services/appliance_application.py`
- `src/open_brain/services/appliance_entrypoints.py`
- `src/open_brain/services/appliance_init.py`
- `src/open_brain/services/appliance_daemon.py`
- `src/open_brain/services/appliance_scheduler.py`
- `src/open_brain/services/appliance_auth.py`
- `src/open_brain/services/appliance_history.py`
- `src/open_brain/services/appliance_lifecycle.py`
- `src/open_brain/services/appliance_recovery.py`
- `src/open_brain/services/appliance_status.py`
- `src/open_brain/services/appliance_supervisors.py`
- `src/open_brain/services/http_server.py`
- `src/open_brain/services/runtime.py`
- `src/open_brain/services/phase1_application.py`
- `src/open_brain/services/phase1_entrypoints.py`
- `src/open_brain/integrations/phase1_ui.py`
- `src/open_brain/profile.py`
- `src/open_brain/config.py`
- `src/open_brain/__main__.py`
- `pyproject.toml`

Engine-owned Phase 3 additions:

- `src/open_brain/engine/authority.py`
- `src/open_brain/engine/backup.py`
- `src/open_brain/engine/backup_ports.py`
- `src/open_brain/engine/maintenance.py`
- `src/open_brain/engine/reconciliation.py`
- `src/open_brain/engine/contracts.py`
- `src/open_brain/engine/local.py`
- `src/open_brain/engine/portability.py`
- `src/open_brain/engine/portability_ports.py`
- `src/open_brain/core/locks.py`
- `src/open_brain/storage/locks.py`

Intentional non-goals for shipping imports:

- `src/open_brain/operations/**`
- `src/open_brain/release/**`
- `src/open_brain/production/**` except existing connector proof paths already accepted in Phase 2

### Target test surfaces

- `tests/integration/services/test_appliance_init.py`
- `tests/integration/services/test_appliance_control.py`
- `tests/integration/services/test_appliance_daemon.py`
- `tests/integration/services/test_appliance_scheduler.py`
- `tests/integration/services/test_appliance_supervisors.py`
- `tests/integration/services/test_appliance_recovery.py`
- `tests/integration/services/test_appliance_upgrade.py`
- `tests/integration/services/test_appliance_uninstall.py`
- `tests/integration/services/test_appliance_run_history.py`
- `tests/integration/services/test_appliance_entrypoints.py`
- `tests/integration/ui/test_phase3_ui.py`
- `tests/integration/engine/test_maintenance.py`
- `tests/integration/engine/test_backup.py`
- `tests/integration/engine/test_reconciliation.py`
- `tests/integration/engine/test_portability.py`
- `tests/integration/services/test_phase1_surfaces.py`
- `tests/integration/services/test_phase2_surfaces.py`
- `tests/integration/services/test_entrypoints.py`
- `tests/integration/services/test_service_composition.py`
- `tests/integration/services/test_protocols.py`
- `tests/security/test_appliance_auth.py`
- `tests/security/test_appliance_logs.py`
- `tests/security/test_direct_edit_reconciliation.py`
- `tests/security/test_architecture_imports.py`
- `tests/security/test_architecture_boundaries.py`
- `tests/security/test_no_network.py`
- `tests/security/test_provider_privacy.py`
- `tests/security/test_public_result_residue.py`
- `tests/security/test_ui_sanitization.py`
- `tests/contract/test_portable_brain_v1.py`
- `tests/unit/storage/test_locks.py`

### Target doc and CI surfaces

- `<repo-root>/docs/architecture.md`
- `<repo-root>/docs/plans/phase-3-appliance-control-plane.md`
- `<repo-root>/docs/engineering/gotchas/README.md`
- `<repo-root>/docs/v0-package-classification.json`
- `<repo-root>/.github/workflows/ci.yml`
- `<repo-root>/.github/workflows/release-audit.yml`

### Process topology

```text
open-brain CLI
  -> appliance entrypoints
  -> Unix domain control socket client
  -> supervised daemon

Local browser UI
  -> loopback HTTP
  -> session + origin + CSRF
  -> supervised daemon

Authenticated HTTP intake
  -> loopback HTTP
  -> generated bearer credential
  -> durable ingress only

MCP stdio
  -> separate process
  -> read-only scoped retrieval capability
  -> no writer, no lifecycle, no admin

Supervised daemon
  -> daemon lifetime authority lease
  -> app-owned scheduler
  -> engine maintenance and portability tasks
  -> metadata-only run history and health receipts
```

## Control, auth, writer, scheduler, and recovery invariants

1. Control transport
   The control client for lifecycle and mutating CLI commands is a root-confined Unix domain socket under `.open-brain/run/`. The run directory is `0700`, the socket is owner-only, symlinks and non-socket replacements fail closed, and stale cleanup is allowed only after the daemon-authority lease proves no daemon is active. The protocol uses bounded canonical JSON request and receipt envelopes. It is not reused for browser traffic or exposed on the network.
2. Daemon lifetime writer authority
   Add `LockScope.DAEMON_AUTHORITY` and its own lock discriminator in `core/locks.py` and `storage/locks.py`. The daemon acquires it before composition and holds it until shutdown. Engine mutating task composition requires an unforgeable active authority capability issued inside that held context. Each mutation then acquires the existing, distinct `SHARED_WRITER` lease for its atomic transition. A second daemon cannot acquire authority, and the default app exposes no mutating task set without it.
3. Minimal durable task inventory
   Default recurring work is limited to `engine-recover` and `markdown-reconcile`. `engine-recover` replays the engine's existing capture, route, proposal, review, and publication stages instead of creating parallel drain implementations. `connector-run:<allowlisted-name>` exists only for an explicitly enabled optional connector and is absent from the default profile. `portable-export` and `backup-create` are durable owner-requested jobs, not autonomous recurrence. `doctor`, `status`, `upgrade`, and `uninstall` remain explicit control actions.
4. Browser auth model
   Initialization generates one owner-only credential seed outside `brain.toml` and derives purpose-bound intake and browser-bootstrap credentials. Share HTTP accepts only the intake bearer. Browser login exchanges only the browser credential for a host-only, `HttpOnly`, `SameSite=Strict` session cookie plus a CSRF token. Browser mutations require a valid session, exact allowed origin, and matching CSRF token. Credentials never appear in URLs, HTML, logs, or canonical files.
5. Local HTTP model
   Loopback remains the default bind. The documented remote path is an authenticated SSH tunnel to loopback. Direct private-network bind requires explicit opt-in plus a preflighted external encryption-termination configuration; otherwise it is refused. Bare LAN reachability is never trusted.
6. Scheduler model
   The daemon owns all default-profile due work. No OS-level one-job-per-task topology remains on the public path. Schedules are durable, idempotent, bounded, and replay-safe.
7. Recovery and rollback model
   Backup and Portable export stay separate. A backup includes exact portable bytes plus SQLite-API snapshots of required engine/app operational databases and bounded run evidence. It excludes credentials, indexes, sockets, lock files, supervisor state, and live WAL/SHM copies; restore regenerates purpose-bound credentials and rebuilds indexes. Restore and import always target an empty disposable root first. Replacement or upgrade completion requires an explicit owner request plus verified backup, restore, compatibility, and doctor receipts. Failed init, install, upgrade, or uninstall must leave no second writer and must emit bounded cleanup guidance.
8. Reconciliation model
   Direct Markdown edits are owner-controlled canonical file changes outside the writer lease. Phase 3 adds detection, validation, and retrieval refresh without silently overwriting invalid files or unrelated content.
9. Read-only process model
   CLI reads while the daemon is active use the control protocol. Offline inspection and stdio MCP use a new engine read view that opens existing state read-only, performs no schema creation or recovery, acquires no writer or daemon-authority lease, and exposes no mutating capability. Only help, version, init preflight, and supervisor lifecycle discovery are root-local entrypoint actions.

## Current gap matrix

| Area | Current baseline at `d93c6dae2a22ef028390f30c990b27968229178e` | Required Phase 3 change | Wave |
|---|---|---|---|
| Initialization | `src/open_brain/profile.py` creates layout and stable identities but not generated credential or app init receipt | Add idempotent init receipt, generated local credential, starter-space policy, and install preflight | `P3-W1` |
| Control transport | `src/open_brain/services/phase1_entrypoints.py` opens full app tasks directly inside the CLI process | Move lifecycle and mutating CLI operations behind daemon control socket | `P3-W2` |
| Writer topology | `src/open_brain/engine/local.py` recovers under per-operation writer lease; no daemon lifetime authority exists | Add daemon-only authority lease and block direct default-profile mutation outside daemon | `P3-W2` |
| Scheduler | Legacy `src/open_brain/operations/scheduler.py` models 30 OS jobs | Replace public topology with app-owned internal durable scheduler | `P3-W2` |
| UI auth and parity | `src/open_brain/integrations/phase1_ui.py` uses bearer auth only and lacks page viewing parity, run history, origin, and CSRF | Add session login, page viewing, run history, doctor/status parity, origin, and CSRF | `P3-W3` |
| Maintenance | No public engine health, reconciliation, backup, restore-validation, or migration task set exists | Add narrow engine maintenance and reconciliation tasks | `P3-W1` to `P3-W4` |
| Recovery lifecycle | Portable engine tasks exist, but no public app orchestration for backup, restore, upgrade, or uninstall exists | Add app-owned recovery and lifecycle orchestration | `P3-W4` and `P3-W5` |
| CI host evidence | `.github/workflows/ci.yml` is Ubuntu-only | Add macOS source-checkout lifecycle lane; keep artifact evidence deferred | `P3-W5` |

## Execution waves

Every wave starts from the previous clean checkpoint and ends only after:

- its focused Pytest command passes
- its focused Ruff command passes
- strict MyPy passes via `uv run mypy`
- the full repo check passes via `make verify`
- the wave-specific behavior gate passes
- the wave-specific failure gate is clear

No wave may read or write live Brain roots, private predecessors, production systems, or secrets. All tests use disposable roots, synthetic fixtures, purpose-scoped fake credentials, and ephemeral CI identities only.

### P3-W0: freeze Phase 3 seams and failure tests

Estimate: 1 day.

Prerequisites:

- Grounded authority files unchanged from this plan.
- Current `make verify` remains green at the baseline checkpoint.

Source surfaces:

- `src/open_brain/services/phase1_application.py`
- `src/open_brain/services/phase1_entrypoints.py`
- `src/open_brain/services/runtime.py`
- `src/open_brain/profile.py`
- `src/open_brain/engine/contracts.py`
- `src/open_brain/engine/local.py`

Test surfaces:

- `tests/integration/services/test_entrypoints.py`
- `tests/integration/services/test_phase1_surfaces.py`
- `tests/integration/services/test_phase2_surfaces.py`
- `tests/security/test_architecture_imports.py`
- `tests/security/test_architecture_boundaries.py`

Doc and CI surfaces:

- `<repo-root>/docs/architecture.md`
- `<repo-root>/docs/plans/phase-3-appliance-control-plane.md`

Work:

1. Freeze the app and engine seams required for Phase 3 without moving to Phase 4 packaging.
2. Add test-first negative contracts for the future daemon-only path inside the wave, then make them pass before the W0 checkpoint; do not leave a deliberately failing repository test at wave exit.
3. Reserve the new appliance module names and entrypoint names in docs and tests before lifecycle logic lands.

Focused commands:

```bash
uv run pytest -q tests/integration/services/test_entrypoints.py tests/integration/services/test_phase1_surfaces.py tests/integration/services/test_phase2_surfaces.py tests/security/test_architecture_imports.py tests/security/test_architecture_boundaries.py
uv run ruff check src/open_brain/services src/open_brain/engine src/open_brain/profile.py tests/integration/services tests/security
uv run mypy
make verify
```

Behavior gate:

- The repo names one new appliance path and one daemon-owned mutation path without changing current Phase 2 product behavior yet.

Failure gate:

- Stop if any shipping path still requires `src/open_brain/operations/**` or a new plan step would need `packages/` or native artifact work.

Clean-checkpoint exit:

- Phase 3 seams are explicit, compatibility surfaces still pass, and no unresolved architecture fork remains for control transport, writer ownership, or auth model.

### P3-W1: init, preflight, generated credential, and engine maintenance seams

Estimate: 2 days.

Prerequisites:

- `P3-W0` clean checkpoint.
- No unresolved import-boundary regression.

Source surfaces:

- `src/open_brain/services/appliance_application.py`
- `src/open_brain/services/appliance_entrypoints.py`
- `src/open_brain/services/appliance_init.py`
- `src/open_brain/services/appliance_status.py`
- `src/open_brain/engine/maintenance.py`
- `src/open_brain/engine/contracts.py`
- `src/open_brain/engine/local.py`
- `src/open_brain/profile.py`
- `src/open_brain/config.py`
- `pyproject.toml`

Test surfaces:

- `tests/integration/services/test_appliance_init.py`
- `tests/integration/services/test_appliance_entrypoints.py`
- `tests/integration/engine/test_maintenance.py`
- `tests/unit/engine/test_profile.py`
- `tests/security/test_no_network.py`
- `tests/security/test_provider_privacy.py`
- `tests/security/test_public_result_residue.py`

Doc and CI surfaces:

- `<repo-root>/docs/architecture.md`
- `<repo-root>/docs/plans/phase-3-appliance-control-plane.md`

Work:

1. Add explicit app init and preflight receipts that cover host family, architecture, runtime availability, permissions, disk, provider mode, and supervisor availability.
2. Generate and persist one owner-only local credential outside `brain.toml`.
3. Make init idempotent over credential, stable identities, starter spaces, schemas, and indexes.
4. Add engine maintenance read surfaces for schema state, index state, writer state, backup evidence, export evidence, and queue age, without importing legacy operations code.
5. Add the non-mutating engine read-view contract used by offline inspection and MCP. It must reject absent/newer schemas without creating, migrating, recovering, or acquiring writer authority.

Focused commands:

```bash
uv run pytest -q tests/integration/services/test_appliance_init.py tests/integration/services/test_appliance_entrypoints.py tests/integration/engine/test_maintenance.py tests/unit/engine/test_profile.py tests/security/test_no_network.py tests/security/test_provider_privacy.py tests/security/test_public_result_residue.py
uv run ruff check src/open_brain/services/appliance_application.py src/open_brain/services/appliance_entrypoints.py src/open_brain/services/appliance_init.py src/open_brain/services/appliance_status.py src/open_brain/engine/maintenance.py src/open_brain/engine/local.py src/open_brain/profile.py src/open_brain/config.py tests/integration/services tests/integration/engine tests/unit/engine tests/security
uv run mypy
make verify
```

Behavior gate:

- `V0-INSTALL-02` through `V0-INSTALL-06` initialization behavior is proven at source-checkout level, including generated credentials, bounded preflight failures, cleanup guidance, and no partially authoritative writer.

Failure gate:

- Stop if init rewrites an existing credential, mutates existing content on replay, requires manual TOML editing, or if the read view creates or recovers state.

Clean-checkpoint exit:

- Init is idempotent, preflighted, credentialized, and represented by app-owned receipts; no daemon install occurs yet.

### P3-W2: daemon control plane, one writer, and internal scheduler

Estimate: 2 to 3 days.

Prerequisites:

- `P3-W1` clean checkpoint.
- App init receipts available.

Source surfaces:

- `src/open_brain/services/appliance_daemon.py`
- `src/open_brain/services/appliance_lifecycle.py`
- `src/open_brain/services/appliance_scheduler.py`
- `src/open_brain/services/appliance_supervisors.py`
- `src/open_brain/services/appliance_entrypoints.py`
- `src/open_brain/services/appliance_application.py`
- `src/open_brain/services/runtime.py`
- `src/open_brain/services/phase1_entrypoints.py`
- `src/open_brain/engine/authority.py`
- `src/open_brain/engine/local.py`
- `src/open_brain/core/locks.py`
- `src/open_brain/storage/locks.py`
- `src/open_brain/__main__.py`
- `pyproject.toml`

Test surfaces:

- `tests/integration/services/test_appliance_control.py`
- `tests/integration/services/test_appliance_daemon.py`
- `tests/integration/services/test_appliance_scheduler.py`
- `tests/integration/services/test_appliance_supervisors.py`
- `tests/integration/services/test_appliance_entrypoints.py`
- `tests/integration/services/test_entrypoints.py`
- `tests/security/test_architecture_imports.py`
- `tests/security/test_architecture_boundaries.py`
- `tests/unit/storage/test_locks.py`

Doc and CI surfaces:

- `<repo-root>/docs/architecture.md`
- `<repo-root>/docs/plans/phase-3-appliance-control-plane.md`
- `<repo-root>/docs/v0-package-classification.json`

Work:

1. Add the confined Unix domain control socket and bounded canonical request/receipt envelopes, including stale-socket and replacement-race tests.
2. Add the distinct daemon-authority lock discriminator and engine-issued active authority capability. Hold daemon authority for the process lifetime while keeping per-operation shared-writer locks distinct and non-nested.
3. Cut `open-brain` and `python -m open_brain` over to `appliance_entrypoints` in `pyproject.toml` and `__main__.py`. Remove the standalone public `open-brain-http` path so HTTP can start only inside the authoritative daemon. Keep `open-brain-mcp` on the W1 non-mutating read view.
4. Route all mutating CLI families through the daemon. Route active-daemon reads through control and offline inspection through the non-mutating read view; never fall back from a failed control mutation to direct engine writes.
5. Replace the public 30-job topology with one internal durable scheduler using the exact task inventory above. Every task returns completed, empty, deferred, or failed metadata-only evidence; provider outage, interrupted work, and delayed retry cannot duplicate capture, proposal, or publication state.
6. Implement launchd and systemd lifecycle adapters for install, start, stop, restart, status, and remove, tested against rendered manifests plus injected host command runners. Failed install removes only the incomplete unit and leaves no authority lease or second writer.

Focused commands:

```bash
uv run pytest -q tests/integration/services/test_appliance_control.py tests/integration/services/test_appliance_daemon.py tests/integration/services/test_appliance_scheduler.py tests/integration/services/test_appliance_supervisors.py tests/integration/services/test_appliance_entrypoints.py tests/integration/services/test_entrypoints.py tests/security/test_architecture_imports.py tests/security/test_architecture_boundaries.py tests/unit/storage/test_locks.py
uv run ruff check src/open_brain/services/appliance_daemon.py src/open_brain/services/appliance_lifecycle.py src/open_brain/services/appliance_scheduler.py src/open_brain/services/appliance_supervisors.py src/open_brain/services/appliance_entrypoints.py src/open_brain/services/appliance_application.py src/open_brain/services/runtime.py src/open_brain/services/phase1_entrypoints.py src/open_brain/engine/authority.py src/open_brain/engine/local.py src/open_brain/core/locks.py src/open_brain/storage/locks.py src/open_brain/__main__.py tests/integration/services tests/security tests/unit/storage
uv run mypy
make verify
```

Behavior gate:

- `V0-INSTALL-06`, `V0-OPS-01` through `V0-OPS-04`, and `V0-GATE-05` are proven at source-checkout level by install rollback, exact installed-entrypoint assertions, daemon kill/restart, provider-outage retry, interrupted-job replay, stale-authority rejection, and single-writer tests with no duplicate publication.

Failure gate:

- Stop if the lifetime authority self-conflicts with a per-operation writer lease, any installed/module/MCP path can obtain direct mutation, restart can create a second writer, or the public path still depends on `JOB-001` through `JOB-030`.

Clean-checkpoint exit:

- One daemon owns lifecycle and schedules, every shipped/default mutation reaches it, MCP is mechanically read-only, and launchd/systemd adapters are source-checkout tested without claiming Phase 4 artifact proof.

### P3-W3: owner UI parity, browser auth, HTTP security, and bounded run history

Estimate: 2 days.

Prerequisites:

- `P3-W2` clean checkpoint.
- Daemon control plane working in tests.

Source surfaces:

- `src/open_brain/integrations/phase1_ui.py`
- `src/open_brain/services/appliance_auth.py`
- `src/open_brain/services/appliance_history.py`
- `src/open_brain/services/appliance_status.py`
- `src/open_brain/services/appliance_application.py`
- `src/open_brain/services/appliance_daemon.py`
- `src/open_brain/services/http_server.py`
- `src/open_brain/services/runtime.py`
- `src/open_brain/capture/auth.py`

Test surfaces:

- `tests/integration/ui/test_phase3_ui.py`
- `tests/integration/services/test_phase1_surfaces.py`
- `tests/integration/services/test_phase2_surfaces.py`
- `tests/integration/services/test_appliance_run_history.py`
- `tests/integration/services/test_service_composition.py`
- `tests/integration/services/test_protocols.py`
- `tests/security/test_appliance_auth.py`
- `tests/security/test_appliance_logs.py`
- `tests/security/test_ui_sanitization.py`

Doc and CI surfaces:

- `<repo-root>/docs/architecture.md`
- `<repo-root>/docs/plans/phase-3-appliance-control-plane.md`

Work:

1. Add browser login bootstrap from the purpose-bound browser credential to a host-only session cookie and CSRF token.
2. Enforce exact origin allow-listing and fail-closed browser mutation checks.
3. Change the real `HttpService` router so bounded `POST /api/...` browser requests reach the UI handler only after session/origin/CSRF checks, while `POST /share` reaches only bearer-authenticated intake. Unknown or cross-capability POST routes fail closed.
4. Extend the UI to include page viewing, doctor/status parity, and bounded run history while keeping CLI and UI on one shared app state surface.
5. Make status and doctor report configuration, provider mode, queue age, schema/index state, daemon and writer ownership, locks, last successful run, backup/export evidence, and bounded remediation through both CLI and UI.
6. Keep share intake loopback-bound by default. Document and test an authenticated SSH tunnel as the default remote path and explicit private-bind rejection without configured encryption termination.
7. Ensure logs, supervisor output, sessions, and run history are metadata-only, bounded, and free of private paths or credential material.

Focused commands:

```bash
uv run pytest -q tests/integration/ui/test_phase3_ui.py tests/integration/services/test_phase1_surfaces.py tests/integration/services/test_phase2_surfaces.py tests/integration/services/test_appliance_run_history.py tests/integration/services/test_service_composition.py tests/integration/services/test_protocols.py tests/security/test_appliance_auth.py tests/security/test_appliance_logs.py tests/security/test_ui_sanitization.py
uv run ruff check src/open_brain/integrations/phase1_ui.py src/open_brain/services/appliance_auth.py src/open_brain/services/appliance_history.py src/open_brain/services/appliance_status.py src/open_brain/services/appliance_application.py src/open_brain/services/appliance_daemon.py src/open_brain/services/http_server.py src/open_brain/services/runtime.py src/open_brain/capture/auth.py tests/integration/ui tests/integration/services tests/security
uv run mypy
make verify
```

Behavior gate:

- `V0-SURFACE-02`, `V0-SURFACE-03`, `V0-SURFACE-05`, `V0-OPS-05`, and `V0-OPS-06` pass in source checkout through the composed listener with browser sessions, loopback default bind, exact origin, CSRF, route-capability separation, page viewing, the complete doctor/status inventory, shared state, and bounded metadata-only logs.

Failure gate:

- Stop if credentials appear in logs, URLs, HTML, or result envelopes; if a browser mutation can succeed without valid session, allowed origin, and CSRF token; or if an API POST can be misrouted to share intake or vice versa.

Clean-checkpoint exit:

- CLI and UI observe the same app state, browser mutations are session-bound, and run history stays bounded and metadata-only.

### P3-W4: recovery, direct-edit reconciliation, backup, restore, and Portable orchestration

Estimate: 2 to 3 days.

Prerequisites:

- `P3-W3` clean checkpoint.
- UI and daemon paths stable.

Source surfaces:

- `src/open_brain/engine/backup.py`
- `src/open_brain/engine/backup_ports.py`
- `src/open_brain/engine/reconciliation.py`
- `src/open_brain/engine/maintenance.py`
- `src/open_brain/engine/portability.py`
- `src/open_brain/engine/portability_ports.py`
- `src/open_brain/services/appliance_recovery.py`
- `src/open_brain/services/appliance_application.py`
- `src/open_brain/services/appliance_daemon.py`

Test surfaces:

- `tests/integration/engine/test_backup.py`
- `tests/integration/engine/test_reconciliation.py`
- `tests/integration/engine/test_portability.py`
- `tests/integration/services/test_appliance_recovery.py`
- `tests/integration/services/test_phase1_surfaces.py`
- `tests/contract/test_portable_brain_v1.py`
- `tests/security/test_direct_edit_reconciliation.py`

Doc and CI surfaces:

- `<repo-root>/docs/architecture.md`
- `<repo-root>/docs/plans/phase-3-appliance-control-plane.md`
- `<repo-root>/docs/v0-package-classification.json`

Work:

1. Add a bounded reconciliation task that detects owner Markdown edits, validates schema and space identity continuity, and refreshes retrieval state without silent overwrite.
2. Re-home only demonstrated generic backup behavior into new engine-owned backup contracts, ports, a root-confined local store, a SQLite-backup-API snapshot adapter, and a restore verifier. Shipping code must not import or reclassify the legacy `operations` or `production/sqlite_backup.py` implementations.
3. Bind immutable backup manifests to exact read-back checksums and publish the manifest last at a separate owner-selected destination. Include portable bytes, required SQLite snapshots, and bounded app run state; exclude credentials, indexes, sockets, locks, supervisor state, and live SQLite sidecars.
4. Add app-owned backup creation, verification, empty disposable restore, replacement preflight, fresh credential generation, index rebuild, and doctor orchestration over the new public engine backup tasks. The implementation goal tests replacement only between disposable roots and does not touch a live Brain.
5. Expose Portable export/import through the same app interface and durable owner-requested daemon job model.
6. Keep backup semantics separate from Portable semantics in code, tests, receipts, and docs.

Focused commands:

```bash
uv run pytest -q tests/integration/engine/test_backup.py tests/integration/engine/test_reconciliation.py tests/integration/engine/test_portability.py tests/integration/services/test_appliance_recovery.py tests/integration/services/test_phase1_surfaces.py tests/contract/test_portable_brain_v1.py tests/security/test_direct_edit_reconciliation.py
uv run ruff check src/open_brain/engine/backup.py src/open_brain/engine/backup_ports.py src/open_brain/engine/reconciliation.py src/open_brain/engine/maintenance.py src/open_brain/engine/portability.py src/open_brain/engine/portability_ports.py src/open_brain/services/appliance_recovery.py src/open_brain/services/appliance_application.py src/open_brain/services/appliance_daemon.py tests/integration/engine tests/integration/services tests/contract tests/security
uv run mypy
make verify
```

Behavior gate:

- `V0-DATA-05`, `V0-OPS-04`, `V0-OPS-07`, and `V0-GATE-14` pass through the public app interface. Source-checkout backup, disposable restore, doctor, and exact-byte evidence prove the Phase 3 behavior required by `V0-GATE-08`; the contract's release-artifact gate remains Phase 4.

Failure gate:

- Stop if a shipping import reaches legacy backup code, restore/import can overwrite the only live root, credentials or live SQLite sidecars enter a backup, direct Markdown edits get silently replaced, or backup/export semantics collapse into one undocumented path.

Clean-checkpoint exit:

- New engine-owned backup/reconciliation tasks and app-owned recovery/export/import orchestration are replay-safe and verified on disposable roots without legacy imports, artifact claims, or live data.

### P3-W5: upgrade, uninstall, macOS/Linux source-checkout evidence, and Phase 4 boundary lock

Estimate: 2 days.

Prerequisites:

- `P3-W4` clean checkpoint.
- Recovery receipts and disposable restore path working.

Source surfaces:

- `src/open_brain/services/appliance_lifecycle.py`
- `src/open_brain/services/appliance_recovery.py`
- `src/open_brain/services/appliance_supervisors.py`
- `src/open_brain/services/appliance_entrypoints.py`
- `src/open_brain/engine/maintenance.py`
- `.github/workflows/ci.yml`

Test surfaces:

- `tests/integration/services/test_appliance_upgrade.py`
- `tests/integration/services/test_appliance_uninstall.py`
- `tests/integration/services/test_appliance_supervisors.py`
- `tests/integration/services/test_appliance_entrypoints.py`
- `tests/security/test_appliance_logs.py`
- `tests/security/test_public_result_residue.py`

Doc and CI surfaces:

- `<repo-root>/docs/architecture.md`
- `<repo-root>/docs/plans/phase-3-appliance-control-plane.md`
- `<repo-root>/.github/workflows/ci.yml`

Work:

1. Define an app-owned `ArtifactLifecyclePort` for candidate identity, compatibility preflight, activation, rollback, and removal. Phase 3 supplies injected fake and disposable source-checkout adapters only; Phase 4 must supply the native-artifact adapter without changing the orchestration contract.
2. Add upgrade orchestration that requires an explicit owner request, compatible candidate evidence, a verified backup plus disposable restore, versioned engine/app migrations, activation receipt, and post-migration doctor before success. Any failure invokes the port's rollback and preserves prior authority.
3. Add uninstall orchestration that requires an explicit owner request, stops the daemon, removes the one supervisor unit, invokes artifact removal through the port, and preserves the Brain root by default. Data purge is a separate forbidden operation for this goal.
4. Add macOS source-checkout CI coverage for launchd adapter and lifecycle orchestration; keep Linux source-checkout coverage for systemd adapter and full verify.
5. Record the exact Phase 4 evidence boundary for native artifacts, clean-host install time, artifact residue, and prior-release upgrade proofs.

Focused commands:

```bash
uv run pytest -q tests/integration/services/test_appliance_upgrade.py tests/integration/services/test_appliance_uninstall.py tests/integration/services/test_appliance_supervisors.py tests/integration/services/test_appliance_entrypoints.py tests/security/test_appliance_logs.py tests/security/test_public_result_residue.py
uv run ruff check src/open_brain/services/appliance_lifecycle.py src/open_brain/services/appliance_recovery.py src/open_brain/services/appliance_supervisors.py src/open_brain/services/appliance_entrypoints.py src/open_brain/engine/maintenance.py tests/integration/services tests/security
uv run mypy
make verify
```

Behavior gate:

- `V0-OPS-08`, `V0-OPS-09`, `V0-GATE-10`, and the Phase 3 behavior portion of `V0-GATE-09` pass in source checkout through the artifact-lifecycle port. Native adapter, prior-release artifact, residue, and clean-host proof remain Phase 4.

Failure gate:

- Stop if upgrade/uninstall can run without an explicit owner request, upgrade can declare success without verified recovery and doctor receipts, rollback cannot preserve prior authority, or uninstall can delete the Brain root.

Clean-checkpoint exit:

- Upgrade and uninstall behave correctly in source checkout, CI covers both supported host families at the adapter level, and the Phase 4 artifact boundary is explicit and locked.

### P3-W6: same-commit review and audit gate

Estimate: 1 day.

Prerequisites:

- `P3-W5` clean checkpoint.
- No open TODOs or design forks in changed Phase 3 surfaces.

Source surfaces:

- Every Phase 3 touched source file

Test surfaces:

- Every Phase 3 touched test file
- `tests/security/test_release_audit.py`
- `tests/security/test_release_history_audit.py`
- `tests/security/test_no_network.py`
- `tests/security/test_provider_privacy.py`
- `tests/security/test_public_result_residue.py`

Doc and CI surfaces:

- `<repo-root>/docs/architecture.md`
- `<repo-root>/docs/engineering/gotchas/README.md`
- `<repo-root>/.github/workflows/ci.yml`
- `<repo-root>/.github/workflows/release-audit.yml`

Work:

1. Dispatch a fresh read-only Codex reviewer in a separate session against the exact candidate commit. Require `READY` with P0/P1/P2 `0/0/0`; any material repair invalidates the verdict and requires affected checks, full verification, and fresh review.
2. Add any new Phase 3 gotchas to the gotcha registry in the same commit.
3. Build the candidate artifacts, scan source plus built artifacts with the release audit, and scan reachable Git history with the public-history audit, all using one synthetic denylist.
4. Confirm no Phase 3 change enters Phase 4 packaging, publishing, production, or live Brain scope.

Focused commands:

```bash
uv run pytest -q tests/security/test_release_audit.py tests/security/test_release_history_audit.py tests/security/test_no_network.py tests/security/test_provider_privacy.py tests/security/test_public_result_residue.py
uv run ruff check src/open_brain tests
uv run mypy
tmpdir="$(mktemp -d)"
printf '%s%s\n' "phase3-audit-canary-" "$(git rev-parse HEAD)" > "$tmpdir/private-denylist.txt"
make verify
uv run python -m open_brain.dev.release_audit --root . --private-denylist "$tmpdir/private-denylist.txt" --artifacts dist/*
uv run python -m open_brain.dev.public_history_audit --repository . --private-denylist "$tmpdir/private-denylist.txt"
git diff --check
```

Behavior gate:

- The same commit proves the full Phase 3 behavior set; passes source, built-artifact, and reachable-history safety audits; preserves Phase 2 privacy and boundary guarantees; and contains no publishing or live-data claims.

Failure gate:

- Stop if the audit finds private-value residue, if any new shipping import reaches legacy lifecycle code, or if the review reveals a wave requirement was skipped without explicit traceability.

Clean-checkpoint exit:

- Phase 3 is merge-ready at source-checkout scope, fully reviewed against contract IDs, and explicitly hands native artifact proof to Phase 4 only.

## Host and CI evidence matrix

| Surface | Host / CI target | Evidence in Phase 3 | Deferred to Phase 4 |
|---|---|---|---|
| Linux systemd lifecycle | Ubuntu CI plus local source checkout | Render, install, start, stop, restart, status, remove behavior with fake host runners and disposable roots | Native archive install and clean-host artifact timing |
| macOS launchd lifecycle | `macos-14` CI plus local source checkout | Render, install, start, stop, restart, status, remove behavior with fake host runners and disposable roots | Signed/notarized artifact install and clean-host setup |
| Daemon restart and writer exclusivity | Linux and macOS source-checkout tests | `V0-GATE-05` kill/restart, stale-authority, no-duplicate-publication tests | Artifact-host restart under released bundle |
| Backup and restore | Linux and macOS source-checkout tests | New non-legacy engine backup path, disposable restore, doctor, exact-byte read-back | Artifact-defined `V0-GATE-08`, bundle-level recovery docs, and released artifact reproduction |
| Upgrade | Linux and macOS source-checkout tests | Preflight, recovery evidence, migration, doctor, rollback behavior | Prior released artifact to next released artifact proof for `V0-GATE-09` |
| Uninstall | Linux and macOS source-checkout tests | Stop service, remove supervisor state, preserve Brain root | Native installer removal receipts and clean-host residue scan |
| Packaging | Current repo build and artifact-policy checks | Wheel and sdist remain green through `make verify` and audit | `V0-INSTALL-01`, `V0-GATE-01`, `V0-GATE-08`, `V0-GATE-09`, and `V0-GATE-12` artifact evidence |

## Contract traceability

| Requirement set | Delivery wave | Principal evidence |
|---|---|---|
| `V0-INSTALL-02` | `P3-W1` | init receipt, generated credential, default layout, schema, indexes |
| `V0-INSTALL-03` | `P3-W1` | idempotent replay tests over same root and same credential |
| `V0-INSTALL-04` | `P3-W1` | no-manual-TOML default init and optional starter-space tests |
| `V0-INSTALL-05` | `P3-W1` | bounded preflight receipts for host, runtime, permissions, disk, provider, supervisor |
| `V0-INSTALL-06` | `P3-W1` and `P3-W2` | failed init and failed daemon install cleanup guidance with no partial writer |
| `V0-OPS-01` | `P3-W2` | one-unit lifecycle adapters for launchd and systemd |
| `V0-OPS-02` | `P3-W2` | internal durable scheduler and no public 30-job topology |
| `V0-OPS-03` | `P3-W2` | distinct lifetime daemon authority plus non-nested per-operation writer leases |
| `V0-OPS-04` | `P3-W2` and `P3-W4` | restart-safe engine recovery plus route, review, export, and backup progress receipts |
| `V0-OPS-05` | `P3-W3` | shared status and doctor state across CLI and UI |
| `V0-OPS-06` | `P3-W3` | metadata-only logs and bounded run history tests |
| `V0-OPS-07` | `P3-W4` | verified backup plus disposable restore before live switch |
| `V0-OPS-08` | `P3-W5` | upgrade preflight, verified recovery evidence, migration, doctor |
| `V0-OPS-09` | `P3-W5` | uninstall removes app artifacts and supervisor state, preserves Brain root |
| `V0-SURFACE-02` | `P3-W3` | UI health, inbox, routing, proposals, search, page viewing |
| `V0-SURFACE-03` | `P3-W3` | loopback default, generated credential, private-bind preflight, browser auth model |
| `V0-SURFACE-05` | `P3-W3` | CLI/UI state parity over one application interface |
| `V0-DATA-05` | `P3-W4` | direct Markdown edit reconciliation and stable space-rename provenance |
| `V0-GATE-05` | `P3-W2` | daemon kill/restart with no lost accepted capture or duplicate publication |
| `V0-GATE-08` behavior only | `P3-W4` | source-checkout backup, disposable restore, and doctor through app interface |
| `V0-GATE-09` behavior only | `P3-W5` | source-checkout upgrade orchestration and rollback behavior |
| `V0-GATE-10` | `P3-W5` | uninstall preserves readable Markdown and source records |
| `V0-GATE-14` | `P3-W4` | app-driven Portable export/import with exact bytes and stable identities |
| `V0-INSTALL-01`, `V0-GATE-01`, `V0-GATE-08` artifact proof, `V0-GATE-09` artifact proof, `V0-GATE-12` | Deferred to Phase 4 only | native artifact, clean-host recovery, prior-release upgrade, and packaging publication evidence |

## Stop and rollback rules

1. Stop on authority conflict.
   If current source contradicts the approved product contract, stop and resolve against the contract before adding more code.
2. Stop on Phase 4 leakage.
   If a change would require `packages/`, native bundlers, clean-host installers, signing, notarization, or publishing, stop and record it as Phase 4.
3. Stop on private or live-data dependency.
   If a test or implementation step needs live Brain data, private predecessor roots, real secrets, or production systems, replace it with synthetic fixtures or stop.
4. Roll back to the previous clean checkpoint on writer-authority breach.
   Any second-writer path, direct default-profile mutation bypass, or non-daemon canonical-write route blocks forward progress.
5. Roll back on unsafe recovery behavior.
   If restore, import, upgrade, or uninstall can destroy the only live root or erase data without the contractually required explicit gate, revert the wave and rework it.
6. Two identical failures require a new hypothesis.
   Do not run the same failing command a third time without changing code, inputs, or approach.

## Definition of done

Phase 3 is done when one same-commit source-checkout implementation:

1. passes the wave sequence above through `P3-W6`
2. proves `V0-INSTALL-02` through `V0-INSTALL-06`
3. proves `V0-OPS-01` through `V0-OPS-09`
4. proves `V0-SURFACE-02`, `V0-SURFACE-03`, `V0-SURFACE-05`, `V0-DATA-05`, `V0-GATE-05`, `V0-GATE-10`, and `V0-GATE-14`
5. proves the Phase 3 behavior portions of recovery and upgrade while deferring the artifact-defined `V0-GATE-08` and `V0-GATE-09` proofs to Phase 4
6. preserves all Phase 2 privacy, portability, MCP, connector-empty, import-direction, and no-provider guarantees
7. explicitly does not enter Phase 4 packaging, publishing, production, or live Brain scope

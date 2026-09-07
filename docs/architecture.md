# Architecture

> Current product authority: [`product-family.md`](product-family.md). The older
> [`architecture/proposed-v0-system-architecture.md`](architecture/proposed-v0-system-architecture.md)
> remains historical appliance-design evidence.

## Product boundary

Open Brain is the default direct local product. It creates one platform-local Brain automatically
and provides SQLite-backed capture, search, and Portable Brain export without a required daemon or
service. It does not claim application-level encryption.

Secure Node is the opt-in advanced profile. It adds encrypted custody, compartments,
authorization, receipts, fencing, certified purge, recovery controls, daemon or service operation,
and multi-client access. Brain Protocol v1 and the work historically named M1 belong to Secure
Node.

Both products depend on the same engine-owned record identities and Portable Brain v1 semantic
core. Default-to-Secure-Node upgrade validates an export, preserves its shared identities and bytes,
then applies new custody and compartment policy in the target. It does not reinterpret or copy the
default product's live SQLite files.

The selected near-term packaging keeps one app distribution: `open-brain` installs only the base
path, while `open-brain[secure-node]` adds `open-brain-engine[secure-node]` and the advanced runtime
dependencies. A separate `open-brain-secure-node` distribution remains the stronger isolation
option if the extra cannot pass dependency and release-boundary tests.

`OB1-W0` implements this package boundary. The base app depends only on the base engine. The
`secure-node` extra owns the advanced engine dependencies plus Starlette and Uvicorn. Plain
`open-brain` and `python -m open_brain` route to the direct local CLI. The explicit Secure Node
launchers stay inert until the extra is installed.

The base native artifact has its own entry point and PyInstaller spec under
`release/open-brain`. Its module inventory excludes the retained appliance, server, connector,
legacy, service-management, Secure Node custody, and advanced dependency roots. The historical
artifact under `release/native` remains Secure Node precursor evidence.

## Current transitional architecture

The repository is one uv workspace. Engine, app, connector, and private
legacy code live in separate buildable distributions. Private compatibility source is physically
quarantined under `packages/legacy`; workspace-only release tooling lives under
`tools/open_brain_dev`. The old
`src/open_brain` monolith no longer exists. The retained `single-user-local` appliance profile opens
one engine task set for one owner and one explicit Brain root. It predates the split and is not the
target default `local` profile.

The package map uses these ownership boundaries:

- `core`: immutable values, policies, and ports; no filesystem, network, provider, CLI, HTTP, configuration, integration, or operations imports.
- `capture`, `ledger`, and `review`: application services that depend on typed ports.
- `engine`: the public task surface for Portable identities, generic capture, spaces, independent
  review, publication, recovery, lexical retrieval, and validate/export/clean-import/index-rebuild.
- `app`: profile and application composition. It owns the direct default CLI plus the opt-in Secure
  Node composition and supplies only
  the capability each one needs: CLI, authenticated HTTP/share, local UI, scoped stdio MCP, and
  public-job sinks.
- `connector`: the optional `open-brain-connectors` distribution. Published provisional values live
  under `open_brain.extensions`; the app owns discovery, meters, worker limits, metadata receipts,
  and checkpoint evidence. Connector code receives capture-only authority.
- `storage`, `providers`, and `integrations`: retained adapters selected by composition. Adapters
  do not import CLI/HTTP handlers or one another.
- `operations`: doctor, scheduling, retention, backup, and recovery behavior.
- `dev`: workspace-only development and release-safety tools under `tools/open_brain_dev`.
- `legacy`: retained predecessor and operational compatibility files under the private
  `open_brain_legacy` namespace. It depends only on the published engine and keeps the app and
  source-specific behavior needed by the predecessor inside its private `_compat` namespace. No
  default or shipping artifact depends on or packages legacy code.

Private deployment configuration contains values and rendered manifests, never patched or copied application source.

The file-level classification is authoritative across the workspace: every runtime file has one
owner, such as `engine`, `app`, `connector`, `legacy`, or `workspace`. Every runtime file is now at
its final P4A path; no file is unclassified or assigned to multiple owners.

The current app-owned `profile` module compiles one `single-user-local` Brain root into an
engine-owned context. The engine does not import profile, CLI, UI, service, migration,
or parity code.

`services/phase1_application.py` is the retained Phase 1 composition root, and
`services/phase1_entrypoints.py` is now a compatibility delegate to the appliance entrypoints for
CLI and MCP while keeping HTTP fail-closed through the non-starting appliance stub.
`services/application.py` and `services/entrypoints.py` retain predecessor and scheduled
compatibility behind the legacy boundary; package metadata does not point a default process at
either module.
Phase 3 W1 adds app-owned appliance initialization and status reads. `services/appliance_init.py`
preflights host/runtime/permissions/disk/provider mode/supervisor availability, creates one
owner-only generated local credential outside `brain.toml`, and records an idempotent local init
receipt. `services/appliance_application.py` and `services/appliance_entrypoints.py` expose a
strictly non-mutating offline/MCP read path backed by `profile.open_existing_single_user_local()`
and `engine.local.open_local_read_view()`, which reject absent or newer state schemas instead of
creating, migrating, recovering, or acquiring writer authority.
Phase 3 W2 historically cut the installed scripts over to `services/appliance_entrypoints.py` and
removed a standalone public HTTP entrypoint. The product split later replaced those bindings with
the local default and explicit Secure Node commands. `services/appliance_daemon.py`, `services/appliance_lifecycle.py`,
`services/appliance_scheduler.py`, and `services/appliance_supervisors.py` now own the appliance
control plane: one daemon acquires verified daemon-authority before mutating composition and socket
binding, serves bounded canonical Unix-domain control requests through the owner-only
`.open-brain/run/control.sock` with a bounded listen backlog and accepted-client timeout, owns the
recurring `engine-recover` and `markdown-reconcile`
scheduler inventory under `.open-brain/state/appliance-scheduler/`, and renders deterministic
launchd/systemd units that enter the daemon through the source-checkout-safe
`open_brain.services.appliance_daemon` module with an explicit absolute root. Source and wheel
Secure Node users launch the same process with `open-brain-secure-node daemon`. Mutating Phase 1 CLI
families never fall back to local writes; active reads prefer control and offline inspection stays
on the read-only engine view.
Phase 3 W3 keeps that ownership model for browser traffic. The appliance daemon composes and owns
the single loopback HTTP listener while authority is active, browser sessions bootstrap from the
generated local credential into host-only cookies plus CSRF, and page reads stay on the public
engine retrieval surface instead of importing storage adapters into the app layer. Private-network
binds require explicit opt-in, explicit external HTTPS termination, and an exact external browser
origin; the documented remote path remains an authenticated SSH tunnel to the loopback listener.
Phase 3 W4 adds engine-owned direct-Markdown reconciliation and immutable backup tasks plus
app-owned recovery orchestration. `engine/reconciliation.py` scans only canonical owner Markdown
under `content/spaces/`, rejects symlinks, special files, malformed replacements, and over-budget
inputs, and updates retrieval state without rewriting owner content. `engine/backup.py` and
`engine/backup_ports.py` publish immutable manifests last at a separate destination, include exact
Portable bytes plus required SQLite-backup-API snapshots and bounded immutable appliance run
receipts, and
exclude credentials, indexes, sockets, locks, supervisor state, temporary data, and live SQLite
sidecars. `services/appliance_recovery.py` keeps backup/restore separate from Portable export/import,
restores only into a proven empty disposable root, regenerates a purpose-scoped local credential,
rebuilds the index, and runs doctor/status reads through the appliance authority boundary. The
daemon control socket submits durable owner-requested `backup-create`, `portable-export`, and
`portable-import` jobs to the scheduler attached to the daemon's existing application. They are
replay-safe requests, not recurring background work. Mutable scheduler state is recreated after a
restore so retry bookkeeping cannot change the identity of an already published backup.
Phase 3 W5 keeps upgrade and uninstall at the app boundary. `services/appliance_lifecycle.py`
defines the typed `ArtifactLifecyclePort` for bounded candidate identity, compatibility preflight,
activation, rollback, and removal receipts. Source checkout proves that orchestration only through
injected fake or disposable adapters, verified backup plus disposable-restore preflight, versioned
engine and app migration evidence, authority-preserving restart checks, post-migration doctor, and
data-preserving uninstall. The source-checkout artifact effect boundary stays fail-closed. The P4-W5
frozen entry point is the first composition that injects the manifest-bound native adapter and
bounded host supervisor effects; its target-native smoke uses only isolated temporary supervisor
state. Upgrade checks compatibility while the current daemon is active, journals a quiesce stage,
unloads a launchd KeepAlive job or stops the systemd unit before offline recovery and migrations,
then resumes the correct job after success or failure. Failure receipts distinguish artifact
rollback from daemon restoration. Native builds run from an isolated archive of the named Git tree;
replacement refs and repository-local attributes are rejected, external Git configuration is
neutralized, and extracted blob IDs and modes must equal the raw no-replace tree. Their
package-resource inventory is derived from tracked files and rejected on any extra member. A
distinct kernel-backed lifecycle lease serializes owner requests, while canonical
root-confined journals preserve request identity, stage, terminal receipt, conflict detection, and
crash rollback across processes. The CLI exposes upgrade and uninstall only when composition injects
that lifecycle port; it does not run the self-restarting lifecycle inside the daemon control loop.

Every engine mutation and recovery pass holds the root-confined shared-writer lease across
its SQLite reservation and portable file transitions. Reads remain available outside that
lease and observe only durable stages or atomically replaced files.

`EngineTaskSet` is the composition object, not a representation capability. The Phase 1 UI gets
the narrower Phase 1 task set, each CLI adapter gets its one task protocol, HTTP gets capture,
MCP gets an already scoped retrieval capability plus app feedback, and public jobs get a
`PublicJobCaptureSink`. The MCP adapter rejects an unrestricted retrieval task rather than storing
it and applying scope only during calls.
All of them share the underlying task identities created for the same root.

Retained optional integration metadata is an app-owned extension host. Composition selects a
closed `OptionalProvider` identity whose immutable registry owns the corresponding lazy loader.
The registry currently contains only the declared `openai` extra; arbitrary module strings and
internal package roots are rejected at runtime. Disabled integrations perform no import, while
enabled installed providers do not depend on unrelated preload state. P4H009 checks a finite
adversarial corpus for architecture regressions and is not a malicious-code sandbox. The registry
and its host role are recorded exactly in the package
classification.

The implemented application layers are:

- `capture`: durable intake, authenticated share handling, pinned egress, normalized extraction, versioned redaction, and recovery-safe raw/event/distillation orchestration.
- `providers`: retained legacy provider composition. The Phase 2 single-user profile uses provider
  `none`; provider configuration is not loaded by the default local journey.
- `ledger`: taxonomy-bound scan/stage records, opaque sanitized leaves, trusted citations, a metadata-only inflight journal, independently read-back-verified publication manifests, archive-first slimming, and atomically persisted structured synthesis outside writer locks.
- `review`: closed reference/hold/review-only routing, owner-only terminal decisions, atomic approval/outbox persistence, opaque capture references, receipt-verified owner-output delivery, and predecessor-parity target edit/archive maintenance.

The retained Phase 2 compatibility composition starts with no listener, provider, connector, or
network operation. Its synthetic `JOB-029` proof remains legacy characterization only and is not a
packaged entry point. The Phase 3 appliance daemon instead owns the internal scheduler described
above, with no connector jobs in that retained appliance profile.

Public task results are projections produced after storage and ranking. They retain opaque IDs,
bounded provenance, canonical-state visibility, and useful titles/excerpts while excluding raw or
encoded protected references, absolute paths, credentials, storage-derived slugs and paths, and
reversible source-reference digests. This projection affects results only; Portable/source bytes
remain exact.

Phase 3 owns the appliance lifecycle: initialization, one supervised daemon, internal scheduling,
launchd/systemd integration, backup/restore, upgrade, and uninstall orchestration. The current
Phase 4 split produces isolated, unpublished engine, app, and connector wheels and sdists. The
connector interface is provisional v1. Parent discovery reads entry-point metadata without loading
connector code; explicit allow-list and capability checks precede a bounded child process. The
reference conformance run proves capture-only execution and replay through synthetic host-mediated
transport. A stable Connector SDK promise remains blocked until reference, event, and measurement
proofs all pass. The current v0 release path is Python 3.14 source/wheels on macOS and a checksummed
native archive bundling Python 3.14 on Linux. The P4 macOS native subjects remain unpublished
evidence; DMG signing and notarization are deferred to a later release. Public package publication,
tags, releases, and owner-gated deployment remain separate work.

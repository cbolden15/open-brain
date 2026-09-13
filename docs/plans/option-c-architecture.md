# Option C architecture plan

- Status: Historical package-architecture plan; product boundary superseded 2026-09-07
- Baseline: repository state at `41666f9`
- Current product authority: [`../product-family.md`](../product-family.md)
- Current roadmap: [`product-roadmap.md`](product-roadmap.md)
- Historical product authority: [`../v0-product-contract.md`](../v0-product-contract.md)
- Contract version: `0.3`
- Direction: split the portable engine, self-hosted application, optional connectors, and legacy work; keep a clean dependency path for a later managed service
- Delivery rule: prove the v0 journey before moving the package tree

The four-distribution ownership work in this plan remains accepted evidence. Its appliance-shaped
default, mandatory daemon, selected Brain root, and hosting-first product-family terminology do not.
Under the current split, plain `open-brain` is the five-minute direct local product and Secure Node
owns the advanced daemon, custody, authorization, and multi-client architecture.

## Decision

Option C is the target OSS architecture. Open Brain will remain one monorepo at first, but it will produce four separately owned distributions or work areas:

1. `open-brain-engine`: the portable knowledge engine and its canonical local reference adapters.
2. `open-brain`: the installable application and control plane.
3. `open-brain-connectors`: optional source, provider, and action adapters, installed separately.
4. `open-brain-legacy`: migration, parity, cutover, and predecessor-replacement work, excluded from the default public artifact.

A later managed deployment, referred to here as `open-brain-hosted`, is a fifth consumer rather than a fifth dependency of the OSS runtime. It uses the published engine and Portable Brain contract, adds tenant provisioning, managed identity, envelope encryption, scoped workers, audit, and fleet operations, and cannot redefine canonical schemas. Its repository and publication policy can be decided after the OSS alpha; its dependency direction is fixed now.

The physical split is not the first implementation step. The first step is a contract-level vertical slice through generic typed capture, unassigned inbox routing, independent derived-output review, publication, space-aware lexical retrieval, and portable export. The existing monolith stays in place while that journey is made executable and tested. Phase 2 may extract single-owner modules inside the existing `src/open_brain` tree. The four-distribution move begins only in Phase 4.

This sequencing keeps Option C from becoming another architecture project that delays a usable release.

## Goals

The architecture must make these outcomes ordinary:

- install the product without cloning the repository;
- initialize one Brain root with one default profile;
- run one supervised daemon with one canonical writer;
- accept text, reference or file, event, and measurement payloads without mandatory organization;
- organize with user-created spaces and route unassigned captures later;
- derive several independently reviewable outputs from one immutable capture;
- capture, inspect, review, publish, retrieve, export, and import through task-shaped interfaces;
- add or remove optional connectors without changing the engine;
- remove the application while leaving readable Markdown;
- preserve stable tenant, actor, role, space, and record identities across self-hosted and hosted deployments;
- let a hosted owner export and continue on the self-hosted product without losing Brain-owned data;
- keep private migration and predecessor concerns out of the public runtime.

Every implementation phase must trace to requirement IDs in the [v0 product contract](../v0-product-contract.md). The contract governs if a migration convenience conflicts with a v0 outcome.

## Non-goals of this plan

This plan does not design:

- the managed hosting control plane, billing, or fleet operations required to launch the hosted product;
- multiple writers, shared organizational Brains, or multi-user collaboration;
- a graph or vector store as a default dependency;
- a general plugin marketplace or third-party SDK;
- separate network processes for each internal job;
- compatibility shims for unpublished Python import paths.

## Current architecture constraints

The current code is a ports-and-adapters monolith, but its package ownership no longer matches the public product.

| Constraint | Current evidence | Consequence for Option C |
|---|---|---|
| The domain core is real | `capture`, `ledger`, `review`, `storage`, and `core` contain durable queues, provenance, review, publication, and local persistence | Preserve these invariants and deepen them behind a smaller engine interface |
| Composition is too broad | `cli/composition.py` selects providers, stores, integrations, HTTP behavior, and 30 scheduled routes | Move composition to the app and split it by profile, daemon, and optional extension ownership |
| Dependency direction is already leaking | `production` imports CLI code, configuration imports ledger models, and storage imports an operations lock type | Add import rules before physical moves; keep one lock vocabulary and prevent the engine from depending on presentation or migration code |
| Operations mixes different lifecycles | `operations` contains normal maintenance alongside cutover, shadow, predecessor parity, and rendered job catalogs | Split by product ownership, not by renaming the whole package |
| Integrations is not one coherent module | It contains retrieval and UI helpers alongside messaging, finance, LifeOS, Obsidian, and repository behavior | Put core retrieval in the engine, user surfaces in the app, and external systems in connectors |
| The existing scheduler models 30 operating-system jobs | Most routes target launchd, and Linux support covers only a subset | Replace the public profile with one daemon and internal schedules; keep old route compatibility in legacy work only |
| Configuration assumes a private deployment | Six absolute roots, writer identity, provider files, inventories, and integration inputs are required | Compile one public profile from one Brain root and keep advanced overrides explicit |
| The no-provider journey is incomplete | Capture can persist raw work, but model-free publication and retrieval are not proven as one product path | Treat provider mode `none` as the first vertical-slice risk |
| Capture is still text and source centric | The public journey and active adapters primarily model owner text, URLs, and source-specific integrations | Introduce one versioned envelope for text, reference or file, event, and measurement payloads before adding more connectors |
| Organization is fixed by deployment paths | The proposed v0 layout and current configuration assume named work and personal roots rather than user-created spaces | Add stable space identity and an unassigned inbox; treat names and slugs as mutable presentation |
| The data model has no product-family isolation contract | Current local behavior relies on one owner and writer without a portable tenant, actor, and role boundary | Generate local identities in v0 and require them on durable records so hosting does not need a schema fork |

## Target dependency graph

```text
                               optional install
open-brain-connectors  -------------------------------+
       | implements app extension interfaces           |
       | uses public engine values                      v
       +----------------------->  open-brain  ------>  open-brain-engine
                                      |                        |
                                      | composition            | local adapters
                                      v                        v
                              daemon / CLI / UI      Markdown / open records / SQLite / FTS
                                      |
                                      +--> stdio MCP reads through a space-scoped interface

open-brain-hosted  --------------------------------->  open-brain-engine
  managed-only consumer       published task and Portable Brain contracts
        |
        +--> tenant routing / envelope encryption / scoped workers / audit

open-brain-legacy  ----> published engine and app import/migration interfaces only

Forbidden declared or static directions:
engine -> app | connectors | hosted | legacy
app -> connectors | hosted | legacy
connectors -> app internals
hosted -> app internals | legacy
public runtime -> legacy
```

Runtime discovery does not reverse the package dependency. The app owns a narrow extension interface. An installed connector distribution implements that interface through Python entry points or an equivalent explicit registry. The app does not import connector modules by name and does not require the connector distribution.

The managed service is also downstream of the engine. It may provide hosted storage, encryption, identity, scheduler, and transport adapters, but it cannot require the engine or Portable Brain schemas to import hosted code. A conformance fixture, not private implementation knowledge, is the compatibility boundary.

The extension interface is an internal v0 seam, not a promise of a stable third-party plugin SDK. It can become public only after one reference connector, one event connector, and one measurement connector prove the compatibility needs against the same conformance suite.

## Target modules

### 1. `open-brain-engine`

Purpose: hide the hard portable knowledge invariants behind task-shaped interfaces.

The engine owns:

- stable tenant, actor, role-claim, space, capture, proposal, and record identities;
- the common capture envelope and text, reference or file, event, and measurement payload values;
- capture identity, provenance, privacy, inherited defaults, unassigned routing, and durable acceptance;
- source records, zero-to-many linked derived outputs, independent review decisions, and publication receipts;
- canonical Markdown identity and atomic publication;
- versioned open source-record schemas, content-addressed preserved files, and Portable Brain import/export rules;
- operational SQLite schemas required by those workflows;
- one canonical writer lease and idempotent worker transitions;
- deterministic lexical or FTS retrieval and result explanations;
- validation and reconciliation of owner edits made through ordinary Markdown tools;
- local backup, restore-validation, and index-rebuild rules that protect canonical data;
- provider, clock, identity, encryption, filesystem, and outbound transport ports;
- deterministic `none` provider behavior.

The engine does not own CLI parsing, HTTP listeners, UI rendering, operating-system supervision, profile discovery, external connector schedules, cloud SDK construction, or legacy cutover.

Its public interface is grouped by user task, not by storage table:

| Interface | Representative operations | Required properties |
|---|---|---|
| Capture | accept a normalized text, reference or file, event, or measurement envelope; distinguish explicit canonical-note from quick capture; inspect its receipt and state | durable before return, optional space/intent/reason, idempotent identity, immutable provenance, explicit publication authority |
| Inbox and spaces | list unassigned and pending captures; create, rename, and route spaces | stable IDs, mutable names, inherited safe defaults, no hard-coded domains |
| Review | list linked proposals; approve, reject, or safely edit each against its expected receipt | independent terminal decisions, sibling isolation, source retained, publication atomic |
| Retrieval | search eligible content and typed sources across or within allowed spaces; fetch bounded representations | lexical baseline, type/space/trust/provenance labels, explainable selection |
| Portability | export or import one versioned Portable Brain; validate conformance | exact canonical and source bytes, stable IDs, review history, credentials excluded |
| Maintenance | execute one named due task; inspect data health; validate backup or rebuild | replay safe, lease aware, bounded receipts |

These can be Python protocols or concrete facades where there are multiple callers. They must not become a generic `execute()` bus. CLI, UI, HTTP, daemon jobs, and MCP already provide enough independent callers to make the task interface a real seam.

The engine is a deep module under the deletion test. If removed, identity, spaces, capture, privacy, review, writer, publication, portability, and data-integrity rules would have to be reimplemented across every surface. A separate `contracts`, `storage`, or `sdk` distribution is rejected for v0 because it would mostly relay engine types and move complexity back to callers. The Portable Brain schema is a published contract inside the engine distribution, not a second shallow package.

### 2. `open-brain`

Purpose: turn the engine into one installable, observable appliance.

The application owns:

- `single-user-local` profile compilation from one Brain root, including stable local tenant and owner identities;
- optional starter-space templates with no privileged engine behavior;
- `init`, `doctor`, upgrade, backup, restore, portable export/import, and uninstall orchestration;
- provider selection and construction after privacy and authority checks;
- one daemon, its internal scheduler, retry policy, and bounded run history;
- native launchd and systemd lifecycle adapters;
- CLI command parsing and JSON/text representations;
- typed CLI and loopback HTTP intake, the local unassigned-inbox/space/review/search UI, and read-only space-scoped stdio MCP;
- generated local credentials and owner-only application state;
- optional connector discovery, configuration, and scheduling;
- one composition root for each executable process.

The app exposes the same task interface to CLI, UI, and HTTP handlers. Representation code maps typed results to text, JSON, or HTML and cannot reach SQLite or Markdown stores directly.

The app is also a deep module. If removed, profile resolution, process ownership, supervision, scheduling, credentials, recovery orchestration, and surface consistency would spread into shell scripts, manifests, and handlers.

### 3. `open-brain-connectors`

Purpose: isolate source-specific polling, extraction, checkpoints, optional remote providers, credentials, and approved external actions from the default product.

Each connector is an adapter, not a new product layer. Candidate adapters include YouTube, media transcription, iMessage, social feeds, messaging, LifeOS, fitness and health services, finance, mail/calendar, repository sync, project commit bridges, and an optional cloud provider adapter.

The real seam has two deliberately separate interfaces:

1. A capture connector may collect external material, normalize it to a common text, reference or file, event, or measurement envelope, and submit it through the engine capture interface.
2. An action connector may execute only a receipt-bound, explicitly approved action through a separate app extension interface.

Capture authority never implies action authority. Connector code receives bounded runtime capabilities, such as an approved transport, checkpoint store, capture sink, clock, and metadata-only logger. It does not receive the Brain root, canonical writer, raw database handles, or app composition internals.

A provider adapter implements the engine provider interface separately. Installing a cloud provider adapter grants no capture or action authority, and provider construction still occurs only after the app's privacy and egress checks.

The connector collection is not treated as one deep framework. Each adapter should be independently removable. The shared discovery and run protocol stays internal and small until reference, event, and measurement adapters prove a repeated need.

Generic text, reference or bounded-file, event, and measurement intake remain built into the app because they define the v0 journey and require no optional external system. YouTube, social, development, health, business, or other source-specific behavior stays in removable connectors.

### 4. `open-brain-legacy`

Purpose: quarantine work that exists to replace or migrate a predecessor, not to operate the public v0 product.

It owns:

- predecessor configuration and content migration;
- parity harnesses and observation runners;
- shadow, cutover, stabilization, and retirement evidence;
- compatibility wrappers needed only by the private deployment;
- old 30-job catalog compatibility where it remains temporarily necessary.

Legacy code may call published import or migration interfaces. The engine and app cannot import it. It is excluded from the default wheel and native artifact.

Before a public repository is created, this work area receives a privacy and history audit. Private-only topology, fixtures, or replacement logic move to a separate private repository. Generic importers may stay or return later under a documented public data format.

This work area is a release firewall, not an abstraction claimed to be deep.

### 5. Workspace and release tooling

`dev` remains repository tooling. The current `release` package is split: native installation lifecycle belongs to the app, predecessor replacement and stabilization evidence belongs to legacy, and artifact audits belong to workspace tooling. Tooling does not become a runtime dependency. The root workspace owns shared lint, typecheck, test, artifact, license, clean-host, and Portable Brain conformance checks. Each shipping distribution must also build and test in isolation.

### 6. `open-brain-hosted` managed deployment

Purpose: operate many isolated single-owner Brains without changing the engine or trapping users in a hosted-only format.

This is a later consumer, not part of the self-hosted v0 artifact or its critical path. It owns:

- customer authentication, tenant provisioning, and bounded role claims;
- routing each request and worker to exactly one tenant context;
- per-tenant envelope encryption and managed key-service integration;
- narrowly scoped data-plane workers and metadata-only service telemetry;
- disabled-by-default human content access and immutable exceptional-access audit;
- hosted backup, recovery, fleet operations, and Portable Brain export orchestration;
- service concerns such as billing that remain outside the Portable Brain.

The hosted deployment uses the published engine task interface and supplies managed storage, encryption, identity, transport, and scheduling adapters. It must pass the same conformance suite as the self-hosted app. A hosted-only canonical record, space type, proposal state, or migration prerequisite is an architecture violation.

Client-held encryption is a later mode, not a v0 promise. The encryption, retrieval, provider, and connector ports must still allow plaintext work to move to a client-owned worker or separately evaluated trusted-compute boundary without replacing the portable schemas.

## Proposed source layout

This is the target after the vertical slice and in-place dependency cleanup:

```text
packages/
  engine/
    pyproject.toml                 # distribution: open-brain-engine
    src/open_brain_engine/
      domain/
      identity/
      spaces/
      capture/
      review/
      knowledge/
      retrieval/
      portability/
      maintenance/
      local/                       # Markdown, SQLite, FTS implementations
  app/
    pyproject.toml                 # distribution and command: open-brain
    src/open_brain/
      profile/
      daemon/
      lifecycle/
      scheduler/
      cli/
      http/
      ui/
      mcp/
      portability/
      extensions/
      composition/
  connectors/
    pyproject.toml                 # optional distribution
    src/open_brain_connectors/
      youtube/
      media/
      imessage/
      messaging/
      repositories/
  legacy/
    pyproject.toml                 # not in the default artifact
    src/open_brain_legacy/
      migrate/
      parity/
      cutover/
      compatibility/
tools/
tests/
```

The internal engine names are provisional until the vertical slice shows where behavior has locality. The four OSS ownership areas and dependency direction are the architectural decision. The managed hosted deployment stays outside this source layout until the OSS conformance boundary is published and proven.

## Current-to-target ownership map

No existing mixed package should be moved wholesale without separating its responsibilities.

| Current package or file | Target owner | Migration note |
|---|---|---|
| `core` | engine | Keep values and policies; add tenant, actor, role-claim, space, and portable-record identities without exposing all ports |
| `capture` | engine | Keep durable capture, provenance, queues, and deterministic extraction rules; replace source-specific public shapes with the common typed envelope; source-specific media adapters move out |
| `events` | engine | Reuse durable event values for the common event payload and occurrence-time model; keep local persistence with the workflow that owns it |
| `ledger` | engine | Rename only if the vertical slice proves a clearer knowledge/publication module; preserve receipts and writer rules |
| `review` | engine | Keep proposal and decision behavior; add zero-to-many sibling proposals with independent receipts; UI and CLI representations move to app |
| `storage` | engine local implementation | Move `LockScope` out of `operations.models` before extraction so lock identity stays singular; do not publish raw stores as the product interface |
| `providers` | split | Provider port and `none` implementation belong to engine; one local HTTP adapter belongs to app; cloud implementation belongs to an optional adapter artifact |
| `config.py` | app profile | Define app-owned settings without importing ledger models; compile one root into paths, stable local identities, optional space templates, and bounded capabilities |
| `cli` | app | Split parsing, representation, and composition; remove adapter construction from command handlers |
| `services` | app | Fold HTTP, UI, and MCP process entry points into app-owned surfaces and composition roots |
| `production` | split | Local canonical adapters go to engine; provider/runtime and daemon adapters go to app; source-specific bridges go to connectors |
| `operations` | split | Writer, backup validation, index rebuild, and run receipts go with engine/app maintenance; cutover and shadow go to legacy; connector jobs go with their adapter |
| `integrations` | split | Retrieval behavior and space filtering go to engine; UI/MCP representation goes to app; external systems normalize through connectors |
| `migrate` | legacy | Retain only generic public importers in a shipping artifact after format review |
| `parity` | legacy | Never required by the app or release acceptance suite |
| `release` | split | Native install lifecycle and export/import orchestration go to app, replacement and stabilization evidence goes to legacy, and artifact plus portability conformance audits stay workspace tooling |
| `dev` | workspace tooling | Keep out of runtime dependency graphs and native artifacts |
| `__main__.py` and package metadata | app | `python -m open_brain` and `open-brain` remain user entry points |

## Brain root and local data layout

Initialization compiles one owner-selected root into a versioned internal layout:

```text
<brain-root>/
  brain.toml                       # public, versioned profile and layout version
  content/                         # canonical readable Markdown
    spaces/
      <space-slug>/
        _space.md                  # stable space ID, mutable name, safe defaults
        ...                        # owner pages in that space
  sources/
    captures/
      YYYY/MM/<capture-id>.json    # one immutable typed envelope per accepted capture
    batches/
      YYYY/MM/<batch-id>.jsonl     # referenced high-volume event or measurement rows
    blobs/
      sha256/<first-two-hex>/<sha256> # content-addressed preserved files
  history/
    proposals/YYYY/MM/<proposal-id>.json
    decisions/YYYY/MM/<decision-id>.json
    publications/YYYY/MM/<publication-id>.json
    actions/YYYY/MM/<action-id>.json
  .open-brain/
    state/open-brain.sqlite3       # operational queues, leases, and writer state
    indexes/search.sqlite3         # disposable FTS or future projections
    run/                           # bounded metadata-only runtime records
    credentials/                   # owner-only local credentials; never portable
```

The hybrid open storage model and version `1` directory names are approved. Markdown owns canonical human knowledge and space definitions; versioned structured records own typed captures, provenance, and portable history; content-addressed blobs own preserved files; SQLite owns only operational state and disposable indexes.

Every accepted capture has one canonical UTF-8 JSON envelope. High-volume event and measurement imports use immutable JSONL sidecars referenced by their envelope. Proposals, decisions, publication receipts, and action receipts remain individual JSON records. `YYYY/MM` is derived from immutable UTC acceptance or recording time, not mutable source occurrence time. Corrections append records linked by `supersedes`.

Phase 0 must check in the exact versioned JSON Schemas and conformance fixtures before `V0-INSTALL-02` is implemented. The fixtures freeze deterministic serialization, UTC RFC 3339 timestamps, one canonical record per JSONL line, LF termination, SHA-256 manifest checksums, and Portable Brain round trips. The following invariants are already fixed:

- canonical Markdown, space definitions, typed source records, preserved files, and portable history remain inspectable without the process;
- tenant, actor, role-claim, space, capture, proposal, and record identities survive directory renames, export, and import;
- space names and slugs are presentation; the engine gives no special behavior to Personal, Work, Projects, Health, Learning, or any other template name;
- unassigned captures remain in the source/inbox lifecycle until routed and are not forced into a fake default space;
- operational state and indexes are distinct from canonical content;
- indexes can be deleted and rebuilt;
- generated credentials are owner-only and are not written into public `brain.toml`;
- advanced paths may be overridden explicitly, but the default profile derives them from one root;
- backups use a separately selected destination so a failed disk does not take both live data and its backup.

The profile compiler is the only module that turns a Brain root into concrete paths and generated local identity context. Handlers and connectors receive typed capabilities, tenant and actor context, and optional stable space IDs, not path strings.

A Portable Brain export materializes the versioned public portions of this layout and excludes credentials, indexes, supervisor state, and undocumented live SQLite copies. A hosted export must materialize the same contract even if its live storage adapters use different infrastructure.

## Process topology

v0 installs one supervised daemon:

```text
launchd or systemd
        |
        v
open-brain daemon
  - owns the canonical writer lease
  - serves loopback HTTP and the local UI
  - drains capture and review queues
  - runs bounded internal schedules
  - records metadata-only health and run receipts
```

The CLI is a short-lived control client. Capture intake may append safely to the durable ingress queue, but canonical publication and review mutation occur through the daemon writer. Read commands use the same application interface against a safe read view.

MCP remains a separate stdio process started on demand. It receives only the read-only retrieval interface, an explicit space allow-list, and allow-listed metadata feedback. It cannot obtain capture, review, configuration, connector, writer, or unlisted-space capabilities.

Internal schedules use durable due times and idempotent task identities. The scheduler may retry a task after interruption, so each task must return a receipt that distinguishes completed, empty, deferred, and failed work. One slow connector cannot block capture publication; connector execution uses bounded time and concurrency.

A bounded reconciliation task detects direct Markdown edits, validates the versioned page schema, and refreshes retrieval state. It records invalid files for owner remediation and never replaces them silently.

## Local HTTP security

Loopback is a transport default, not an authentication decision. Share intake uses a generated bearer credential. The browser UI uses an owner-authenticated local session, strict origin checks, and CSRF protection for review or administration mutations. Credentials never appear in URLs, logs, HTML, or canonical files.

The default documented remote path is an encrypted tunnel to the loopback listener. A direct non-loopback bind is refused unless an explicit profile enables it and doctor verifies the configured authentication and encryption termination. The app does not claim that a bare private LAN is trusted.

Captured text, structured values, filenames, preserved files, and provider-derived content are untrusted at every representation. HTML escapes them and applies a restrictive content security policy. Reference intake preserves the submitted record without fetching when egress is off. An authorized fetch can use only the engine's bounded outbound port, including DNS pinning, private-address rejection, redirect limits, byte limits, and timeouts. File and media processing use bounded isolated adapters.

## Configuration and composition

Composition follows this order:

1. Parse the public profile and explicit command inputs.
2. Resolve the versioned Brain layout without opening providers or listeners.
3. Resolve or validate the stable tenant, actor, role-claim, and optional space context.
4. Validate permissions, schemas, writer state, and requested capabilities.
5. Apply privacy and egress authority.
6. Resolve only the secret or encryption references required by the selected capability.
7. Construct local adapters and the selected provider.
8. Start the requested process or execute one bounded task.

Provider modes are:

- `none`: deterministic capture, publication allowed by review rules, lexical retrieval, pending enrichment visible;
- `local`: one documented loopback provider, preflighted before use;
- `cloud`: an optional extra, disabled by default and constructed only after privacy and egress approval.

No module may read ambient environment variables, start a listener, acquire the writer lease, or construct a provider at import time.

## Managed hosting data plane boundary

The managed control plane authenticates a customer and resolves one tenant, actor, and bounded role claim before invoking a data-plane worker. A worker receives authority for one tenant and one operation. It may unwrap that tenant's data-encryption key, operate through the published engine interface, encrypt changed data before persistence, and emit only metadata-safe audit and health receipts.

Tenant data never enters shared logs, caches, analytics, billing records, or support tools. Human content access is absent from normal operations. If an exceptional-access mechanism is later introduced, it requires a reason, expiring authority, and an immutable audit record.

The live hosted storage layout may differ from the local filesystem implementation, but its logical schemas cannot. Hosted import/export goes through the Portable Brain port and conformance suite. This is the test that keeps hosting from becoming a fork.

Per-tenant envelope encryption is the first hosted mode. Client-held end-to-end encryption remains a later architecture option. To keep that option viable, plaintext-dependent retrieval, provider, and connector work must sit behind replaceable capabilities that can run in a client-owned worker or separately evaluated trusted-compute boundary.

## Interface and import rules

The repository will enforce these rules with AST import tests or an import-linter configuration before package movement:

1. Engine code cannot import app, connector, hosted, or legacy namespaces.
2. App code can import only the engine public surface, not engine adapter internals.
3. Connector code can import the public extension interface and engine command/value types, not app composition or local stores.
4. Legacy code can import published migration interfaces; no shipping namespace can import legacy.
5. Hosted code can import the engine public task and Portable Brain surfaces, not local app composition, local stores, or legacy.
6. CLI, HTTP, UI, and MCP representations cannot import SQLite stores, Markdown writers, or provider implementations.
7. External adapter implementations cannot import one another.
8. Workspace tooling cannot become a runtime dependency.

The app and engine publish explicit `__all__` surfaces. Cross-package tests import only those surfaces. Old internal import paths are removed rather than maintained indefinitely. If the private deployment needs a compatibility path, legacy owns it.

## Artifact architecture

The public release contains:

- independently buildable engine and app wheels/sdists;
- a self-contained macOS `arm64` artifact for macOS 14 or newer;
- a self-contained Linux `x86_64` artifact tested on Ubuntu 24.04 LTS, Ubuntu 26.04 LTS, and Debian 13;
- one native supervisor template per host family;
- checksums, supported-version metadata, licenses, and an artifact-based quickstart;
- the versioned Portable Brain schema, conformance fixture, and export/import compatibility range;
- optional connector artifacts built and tested separately.

The target install experience is a self-contained application artifact; `pipx` plus the app wheel remains a development and recovery path. Linux builds use Ubuntu 24.04 as the compatibility baseline. Intel macOS, Linux `arm64`, and Windows are not supported in v0.

PyInstaller 6 one-folder mode is the first bundler candidate. A one-working-day spike must verify clean installation, daemon startup, package and resource discovery, upgrade, backup and restore, and uninstall on the accepted host matrix. The macOS result is wrapped in a signed and notarized artifact; the Linux result is a checksummed archive. Phase 4 runs this spike after the Phase 3 appliance surfaces exist. If PyInstaller fails, Phase 4 evaluates Nuitka standalone under the same gate. Neither route may require system Python on a release host.

Containers are evaluated after native artifacts pass. A container does not replace the Mac launchd path, and it is not accepted for Linux until ownership, volume, backup, upgrade, and localhost-auth behavior pass the same product gates.

## Connector maturity path

The connector interface stays internal while three deliberately different source adapters are built:

| Proof | Candidate sources | What it must expose |
|---|---|---|
| Reference connector | YouTube polling | polling, cursor checkpoints, duplicate delivery, bounded fetch, preserved source material |
| Event connector | GitHub | occurrence time, ordering, updates, webhook or OAuth authority, replay |
| Measurement connector | CSV importer | units, dimensions, batches, corrections, volume limits, deterministic import |

The first reference connector may be built during Phase 2 to test the internal seam, but no source-specific connector is a self-hosted v0 release gate. The event and measurement proofs may follow the OSS alpha. Exact vendors are delivery choices; the three behavioral categories are the product decision.

After all three pass, compare their implementations and extract only demonstrated common behavior. The public Connector SDK then includes:

1. a versioned connector API and typed capture values;
2. a capability manifest declaring payloads, egress, secrets, schedules, and action authority;
3. a reusable conformance suite for identity, privacy, checkpoints, duplicates, restart, and bounded output;
4. an isolated worker runtime with bounded time, memory, network, and logs;
5. package signing and install-time verification;
6. a compatibility and deprecation policy tied to engine and Portable Brain versions.

Publishing a stable SDK before the three proofs pass is forbidden. Capture connectors and action connectors remain separate capabilities even after the SDK exists.

## Migration plan

### Phase 0: freeze the release boundary

Estimate: 2 to 3 days.

Work:

- treat the approved v0 product contract as authority and place every unlisted feature on an expansion backlog;
- add the import-rule test in the current namespace;
- record the current public CLI, data schemas, and artifact build as characterization evidence;
- implement the approved version `1` Brain-root layout and host matrix, then check in the exact typed-record schemas, serialization fixtures, batch fixtures, and Portable Brain manifest;
- freeze the common capture envelope, stable tenant/actor/role/space identities, and the four payload families;
- audit legacy code and history for private-only material before public extraction.

Exit gate:

- contract change control remains recorded as accepted;
- no top-level package is unclassified;
- the Portable Brain conformance fixture and stable-identity rules are reviewable;
- architecture rules fail on a deliberate forbidden import;
- default artifact contents and exclusions are explicit.

### Phase 1: prove the vertical slice in place

Estimate: 5 to 8 days.

Work:

- introduce the task-shaped engine interface inside the current package without moving files;
- implement provider mode `none` as a real composition choice;
- compile `single-user-local` from one disposable Brain root with generated tenant/owner identities and optional starter spaces;
- accept text, reference or bounded file, event, and measurement envelopes with optional space, intent, and reason;
- prove that explicit owner canonical-note input publishes immediately while identical quick-capture input remains durable in the inbox;
- prove unassigned inbox routing and a space rename without changing stable identities;
- run one multi-meaning capture through several sibling proposals with independent approval and rejection;
- run accepted knowledge and typed source records through space-aware lexical retrieval;
- add restart and duplicate-delivery fault points around every durable transition;
- make CLI and a minimal local UI exercise the same interface.

Exit gate:

- `V0-GATE-02` through `V0-GATE-07` and `V0-GATE-13` pass against one disposable root;
- no-model capture and retrieval are proven, not simulated;
- CLI and UI observe the same capture, space, proposal, and terminal review IDs;
- package movement remains unnecessary to understand or test the journey.

Stop rule: if this slice requires broad legacy, connector, graph, vector, or multi-agent work, the requirement has drifted and must return to product review.

### Phase 2: deepen the modules in place

Estimate: 1 to 2 weeks.

Work:

- move all composition out of CLI handlers;
- remove `production -> cli` and `config -> ledger model` dependency leaks;
- relocate the canonical lock-scope value so `storage` no longer imports `operations.models` and no duplicate lock vocabulary is created;
- split mixed `operations`, `integrations`, and `production` behavior into single-owner internal modules inside the existing source tree; defer separate distributions and the `packages/` move to Phase 4;
- make CLI, HTTP, UI, MCP, and daemon jobs depend on the engine task interface;
- define the internal connector discovery/run interface and build the first reference connector proof without making it part of the default profile;
- implement the versioned open source-record, content-addressed blob, portable history, and export/import interfaces;
- keep encryption and tenant storage behind engine ports without building the hosted control plane;
- ensure storage, provider, and transport implementations are selected only at composition roots.

Exit gate:

- all import rules pass;
- connector discovery may return no installed connectors and the default journey remains intact; Phase 4 repeats this gate with the connector distribution physically absent;
- the engine test suite runs without importing CLI, HTTP listeners, OS supervisors, or legacy code;
- the same conformance fixture round-trips through local engine interfaces with stable identities and exact bytes;
- public result envelopes contain no absolute paths, secrets, or raw private content.

### Phase 3: build the appliance control plane

Estimate: 1 to 2 weeks.

Work:

- implement `init`, daemon lifecycle, generated credential, and profile preflight;
- replace the public 30-job supervisor topology with the internal durable scheduler;
- implement launchd and systemd install, start, stop, restart, status, and remove adapters;
- finish inbox/review/search UI, actionable doctor output, and bounded run history;
- exercise verified backup, disposable restore, Portable Brain export/import, upgrade, and uninstall through the app interface.

Exit gate:

- `V0-INSTALL-02` through `V0-INSTALL-06` and `V0-OPS-01` through `V0-OPS-09` pass;
- one and only one OS unit is installed for the default profile;
- reboot, provider outage, interrupted job, and stale lease scenarios recover without duplicate publication;
- `V0-GATE-14` passes through the public app interface;
- uninstall leaves the Brain root readable.

### Phase 4: split, prove artifacts, and perform the full-stop cutover

Estimate: 2 to 3 weeks for the code and artifact gates, followed by one
owner-gated production window. The production window has no downtime cap; its
duration is measured for evidence and does not authorize skipping a safety
check.

Phase 4 uses three ordered gates. The detailed executable contract lives in
[`phase-4-physical-split-native-artifacts-and-cutover.md`](phase-4-physical-split-native-artifacts-and-cutover.md).

#### P4-W0: freeze the move and acceptance contracts

Before moving a source file, extend the existing package classification into
one machine-validated move manifest. It must assign every runtime file, test,
fixture, schema, entry point, and packaged resource exactly one distribution,
destination, API status, and artifact disposition. Destination collisions,
stale or missing paths, unresolved old imports, and disallowed dependency
edges fail the manifest gate.

Build the Phase 4 acceptance harness at the same time. It must be able to
prove, from installed artifacts rather than the repository source path, that:

- the engine wheel works with no app source available;
- the app works with the engine installed and connectors and legacy physically absent;
- connector code uses only the versioned extension interface and public engine values;
- default artifacts contain no legacy, workspace, private-fixture, or unselected cloud code;
- every distribution, native artifact, and Portable Brain export reports one release identity and schema compatibility range.

The current monolith produces bounded expected-red findings from this harness;
the default repository test gate remains green. P4-W0 also pins the workspace,
build backend, native build Python, PyInstaller and hooks versions, fallback
criteria, signing/notarization prerequisites, and the first draft-PR CI lanes.

#### Gate P4A: physical split and distribution isolation

- create the four workspace areas and move code in dependency order: engine, app, connectors, then legacy;
- use distinct top-level namespaces and published package interfaces; do not use a shared namespace or artifact include/exclude rules as a substitute for physical ownership;
- keep move commits behavior-neutral; any required behavior repair gets a red regression and a separate verified commit;
- remove old import paths rather than retaining indefinite compatibility shims;
- build and test each shipping distribution in a disposable isolated environment;
- ship a versioned provisional connector interface, conformance kit, and isolated worker runtime without claiming stable SDK compatibility before the reference, event, and measurement proofs all pass;
- open a draft PR after P4-W0 and require the applicable macOS and Linux lanes to pass at each clean checkpoint.

P4A exits only when the isolated engine, app, and connector checks pass, legacy
and workspace code are physically absent from the default app environment,
all old import paths are gone, the root suite passes, and a fresh read-only
review accepts the exact green-CI candidate.

#### Gate P4B: native artifacts and clean-host proof

- run the bounded PyInstaller 6 one-folder spike on native macOS `arm64` and Linux `x86_64` builders; switch to the accepted Nuitka standalone fallback only after the recorded PyInstaller failure gate is met;
- implement the real app-owned native artifact lifecycle adapter without changing the Phase 3 orchestration contract;
- build the macOS artifact on macOS and the Linux artifact on Linux; neither artifact may require system Python on its release host;
- run clean-host install, initialization, daemon, capture, review, retrieval, backup, disposable restore, Portable export/import, prior-schema upgrade, uninstall, and residue checks against the exact artifacts;
- sign and notarize the macOS candidate, checksum the Linux archive, and bind wheels, sdists, native artifacts, supervisor templates, supported-version metadata, and schema compatibility to one unpublished release-candidate manifest;
- scan source, complete Git history, every Python artifact, and unpacked native artifacts for private or forbidden content.

P4B exits only after the full host matrix and required CI are green and a
fresh exact-candidate review returns no P0, P1, or P2 finding. Phase 4 proves
publishable artifacts but does not upload packages, create tags or releases,
or satisfy the publication half of `V0-INSTALL-01`; public release remains
Phase 5.

#### Gate P4C: owner-gated full-stop production cutover

- verify fresh encrypted recovery points and disposable restores against the exact P4B artifact;
- rehearse one transaction that stops all predecessor writers together, proves zero remaining predecessor writer authority, performs copy-only/idempotent migration and derived-state rebuilds, starts Open Brain, verifies all required surfaces, and either accepts the result or restores the complete prior owner map;
- after independent receipt review, run the same transaction in the authorized production window;
- prove data integrity, privacy, queue health, exactly one writer for every shared surface, and healthy Open Brain entry points before restoring synchronization or declaring success;
- retain disabled predecessors, backups, manifests, helpers, and all rollback assets; deletion, decommissioning, and irreversible cleanup remain unauthorized;
- record the day-0 stabilization baseline and reset rules on the final unchanged production state.

This gate supersedes the phased/one-surface-at-a-time transition order and
the rehearsal-derived four-hour downtime formulas in Goals `#24` and `#41`.
It does not weaken their parity, backup, verified-restore, privacy,
exactly-one-writer, rollback, retention, or day-0 evidence requirements.

Phase 4 exits only when P4A, P4B, and P4C all pass against bound source,
artifact, configuration, migration, and recovery identities. No predecessor
or rollback asset is deleted, and no public package or release is published.

### Phase 5: release the public alpha

Estimate: 3 to 5 days after the split is stable.

Work:

- run the clean-host macOS and Linux matrix from release artifacts;
- verify generic capture, space routing, multi-output review, retrieval, reboot, backup/restore, export/import, upgrade, and removal;
- publish quickstart, support limits, security model, data layout, and recovery instructions;
- publish checksums and a release manifest that binds every artifact and test result.

Exit gate:

- all `V0-GATE-01` through `V0-GATE-14` pass;
- a new user can complete the documented journey without repository knowledge;
- known limitations are product-facing and do not rely on private deployment context.

Total estimate: about 5 to 10 weeks for one experienced maintainer after the contract is approved. The range depends most on generic source serialization, Portable Brain round-trip fidelity, no-provider publication, daemon consolidation, and self-contained packaging.

### Phase 6: prove the hosted compatibility boundary

This phase begins after the OSS alpha and is excluded from the estimate above. It is a managed-product spike, not a requirement to launch billing or a full service.

Work:

- run the published engine against one isolated hosted storage adapter;
- propagate tenant, actor, and bounded role claims through every task and receipt;
- implement per-tenant envelope encryption with one scoped worker path;
- prove metadata-only logs and disabled human content access;
- export the hosted fixture and import it into an unmodified self-hosted release;
- evaluate where plaintext retrieval and enrichment would run under a future client-held-key mode.

Exit gate:

- the hosted adapter passes the OSS Portable Brain conformance suite;
- cross-tenant negative tests prove isolation at authorization, key, storage, index, queue, and cache boundaries;
- no hosted-only canonical schema or migration dependency is present;
- a hosted export continues locally with stable identities, exact bytes, provenance, review outcomes, and spaces.

## Contract traceability

| Contract area | Primary owner | Delivery phase | Principal acceptance evidence |
|---|---|---|---|
| `FOUNDATION-IDENTITY-*` and `FOUNDATION-SPACE-*` | engine; app for local identity creation | Phases 0 through 2 | stable ID fixtures, unassigned routing, space rename, role-at-receipt checks |
| `FOUNDATION-PORTABLE-*` | engine portability; app orchestration; workspace conformance | Phases 0 through 4 | export/import round trip, exact bytes, review history, credential exclusion |
| `V0-INSTALL-*` | app | Phases 3 and 4 | clean initialization, artifact install, idempotent retry, failed-install cleanup |
| `V0-CAPTURE-*` | engine; app for intake; connectors for optional sources | Phases 1 and 2 | four-payload no-provider slice, optional metadata, duplicate/conflict cases, egress-off reference receipt |
| `V0-REVIEW-*` | engine; app representations | Phases 1 and 2 | sibling proposal IDs, independent receipt-bound decisions, third-party rejection |
| `V0-DATA-*` and `V0-QUERY-*` | engine | Phases 1, 2, and 4 | exact-byte publication, typed source labels, space filters, lexical fixtures, delete/rebuild index |
| `V0-SURFACE-*` | app | Phases 1 and 3 | command help/JSON, authenticated UI and HTTP, read-only MCP capability test |
| `V0-OPS-*` | app lifecycle plus engine maintenance | Phase 3 | one-unit lifecycle, restart faults, doctor, backup/restore, upgrade, uninstall |
| `V0-PRIVACY-*` | engine policy and app composition | Every phase | no-egress default, late secret resolution, URL-fetch controls, output residue scan |
| `V0-GATE-*` | release workspace | Phases 4 and 5 | isolated artifacts and the clean-host matrix |
| `HOSTED-*` | later managed control and data planes | Phase 6 and later | conformance, tenant-isolation negatives, scoped decryption, audit, hosted-to-local export |

## Verification strategy

### Product acceptance tests

Add black-box tests under a product-level suite. They install or invoke public artifacts, initialize a disposable root, and exercise requirement IDs rather than internal classes.

Required scenarios include:

- text, reference or bounded file, event, and measurement first value with provider `none`;
- explicit owner canonical-note publication and identical quick-capture inbox retention;
- capture with no space, intent, or reason followed by later space routing;
- user-created space creation and rename with stable identity;
- one capture producing several sibling proposals with independent approval and rejection;
- third-party proposal approval and rejection;
- duplicate capture, conflicting bytes, and process death at each durable stage;
- daemon restart, stale writer lease, provider outage, and delayed retry;
- lexical retrieval freshness after edit and index rebuild;
- valid and invalid direct Markdown edit reconciliation;
- Portable Brain export/import conformance with exact canonical/source bytes and credential exclusion;
- backup, disposable restore, upgrade, uninstall, and data preservation;
- default-profile proof of no cloud construction, credential resolution, or egress.

### Interface tests

Promote existing capture, review, ledger, storage, provider, scheduler, and service tests into reusable interface suites. Every adapter claiming an interface must pass the same behavior tests. Examples:

- raw/event/queue persistence and replay behavior;
- capture-envelope normalization for every payload family and optional metadata combination;
- tenant, actor, role-claim, and space propagation through every durable receipt;
- proposal decision compare-and-swap and terminal idempotency;
- sibling-proposal independence;
- Markdown publication atomicity and read-back verification;
- provider timeout, malformed output, and `none` behavior;
- connector checkpoint, duplicate delivery, bounded result, and action-authority separation;
- launchd/systemd lifecycle render, install, status, and remove behavior.

### Architecture and artifact tests

- import direction and public-surface allow-list tests;
- isolated wheel builds and installs for every distribution;
- native artifact content inspection;
- schema compatibility and migration tests;
- Portable Brain manifest, fixture, and round-trip tests;
- private-value residue scans across logs, JSON, HTML, manifests, and artifacts;
- clean-host tests on every supported OS and architecture.

## Risks and mitigations

| Risk | Early signal | Mitigation |
|---|---|---|
| Package movement becomes the project | Many renamed files but no passing first-value test | Keep Phase 1 in the monolith and prohibit physical moves before its gate |
| Mixed packages hide dependency cycles | Import-rule exceptions multiply | Split by capability one cluster at a time; do not rename `operations`, `production`, or `integrations` wholesale |
| Provider `none` is not actually useful | A capture remains pending but cannot be found, or owner text cannot be safely published | Make model-free first value the first release gate and keep source preservation and indexing separate from enrichment |
| Generic capture becomes four unrelated pipelines | Payload families acquire separate identity, privacy, queue, or publication code | Keep one envelope and interface suite; payload-specific behavior begins only after durable acceptance |
| Starter spaces become hidden product domains | Code branches on names such as Work or Personal | Use stable IDs and user-owned metadata; run a test that renames every starter space |
| The Connector SDK freezes around one source | YouTube, GitHub, or health-specific concepts appear in the public interface | Keep the seam internal until reference, event, and measurement adapters pass one conformance suite; extract only behavior shared by all three |
| Hosted requirements take over the OSS release | KMS, billing, fleet, or shared-user work appears in Phases 0 through 5 | Build only identity, encryption ports, and portability conformance in OSS v0; defer the managed implementation to Phase 6 |
| Hosted storage quietly forks the Brain | Hosted export needs private transforms or loses review history | Make hosted-to-local round trip a conformance gate and prohibit hosted-only canonical schemas |
| One daemon creates a large failure domain | A slow connector delays capture or review | Durable queues, per-task budgets, isolated connector runs, and supervisor restart tests |
| Artifact ambition delays the alpha | PyInstaller fails late or unsupported architectures enter the release matrix | Run the one-working-day spike in Phase 4 after the appliance surfaces exist, keep the accepted matrix bounded, and move to Nuitka on recorded failure |
| Legacy/private behavior leaks into OSS | Public artifact or docs require predecessor names, inventories, or private fixtures | Enforce the legacy dependency rule, artifact inspection, and pre-publication history audit |

## Approved decisions and remaining evidence

The owner approved these bounded decisions on 2026-08-30:

1. Use the readable, date-partitioned Brain-root layout version `1` specified above.
2. Use individual canonical JSON records plus referenced JSONL sidecars for high-volume event and measurement batches.
3. Support macOS 14 or newer on Apple Silicon and Linux `x86_64` on Ubuntu 24.04 LTS, Ubuntu 26.04 LTS, and Debian 13.
4. Run PyInstaller 6 one-folder mode as the first bundler candidate under a one-working-day clean-host spike, with Nuitka standalone as the accepted fallback.

No additional owner choice is required for these four items. Phase 0 produced the reviewable JSON Schemas and conformance fixtures. Phase 4 must produce the bundler spike evidence before release artifact implementation proceeds.

## Definition of done

Option C is complete when:

- the v0 contract gates pass from release artifacts on supported clean hosts;
- tenant, actor, role-claim, capture, proposal, and space identities survive restart, rename, export, and import;
- text, reference or file, event, and measurement payloads use one durable capture interface;
- user-created spaces and an unassigned inbox replace hard-coded life domains;
- one capture can produce independently decided linked outputs;
- explicit owner canonical-note input publishes immediately while quick or ambiguous capture remains in the inbox;
- engine, app, connectors, and legacy have enforced one-way dependencies;
- the default app installs and operates without connectors, cloud packages, or legacy code;
- one daemon owns schedules and canonical publication;
- Markdown, typed source records, portable history, and preserved files remain readable after uninstall;
- a Portable Brain round trip passes without credentials or hosted-only dependencies;
- package and artifact inspection proves private migration and optional integration code are absent from the public default release.

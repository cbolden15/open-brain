# Historical Open Brain v0 appliance contract

- Status: Superseded for product scope on 2026-09-07
- Contract version: `0.5`
- Revised: 2026-09-04
- Approved: 2026-08-30
- Selected direction: Option C
- Release sequence: self-hosted OSS public alpha first; managed hosting follows
- v0 target: one owner running one always-on Mac or Linux home server

> This file preserves the accepted v0.5 appliance contract as historical evidence. The current
> product authority is [`docs/product-family.md`](product-family.md), version `0.7`. In particular,
> one daemon, a user-selected Brain root, service installation, and the 15-minute target no longer
> define the default Open Brain product. They belong to the opt-in Secure Node profile where the
> current contract retains them. The requirements below are intentionally not rewritten because
> completed Phase 0 through Phase 4 evidence cites their exact IDs and wording.

## Product promise

Open Brain v0 lets one person install a local second brain, capture something without first organizing it, see what happened to it, review any derived meaning, retrieve it later, and recover or export the system without losing ownership of the data.

The built-in capture contract covers four payload families: text; reference or file; event; measurement. Personal, business, development, health, learning, and other areas use the same pipeline. Users organize their Brain with spaces they create, not product domains embedded in the code.

The user does not need to understand the package graph, create unrelated paths, write private TOML by hand, install dozens of operating-system jobs, choose a taxonomy before saving, or create a cloud account. Canonical knowledge remains readable Markdown when Open Brain is stopped or removed.

The same engine and portable data contract will support a later managed Open Brain service. Hosting is an operating model for the same Brain, not a separate data format or a fork of the product.

## Contract authority

For the historical v0.5 program, this document defined the self-hosted product and the foundation
that self-hosted and hosted deployments shared. New architecture and implementation plans trace to
the current product-family contract. Historical evidence may continue to cite these requirement IDs.

Requirements beginning with `V0-` are release requirements for the self-hosted public alpha. Requirements beginning with `FOUNDATION-` are also required in v0 because they prevent a later hosted fork or data migration. Requirements beginning with `HOSTED-` constrain the future managed service but do not block the self-hosted v0 release unless a `FOUNDATION-` requirement says otherwise.

When this contract conflicts with migration, parity, cutover, private integration, or predecessor-replacement work, this contract governs the public release. Existing privacy, provenance, durability, and single-writer invariants remain mandatory.

Changes to a `MUST` requirement require an explicit product decision before implementation. A passing test cannot silently redefine the contract.

## Product family boundary

Open Brain has two deployment products built on one foundation:

1. **Self-hosted Open Brain.** One owner runs one Brain on a Mac or Linux machine. The Brain root, local credentials, daemon, and recovery workflow are under that owner's control.
2. **Managed Open Brain.** The service runs many isolated single-owner Brains. It adds managed identity, provisioning, encryption, operations, and export around the same engine and portable data contract.

The self-hosted v0 release does not include the managed control plane, billing, shared organizational Brains, or multi-user editing. It must still create the stable tenant, actor, role, space, and record identities that the managed product needs.

## Target user and deployment

The v0 user is a single owner who:

- has one Mac or Linux machine that can remain on;
- wants local, inspectable storage rather than a required hosted account;
- wants to capture across several parts of life without learning a rigid taxonomy;
- may use Obsidian or another Markdown editor, but does not need either;
- can use Python 3.14 and `uv` for the supported macOS source/wheel path, or install a
  checksummed native archive on Linux, then run a setup command;
- expects the product to explain missing prerequisites and safe recovery steps.

The v0 deployment has one canonical writer. Other local processes may submit captures or read bounded results, but they cannot publish canonical knowledge without the writer and review rules.

## Domain terms

| Term | Meaning |
|---|---|
| Brain | One isolated body of captures, sources, spaces, canonical knowledge, review history, and operational state. |
| Brain root | The one directory selected by a self-hosted owner. Open Brain creates and manages its versioned internal layout. |
| Tenant | The stable isolation and ownership boundary for one Brain. Self-hosted v0 generates one local tenant identity even though it has no tenancy UI. |
| Actor | A stable human or system identity responsible for a capture, decision, transformation, or action. |
| Role claim | The bounded authority under which an actor performed an operation. v0 normally has one owner plus system and connector capabilities. |
| Space | A user-created organizational context with a stable identity, mutable name, and optional privacy or routing defaults. Starter templates may create spaces but do not change their semantics. |
| Capture envelope | The common immutable identity, provenance, privacy, source, time, actor, optional space, optional intent, optional reason, and typed payload metadata surrounding every capture. |
| Payload family | One of the shared input shapes: text, reference or file, event, or measurement. A client or connector supplies or infers the normalized shape; the owner does not need to choose it manually. |
| Capture | An immutable accepted envelope and payload. Capture is durable before routing, enrichment, or publication. |
| Source record | Preserved owner or third-party source material plus transformation receipts. It is not automatically an owner-authored claim. |
| Derived output | A proposed or accepted knowledge update, entity relationship, event or measurement record, or action linked to one or more captures. |
| Proposal | One independently reviewable derived output or change. One capture may produce several proposals. |
| Canonical page | Human-readable Markdown accepted as durable owner knowledge. |
| Portable Brain | The versioned, documented export containing canonical knowledge, source records, space definitions, provenance, and review history needed to continue on another Open Brain deployment. |
| Operational state | SQLite queues, leases, review state, run metadata, and other state needed to operate safely. |
| Index | A disposable projection used to accelerate retrieval. It must be rebuildable from portable data. |
| Connector | An optional adapter that normalizes an external source into the common capture envelope, supplies an enrichment provider, or performs a separately authorized external action. |
| Profile | A documented set of defaults and enabled capabilities. v0 ships one default profile: `single-user-local`. |

## Shared foundation requirements

| ID | Requirement |
|---|---|
| `FOUNDATION-IDENTITY-01` | Every durable capture, source record, proposal, decision, publication receipt, and external-action receipt MUST carry a stable tenant identity and actor identity. Self-hosted v0 MUST create these values without exposing tenancy setup to the owner. |
| `FOUNDATION-IDENTITY-02` | Security-sensitive receipts MUST record the actor's bounded role or capability at decision time. Authorization MUST NOT be inferred later from a mutable current role. |
| `FOUNDATION-SPACE-01` | Spaces MUST be user-created and identified by stable opaque IDs independent of mutable display names or directory slugs. The engine MUST NOT embed Personal, Work, Development, Health, or any other life domain as privileged behavior. |
| `FOUNDATION-SPACE-02` | A capture MAY be accepted without a space. Unassigned or ambiguous captures MUST remain visible in an inbox until an owner decision or deterministic rule routes them. Routing MUST NOT change capture identity or provenance. |
| `FOUNDATION-PORTABLE-01` | Self-hosted and hosted deployments MUST use the same versioned capture, source, space, proposal, provenance, and canonical-page schemas. Deployment-specific control-plane metadata MUST remain outside that contract. |
| `FOUNDATION-PORTABLE-02` | A Portable Brain export MUST contain canonical knowledge, source records and preserved files, space definitions, provenance, and review history. It MUST exclude credentials and MAY exclude disposable indexes and host-specific supervisor state. |
| `FOUNDATION-PORTABLE-03` | Export from a conforming deployment and import into a clean self-hosted release MUST preserve stable identities, canonical bytes, trust labels, provenance links, review outcomes, and space membership. |
| `FOUNDATION-PORTABLE-04` | The project MUST publish a conformance fixture and round-trip test for the Portable Brain contract so a hosted implementation cannot quietly diverge from OSS. |

## Supported self-hosted journey

The exact command flags are not frozen by this draft, but these outcomes are:

1. Install Open Brain from a versioned source checkout or published app and engine wheels on
   macOS, or from a checksummed native archive on Linux.
2. Initialize `single-user-local` beneath one chosen Brain root without hand-editing configuration. The owner may start empty or apply optional space templates.
3. Install and start one supervised daemon process with an explicit owner action.
4. Capture text, a reference or bounded file, an event, or a measurement without first choosing a space, intent, or reason, then see its durable status in the inbox.
5. Route an unassigned capture, review several independently derived outputs from one capture, and retrieve accepted knowledge or labeled source material through the CLI, local UI, or space-scoped MCP.
6. Export a Portable Brain and validate that it can be imported into a clean compatible root.

The quickstart must use no cloud account. Model download time is excluded from the provisional setup-time target, and a model cannot be required to durably accept or retrieve the first captures.

## Installation and initialization requirements

| ID | Requirement |
|---|---|
| `V0-INSTALL-01` | The project MUST publish versioned source and the `open-brain` and `open-brain-engine` wheels for Python 3.14 on macOS 14 or newer on Apple Silicon, and a checksummed native archive bundling Python 3.14 for supported Linux x86_64 hosts. A native macOS DMG and Apple notarization are deferred to a later release and MUST NOT block v0. |
| `V0-INSTALL-02` | Initialization MUST accept one Brain root and create the default layout, configuration, stable local tenant and owner identities, generated local credential, permissions, schema, and empty indexes. |
| `V0-INSTALL-03` | Initialization MUST be idempotent. Re-running it against the same compatible root MUST not replace user content, stable identities, space definitions, or credentials. |
| `V0-INSTALL-04` | The default profile MUST require no manual TOML editing. The owner MAY start with no named spaces or select optional starter templates such as Personal, Work, Projects, Health, and Learning. Advanced root and capability overrides MAY use explicit configuration after initialization. |
| `V0-INSTALL-05` | The application MUST preflight the supported host and architecture, any runtime not bundled in the artifact, filesystem permissions, available disk space, selected provider mode, and supervisor availability before installing the daemon. |
| `V0-INSTALL-06` | A failed initialization or daemon installation MUST report a bounded cause and rollback or cleanup instructions. It MUST not leave a partially authoritative writer. |

## Capture and processing requirements

| ID | Requirement |
|---|---|
| `V0-CAPTURE-01` | The CLI and authenticated HTTP intake MUST accept four common payload families: text; reference or bounded file; event; measurement. A responsive local UI MAY use the same intake interface. |
| `V0-CAPTURE-02` | Durable intake MUST require only an authenticated actor and a valid payload. Space, intent, and `capture_why` MUST be optional. Missing values MUST be represented as absent or unresolved rather than fabricated. |
| `V0-CAPTURE-03` | Every capture MUST preserve tenant, actor, capture identity, source, capture timestamp, normalized payload family and schema version, immutable provenance, privacy decision, and original supplied payload. It MUST also preserve occurrence time, space, intent, and `capture_why` when supplied or later assigned through a receipt-bound change. |
| `V0-CAPTURE-04` | A reference payload MAY contain an HTTP or HTTPS URL and supplied text without authorizing a network fetch. A file payload MUST preserve bounded bytes or a content-addressed local copy. When egress is disabled, supplied content remains a durable source record. |
| `V0-CAPTURE-05` | Event and measurement payloads MUST preserve supplied occurrence time separately from capture time. If occurrence time is absent, it MUST remain unresolved rather than being fabricated. Supplied measurement units and source dimensions MUST remain explicit rather than being flattened into prose. |
| `V0-CAPTURE-06` | Durable capture acceptance MUST complete before routing, fetching, model enrichment, or derived-output generation. Failure after acceptance MUST leave an inspectable pending item rather than lose or reject the capture. |
| `V0-CAPTURE-07` | Replay of the same capture identity and bytes MUST be idempotent. Conflicting bytes under one identity MUST quarantine rather than overwrite. |
| `V0-CAPTURE-08` | Source preservation, deterministic extraction, and indexing of eligible source records MUST have a no-model path for every required payload family. Model output MAY enrich or propose, but MUST NOT be required to preserve or find the source record. |
| `V0-CAPTURE-09` | Source-specific connectors MUST normalize data through the public capture interface. They MUST NOT create private source-specific publication pipelines or storage schemas that bypass capture identity, privacy, or provenance. |
| `V0-CAPTURE-10` | YouTube polling, media transcription, iMessage, social feeds, messaging, email/calendar, Git activity, finance, fitness, and other domain integrations are optional connectors and MUST NOT be required by the default profile. |

## Review and publication requirements

| ID | Requirement |
|---|---|
| `V0-REVIEW-01` | The owner MUST be able to list unassigned captures, pending items, and proposals through both CLI and local UI. |
| `V0-REVIEW-02` | The owner MUST be able to approve, reject, or safely edit each proposal through a receipt-bound decision. |
| `V0-REVIEW-03` | Third-party content MUST NOT become an owner-authored idea, fact, relationship, or action without explicit approval. |
| `V0-REVIEW-04` | `idea` and `action_candidate` intent MUST create proposals. Capture alone MUST NOT create an external task or action. |
| `V0-REVIEW-05` | One capture MAY create zero, one, or several linked derived-output proposals. Each proposal MUST have its own stable identity, state, expected receipt, and terminal decision. Rejecting one MUST NOT silently reject or approve its siblings. |
| `V0-REVIEW-06` | Rejection MUST preserve the source and audit record while preventing that proposal's canonical promotion or external action. |
| `V0-REVIEW-07` | Every proposal MUST show its source identity, space or unassigned state, privacy tier, supplied reason when present, proposed change, bounded evidence, and sibling-output context. |
| `V0-REVIEW-08` | Background curation MAY apply deterministic metadata repairs. Meaningful knowledge changes MUST otherwise wait for owner review or the explicit canonical-note action defined by `V0-REVIEW-10`. |
| `V0-REVIEW-09` | An external action MUST require an action-specific approval receipt. Approval to capture, classify, or publish knowledge MUST NOT grant action authority. |
| `V0-REVIEW-10` | Owner-authored text MUST publish immediately only when the owner explicitly chooses a canonical-note action. Quick capture, missing intent, and ambiguous owner text MUST remain durable in the inbox without canonical publication until later routing or review. |

## Canonical data and retrieval requirements

| ID | Requirement |
|---|---|
| `V0-DATA-01` | Canonical owner knowledge MUST be Markdown using a versioned schema with stable tenant, page, actor, and space identities; status; privacy and trust labels; provenance links; and modification metadata. |
| `V0-DATA-02` | Source records and typed payloads MUST use a documented, versioned, open representation and remain distinguishable from canonical pages and owner-authored claims. Preserved file bytes MUST be content-addressed. |
| `V0-DATA-03` | Operational SQLite databases MUST remain outside canonical and portable content and use schema checks, safe locking, and SQLite backup semantics. |
| `V0-DATA-04` | Indexes MUST declare their source generation and MUST be deletable and rebuildable without losing portable Brain data. |
| `V0-DATA-05` | Direct Markdown edits by the owner MUST be detected, validated, and reflected in retrieval without silently overwriting invalid or unrelated content. Space renames MUST preserve stable space identity and page provenance. |
| `V0-DATA-06` | Export and import MUST operate through the Portable Brain contract, not through undocumented copies of live SQLite files or hosted control-plane databases. |
| `V0-QUERY-01` | v0 MUST provide deterministic lexical or FTS retrieval over canonical pages and the text and metadata of eligible typed source records. A source result MUST remain labeled as source material rather than owner knowledge. |
| `V0-QUERY-02` | Results MUST include an opaque result ID, title or type label, bounded excerpt or structured value, trust label, space or unassigned state, payload family, provenance, and a bounded explanation of why the result matched. |
| `V0-QUERY-03` | Retrieval MUST support all-spaces search and explicit space filtering without assigning privileged semantics to starter space names. |
| `V0-QUERY-04` | The default profile MUST remain useful without embeddings, a graph database, or a hosted retrieval dependency. |
| `V0-QUERY-05` | Local vector or graph retrieval MAY be added only behind a measured, replaceable adapter after it beats the v0 retrieval fixture. |
| `V0-QUERY-06` | MCP MUST remain read-only except for allow-listed metadata feedback and MUST receive an explicit space allow-list. Capture, approval, publication, administration, and unlisted-space access require separate authority. |

## User surfaces

| ID | Requirement |
|---|---|
| `V0-SURFACE-01` | The CLI MUST expose command-specific help, stable exit classes, JSON output, and actionable remediation without leaking private values. |
| `V0-SURFACE-02` | The local UI MUST provide health, capture inbox, space routing, proposal review, search, and page viewing. Canonical page editing is not required in v0. |
| `V0-SURFACE-03` | HTTP MUST bind to loopback by default and require a generated local credential. The documented remote-access path MUST remain authenticated and encrypted. Direct private-network binding requires explicit opt-in and a security preflight. |
| `V0-SURFACE-04` | MCP MUST run over stdio by default and expose only bounded retrieval for explicitly allowed spaces plus metadata feedback. |
| `V0-SURFACE-05` | CLI and UI MUST report the same capture, space, proposal, review, and run states through the same application interface. |
| `V0-SURFACE-06` | Intake clients SHOULD infer payload family and inherit safe defaults. The owner MUST NOT be forced to choose a space, intent, privacy tier, or reason before durable acceptance. |
| `V0-SURFACE-07` | CLI and UI MUST expose an explicit canonical-note action separately from quick capture and MUST make the resulting canonical or inbox state visible. |

## Always-on and recovery requirements

| ID | Requirement |
|---|---|
| `V0-OPS-01` | The application MUST own install, start, stop, restart, status, and removal of one supervised daemon on launchd and systemd. |
| `V0-OPS-02` | The daemon MUST own the default profile's schedules internally. v0 MUST NOT require one operating-system job per application task. |
| `V0-OPS-03` | One canonical writer identity and lease MUST govern canonical writes. Restart MUST NOT create a second writer. |
| `V0-OPS-04` | Queue, review, publication, space routing, and export progress MUST survive process termination and host restart without duplicate publication or identity changes. |
| `V0-OPS-05` | Status and doctor MUST report configuration, provider mode, queue age, schema and index state, writer ownership, locks, last successful run, backup and export evidence, and actionable remediation. |
| `V0-OPS-06` | Logs and supervisor output MUST be metadata-only, bounded, and free of captured content, credentials, and private paths. |
| `V0-OPS-07` | The owner MUST be able to create a verified backup and restore it into an empty disposable root before replacing live state. |
| `V0-OPS-08` | Upgrade MUST preflight compatibility, create verified recovery evidence, apply versioned migrations, and run doctor before declaring success. |
| `V0-OPS-09` | Uninstall MUST remove application artifacts and supervisor state while preserving the Brain root by default. Data deletion requires a separate explicit action. |

## Privacy and security requirements

| ID | Requirement |
|---|---|
| `V0-PRIVACY-01` | Unknown, ambiguous, unassigned, and personal content MUST default to local-only handling in the self-hosted profile. Space defaults MAY tighten handling but MUST NOT silently authorize external egress. |
| `V0-PRIVACY-02` | Cloud and external egress MUST be disabled in the default profile. Enabling either requires explicit configuration and authority. |
| `V0-PRIVACY-03` | Local capture and retrieval MUST NOT require a cloud credential or optional cloud dependency. |
| `V0-PRIVACY-04` | Private directories and state MUST be owner-only; regular private files MUST use owner-only permissions. Symlink and traversal checks remain mandatory. |
| `V0-PRIVACY-05` | Secrets MUST be generated or referenced separately from public configuration and MUST never enter result envelopes, logs, representations, exports, or canonical Markdown. |
| `V0-PRIVACY-06` | Model calls MUST receive only the minimum privacy-authorized content. Final cloud prompts, if cloud is enabled, MUST be checked before credential resolution or adapter construction. |
| `V0-PRIVACY-07` | URL fetching MUST be separately authorized and use scheme, DNS, resolved-address, redirect, timeout, and size controls that prevent access to private or link-local targets. Untrusted file and media processing MUST remain bounded and isolated. |
| `V0-PRIVACY-08` | CLI, JSON, HTML, typed records, filenames, and provider-derived content MUST be treated as untrusted. The local UI MUST escape content and use a restrictive content security policy. |

## Managed hosting compatibility requirements

These requirements govern the later hosted product. They are not a requirement to build hosting before the self-hosted alpha.

| ID | Requirement |
|---|---|
| `HOSTED-01` | Managed Open Brain MUST run the published engine and pass the same Portable Brain conformance suite as self-hosted Open Brain. Hosted-only canonical schemas or hidden migration dependencies are forbidden. |
| `HOSTED-02` | The initial hosted product MUST provision many isolated single-owner Brains. Shared organizational writing MAY be added later through actor and role capabilities; it MUST NOT weaken tenant isolation. |
| `HOSTED-03` | Hosted content, sources, review state, and derived records MUST use per-tenant envelope encryption. Each tenant data-encryption key MUST be wrapped separately by a managed key service or equivalent isolated key boundary. |
| `HOSTED-04` | Only a narrowly authorized workload identity MAY request decryption for one tenant and one bounded operation. Plaintext MUST be limited to the authorized processing boundary and MUST NOT enter shared logs, queues, caches, analytics, or support tools. |
| `HOSTED-05` | Human access to customer content MUST be disabled by default. Any exceptional access mechanism MUST require an explicit reason and expiring authority and MUST write a complete immutable audit record. |
| `HOSTED-06` | The encryption, indexing, provider, and connector boundaries MUST preserve a future path to client-held keys. A future end-to-end encrypted mode MAY move plaintext search or enrichment to a client-owned worker or separately evaluated trusted-compute boundary. |
| `HOSTED-07` | A hosted owner MUST be able to request a complete Portable Brain export and continue with a compatible self-hosted release without losing stable identities, canonical knowledge, sources, provenance, review history, or spaces. |
| `HOSTED-08` | Billing, service telemetry, fleet operations, and other hosted control-plane records MUST remain outside the Portable Brain and MUST NOT be required to operate an exported Brain. |

## Connector product path

Source-specific connectors follow a staged product path and do not block the self-hosted v0 release:

1. Build YouTube polling as the reference connector against an internal connector interface.
2. Build GitHub as the event connector against the same interface.
3. Build a CSV importer as the measurement connector against the same interface.
4. Compare all three implementations and freeze only the behavior they genuinely share.
5. Publish a versioned Connector SDK with a capability manifest, conformance suite, isolated worker runtime, package signing, and compatibility policy.

The SDK MUST NOT be declared stable before all three representative connectors pass the same identity, privacy, checkpoint, duplicate-delivery, restart, bounded-output, and no-action-authority tests. The proof set is YouTube polling for references, GitHub for events, and CSV import for measurements. These choices do not change the four payload families or the v0 release boundary.

## Storage model decision

Open Brain uses a hybrid open format. Canonical pages and space definitions use human-readable Markdown. Captures, provenance, portable history, events, and measurements use versioned open structured records. Preserved files use content-addressed blobs. SQLite is limited to operational state and disposable indexes and is not the portable source of truth.

The version `1` path and format boundaries are fixed:

```text
<brain-root>/
  brain.toml
  content/
    spaces/
      <space-slug>/
        _space.md
        ...
  sources/
    captures/
      YYYY/MM/<capture-id>.json
    batches/
      YYYY/MM/<batch-id>.jsonl
    blobs/
      sha256/<first-two-hex>/<sha256>
  history/
    proposals/YYYY/MM/<proposal-id>.json
    decisions/YYYY/MM/<decision-id>.json
    publications/YYYY/MM/<publication-id>.json
    actions/YYYY/MM/<action-id>.json
  .open-brain/
    state/open-brain.sqlite3
    indexes/search.sqlite3
    run/
    credentials/
```

Every accepted capture has one immutable JSON envelope. Text, references, files, and individual events or measurements can remain in that envelope. A high-volume event or measurement import uses an immutable JSONL sidecar referenced by the envelope. Each proposal, decision, publication receipt, and external-action receipt is an individual JSON record.

`YYYY/MM` comes from the record's immutable UTC acceptance or recording timestamp, not mutable source occurrence time. Files never move because a source timestamp is corrected. Corrections append a new record linked with `supersedes`; they do not rewrite accepted history.

JSON files and JSONL lines use UTF-8, a versioned JSON Schema, deterministic canonical serialization, UTC RFC 3339 timestamps, and SHA-256 manifest checksums. JSONL contains one canonical record per line and ends each complete record with LF. The exact field-level schemas and conformance fixtures are Phase 0 specification artifacts. They may clarify representation but cannot change these accepted path, authority, identity, or format boundaries without a new product decision.

`brain.toml` and the portable directories contain no credentials. `.open-brain/credentials/`, operational SQLite, disposable indexes, and runtime files are excluded from Portable Brain exports.

## Supported v0 host matrix

The public alpha supports these release targets:

| Installation | Supported hosts | Release baseline |
|---|---|---|
| Versioned source checkout or app and engine wheels | macOS 14 or newer on Apple Silicon | Test source and isolated wheel installation on macOS 14 with Python 3.14 |
| Linux `x86_64` | Ubuntu 24.04 LTS, Ubuntu 26.04 LTS, and Debian 13 | Bundle Python 3.14, build on Ubuntu 24.04, and run clean-host tests on every listed distribution |

Intel macOS, Linux `arm64`, and Windows are outside the v0 support promise. Community reports may inform later expansion, but an untested artifact is not labeled supported.

PyInstaller 6 in one-folder mode is the Linux self-contained bundler and uses Python 3.14 for the
current release. Its clean-host evidence must
cover installation, daemon startup, package and resource discovery, upgrade, recovery, and
uninstall on the supported Linux matrix. A failing Linux build moves to Nuitka standalone. macOS
uses the versioned source/wheel path and may require supported Python and `uv`. A native macOS DMG,
including Developer ID signing and notarization, is deferred to a later release.

## Default profile

`single-user-local` is the only v0 release profile. It has these defaults:

| Setting | v0 default |
|---|---|
| Identity | One generated local tenant, one owner actor, bounded system and connector capabilities |
| Writer topology | One local canonical writer |
| Spaces | User-created; optional starter templates; unassigned inbox supported |
| Content | Markdown beneath the selected Brain root, grouped by user-created space |
| Sources | Versioned typed records and content-addressed preserved files beneath the Brain root |
| Storage model | Hybrid open format; Markdown, structured records, blobs, and non-canonical SQLite each have separate roles |
| Operational state | Local SQLite beneath the Brain root |
| Retrieval | Lexical or FTS across allowed spaces and eligible sources; embeddings and graphs off |
| Provider mode | `none` until one documented local provider is configured |
| Cloud and egress | Off |
| Network | Loopback HTTP; stdio MCP with an explicit space allow-list |
| Built-in capture | Four families: text; reference or bounded file; event; measurement |
| Owner-authored text | Explicit canonical-note action publishes immediately; quick or ambiguous capture remains in inbox |
| Optional connectors | No source-specific connector required |
| Background operation | One supervised daemon with internal schedules |
| Uninstall behavior | Preserve data |

Provider mode `none` is a complete operating mode. It preserves captures, exposes pending enrichment, permits deterministic publication allowed by the review rules, and keeps lexical retrieval available over eligible canonical content and source records.

## Release acceptance gates

| Gate | Pass condition |
|---|---|
| `V0-GATE-01` Clean install | On macOS, a clean versioned source checkout and an isolated app/engine wheel installation initialize and start the foreground daemon without hand-edited TOML. On Linux, the checksummed native archive bundles Python 3.14 and installs on each supported host without a source checkout or system Python. Each path meets the provisional 15-minute target, excluding model download time. |
| `V0-GATE-02` Generic first value | Text, reference or bounded file, event, and measurement fixtures can each be captured, found in the inbox, and retrieved without a model. Explicit canonical-note owner text publishes immediately, while the same text submitted through quick capture remains in the inbox. |
| `V0-GATE-03` Third-party safety | A third-party reference remains a labeled, retrievable source record or proposal until approval; rejection never promotes it. |
| `V0-GATE-04` Provider degradation | A provider outage leaves a durable pending item; later retry enriches it without a duplicate capture, output, or page. |
| `V0-GATE-05` Restart | Killing and restarting the daemon during each durable stage loses no accepted capture and creates no duplicate publication or derived output. |
| `V0-GATE-06` Retrieval | Exact, lexical, space-filter, typed-source, and freshness-after-edit fixtures meet fixed expected-result thresholds and return trust and provenance. Paraphrase and one multi-hop fixture record a baseline but have no v0 release threshold. |
| `V0-GATE-07` Review | One capture creates multiple fixture proposals; CLI and UI independently approve, reject, and safely edit them while preserving one terminal result per proposal. |
| `V0-GATE-08` Recovery | Backup, disposable restore, and doctor pass against release artifacts and preserve exact canonical and source bytes. |
| `V0-GATE-09` Upgrade | One prior supported v0 schema upgrades with verified recovery evidence and no private-data residue in output. |
| `V0-GATE-10` Removal | Removing the application stops its processes and leaves canonical Markdown and portable source records readable; explicit data purge is separately gated. |
| `V0-GATE-11` Privacy | Default-profile tests prove no cloud construction, credential resolution, external egress, or private-value output. |
| `V0-GATE-12` Packaging | Versioned source, app/engine wheels and sdists, the Linux native archive, checksums, supervisor resources, installation instructions, and supported-version metadata refer to the same release. A macOS DMG is not a v0 coordinate. |
| `V0-GATE-13` Spaces | The owner can create and rename a space, accept an unassigned capture, route it later without changing capture identity, and retrieve by space or across spaces. Starter space names receive no privileged behavior. |
| `V0-GATE-14` Portability | Export from a populated release root and import into a clean compatible root preserve identities, spaces, exact canonical and source bytes, provenance links, and review outcomes while excluding credentials. |

## Non-goals

The self-hosted v0 release does not include:

- the managed hosting control plane, billing, or fleet operations;
- multiple human owners, shared organizational writing, or collaborative conflict resolution;
- high availability, distributed consensus, or multiple canonical writers for one Brain;
- mandatory cloud models, hosted retrieval, graph storage, or vector infrastructure;
- a broad catalog of source-specific connectors;
- a public third-party Connector SDK before the three internal reference connectors prove its contract;
- a native macOS DMG or one-click macOS installer;
- full parity with private predecessor integrations, schedules, migration, or cutover evidence;
- a plugin marketplace, autonomous multi-agent memory, or automatic owner-knowledge rewriting.

Obsidian compatibility comes from the canonical Markdown contract. An Obsidian plugin, graph visualization, daily planner, shared organizational Brain, and broad connector catalog are later product decisions.

## Approved product-family decisions

The following decisions were approved on 2026-08-30 and are no longer open:

1. Hosted and self-hosted Open Brain use the same engine and Portable Brain contract.
2. Hosted launch means many isolated single-owner Brains; tenant, actor, and role identities exist from the start.
3. Organization uses user-created spaces with optional starter templates, not hard-coded life domains.
4. One capture pipeline accepts text, reference or file, event, and measurement payloads.
5. One immutable capture may produce several independently reviewable linked outputs.
6. Hosted content starts with per-tenant envelope encryption, scoped worker decryption, disabled-by-default human access, full exceptional-access auditing, and an architectural path to client-held encryption.
7. Durable capture accepts immediately with safe inherited defaults; space, intent, and reason are optional and unresolved captures remain in an inbox.
8. Connector development starts with internal reference, event, and measurement adapters; a public SDK follows only after those three prove the shared manifest, conformance, isolation, signing, and compatibility needs.
9. GitHub is the event connector used to prove occurrence time, ordering, updates, webhook or OAuth authority, and replay behavior.
10. CSV import is the measurement connector used to prove units, dimensions, batches, corrections, volume limits, and deterministic replay.
11. YouTube polling is the reference connector used to prove remote polling, cursor checkpoints, duplicate delivery, bounded fetch, and preserved source material.
12. Owner-authored text publishes immediately only through an explicit canonical-note action. Quick or ambiguous captures remain durable in the inbox for later routing.
13. Open Brain uses a hybrid open format: Markdown for canonical human knowledge, versioned structured records for typed and historical data, content-addressed blobs for preserved files, and SQLite only for operational state and disposable indexes.
14. Brain-root layout version `1` uses readable, date-partitioned directories for captures, batches, portable history, and content-addressed blobs, with operational state isolated beneath `.open-brain/`.
15. Individual captures and history records use canonical JSON; high-volume event and measurement batches use referenced JSONL sidecars. Versioned JSON Schemas, deterministic serialization, checksummed manifests, and conformance fixtures are required.
16. The v0 host matrix is macOS 14 or newer on Apple Silicon through versioned source/wheel installation, and Linux `x86_64` on Ubuntu 24.04 LTS, Ubuntu 26.04 LTS, and Debian 13 through a native archive. Intel macOS, Linux `arm64`, and Windows are deferred.
17. PyInstaller 6 one-folder mode is the Linux bundler candidate, with Nuitka standalone as its accepted fallback. A signed and notarized macOS DMG is deferred to a later release.

## Accepted boundaries awaiting evidence

The four bounded architecture choices above are no longer open. Two implementation artifacts remain before their contract clauses can be treated as proven:

1. Check in and review the exact versioned JSON Schemas, canonical serialization fixtures, JSONL batch fixtures, and Portable Brain conformance cases.
2. Prove source and isolated wheel installation on macOS 14 ARM64, and run the time-boxed
   PyInstaller spike on the accepted Linux matrix. Keep PyInstaller for Linux only if it passes;
   otherwise run and document the accepted Nuitka fallback.

## Change control

The owner approved the complete contract on 2026-08-30, including:

1. the `single-user-local` default profile and the product-family boundary;
2. provider mode `none` as a complete operating mode;
3. one daemon with internal schedules rather than many installed jobs;
4. the four built-in payload families and optional source-specific connector boundary;
5. the Portable Brain foundation, release gates, non-goals, and accepted evidence conditions above.

On 2026-09-04, the owner amended the release packaging and runtime decisions: v0 supports Python
3.14 source/wheel installation on macOS, retains the checksummed native archive on Linux, and
defers the signed and notarized macOS DMG to a later release.

Architecture work may refine implementation details but cannot weaken these outcomes without a new product decision.

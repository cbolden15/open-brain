# Open Brain product contract

Status: current product authority

Version: v1.1

Date: 2026-09-14

## Decision

The Open Brain core is an unprivileged local application that runs a requested command in the
foreground and exits. One operating-system user gets one local Brain backed by SQLite and ordinary
files. A dedicated desktop companion is developed separately under
[ADR 0017](architecture/decisions/0017-desktop-companion-boundary.md).

The core never requires root, operating-system capabilities, namespaces, launchd, systemd,
containers, a daemon, a supervisor, or another background service. It has no HTTP listener and no
service lifecycle. Its MCP transport is inherited stdio and lives only for the invoking process.

The shipping optional desktop interface is a plugin installed into Open Brain's dedicated Obsidian vault.
While enabled, the plugin owns one `open-brain plugin` child over inherited stdio and stops that
process on unload. The child is part of the foreground desktop session. It is not a service. The
Obsidian application may remain open after an Open Brain operation finishes.

The optional Tauri companion provides local capture/search and Claude Code/Codex setup. It owns a
matched runtime bundle and is not installed by the core formula. The same setup is available through
the headless CLI. Source onboarding and the optional independent collector remain later milestones;
their dependencies and permissions belong outside the core package.

The first Secure Node implementation and the predecessor package are retained as source history in
`archive/open-brain-secure-node` and `archive/legacy`. Neither archive is part of the uv workspace,
an import path, a build, a test run, an executable, or an installed dependency graph. Any future
Secure Node must be a separate package or repository with its own namespace, entry points,
dependencies, support policy, and release audit.

## Five-minute product

After Homebrew is available, the target install is one command:

```sh
brew install cbolden15/tap/open-brain
```

The first stateful command MUST select the platform data directory, create owner-only directories,
create or reopen the local identity and SQLite state, perform the requested action, and exit. It
MUST NOT ask the user to choose a storage root, edit TOML, configure a service, provide a
certificate, or make a key-custody decision.

The supported command families are:

- `init`, `capture`, `import`, `search`, `space`, `inbox`, `export`, `doctor`, and `status`;
- `workspace` for the dedicated managed Markdown vault;
- `graph` for structural projection, suggestion review, consent, and exclusions;
- `obsidian-plugin` for owned plugin installation, status, and removal; and
- `mcp` over explicitly launched stdio with capabilities selected independently.

`agent setup` previews and applies owned Claude Code/Codex configuration at project or user scope.
It configures explicitly granted capture, search, inbox-read, and organization tools without
accessing client login credentials.

The runtime uses the platform default directory unless an expert supplies an absolute `--data-dir`:

| Host | Default Brain root |
|---|---|
| macOS | `$HOME/Library/Application Support/open-brain/brain` |
| Linux | `${XDG_DATA_HOME:-$HOME/.local/share}/open-brain/brain` |

`OPEN_BRAIN_ROOT` is not part of this product contract and the active CLI MUST ignore it.

## Privacy and trust

Open Brain relies on the operating-system account, filesystem permissions, disk protection, and the
owner's backups. It does not claim application-level encryption, compartment isolation,
cryptographic erasure, certified purge, multi-user authorization, or protection from another
process running as the same user.

MCP search grants the connected client whole-Brain read access. MCP capture writes durable,
unverified content. Inbox reads expose bounded capture previews and space names. Organization
creates/renames spaces and routes captures without publication or changed trust. These grants are
independent of capture/search and off by default. Workspace reads and graph refresh are absent unless their separate flags are
present. MCP cannot accept graph suggestions, resolve workspace conflicts, change provider consent,
or edit exclusions. The current MCP graph-refresh tool returns `provider_not_configured` because MCP
has no provider-credential setup operation. Returned note content is untrusted data. A network-backed
MCP client may send results to its own provider.

Ordinary capture, import, search, space, inbox, workspace, structural graph, export, status, and doctor operations
perform no network egress. Semantic graph refresh may send selected note content to one configured
cloud provider only after the owner acknowledges the managed-vault scope and grants current consent.
The supported direct adapters are OpenAI API, Anthropic API, and Google Gemini API. They use an
explicit API key from the current plugin session or the operating-system credential store, apply
content selection and redaction before resolving that key, and do not fall back to another provider.
Claude subscription is visible as unavailable with `subscription_isolation_unproven`; no note bytes
may enter that path.

The plugin client is confined to a versioned, bounded newline-framed stdio protocol named
`open-brain-client`, with `protocol_version: 1`. It has no database handle and cannot construct
engine authority. The bridge accepts only named operations with exact argument shapes, limits
request and response sizes, filters the child environment, and terminates the child process group
on unload, timeout, or protocol failure.

The managed vault is a sibling projection of accepted records. Open Brain stores stable identity,
revision, consent, exclusion, conflict, and accepted-suggestion state in the private Brain. The
Graphify helper receives one bounded accepted snapshot and produces structural links only. Generated
projection state and `Open Brain Graph.canvas` are rebuildable views, are never semantic sources,
and are excluded from Portable Brain. Inferred suggestions appear separately from explicit links and
cannot modify Markdown until the owner reviews and accepts a current suggestion. Acceptance records
its provenance, adds the permanent link through the revision flow, and rejects stale revisions.

## Shared data boundary

Shared semantic record definitions, canonical encoding, attachment resources, and Portable Brain
v1 stay in `open_brain_engine`. A complete export must preserve all immutable record families,
historical revisions, blobs, and their digests. A verified import reconstructs the same records and
resources without depending on a product-specific envelope.

This shared layer is the only supported interoperability boundary for a future product. Live SQLite
files, service state, authorization envelopes, custody metadata, and product-specific receipts are
not portable contracts.

## Distribution boundary

| Distribution or tree | Contents | Active status |
|---|---|---|
| `open-brain-engine` | Shared records, direct local tasks, workspace and inference contracts, SQLite, retrieval, Portable Brain | Shipping dependency |
| `open-brain` | Local bootstrap, foreground CLI, MCP/plugin stdio, provider adapters, plugin assets, local operations | Shipping application |
| `open-brain-graphify` | Pinned, patched structural Markdown helper with its own dependency closure and licenses | Shipping private helper resource |
| `packages/obsidian-plugin` | Desktop-only Obsidian client compiled into the base release archive | Shipping desktop interface |
| `packages/desktop` | Separately versioned Tauri host, packaged interface, and matched runtime pair | D1 contributor build; not a public desktop release |
| `open-brain-connectors` | Optional connector SDK and worker runtime | Separate optional distribution |
| `packages/collector` (planned) | Source scheduling and optional per-user collection lifecycle | Not implemented or installed |
| `archive/open-brain-secure-node` | Historical appliance, protocol, custody, service, and lifecycle implementation | Never built or installed |
| `archive/legacy` | Historical predecessor implementation | Never built or installed |

The plain `open-brain` wheel MUST expose only the `open-brain` script. It MUST NOT define Secure Node,
HTTP, daemon, scheduler, supervisor, lifecycle, or phase-specific entry points or extras. The base
dependency closure MUST exclude web servers, service managers, cryptography, SQLCipher, keyring,
container tooling, and platform privilege bridges.

The native executable audit MUST reject imports from archived or advanced module families and
reject server, cryptography, container, and service dependencies. Passing a source test is not
enough; the built executable's collected module inventory is the distribution evidence.

## Acceptance

The `OB-*` requirements below apply to the core distribution and its shipping Obsidian client.
Desktop and collector acceptance is recorded separately by milestone; the existing results do not
certify a desktop release or a source integration.

| ID | Requirement |
|---|---|
| `OB-INSTALL-01` | A supported Homebrew install produces one user-invoked `open-brain` executable, one private Graphify helper, and the version-matched plugin assets. |
| `OB-INSTALL-02` | First capture succeeds without configuration and without a second process. |
| `OB-BOUNDARY-01` | The active app source contains only local bootstrap, entry points, operations, stdio MCP, and their small support modules. |
| `OB-BOUNDARY-02` | The base wheel and native executable contain no archived Secure Node or legacy implementation. |
| `OB-OPS-01` | Each command runs in the foreground and leaves no listener, daemon, service unit, container, or supervisor behind. |
| `OB-PRIVACY-01` | Status reports profile `local`, SQLite storage, daemon false, and application encryption false. |
| `OB-PORTABLE-01` | Export and import preserve every shared semantic record and attachment byte for byte. |
| `OB-HISTORY-01` | Secure Node and legacy source remain available only under clearly marked archive directories. |
| `OB-WORKSPACE-01` | The dedicated sibling vault preserves stable note identity, explicit conflict review, and accepted revisions without treating generated output as source input. |
| `OB-GRAPH-01` | Structural and inferred edges remain distinguishable; inference alone cannot edit a note; a stale suggestion cannot be accepted. |
| `OB-CLOUD-01` | Semantic egress requires current owner consent, one explicit provider and access mode, eligible current revisions, and successful redaction. |
| `OB-CLIENT-01` | The Obsidian plugin uses `open-brain-client` protocol version 1 over bounded inherited stdio and owns the child lifecycle. |

## Superseded direction

Versions through v0.8 selected a Secure Node extra inside the Open Brain package. That choice is
superseded. The archive preserves its implementation evidence, but no archived plan, ADR, test, or
source file can add behavior or dependencies back to the active distribution.

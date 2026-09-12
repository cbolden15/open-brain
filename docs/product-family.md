# Open Brain product contract

Status: current product authority

Version: v0.9

Date: 2026-09-11

## Decision

Open Brain is the only active product in this repository. It is an unprivileged local application
that runs a requested command in the foreground and exits. One operating-system user gets one local
Brain backed by SQLite and ordinary files.

Open Brain never requires root, operating-system capabilities, namespaces, launchd, systemd,
containers, a daemon, a supervisor, or another background service. It has no HTTP listener and no
service lifecycle. Its MCP transport is inherited stdio and lives only for the invoking process.

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

- `init`, `capture`, `import`, `search`, `export`, `doctor`, and `status`;
- `mcp` over explicitly launched stdio with capture and search selected independently.

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
unverified content. Returned note content is untrusted data. A network-backed MCP client may send
results to its own provider, but Open Brain itself performs no network egress.

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
| `open-brain-engine` | Shared records, direct local tasks, SQLite, retrieval, Portable Brain | Shipping dependency |
| `open-brain` | Local bootstrap, foreground CLI, stdio MCP, local operations | Shipping application |
| `open-brain-connectors` | Optional connector SDK and worker runtime | Separate optional distribution |
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

| ID | Requirement |
|---|---|
| `OB-INSTALL-01` | A supported Homebrew install produces one `open-brain` executable. |
| `OB-INSTALL-02` | First capture succeeds without configuration and without a second process. |
| `OB-BOUNDARY-01` | The active app source contains only local bootstrap, entry points, operations, stdio MCP, and their small support modules. |
| `OB-BOUNDARY-02` | The base wheel and native executable contain no archived Secure Node or legacy implementation. |
| `OB-OPS-01` | Each command runs in the foreground and leaves no listener, daemon, service unit, container, or supervisor behind. |
| `OB-PRIVACY-01` | Status reports profile `local`, SQLite storage, daemon false, and application encryption false. |
| `OB-PORTABLE-01` | Export and import preserve every shared semantic record and attachment byte for byte. |
| `OB-HISTORY-01` | Secure Node and legacy source remain available only under clearly marked archive directories. |

## Superseded direction

Versions through v0.8 selected a Secure Node extra inside the Open Brain package. That choice is
superseded. The archive preserves its implementation evidence, but no archived plan, ADR, test, or
source file can add behavior or dependencies back to the active distribution.

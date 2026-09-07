# Open Brain product-family contract

- Status: Accepted
- Contract version: `0.6`
- Date: 2026-09-07
- Supersedes: the default-product boundary in `docs/v0-product-contract.md` version `0.5`

## Decision

Open Brain is the five-minute local product. Secure Node is the opt-in advanced product profile.
They share a record model and a portable export boundary, but they do not share an installation or
operating-complexity promise.

This preserves the existing engineering work while preventing Secure Node complexity from becoming
the default OSS experience.

## Product definitions

| Product | Default user | Installation promise | Operating model |
|---|---|---|---|
| Open Brain | One local user with one local Brain | One command on a clean supported macOS or Linux host; first use completes automatic setup | Direct local commands over SQLite-backed capture and search; no required daemon or service |
| Secure Node | An owner who explicitly needs stronger custody, isolation, audit, and multi-client controls | Opt-in installation and explicit secure setup | Encrypted custody, authorization, receipts, fencing, recovery controls, and optional service operation |

“Open Brain” without a qualifier means the default product. “Secure Node” means the advanced
profile. The earlier public names “Reference Node” and “M1 semantic kernel Node” now mean Secure
Node. Stable historical IDs such as `M1-W0` remain valid for traceability, but current planning
names them `SN1-W0`, `SN1-W1`, and so on.

## Open Brain default contract

Open Brain MUST:

- install with one documented command on each supported clean macOS and Linux host;
- create one private platform-local data directory automatically on first stateful command;
- create one local owner identity and one Brain without asking for a storage root;
- capture text locally, find it through SQLite-backed lexical or FTS search, and create a complete
  Portable Brain export without a model or network service;
- run each command directly, with no required background daemon or operating-system service;
- keep cloud access, connectors, and external egress off unless the user later opts in; and
- keep the user's records portable across Open Brain and Secure Node.

The default journey MUST NOT require Docker, TLS certificates, capability grants, key-custody
setup, daemon configuration, launchd or systemd installation, a storage-root decision, manual TOML,
or manual database setup.

The default local data directory is:

| Host | Brain root |
|---|---|
| macOS | `$HOME/Library/Application Support/open-brain/brain` |
| Linux | `${XDG_DATA_HOME:-$HOME/.local/share}/open-brain/brain` |

The parent application data home is the path above without its final `/brain` component. Executable
payloads and launchers live outside it. An optional absolute `--data-dir` names the Brain root
directly for expert and test use. The default journey never prompts for it. `OPEN_BRAIN_ROOT`
remains a Secure Node and legacy-test input; the default CLI does not consume it.

The Brain root is created with owner-only access. Directories use mode `0700`; regular private
files use mode `0600` where the host supports POSIX modes.

Open Brain uses SQLite as its local transaction and search substrate. The on-disk implementation
may also materialize readable Markdown and structured records, but users do not configure or run a
database. A full export is produced through the Portable Brain contract, not by copying a live
SQLite file.

### Default privacy statement

Open Brain protects its local directory with operating-system user permissions and relies on the
host's account, disk-encryption, backup, and physical-security controls. The default product does
not promise application-level encryption at rest, key destruction, cryptographic purge, encrypted
search indexes, or resistance to code running as the same operating-system user. Documentation and
status output MUST NOT attribute Secure Node's encryption guarantees to Open Brain.

## Secure Node contract

Secure Node contains the advanced work formerly described as the M1 Reference Node. It is disabled
and absent from the default dependency closure unless the user selects it.

Secure Node MAY require explicit choices and setup for:

- encrypted ledger, search, blob, key, and credential custody;
- compartments, capability grants, proof of possession, and authorization;
- signed receipts, replay protection, sequencer fencing, and cold transfer;
- provenance-closed certified purge and controlled recovery; and
- daemon or service operation and concurrent local clients.

Secure Node must keep its setup, runtime, and failure messages visibly separate from the Open Brain
quickstart. Installing or invoking Open Brain must not silently initialize Secure Node, generate
Secure Node keys, start a listener, install a service, or claim Secure Node protection.

## Shared record and portability boundary

Both products use the same semantic core for Brain, actor, space, capture, source, proposal,
decision, publication, action, provenance, and stable record identities. A shared semantic identity
is the exact Portable Brain identifier. A Secure Node may assign a role-distinct protocol envelope
identifier, but that identifier does not replace or reinterpret the shared identity. The mapping is
total, deterministic, collision-checked, and keeps the exact source identity inside the protected
record body. Deployment details are not part of that core. SQLite paths, grants, keys, nonces,
service state, locks, projection checkpoints, and host placement remain operational data.

Portable Brain v1 is the required shared interchange profile and the default-to-Secure-Node upgrade
input. A conforming export preserves stable identities, exact portable bytes, payload schemas,
space membership, provenance links, review outcomes, and source material while excluding
credentials and disposable indexes.

Secure Node's Brain Protocol and BrainPack v2 work remains valid as an advanced envelope around the
shared semantic core. It must not replace shared records with Secure-Node-only equivalents. Before
Secure Node persistence work advances, a conformance mapping must prove that every Portable Brain
v1 semantic record has one lossless Secure Node representation. Portable records remain immutable
historical records during import. They are not coerced into active Secure Node proposals, decisions,
or effects when those state machines have different meanings.

The Portable manifest authenticates the source snapshot and stays with import evidence; it is not a
canonical Brain record. Byte equality covers every file declared by that manifest. A later export
may create a new manifest identity and timestamp around those same bytes. Content-addressed source
blobs remain payload attachments and never become public identifiers derived from their content
hashes. Portable-valid owner Markdown without a stable page ID also remains an exact attachment;
the upgrade does not fabricate a semantic identity from its mutable path.

One canonical full-plan digest binds the exact source manifest evidence, ordered shared records,
blob inventory, Secure Node envelope identities, and batch context. Individual commit digests cover
only their protocol items. The later receipt-bound import control operation must bind the full-plan
digest so an omitted or reordered attachment cannot pass as a complete upgrade.

Upgrading an Open Brain uses export and import, not an undocumented copy or in-place rewrite of its
SQLite files. Secure Node import MUST:

1. validate the complete Portable Brain export before writing;
2. preserve all shared IDs, bytes, timestamps, provenance, spaces, and review outcomes;
3. apply an explicitly selected Secure Node compartment and custody policy in a new receipt-bound
   import operation;
4. encrypt newly written Secure Node storage without claiming the source history was previously
   encrypted; and
5. leave the source Open Brain and its export unchanged until the new Secure Node passes
   conformance and search checks.

`CORE-W0` defines only the inbound, pure mapping and a test-only inverse used to prove exact bytes.
It does not expose a Secure Node plaintext export path. User-facing Secure Node export remains
subject to destination authorization, compartment checks, purge state, and a receipt-bound
operation in a later workstream.

## Packaging decision

The next release uses one distribution with a deliberately small default and an opt-in extra:

| Install | Dependency contract | Result |
|---|---|---|
| `open-brain` | Base `open-brain-engine` plus only dependencies needed for direct local capture, SQLite search, and export | Open Brain default |
| `open-brain[secure-node]` | Base plus `open-brain-engine[secure-node]`, cryptography, SQLCipher, key custody, owner control, and service/transport dependencies | Secure Node profile |

The existing engine extra named `node` was renamed to `secure-node`; no default dependency selects
it. Starlette, Uvicorn, SQLCipher, Argon2,
cryptography, keyring, and platform user-presence bridges belong in the Secure Node dependency
closure unless a later default-product requirement independently needs one of them.

The default executable is `open-brain`, and `python -m open_brain` has the same local behavior.
Secure Node uses the explicit `open-brain-secure-node` and `open-brain-secure-node-mcp` command
names. Because Python extras cannot add console scripts conditionally, a base-only installation may
contain those two launcher names only as inert wrappers: each must report that
`open-brain[secure-node]` is not installed before importing advanced code or touching state.

The architecturally strongest alternative is a separate `open-brain-secure-node` distribution with
its own executable and release cadence. It gives the cleanest dependency and support boundary. It
is not selected for the next release because the existing app and artifact policy already provide
one versioned distribution and the extra can preserve that work. Re-evaluate the separate
distribution before release if dependency isolation, CLI dispatch, artifact-policy checks, or
independent versioning cannot be proven with the extra. The current arrangement, where plain
`open-brain` installs the advanced `node` extra automatically, is rejected.

| Option | Boundary quality | Cost | Decision |
|---|---|---|---|
| `open-brain` plus `open-brain[secure-node]` | One release identity; requires executable tests proving the base neither installs nor imports advanced code | Smallest change to the existing package and artifact work | Selected for the next release |
| Separate `open-brain-secure-node` distribution | Strongest dependency, executable, support, and release boundary | Adds an artifact, namespace/entry-point decisions, version coordination, and policy work | Correct-architecture fallback; pending the checks above |
| Plain `open-brain` automatically installing the old `node` extra | No enforceable product boundary | Preserves current metadata but violates the default promise | Rejected |

## Acceptance requirements

| ID | Requirement |
|---|---|
| `OB-INSTALL-01` | The exact clean-host test in `docs/acceptance/five-minute-install.md` passes on every supported macOS and Linux release host in 300 seconds or less. |
| `OB-INSTALL-02` | First capture creates the platform-local private directory, one owner, one Brain, and SQLite state without an init command, prompt, environment variable, or config file. |
| `OB-INSTALL-03` | The installed base dependency graph excludes every Secure Node-only dependency and importing the base CLI loads none of them. |
| `OB-DATA-01` | Capture and search succeed locally with provider mode `none`; a successful capture is durable before the command returns. |
| `OB-DATA-02` | Full export validates against Portable Brain v1 and contains every accepted shared record while excluding credentials, live SQLite files, locks, and caches. |
| `OB-OPS-01` | The five-minute journey starts no daemon, listener, container, launchd unit, systemd user unit, or second writer. |
| `OB-PRIVACY-01` | Status and documentation identify the profile as `local` and application encryption as `false`; no Secure Node guarantee is implied. |
| `SN-INSTALL-01` | Secure Node capabilities appear only after an explicit Secure Node install and setup. |
| `SHARED-UPGRADE-01` | Importing a default export into a fresh Secure Node preserves the shared semantic inventory byte for byte and records only the new custody and authorization metadata. |

## Non-goals of the default product

Open Brain's five-minute contract does not include multi-client authorization, compartments,
cryptographic receipts, certified purge, root-key custody, fencing, service installation, remote
access, or application-level encrypted storage. Those are Secure Node features, not missing default
setup steps.

Hosted service design remains separate. It may consume the shared record and export contracts, but
it cannot redefine them or weaken the default-to-Secure-Node upgrade path.

## Change control

This contract is the current product authority. The historical v0.5 appliance contract and plans
remain useful implementation evidence but cannot make daemon, custody, or Secure Node dependencies
part of the default Open Brain promise. A change that moves an advanced requirement back into the
default installation needs a new explicit product decision and replacement clean-host evidence.

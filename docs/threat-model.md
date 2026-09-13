# Threat model

Open Brain is an unprivileged foreground application for one trusted local operating-system user.
Its primary risks are private-content disclosure, path traversal, malformed imports or exports,
prompt injection, duplicate writers, and unsafe schema migration.

Security properties are enforced by tests and distribution audits, not by documentation alone.
Public fixtures are synthetic.

## Trust boundary

Code already running as the Brain owner's OS user is inside the trust boundary. It can read or alter
the owner's files and process memory. Open Brain does not claim application-level encryption,
hostile same-user isolation, cryptographic erasure, or resistance to a compromised operating system.

The runtime needs no root access, OS capabilities, namespace setup, container, listener, daemon,
supervisor, launchd job, systemd unit, or background service. Each CLI command opens local resources,
performs bounded work, closes them, and exits. While the Obsidian plugin is enabled, it owns one
normal-user child for that desktop session and stops it on unload.

## Filesystem and SQLite

Owner-only paths protect data from other OS users where permissions are supported. Path confinement,
descriptor checks, SQLite transactions, schema migration, process locks, and bounded busy waits
protect against accidental traversal, partial writes, and cooperating concurrent commands.

These controls do not make a filesystem immutable against another process with the same user ID.
Network, synchronized, FUSE, and other filesystems with weak identity or metadata semantics are not
trusted snapshot sources for Markdown import.

## Markdown import

Import pins and rechecks the owner-selected absolute root. It skips symlinks, hardlinks, special
files, Obsidian metadata directories, and source trees that overlap protected roots. Aggregate and
per-file limits run before persistence. Imported filenames and bytes remain inert, unverified data.
The importer does not execute code, follow links or embeds, evaluate HTML, or load plugins.

Source deletion is not a confidentiality purge. Immutable revisions remain locally readable and
exportable.

## Search

The public projection is applied before FTS matching and again before results are returned. Literal
queries prevent user text from becoming FTS syntax. Projection removes protected paths,
credential-like values, source references, and digests. The index remains sensitive plaintext and
may use operating-system temporary storage.

## MCP

`open-brain mcp` uses inherited stdio only. Capture and search are absent unless their matching flags
are supplied. Search grants whole-Brain reads to the connected client. A network-backed client may
forward returned data to its provider. Captured and retrieved text may contain prompt injection and
must never be treated as instructions or authorization.

The capture sink cannot publish owner-authored content or invoke actions. Message, call, and byte
limits bound accidental loops. Idempotency keys bind exact input. EOF prevents further operations
but does not erase completed captures.

Optional workspace reads omit local paths and private policy or credential metadata. Graph refresh
requires a separate launch flag. In version 0.1.0 it returns `provider_not_configured` because MCP
cannot configure providers or receive credentials. It also cannot change consent or exclusions,
accept suggestions, resolve conflicts, or obtain owner mutation authority.

## Obsidian plugin IPC

The plugin resolves an absolute Open Brain executable, spawns it without a shell, and passes a small
allowlist of environment variables. Requests and responses use `open-brain-client` protocol version
1 over inherited stdio. Exact operation names, exact argument sets, identifiers, byte limits,
timeouts, and response validation prevent the desktop client from becoming a general command or
filesystem bridge. Protocol errors and timeouts terminate the child process group. The plugin still
runs with the trusted owner's account and can edit files in the open vault; this protocol does not
claim hostile same-user or malicious-plugin isolation.

## Cloud inference

Direct OpenAI, Anthropic, and Gemini adapters are network egress points. Dispatch requires current
owner consent for one provider, eligible accepted note revisions, exclusion checks, input and attempt
budgets, and a successful redaction/canary check before the API key is resolved. Hosts, paths, models,
headers, response shapes, input/output bytes, and deadlines are fixed or bounded. Provider failures
are redacted and cannot trigger cross-provider fallback.

The owner accepts the provider's privacy, retention, account, quota, and billing terms. Revocation
prevents later dispatch but cannot retract bytes already sent. API keys are session-only or stored by
macOS Keychain or Linux Secret Service when available. Ambient provider credentials are not used.
Claude subscription dispatch fails closed with `subscription_isolation_unproven`; no privileged
staging, namespaces, capabilities, fixture-owner topology, or broader host access is an acceptable
substitute.

## Managed graph

The managed vault is a sibling projection of accepted records. Generated Canvas and app-private
graph cache files are views, never source notes or Portable Brain content. The separate Graphify
helper receives only a bounded accepted snapshot over stdio, has a narrow structural-extraction
protocol, and receives no provider credential or network configuration. A complete generation is
published atomically; failure retains a visibly stale prior generation.

Semantic suggestions remain proposed edges with revision-bound source evidence. Inference alone
cannot write a link. Acceptance rechecks both endpoint revisions and uses the conflict-preserving
workspace flow. User-owned files at the generated Canvas path are never overwritten.

## Portable Brain

Import and export validate canonical records, digests, path names, file types, and attachment bytes.
Promotion uses staging and atomic no-replace behavior within the trusted-owner boundary. Portable
Brain is a data-transfer contract, not a backup service or a product-specific security envelope.

## Distribution boundary

The base dependency graph excludes web servers, service managers, cryptography stacks, container
tooling, and privilege bridges. Its standard-library HTTPS client is present only for the bounded
direct provider adapters. The native distribution audit rejects removed Secure Node engine families
and forbidden dependencies in the built executable. Graphify has a separately audited executable,
dependency closure, protocol, and license inventory. Historical appliance and legacy code remains
under `archive/` and is excluded from imports, builds, tests, and installed artifacts.

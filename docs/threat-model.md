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
supervisor, launchd job, systemd unit, or background service. Each explicit command opens local
resources, performs bounded work, closes them, and exits.

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

## Portable Brain

Import and export validate canonical records, digests, path names, file types, and attachment bytes.
Promotion uses staging and atomic no-replace behavior within the trusted-owner boundary. Portable
Brain is a data-transfer contract, not a backup service or a product-specific security envelope.

## Distribution boundary

The base dependency graph excludes web servers, service managers, cryptography stacks, container
tooling, and privilege bridges. The native distribution audit rejects those modules and removed
Secure Node engine families in the built executable. Historical appliance and legacy code remains
under `archive/` and is excluded from imports, builds, tests, and installed artifacts.

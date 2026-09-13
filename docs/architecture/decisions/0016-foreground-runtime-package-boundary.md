# ADR 0016: Foreground runtime package boundary

Status: accepted

Date: 2026-09-11

## Context

ADR 0012 kept Secure Node as an optional extra inside the Open Brain application. Metadata could
hide its dependencies from a plain install, but the shared namespace still mixed a small local tool
with HTTP, daemon, scheduler, supervisor, lifecycle, custody, and protocol implementation. Optional
connectors also imported worker contracts from the base app namespace.

The user-facing product needs a stronger invariant: installing or importing Open Brain should make
privileged or persistent service operation structurally unavailable, not merely disabled by a
profile flag.

## Decision

Open Brain is one unprivileged foreground runtime. Its application package contains local
entrypoints, bootstrap, operations, bounded stdio MCP, and narrow supporting modules. It has no
Secure Node extra or entry point and no HTTP, daemon, scheduler, supervisor, lifecycle, or Phase 1
application implementation.

Secure-only application and engine source is quarantined under
`archive/open-brain-secure-node`. The predecessor distribution is quarantined under
`archive/legacy`. Both are excluded from the workspace, import paths, builds, tests, and installed
artifacts. Optional connector protocol code lives in the connector distribution's own namespace.

Shared semantic records, canonical encoding, SQLite-backed local tasks, and Portable Brain v1
remain in `open_brain_engine`. They form the interoperability boundary for any future separate
product. Product-specific protocol envelopes, ledgers, custody, authorization, backup, and service
authority do not.

Any future Secure Node must be a separate package or repository with its own namespace, entry
points, dependency graph, tests, release artifacts, and support policy. It cannot return as an
Open Brain extra.

## Consequences

The repository keeps implementation history without shipping or testing it as current product
code. Git moves preserve file ancestry, and archive README files state the non-building status.

The active suite is smaller because archived appliance and predecessor tests no longer run. New
architecture tests pin active source inventories, package edges, entry points, and forbidden import
families. `make native-audit` proves the boundary against the built executable's collected modules.

The persisted SQLite filename containing `phase1` remains for data compatibility. It is a storage
name only and does not restore a Phase 1 service or runtime profile.

ADR 0012 is superseded. ADRs that describe historical Secure Node internals remain implementation
evidence inside repository history, but they cannot broaden the active Open Brain contract.

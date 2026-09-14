# Architecture

The core architecture is an unprivileged foreground process over local SQLite and files. The
optional desktop companion has a separate native host and package boundary. Historical appliance architecture is quarantined under
`archive/open-brain-secure-node` and is not part of this design.

## Active package graph

```text
open-brain
  -> open-brain-engine

open-brain-connectors
  -> open-brain-engine

packages/desktop (separate Tauri build)
  -> bounded inherited stdio -> bundled open-brain
  -> matched bundled Graphify resource
```

`open-brain` does not depend on the connector distribution. Neither active distribution depends on
an archive. `open-brain-engine` imports no app or connector code.

## Engine

`packages/engine/src/open_brain_engine` owns shared record identities, canonical JSON, direct local
tasks, SQLite persistence, retrieval, reconciliation, Markdown import, and Portable Brain v1. The
engine has no appliance authority, HTTP protocol, daemon lifecycle, backup service, or Secure Node
envelope implementation.

The SQLite filename `.open-brain/state/phase1.sqlite3` is retained as an on-disk compatibility name.
It does not identify an active Phase 1 service or runtime profile.

## Application

`packages/app/src/open_brain` is intentionally small:

- `local_entrypoints.py` parses and runs the foreground CLI;
- `local_bootstrap.py` chooses and opens the local Brain;
- `local_operations.py` adapts engine operations for CLI and MCP;
- `local_mcp.py` exposes bounded capture and search over inherited stdio;
- `mcp_protocol.py`, `local_data.py`, and `profile.py` provide narrow support.

Every CLI command opens the Brain, performs its work, closes resources, and returns. The core opens
no network listener, installs no service, and acquires no appliance lifecycle role. The Graphify
adapter runs a bounded helper for structural projection; foreground desktop clients own their
stdio children. Those process lifetimes do not create a persistent core service.

## Desktop and concurrent clients

`packages/desktop` contains packaged interface assets and a Rust host. The renderer invokes named
native commands and cannot open SQLite or select an arbitrary executable. D0 uses only a synthetic
Brain; normal existing-Brain use belongs to D1. The runtime pair is selected by component digest
and protocol compatibility, independently of any CLI installed on PATH. Inside the app resources,
the pair keeps the installed prefix shape at `runtime/bin/open-brain` and
`runtime/libexec/open-brain-graphify`, so core helper discovery exercises the packaged layout.

`local_runtime_session.py` coordinates participating client lifetimes through private lock-held
markers. Admission is serialized with abandoned-session recovery so another healthy opener does
not invalidate a live client's consent or inference state. Crash recovery and final shutdown end
session authority. Older clients that do not participate require a separate compatibility gate
before mixed-release use with an existing Brain.

The optional independent collector remains planned. Its source network access, private control IPC,
credential references, and per-user lifecycle belong outside the core dependency closure.
[ADR 0017](architecture/decisions/0017-desktop-companion-boundary.md) defines these boundaries and
the separate desktop distribution gates.

## Optional connectors

`packages/connectors` is a separate optional distribution. Its connector protocol and worker
runtime live in its own namespace so the base application does not need connector contracts or
dependencies. Installing Open Brain does not install or discover connectors.

## Portable boundary

Portable Brain v1 and the shared record model remain product-neutral. Export contains immutable
semantic records, attachment resources, and digests. Import validates those bytes before rebuilding
local state. Product-specific custody, authorization, service, and transport state are outside this
boundary.

## Distribution proof

The app wheel exposes one `open-brain` entry point. PyInstaller freezes the core and Graphify into
separate artifacts with separate dependency inventories.
`make native-audit` examines the collected module graph and fails if it contains archived modules,
server frameworks, cryptography stacks, container tooling, or removed Secure Node engine modules.
Static architecture tests also pin the active source inventory and dependency edges.

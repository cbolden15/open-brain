# Architecture

The current architecture has one active runtime: an unprivileged foreground process over local
SQLite and files. Historical appliance architecture is quarantined under
`archive/open-brain-secure-node` and is not part of this design.

## Active package graph

```text
open-brain
  -> open-brain-engine

open-brain-connectors
  -> open-brain-engine
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

Every CLI command opens the Brain, performs its work, closes resources, and returns. No component
opens a network listener, installs a service, forks a daemon, supervises a child, or acquires an
appliance lifecycle role.

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

The app wheel exposes one `open-brain` entry point. PyInstaller creates one native executable.
`make native-audit` examines the collected module graph and fails if it contains archived modules,
server frameworks, cryptography stacks, container tooling, or removed Secure Node engine modules.
Static architecture tests also pin the active source inventory and dependency edges.

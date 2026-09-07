# CLI composition

## Target default commands

The Open Brain `local` profile will expose direct `capture`, `search`, `export`, `status`, and
`doctor` commands. First capture performs idempotent setup in the platform-local data directory.
These commands require no explicit root, daemon, listener, grant, certificate, or database setup.
The exact first-use surface is frozen by
[`acceptance/five-minute-install.md`](acceptance/five-minute-install.md).

Secure Node commands and service operation appear only after an explicit
`open-brain[secure-node]` install and setup. Plain CLI help and status must not imply Secure Node
encryption, compartment, receipt, fencing, purge, or multi-client guarantees.

## Current transitional commands

The current installed `open-brain` process opens one explicit single-user Brain root and dispatches six
engine-backed families: `capture`, `inbox`, `proposals`, `query`, `review`, and `spaces`.
The default parser accepts only those families. Each adapter receives one task protocol, and
loading the CLI starts no listener, scheduler, provider, connector, or network operation.

The retained 31-family parser and 30 scheduled routes are legacy compatibility code. They remain
directly testable through the legacy facade but are not imported or selected by the installed
Phase 2 CLI.

This is retained appliance implementation readiness. It is not the five-minute default path or a
claim of live parity, cutover, or service health.

## Service processes

`open-brain-mcp` runs the space-scoped stdio MCP server over the single-user application. It does not
require the HTTP credential. Its tools are `brain_query`, `brain_fetch`, and metadata-only
`brain_retrieval_feedback`; retrieval is scoped by the explicit
`OPEN_BRAIN_MCP_ALLOWED_SPACE_IDS` JSON array; set it to `[]` for an empty scope.

There is no standalone `open-brain-http` writer process. `open-brain daemon` starts the foreground
process that composes the bounded UI/share server, and it owns that listener for the life of the
daemon-authority lease. Browser logins bootstrap from the generated local appliance
credential into a host-only session cookie plus CSRF token. The listener defaults to
`127.0.0.1:8788`; the documented remote path is an authenticated SSH tunnel to loopback. A
private-network bind requires explicit opt-in plus `OPEN_BRAIN_UI_EXTERNAL_TLS_TERMINATION=true`
and an exact `OPEN_BRAIN_UI_EXTERNAL_ORIGIN=https://...` value. Without those settings the daemon
refuses the bind.

## Commands

The default families cover the Phase 1 journey:

| Family | Operations |
| --- | --- |
| `capture` | Quick capture for text, reference, bounded file, event, or measurement; explicit canonical-note text |
| `inbox` | List all or only unassigned captures |
| `spaces` | List, create, rename, and route by opaque space ID |
| `proposals` | List proposals, optionally filtered by capture or state |
| `review` | Approve, reject, or edit one proposal with a delivery ID |
| `query` | Lexical retrieval with optional space, family, type, and limit filters |

An injected adapter receives the exact arguments after its command family. Adapter output
must name the selected family, include a status, be JSON-safe, and pass the public-output
redaction checks. A missing adapter returns `command_adapter_unavailable`. An exception,
malformed result, mismatched command, echoed argument, credential-like value, URL, path,
traceback, or exception residual returns `command_adapter_failed`. Neither response
includes the rejected value.

Ordinary family adapters cannot emit live, parity, or cutover readiness fields at any
nesting level. Public strings and field names are checked through at most three rounds of
percent-decoding; output that has not converged at that bound is rejected without residue.

## Output and exits

Use `--json` before or after the family name for a deterministic JSON envelope. Global help,
family help, and `--version` do not require a Brain root; adding `--json` to a help or version
request does not change that. A `--dry-run` request is never discarded: the current Phase 1
adapters have no preview operation, so they reject it with usage exit `2` before mutation.

| Exit | Class | Meaning |
| --- | --- | --- |
| 0 | success | No command was requested, or help/version was requested. |
| 1 | failure | Adapter unavailable, failed, or returned unsafe/invalid output. |
| 2 | usage | The command or its arguments are invalid. |
| 3 | deferred | A review decision was explicitly deferred; this is not an unimplemented capability. |

Error envelopes include only a stable error code, a generic message, and
`redacted: true`. Exception text, configuration values, paths, credentials, URLs, and
input arguments are not emitted.

Engine task results are projected before this representation serializes them. Titles and excerpts
remain useful, while raw or encoded protected references, absolute paths, credentials, reversible
bare SHA-256 tokens, storage-derived space slugs, and canonical storage paths are replaced with bounded or
opaque values. Portable/source bytes are unchanged.

# CLI composition

## Default Open Brain commands

The Open Brain `local` profile exposes help, version, `init`, `capture`, `import`, `search`, `export`,
`status`, and `doctor` through both `open-brain` and `python -m open_brain`. Every stateful command
creates or reopens one owner, one Brain, and its SQLite schema in the platform-local data directory.
The first capture needs no separate init command. No command requires an explicit root, daemon,
listener, grant, certificate, model, network service, or database setup.

```sh
open-brain capture "A note to remember"
open-brain import /absolute/path/to/markdown --yes
open-brain search "remember"
open-brain export /absolute/path/to/brain-export --verify
open-brain status --json
open-brain doctor --check private-data-directory
open-brain doctor --check no-background-runtime
open-brain doctor --check base-dependency-closure
```

`capture` stores owner-authored text durably before returning. `import` recursively captures
lowercase `.md` files without changing the source tree, skips Obsidian metadata directories, and
labels imported records unverified. Its first run for a directory warns that immutable revisions
remain in history and export after source removal. The first non-interactive import of a new root
requires `--yes`; registered-root reruns do not. An explicit `--allow-large-vault` retry bypasses
only aggregate scan bounds. Markdown links, frontmatter, HTML, and embeds remain inert text. `search`
prints bounded public result text. `export --verify` promotes
and reopens a full Portable Brain v1 export before recording
metadata-only verification evidence. Status reports `profile=local`, `brain_count=1`,
`storage=sqlite`, `daemon_running=false`, `application_encryption=false`, and the last verified
Portable export state. The exact first-use surface is frozen by
[`acceptance/five-minute-install.md`](acceptance/five-minute-install.md).

An optional absolute `--data-dir` names the Brain root for tests or expert use. The default path
ignores `OPEN_BRAIN_ROOT`, which remains a Secure Node compatibility setting. Local failures use
bounded messages and do not echo captured text, rejected arguments, or private paths. JSON error
output remains bounded whether `--json` appears before or after the subcommand.

Status observes the daemon-authority lease instead of assuming it is absent. If daemon authority or
runtime artifacts are present, direct local initialization, capture, import, search, and export fail closed;
status remains available, and `no-background-runtime` reports the conflict. The
`base-dependency-closure` doctor check validates the complete installed base declaration chain from
`open-brain` through `open-brain-engine` and `rfc8785`. Fresh-process tests and the release artifact
inventory separately verify that the default command loads no Secure Node modules.

Secure Node commands and service operation appear only after an explicit
`open-brain[secure-node]` install and setup. Plain CLI help and status must not imply Secure Node
encryption, compartment, receipt, fencing, purge, or multi-client guarantees.

## Secure Node precursor commands

With `open-brain[secure-node]` installed, `open-brain-secure-node` opens one explicit single-user
Brain root and dispatches six engine-backed families: `capture`, `inbox`, `proposals`, `query`,
`review`, and `spaces`. Each adapter receives one task protocol. Loading the CLI starts no listener,
scheduler, provider, connector, or network operation.

The retained 31-family parser and 30 scheduled routes are legacy compatibility code. They remain
directly testable through the legacy facade but are not imported or selected by the installed
Phase 2 CLI.

This is retained appliance implementation readiness. It is not the five-minute default path.

## Service processes

`open-brain-secure-node-mcp` runs the space-scoped stdio MCP server over the single-user
application. It does not require the HTTP credential. Its tools are `brain_query`, `brain_fetch`, and metadata-only
`brain_retrieval_feedback`; retrieval is scoped by the explicit
`OPEN_BRAIN_MCP_ALLOWED_SPACE_IDS` JSON array; set it to `[]` for an empty scope.

There is no standalone `open-brain-http` writer process. `open-brain-secure-node daemon` starts the
foreground process that composes the bounded UI/share server, and it owns that listener for the
life of the daemon-authority lease. Browser logins bootstrap from the generated local appliance
credential into a host-only session cookie plus CSRF token. The listener defaults to
`127.0.0.1:8788`; the documented remote path is an authenticated SSH tunnel to loopback. A
private-network bind requires explicit opt-in plus `OPEN_BRAIN_UI_EXTERNAL_TLS_TERMINATION=true`
and an exact `OPEN_BRAIN_UI_EXTERNAL_ORIGIN=https://...` value. Without those settings the daemon
refuses the bind.

## Retained Secure Node command families

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

## Secure Node precursor output and exits

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

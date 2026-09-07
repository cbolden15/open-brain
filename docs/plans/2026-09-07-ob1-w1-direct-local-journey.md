# OB1-W1: direct local capture, search, export, status, and doctor

- Status: Third review fixes implemented; focused, full, and native gates pass; fresh review pending
- Product: Open Brain default `local` profile
- Branch: `goal/open-brain-five-minute-install`
- Starting commit: `4c927660e52d3ea6230f7e14997a74d8ea6062ff`
- Product authority: [`../product-family.md`](../product-family.md)
- Acceptance authority: [`../acceptance/five-minute-install.md`](../acceptance/five-minute-install.md)
- Roadmap authority: [`product-roadmap.md`](product-roadmap.md)

## Objective

Complete the daemonless data journey used by the exact five-minute acceptance test. A first
`open-brain capture` must create one private local Brain automatically, durably store owner text in
SQLite and Portable records, make it immediately searchable, and include it in a validated full
Portable Brain export. `status --json` and the three named doctor checks must report the observed
default-product state without importing or claiming Secure Node behavior.

This workstream implements data use only. It does not publish artifacts or claim the cross-host
300-second gate; those belong to `OB1-W2` after `CORE-W0` closes.

## Allowed scope

- The default CLI under `packages/app/src/open_brain/services/local_*`.
- Public task contracts already owned by `open-brain-engine`, but only where a small additive API is
  required for an honest local status or export receipt.
- Focused app, engine, security, native-artifact, and acceptance tests.
- Base native module membership when a new default-only module becomes reachable.
- CLI, roadmap, package-classification, artifact-policy, and workstream documentation required to
  describe the implemented behavior.

## Forbidden scope

- Secure Node semantic-kernel, ledger, custody, grant, fencing, purge, daemon, service, HTTP, UI,
  or MCP implementation.
- Connector execution, cloud providers, model calls, network listeners, subprocess workers, or
  background threads.
- Release publication, live migration, deployment, or predecessor changes.
- Copying a live SQLite database as an export or exposing absolute local paths in command output.
- Claiming application-level encryption for the default product.

## Command contract

| Command | Required behavior |
|---|---|
| `open-brain capture TEXT` | Automatically prepare the platform-local private root, open the provider-`none` engine directly, durably accept one owner-authored `TextPayload`, and return only a bounded receipt. |
| `open-brain search QUERY` | Open the same local Brain directly and print bounded public retrieval results; plain output must contain matching safe text and JSON output must use stable fields. |
| `open-brain export DESTINATION --verify` | Create a fresh full Portable Brain v1 export outside the live root, validate it from disk before success, and persist only metadata-safe verification evidence in the live root. |
| `open-brain status --json` | Report exactly one local Brain, SQLite storage, no daemon, no application encryption, and whether a verified export is recorded. |
| `open-brain doctor --check CHECK` | Run one of `private-data-directory`, `no-background-runtime`, or `base-dependency-closure`; return zero only for observed passing evidence. |

All stateful commands accept an optional absolute `--data-dir` for tests and expert use. They ignore
`OPEN_BRAIN_ROOT`. Help and version stay root-free. Invalid input and unsafe local state fail with a
bounded generic message that does not echo text or paths.

## Implementation invariants

1. Hold and revalidate the selected Brain-root identity across bootstrap and each first mutation.
2. Reuse the engine's existing capture, retrieval, and Portable tasks. Do not create a second record
   model or app-owned data store.
3. Generate operation identifiers locally with the existing typed identifier formats. A successful
   capture is committed and indexed before the process returns.
4. Validate the promoted export from its destination. Record only the export identifier, timestamp,
   and manifest digest beneath private operational state.
5. Doctor evidence combines root ownership/mode validation, absence of local daemon artifacts or
   held writer leases, and the full installed base dependency declaration chain. Fresh-process
   tests and the native artifact inventory separately prove that loading the base command does not
   admit Secure Node modules.
6. Parse failures emit only a bounded generic error and never echo rejected values. Status observes
   daemon authority. Direct local operations fail closed when daemon authority or runtime artifacts
   are present, and every engine write boundary rechecks that ownership state. Existing runtime
   evidence is inspected read-only before profile compilation so a rejected command cannot create
   identity, layout, or SQLite state. Root-level held or malformed lease evidence remains observable
   even when `brain.toml` is absent and must fail closed before identity reconstruction.

## Tests first

Add focused failing tests that prove:

- first capture succeeds without `init`, `OPEN_BRAIN_ROOT`, a daemon, listener, subprocess, prompt,
  network call, or Secure Node import;
- a later process finds the exact captured text and a verified export contains its Portable source
  record but excludes `.open-brain`, SQLite, lock, cache, and credential state;
- status reports the six fields required by the exact acceptance script and changes export state only
  after verified export evidence exists;
- each doctor check passes on safe state and fails closed for its relevant poisoned fixture;
- output and failures do not disclose the selected root, user text, traceback, or internal exception;
- malformed command lines stay redacted in plain and JSON modes, regardless of JSON flag position;
- a separately held daemon-authority lease is reported by status and blocks direct capture, while a
  runtime artifact or malformed lease blocks `init`, capture, search, and export without changing a
  partial root;
- a live daemon-authority lease still blocks bootstrap without mutation after `brain.toml` is lost;
- both option positions supported by the accepted script parse correctly; and
- the base native artifact contains the direct local journey while the Secure Node denylist remains
  absent.

## Verification

Focused gate:

```sh
make ob1w1-preflight
make ob1w1-native
```

Full gate: `make verify` from the repository root.

## Mechanical exit

`OB1-W1` closes only when:

1. The exact capture, search, export, status, and doctor commands in
   [`../acceptance/five-minute-install.md`](../acceptance/five-minute-install.md) pass from a built
   local base artifact in an isolated home.
2. Focused and full gates pass at one clean commit.
3. A fresh read-only reviewer returns `READY` with P0/P1/P2 all zero for that exact commit.
4. The roadmap and validated workstream handoff record the same commit and evidence.

`OB1-W2` remains gated by both this closure and the paused `CORE-W0` portability closure. No artifact
is published from this workstream.

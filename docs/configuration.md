# Configuration

## Target default

Open Brain's default `local` profile has no required configuration. The first stateful command
selects the platform-local data directory, creates one owner and one Brain, opens its SQLite state,
and keeps provider mode `none` with egress off. The five-minute journey does not set
`OPEN_BRAIN_ROOT`, edit TOML, choose storage locations, install a service, or configure a database.

An optional absolute `--data-dir` names the Open Brain Brain root directly for expert and test use.
`OPEN_BRAIN_ROOT` remains a Secure Node and legacy-test input. Secure Node owns advanced custody,
grant, listener, service, and multi-client configuration after an explicit
`open-brain[secure-node]` installation. Plain Open Brain does not load those settings and makes no
application-level encryption claim.

## Current transitional configuration

The remainder documents the retained appliance and compatibility composition. It predates the
product split and is Secure Node precursor evidence, not the target default setup.

Configuration precedence for retained composition is:

```text
explicit function or CLI option > environment > configuration file > safe default
```

The Phase 2 default is a `single-user-local` profile with one absolute `OPEN_BRAIN_ROOT`.
It carries the non-secret tenant, owner actor, owner role, and role-claim identity in
`brain.toml`; the engine opens that one root with provider mode `none`. The retained composition
below remains directly testable through the legacy facade for scheduled compatibility. It is not
imported by the installed CLI and is not a second default profile.

The retained roots are explicit and distinct:

- `work_root`
- `personal_root`
- `capture_root`
- `saved_content_root`
- `state_root`

`backup_root` is a sixth explicit destination. It must be absolute and must not overlap
any retained root. Backup data, immutable manifests, and replay reservations are written
there; private configuration supplies the real host path.

Phase 3 appliance recovery uses that separate destination for immutable backup publication only.
Portable export remains a distinct owner-requested data contract and does not reuse backup IDs,
backup manifests, or restore semantics. Backup includes exact Portable bytes, required
`.open-brain/state/phase1.sqlite3` snapshots produced through the SQLite backup API, and bounded
immutable appliance evidence and completed scheduler run receipts. It excludes mutable scheduler
state, credentials, indexes, sockets, locks, supervisor state, temporary or staging data, and live
SQLite sidecars such as `-wal`, `-shm`, or `-journal`. Restore and Portable import both target a
fresh empty disposable root first; only the restored appliance generates a new local owner
credential, initializes fresh scheduler state, and rebuilds indexes before a later live replacement
decision.
Secure Node upgrade and uninstall orchestration stays app-owned and source-checkout scoped. The
`ArtifactLifecyclePort` accepts bounded candidate identity plus compatibility, activation, rollback,
and removal receipts. Its default effect path is fail-closed, and the source tree uses injected fake
or disposable adapters. The old frozen native adapter and release harness were removed. Plain Open
Brain delegates executable upgrade and uninstall to Homebrew and keeps its data outside the prefix.
Secure Node upgrade and uninstall CLI commands still fail closed unless composition injects a
lifecycle port. Accepted owner requests use one root-scoped
lifecycle lease and bounded journals below `.open-brain/state/appliance-lifecycle/`; retries replay
terminal receipts, conflicting identities fail, and interrupted forward work rolls back before the
journal becomes terminal.

`host.identity` is required on a writer host. Scheduled manifests pass the single
`OPEN_BRAIN_CONFIG` reference to the process entry point, and backup writers run only when
that identity matches the durable canonical-writer record in `state_root`.

Content Markdown stays in place. Configuration points to these roots; migration does not
relocate them. Roots must be absolute, non-overlapping, and physically distinct when
validated identity metadata is available.

Credentials, provider configuration, host labels, connector configuration, and rendered service paths are runtime
inputs. Public TOML contains typed secret references only. Secret values belong in a
separate private environment file with owner-only permissions. Public examples use
placeholders and synthetic paths only.

`JOB-010` requires an owner-only canonical `OPEN_BRAIN_PROVIDER_CONFIG`. It names the
loopback local endpoint and model, the allow-listed cloud module and model, and one named
credential reference. Local selection never resolves the cloud credential. Cloud selection
requires both cloud and egress enablement, the optional `cloud` package extra, and a
privacy decision that grants cloud authority before the credential or SDK is loaded.

The scheduled repository-sync composition requires one named `git_inventory` file reference
in `[secrets]`. Despite the shared reference mechanism, this file contains private topology,
not credentials. It must be owner-only canonical JSON and is never included in configuration
output. The inventory names the private home and development roots, allocates explicit relative
repositories, and stores only a SHA-256 binding for each permitted push target. Personal
repositories cannot declare a push target.

Config migration is dry-run by default. Apply requires explicit prerequisite, backup,
overwrite, publication, and recovery capabilities. The public/private output pair uses
compare-and-swap publication with verified rollback; no live filesystem adapter is enabled
by default.

The default application has no connector capability and egress is disabled. Explicit retained
`JOB-029` composition
requires an absolute `OPEN_BRAIN_YOUTUBE_CONFIG` reference plus egress authority. The connector
receives a bounded capture-only identity and cannot route to spaces, approve content, or perform
actions. Host evidence, not connector counters, authorizes checkpoint advancement after the exact
sink-issued receipt, delivery ID, and source reference are bound.

`OPEN_BRAIN_MCP_ALLOWED_SPACE_IDS` is a JSON array of opaque space IDs. MCP gets only scoped
retrieval and metadata feedback; search and fetch both enforce that allow-list. Set it to `[]` for
the empty scope. The appliance daemon owns the only public HTTP listener. Its non-secret bind
configuration is:

- `OPEN_BRAIN_UI_BIND`
- `OPEN_BRAIN_UI_PORT`
- `OPEN_BRAIN_UI_ALLOW_PRIVATE`

Loopback uses the exact browser origin `http://<bind-host>:<bind-port>`. For remote access, create an
authenticated SSH tunnel to that loopback listener:

```console
ssh -N -L 8788:127.0.0.1:8788 user@appliance-host
```

Then open `http://127.0.0.1:8788` locally. Do not put the appliance credential in the URL.
Private-network binding is refused unless all of these are set exactly:

- `OPEN_BRAIN_UI_ALLOW_PRIVATE=true`
- `OPEN_BRAIN_UI_EXTERNAL_TLS_TERMINATION=true`
- `OPEN_BRAIN_UI_EXTERNAL_ORIGIN=https://...`

The external origin must be a syntactically exact HTTPS origin with no path, query, fragment, or
userinfo. Without that explicit external browser origin, private-network binding still rejects
public or wildcard addresses.

Ledger taxonomy is configuration, not model output. Each route binds a trusted path prefix to a synthetic-safe topic ID, label, and privacy tier. Unknown paths have no topic and retain fail-closed privacy authority.

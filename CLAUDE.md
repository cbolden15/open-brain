# Open Brain

**GitHub:** `cbolden15/open-brain`
**Stack:** Python 3.14, uv workspace packages, SQLite, Markdown, PyInstaller, Homebrew; TypeScript for the Obsidian plugin

## Quick reference

| Action | Command or authority |
|---|---|
| Install target | `brew install cbolden15/tap/open-brain`; Homebrew is a prerequisite |
| Local CLI | `uv run open-brain status --json` |
| Full verification | `make verify` |
| Native distribution audit | `make native-audit` |
| Native integration smoke | `make native-integration-smoke` |
| Complete contributor check | `make contributor-check` |
| Product authority | `docs/product-family.md` |
| Acceptance | `docs/acceptance/five-minute-install.md` |

Use `docs/install.md` for the core release and tap instructions.

## Product boundary

The Open Brain core is one unprivileged, foreground-only local runtime. It gives one operating-system user
one automatically selected Brain, direct SQLite-backed capture and search, Markdown import, a
managed sibling vault, a desktop Obsidian plugin, graph projections, and a full Portable Brain
export. Open Brain never requires root, operating-system capabilities, namespaces, launchd, systemd,
containers, a daemon, or another background service. The enabled plugin owns one foreground
`open-brain plugin` child over inherited stdio and stops it on unload.

The Tauri desktop companion was archived to `archive/open-brain-desktop` on 2026-10-23; it was
never released. It is excluded from the workspace, Makefile and CI. Priority Gmail, Drive and
selected Claude Code/Codex capture share `packages/collector/src/open_brain_collector/live_manager.py`,
and the collector CLI exposes the source operations headlessly.
The existing Slack implementation is deferred from the active priority milestone.
The optional collector owns scheduling, credential references, private Unix control IPC, and service
permissions. Source enablement and background service installation are separate opt-ins. The
priority source plan and live acceptance remain in `docs/plans/2026-09-16-priority-capture.md`.
Installing the core
does not install a desktop app, collector, scheduler, network listener, or service manager
dependency in the core. Do not add those dependencies or a listener to the core, and do not restore
archived modules.

Secure Node is not an extra, profile, entry point, or dependency of the Open Brain distribution.
The first Secure Node implementation is quarantined under `archive/open-brain-secure-node` as
non-building history. Any future Secure Node must be a separate distribution or repository with a
separate import namespace and release boundary.

Portable Brain v1 and shared record identities are the interoperability seam. They remain in the
engine. Never migrate between products by copying or reinterpreting live SQLite files.

## Architecture

| Module | Responsibility |
|---|---|
| `packages/engine/src/open_brain_engine` | Shared records, local tasks, SQLite storage, retrieval, and Portable Brain schemas |
| `packages/app/src/open_brain` | Direct local bootstrap, CLI, MCP over stdio, and local operations |
| `packages/app/src/open_brain/services/local_entrypoints.py` | Installed default `open-brain` callable |
| `packages/app/src/open_brain/services/harness_native_entrypoint.py` | Optional fast foreground harness callable; same audited core dependency closure |
| `packages/app/src/open_brain/services/plugin_bridge.py` | Bounded `open-brain-client` protocol version 1 server and plugin-session lifecycle |
| `packages/app/src/open_brain/services/managed_providers.py` | Consent-gated, bounded OpenAI, Anthropic, and Gemini direct adapters |
| `packages/obsidian-plugin` | Desktop-only Obsidian source, bounded stdio client, and compiled plugin checks |
| `packages/connectors` | Optional connector distribution; not a default dependency |
| `packages/collector` | Optional collector distribution; not a default or native base dependency |
| `archive/open-brain-secure-node` | Historical Secure Node implementation; excluded from builds and tests |
| `archive/legacy` | Historical predecessor; excluded from the workspace, builds, imports, and tests |
| `archive/open-brain-desktop` | Archived Tauri desktop companion; excluded from builds, tests and CI |
| `tools/open_brain_dev/base_native.py` | Paired native build, module audit, component manifest, and formula renderer |
| `release/open-brain/open-brain.spec` | One-folder core and optional onedir foreground harness specification |
| `release/open-brain-graphify` | Pinned, patched, separately frozen Graphify structural helper |

The engine cannot import the app, connectors, archives, or workspace modules. The app depends
exactly on `open-brain-engine==0.1.0`. The default dependency graph contains no server, service
manager, cryptography, key-custody, or container dependency. The native audit rejects those module
families if they enter the executable. Native builds require the standard-library
compiled Decimal implementation and omit its unused Python fallback; the build
and installed self-check refuse an unavailable or substituted implementation.
Artifact audit limits and reviewed standard-library payload hashes remain fixed.

## Release surface

The supported end-user lifecycle is Homebrew. Each platform produces a base archive containing the
app and plugin assets plus a separate Graphify helper archive. The component manifest binds one
version to both platforms, matching resource roles, archive and executable SHA-256 values, filenames,
and install destinations. The executable starts only when the user invokes it and exits when that
foreground command, stdio MCP session, or desktop plugin session ends.

New builds also produce an optional harness resource with the same audited Python
closure and stable adapter assets. Existing two-resource manifests remain supported;
manifests including the harness must include it consistently across platforms.
The helper runs bounded foreground operations without a listener or recurring service.
See `docs/harness-setup.md` for preview-bound optional integration setup and removal.

The base archive is a one-folder build. A separate embedding archive carries the pinned model and a
native helper with ONNX Runtime statically linked; a release that includes it uses component
manifest v2, whose archives hold final installed prefix-relative paths and their own inventories.
Offline semantic search stays off until `open-brain embedding enable`, runs only the installed
helper, and is not network egress. See `docs/semantic-search.md`.

Semantic refresh is the core's only active product network-egress path. It requires current owner consent,
eligible note revisions, exclusions, redaction, and one explicit direct API-key provider. Never use
ambient provider credentials or cross-provider fallback. Claude subscription remains closed with
`subscription_isolation_unproven`; do not introduce root staging, namespaces, capabilities, fixture
owners, or broader host access to enable it.

Generated graph state and `Open Brain Graph.canvas` are rebuildable views, not sources or Portable
Brain content. Inferred suggestions cannot edit notes until explicit acceptance revalidates the
source revisions and enters the normal conflict-preserving revision flow.

## Common commands

```sh
uv sync --frozen --group dev --group native-build
make lint
make typecheck
make test
make test-fast
make build
make plugin-test
make verify
make native-audit
make homebrew-smoke
make native-integration-smoke
make contributor-check
```

`make contributor-check` runs `make verify` and `make native-integration-smoke`.
It covers the plugin test suite and Homebrew integration. GUI timing
and real-provider evidence remain separate exact-candidate checks. Private release audits remain
separate owner checks.

## Data and configuration

The local runtime needs no configuration. macOS data lives under
`$HOME/Library/Application Support/open-brain/brain`; Linux data lives under
`${XDG_DATA_HOME:-$HOME/.local/share}/open-brain/brain`. An absolute `--data-dir` is available for
expert and test use. `OPEN_BRAIN_ROOT` is a historical compatibility setting and is not consumed by
the active package.

Agent setup uses `agent setup` in the CLI and named plugin operations. The CLI supports separate
capture, search, content-read, history-read, inbox-read, organize, review-read, review-propose, review-decide,
followup-read, followup-propose, and followup-decide grants,
all off by default; plugin setup currently
exposes capture/search. `space` and `inbox` commands share `services/space_inbox.py` with explicitly
injected MCP callbacks. Routing changes assignment/search metadata, not publication or source trust.
Preview IDs bind configuration preimages and grants; only owned fragments may
be changed. Review CLI/MCP operations share `services/review_publication.py`; the engine owns
source-bound proposals and stable page revisions. See `docs/review-publication.md` and
`docs/records-and-history.md` and `docs/portable-brain-v7.md`. Use synthetic client profiles for tests.
Sharing was introduced at schema/runtime/Portable12/7/7. The current follow-up store candidate requires 23/16/18; see `docs/portable-brain-v18.md`. Follow-up items (commitments and open questions) are documented in `docs/followups.md`. Follow-up CLI/MCP operations share `services/followups.py`; the engine owns items, immutable evidence and the append-only decision log, and `followup_visibility.py` is the only place scope rules live. Follow-up MCP tools are Work-only by default in every MCP session, including unscoped `open-brain mcp`, unless the launcher policy explicitly allows another tier; external-provider sessions get no follow-up tools. The owner egress policy predecessor used 22/16/17. The local embedding policy predecessor used 21/16/16; see `docs/portable-brain-v16.md`. The engine consumer-intake predecessor used20/15/15; see `docs/consumer-intake.md` and `docs/portable-brain-v15.md`. Both external parser profiles remain inactive. Owner retraction approval bytes retain their19/14/14 predecessor meaning; see `docs/owner-retraction.md` and `docs/portable-brain-v14.md`.
The capture lifecycle predecessor requires18/13/13; see `docs/portable-brain-v13.md`.
The Slack approved-evidence predecessor used17/12/12; see `docs/portable-brain-v12.md`.
The Slack capture predecessor used16/11/11; see `docs/portable-brain-v11.md`.
The frozen facet predecessor used15/10/10; see `docs/portable-brain-v10.md`.
The historical-compatibility predecessor used14/9/9; its required mixed historical sidecar,
forward recovery and retained import evidence are documented in `docs/portable-brain-v9.md`.
Frozen13/8/8 semantics remain documented in `docs/portable-brain-v8.md`.
merged source-continuity B used 11/6/6. Never weaken compatibility checks to admit an older client. Owner-only legacy write
recovery is documented in `docs/managed-workspace-recovery.md`. Its read-only preview precedes
ordinary bootstrap; abandonment requires exclusive admission and never exposes general engine tasks.

## Safety and verification

Automated tests use synthetic fixtures. Real-source checks require owner-authorized selections
and private receipts; they do not replace the automated suite. Never commit private notes, captures, transcripts, credentials,
hostnames, infrastructure addresses, logs, databases, or generated private configuration.

After code changes, run `make verify`. After native or packaging changes, also run
`make native-audit` and `make homebrew-smoke` on the current platform. Run `git diff --check` and
`actionlint .github/workflows/ci.yml` before handoff.

Release auditing requires `PRIVATE_DENYLIST` to point at an absolute, uncommitted file with one
private term per line. Publishing, pushing, repository settings, and release creation remain
separate owner-authorized actions.

Project gotchas live in `docs/engineering/gotchas/README.md`.

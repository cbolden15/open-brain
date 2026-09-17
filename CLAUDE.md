# Open Brain

**GitHub:** `cbolden15/open-brain`
**Stack:** Python 3.14, uv workspace packages, SQLite, Markdown, PyInstaller, Homebrew; Tauri/Rust and React/TypeScript for the separate desktop

## Quick reference

| Action | Command or authority |
|---|---|
| Install target | `brew install cbolden15/tap/open-brain`; Homebrew is a prerequisite |
| Local CLI | `uv run open-brain status --json` |
| Full verification | `make verify` |
| Native distribution audit | `make native-audit` |
| Native integration smoke | `make native-integration-smoke` |
| Complete contributor check | `make contributor-check` |
| Desktop native proof (macOS) | `make desktop-native-proof`, then a separate native UI check |
| Product authority | `docs/product-family.md` |
| Acceptance | `docs/acceptance/five-minute-install.md` |

Use `docs/install.md` for the core release and tap instructions. The desktop contributor build is not a public desktop release.

## Product boundary

The Open Brain core is one unprivileged, foreground-only local runtime. It gives one operating-system user
one automatically selected Brain, direct SQLite-backed capture and search, Markdown import, a
managed sibling vault, a desktop Obsidian plugin, graph projections, and a full Portable Brain
export. Open Brain never requires root, operating-system capabilities, namespaces, launchd, systemd,
containers, a daemon, or another background service. The enabled plugin owns one foreground
`open-brain plugin` child over inherited stdio and stops it on unload.

The separately packaged Tauri companion in `packages/desktop` follows
`docs/architecture/decisions/0017-desktop-companion-boundary.md`. D1 provides local capture/search
and shared CLI/desktop agent setup against the same Brain. Priority Gmail, Drive and selected
Claude Code/Codex capture share `packages/collector/src/open_brain_collector/live_manager.py`.
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
| `packages/app/src/open_brain/services/plugin_bridge.py` | Bounded `open-brain-client` protocol version 1 server and plugin-session lifecycle |
| `packages/app/src/open_brain/services/managed_providers.py` | Consent-gated, bounded OpenAI, Anthropic, and Gemini direct adapters |
| `packages/obsidian-plugin` | Desktop-only Obsidian source, bounded stdio client, and compiled plugin checks |
| `packages/desktop` | Optional Tauri/React UI, native stdio bridge, local capture/search, and agent setup |
| `packages/connectors` | Optional connector distribution; not a default dependency |
| `packages/collector` | Optional collector distribution; not a default or native base dependency |
| `archive/open-brain-secure-node` | Historical Secure Node implementation; excluded from builds and tests |
| `archive/legacy` | Historical predecessor; excluded from the workspace, builds, imports, and tests |
| `tools/open_brain_dev/base_native.py` | Paired native build, module audit, component manifest, and formula renderer |
| `release/open-brain/open-brain.spec` | One-file PyInstaller specification |
| `release/open-brain-graphify` | Pinned, patched, separately frozen Graphify structural helper |

The engine cannot import the app, connectors, archives, or workspace modules. The app depends
exactly on `open-brain-engine==0.1.0`. The default dependency graph contains no server, service
manager, cryptography, key-custody, or container dependency. The native audit rejects those module
families if they enter the executable.

## Release surface

The supported end-user lifecycle is Homebrew. Each platform produces a base archive containing the
app and plugin assets plus a separate Graphify helper archive. The component manifest binds one
version to both platforms, both resource roles, archive and executable SHA-256 values, filenames,
and install destinations. The executable starts only when the user invokes it and exits when that
foreground command, stdio MCP session, or desktop plugin session ends.

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
make build
make plugin-test
make verify
make native-audit
make homebrew-smoke
make native-integration-smoke
make contributor-check
```

`make contributor-check` runs `make verify`, `make native-integration-smoke`, and `make desktop-native`.
It covers the plugin and desktop test suites, native desktop compilation, and Homebrew integration. GUI timing
and real-provider evidence remain separate exact-candidate checks. Private release audits remain
separate owner checks.

## Data and configuration

The local runtime needs no configuration. macOS data lives under
`$HOME/Library/Application Support/open-brain/brain`; Linux data lives under
`${XDG_DATA_HOME:-$HOME/.local/share}/open-brain/brain`. An absolute `--data-dir` is available for
expert and test use. `OPEN_BRAIN_ROOT` is a historical compatibility setting and is not consumed by
the active package.

Agent setup uses `agent setup` in the CLI and named plugin operations. The CLI supports separate
capture, search, inbox-read, organize, review-read, review-propose, and review-decide grants,
all off by default; desktop/plugin setup currently
exposes capture/search. `space` and `inbox` commands share `services/space_inbox.py` with explicitly
injected MCP callbacks. Routing changes assignment/search metadata, not publication or source trust.
Preview IDs bind configuration preimages and grants; only owned fragments may
be changed. Review CLI/MCP operations share `services/review_publication.py`; the engine owns
source-bound proposals and stable page revisions. See `docs/review-publication.md` and
`docs/portable-brain-v3.md`. Use synthetic client profiles for tests. Existing Brain state requires schema 6 and
runtime session version 1; never weaken the compatibility checks to admit an older client. Owner-only legacy write
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

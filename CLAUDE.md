# Open Brain

**GitHub:** `cbolden15/open-brain`

## Quick reference

| Action | Command or authority |
|---|---|
| Local CLI | `uv run open-brain status --json` |
| `packages/app/src/open_brain/services/local_entrypoints.py` | Installed default `open-brain` callable |
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

Release build chain: `tools/open_brain_dev/base_native.py` (paired native build, module audit, component manifest, formula renderer), `release/open-brain/open-brain.spec`, and `release/open-brain-graphify` (pinned, patched, separately frozen).

The engine cannot import the app, connectors, archives, or workspace modules. The app depends
exactly on `open-brain-engine==0.1.0`. The default dependency graph contains no server, service
manager, cryptography, key-custody, or container dependency. The native audit rejects those module
families if they enter the executable.

## Release surface

Semantic refresh is the core's only active product network-egress path. It requires current owner consent,
eligible note revisions, exclusions, redaction, and one explicit direct API-key provider. Never use
ambient provider credentials or cross-provider fallback. Claude subscription remains closed with
`subscription_isolation_unproven`; do not introduce root staging, namespaces, capabilities, fixture
owners, or broader host access to enable it.

Generated graph state and `Open Brain Graph.canvas` are rebuildable views, not sources or Portable
Brain content. Inferred suggestions cannot edit notes until explicit acceptance revalidates the
source revisions and enters the normal conflict-preserving revision flow.

## Common commands

Setup: `uv sync --frozen --group dev --group native-build`. Every other target is in the Makefile.

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

Agent setup, space/inbox, review-publication, and legacy write-recovery conventions live in `packages/app/CLAUDE.md`. Never weaken the schema 7 / runtime session version 2 compatibility checks to admit an older client.

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

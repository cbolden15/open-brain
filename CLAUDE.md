# Open Brain

**GitHub:** `cbolden15/open-brain`
**Stack:** Python 3.14, uv workspace packages, SQLite, Markdown, PyInstaller, Homebrew

## Quick reference

| Action | Command or authority |
|---|---|
| Install target | `brew install cbolden15/tap/open-brain`; Homebrew is a prerequisite |
| Local CLI | `uv run open-brain status --json` |
| Full verification | `make verify` |
| Native distribution audit | `make native-audit` |
| Complete contributor check | `make contributor-check` |
| Product authority | `docs/product-family.md` |
| Acceptance | `docs/acceptance/five-minute-install.md` |

No release or tap is published yet.

## Product boundary

Open Brain is one unprivileged, foreground-only local runtime. It gives one operating-system user
one automatically selected Brain, direct SQLite-backed capture and search, Markdown import, and a
full Portable Brain export. Open Brain never requires root, operating-system capabilities,
namespaces, launchd, systemd, containers, a daemon, or another background service.

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
| `packages/connectors` | Optional connector distribution; not a default dependency |
| `archive/open-brain-secure-node` | Historical Secure Node implementation; excluded from builds and tests |
| `archive/legacy` | Historical predecessor; excluded from the workspace, builds, imports, and tests |
| `tools/open_brain_dev/base_native.py` | Native build, module audit, manifest, and formula renderer |
| `release/open-brain/open-brain.spec` | One-file PyInstaller specification |

The engine cannot import the app, connectors, archives, or workspace modules. The app depends
exactly on `open-brain-engine==0.1.0`. The default dependency graph contains no server, service
manager, cryptography, key-custody, or container dependency. The native audit rejects those module
families if they enter the executable.

## Release surface

The supported end-user lifecycle is Homebrew. Each platform produces
`open-brain-<version>-<platform>.tar.gz`, containing one executable, plus a manifest with exact
version, platform, filename, and SHA-256. The executable starts only when the user invokes it and
exits when that foreground command or stdio MCP session ends.

## Common commands

```sh
uv sync --frozen --group dev --group native-build
make lint
make typecheck
make test
make build
make verify
make native-audit
make homebrew-smoke
make contributor-check
```

`make contributor-check` runs `make verify` followed by `make homebrew-smoke`. Private release
audits remain separate owner checks.

## Data and configuration

The local runtime needs no configuration. macOS data lives under
`$HOME/Library/Application Support/open-brain/brain`; Linux data lives under
`${XDG_DATA_HOME:-$HOME/.local/share}/open-brain/brain`. An absolute `--data-dir` is available for
expert and test use. `OPEN_BRAIN_ROOT` is a historical compatibility setting and is not consumed by
the active package.

## Safety and verification

Use synthetic fixtures only. Never commit private notes, captures, transcripts, credentials,
hostnames, infrastructure addresses, logs, databases, or generated private configuration.

After code changes, run `make verify`. After native or packaging changes, also run
`make native-audit` and `make homebrew-smoke` on the current platform. Run `git diff --check` and
`actionlint .github/workflows/ci.yml` before handoff.

Release auditing requires `PRIVATE_DENYLIST` to point at an absolute, uncommitted file with one
private term per line. Publishing, pushing, repository settings, and release creation remain
separate owner-authorized actions.

Project gotchas live in `docs/engineering/gotchas/README.md`.

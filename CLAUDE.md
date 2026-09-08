# Open Brain

**GitHub:** `cbolden15/open-brain`
**Stack:** Python 3.14, uv workspace packages, SQLite, Markdown, PyInstaller, Homebrew

## Quick reference

| Action | Command or authority |
|---|---|
| Install target | `brew install cbolden15/tap/open-brain`; Homebrew is a prerequisite |
| Local CLI | `uv run open-brain status --json` |
| Full verification | `make verify` |
| Native build | `make native` |
| Homebrew product smoke | `make homebrew-smoke` |
| Product authority | `docs/product-family.md` |
| Roadmap | `docs/plans/product-roadmap.md` |
| Acceptance | `docs/acceptance/five-minute-install.md` |

No release or tap is published yet.

## Product split

Plain `open-brain` is the default `local` product: one user, one automatically selected Brain,
direct SQLite-backed capture and search, full Portable Brain export, and no required daemon, Docker,
certificate, grant, storage choice, or manual database setup. It does not claim application-level
encryption.

`open-brain[secure-node]` is the opt-in advanced profile. Brain Protocol v1 and work historically
named M1 belong to Secure Node. Preserve stable M1 evidence IDs, but use `SN1-W*` in current plans.
Do not move Secure Node dependencies, setup, runtime effects, or security claims into plain Open
Brain.

Portable Brain v1 and shared record identities are the upgrade seam. Never upgrade by copying or
reinterpreting the default product's live SQLite files. The protected envelope is record-level:
Open Brain stores the shared body as plaintext; Secure Node later encrypts that same body and keeps
attachments digest-addressed.

## Architecture

| Module | Responsibility |
|---|---|
| `packages/engine/src/open_brain_engine` | Product-neutral records, tasks, SQLite storage, retrieval, Portable schemas, and conformance data |
| `packages/app/src/open_brain` | Direct local CLI plus explicit Secure Node composition |
| `packages/app/src/open_brain/services/local_entrypoints.py` | Installed default `open-brain` callable |
| `packages/app/src/open_brain/services/appliance_entrypoints.py` | Retained Secure Node CLI and MCP implementation |
| `packages/connectors` | Optional connector distribution; not a default dependency |
| `packages/legacy` | Quarantined predecessor compatibility; not a shipping dependency |
| `tools/open_brain_dev/base_native.py` | Native build, product-scope audit, digest manifest, and Homebrew formula renderer |
| `release/open-brain/open-brain.spec` | One-file default-product PyInstaller specification |

The engine cannot import app, connector, legacy, or workspace modules. The base app depends exactly
on `open-brain-engine==0.1.0`; the `secure-node` extra selects advanced dependencies. The native module
audit rejects Secure Node, server, connector, legacy, and advanced cryptography modules.

## Release surface

The supported end-user lifecycle is Homebrew. There is no curl installer, custom preflight,
transactional activation, installation receipt, custom uninstall, clean-host harness, VM matrix,
notarization pipeline, or build attestation.

Each platform produces `open-brain-<version>-<platform>.tar.gz`, containing one executable, plus a
manifest with exact version, platform, filename, and SHA-256. The macOS executable must be arm64 and
pass code-signature verification before hashing. Homebrew formula values are rendered from the final
manifest and point at immutable GitHub Release URLs.

## Common commands

```sh
uv sync --frozen --group dev --group native-build
make lint
make typecheck
make test
make build
make verify
make native
make smoke
make homebrew-smoke
```

`make homebrew-smoke` refuses to replace an existing Homebrew installation of `open-brain`, installs
the local archive, runs capture/search/export/status/doctor in a temporary home, and uninstalls only
the formula it installed.

## Data and configuration

The default local profile needs no configuration. macOS data lives under
`$HOME/Library/Application Support/open-brain/brain`; Linux data lives under
`${XDG_DATA_HOME:-$HOME/.local/share}/open-brain/brain`. An absolute `--data-dir` is available for
expert and test use.

`OPEN_BRAIN_ROOT` and the retained provider, service, UI, and MCP environment variables belong to
Secure Node or compatibility code. The default CLI does not consume them.

## Safety and verification

Use synthetic fixtures only. Never commit private notes, captures, transcripts, credentials,
hostnames, infrastructure addresses, logs, databases, or generated private configuration.

After code changes, run `make verify`. After native or packaging changes, also run `make native` and
`make homebrew-smoke` on the current platform. CI provides the other architecture. Run
`git diff --check` and `actionlint .github/workflows/ci.yml` before handoff.

Release auditing requires `PRIVATE_DENYLIST` to point at an absolute, uncommitted file with one private
term per line. Public publishing, pushing, repository settings, and release creation remain separate
owner-authorized actions.

Project gotchas live in `docs/engineering/gotchas/README.md`.

# Five-minute Open Brain acceptance test

Status: accepted target contract; W7 contributor checks pass locally on macOS arm64; exact-head
two-platform CI is pending. The public release and tap are not published yet.

Date: 2026-09-08

## Supported starting point

The test starts with a regular user account on one of these hosts:

- macOS on Apple Silicon (`arm64`)
- Linux on `x86_64`

Homebrew must already be installed and available as `brew`. The account has no prior Open Brain
formula or Open Brain data. Network access to Homebrew and GitHub Releases is available.

This is a clean Open Brain state, not a factory-clean operating system. Installing Homebrew is
outside the product journey.

## Exact journey

Run this block in a fresh shell:

```sh
set -eu

brew --version
! brew list --formula open-brain >/dev/null 2>&1

TOKEN="five-minute-$(date +%s)-$$"
EXPORT_PARENT="$(mktemp -d "${TMPDIR:-/tmp}/open-brain-export.XXXXXX")"

brew install cbolden15/tap/open-brain
open-brain capture "$TOKEN"
open-brain search "$TOKEN" | grep -F "$TOKEN"
open-brain export "$EXPORT_PARENT/brain" --verify
open-brain status --json
open-brain doctor --check private-data-directory
open-brain doctor --check foreground-runtime
open-brain doctor --check base-dependency-closure
```

The five-minute product target runs from the start of `brew install` through the successful verified
export. CI records ordinary job duration, but the repository has no custom 300-second watchdog or
timing-evidence format.

## Pass conditions

The journey passes when all of these are true:

1. Homebrew installs one `open-brain` executable and verifies the release archive against the
   formula's SHA-256.
2. The first capture creates one private platform-local Brain and SQLite state without a setup
   prompt, configuration file, storage choice, or database command.
3. Search returns the exact token, and the verified Portable Brain export contains that capture.
4. Status reports `profile=local`, `storage=sqlite`, `daemon_running=false`, and
   `application_encryption=false`.
5. Every command exits zero and no daemon, listener, service, container, or runtime process remains.

The automatic Brain root is:

| Host | Brain root |
|---|---|
| macOS | `$HOME/Library/Application Support/open-brain/brain` |
| Linux | `${XDG_DATA_HOME:-$HOME/.local/share}/open-brain/brain` |

## CI smoke

CI has exactly two native jobs: `macos-latest` with an explicit `arm64` assertion and
`ubuntu-latest` with an explicit `x86_64` assertion. Each invokes `make contributor-check`, the same
command documented for contributors. It runs repository verification before building the native
executable, rendering a temporary keg-only `open-brain-smoke` formula from the release manifest,
installing it with Homebrew, and invoking its unlinked binary by absolute prefix path.

The smoke snapshots any existing product's prefix, version, link, and binary digest and requires them
to remain unchanged. It does not install a product-named stand-in when the real product is absent.
Production-shell tests with a command-recording fake Homebrew prove preservation of a modeled
installed product, INT/TERM cleanup, and next-run recovery after SIGKILL. The local live run verified
the absent-product case. Private release audits are separate owner checks, excluded from this target.

The five-minute acceptance boundary ends after the verified export in the timed data journey above.
The same installed binary then runs status and doctor, followed by the W4 smoke: import the committed
synthetic Markdown fixture, prove an unchanged rerun, search a nested marker, and verify exact
exported bytes plus unverified provenance. Capture-only MCP replay and search-only MCP exchange
then verify the W6 CLI/MCP contract. These post-export checks do not change the five-minute
acceptance boundary.

The CI formula uses local build output. A published-release smoke uses the public tap and immutable
GitHub Release URLs.

## Deliberate exclusions

This acceptance test does not cover curl installation, Docker, certificates, grants, service setup,
manual databases, storage-root selection, reinstall, fault injection, offline behavior, receipt-bound
uninstall, VM matrices, notarization, artifact attestations, or metadata-only evidence bundles.

`brew uninstall open-brain` removes the Homebrew-managed executable. It does not remove the Brain
data directory. Data removal is a separate, explicit user action.

Open Brain relies on the operating-system account and disk protections. It does not claim
application-level encryption, custody, compartment, purge, fencing, or recovery guarantees.

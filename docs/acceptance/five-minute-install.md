# Five-minute Open Brain acceptance test

- Status: Required target; not yet implemented
- Product: Open Brain default only
- Time limit: 300 seconds from installer start through verified export
- Secure Node: explicitly excluded

## Clean-host contract

Run the test with a fresh unprivileged user on every supported release host. The user has a normal
home directory, outbound HTTPS, POSIX `sh`, `curl`, and standard certificate roots. The host has no
Open Brain files, command, cache, Python environment, database service, container runtime, or
background Open Brain process. The installer may provide its own runtime.

The initial release matrix is macOS 14 or newer on Apple Silicon and Linux x86_64 on Ubuntu 24.04
LTS, Ubuntu 26.04 LTS, and Debian 13. Passing on one host does not support a broader platform claim.

## One-command installation

The public command is:

```sh
curl --proto '=https' --tlsv1.2 -LsSf https://github.com/vora-technology/open-brain/releases/latest/download/install.sh | sh
```

The release gate replaces `latest` with the exact candidate tag and runs without a preinstalled
Python or package manager. The installer must verify the downloaded release manifest and artifact
checksum before activation. It installs the base `open-brain` product, never the Secure Node extra.

## Exact test

The release harness sets `OPEN_BRAIN_ACCEPTANCE_VERSION` to the candidate version, then runs this
script in the clean user's login shell:

```sh
set -eu

started_at="$(date +%s)"
install_url="https://github.com/vora-technology/open-brain/releases/download/v${OPEN_BRAIN_ACCEPTANCE_VERSION}/install.sh"

test -z "${OPEN_BRAIN_ROOT:-}"
test ! -e "$HOME/.local/share/open-brain"
test ! -e "$HOME/Library/Application Support/open-brain"

curl --proto '=https' --tlsv1.2 -LsSf "$install_url" | sh

command -v open-brain >/dev/null

token="open-brain-five-minute-acceptance"
open-brain capture "$token"
open-brain search "$token" | grep -F "$token" >/dev/null

OPEN_BRAIN_ACCEPTANCE_TMP="$(mktemp -d)"
open-brain export "$OPEN_BRAIN_ACCEPTANCE_TMP/brain-export" --verify
test -f "$OPEN_BRAIN_ACCEPTANCE_TMP/brain-export/portable-manifest.json"

status="$(open-brain status --json)"
compact_status="$(printf '%s' "$status" | tr -d '[:space:]')"
printf '%s\n' "$compact_status" | grep -F '"profile":"local"' >/dev/null
printf '%s\n' "$compact_status" | grep -F '"brain_count":1' >/dev/null
printf '%s\n' "$compact_status" | grep -F '"storage":"sqlite"' >/dev/null
printf '%s\n' "$compact_status" | grep -F '"daemon_running":false' >/dev/null
printf '%s\n' "$compact_status" | grep -F '"application_encryption":false' >/dev/null
printf '%s\n' "$compact_status" | grep -F '"portable_export":"verified"' >/dev/null

open-brain doctor --check private-data-directory
open-brain doctor --check no-background-runtime
open-brain doctor --check base-dependency-closure

finished_at="$(date +%s)"
test "$((finished_at - started_at))" -le 300
```

The harness removes the temporary export and test account after collecting metadata-only timing and
version evidence. It never publishes captured text or absolute home paths.

## Pass conditions

The test passes only when all of these are true:

1. The one installer command succeeds without a prompt, sudo, source checkout, Python setup, Docker,
   certificate creation, database setup, root selection, config editing, or service installation.
2. The first capture creates the documented platform-local directory, one local owner, one Brain,
   and private SQLite state automatically.
3. The exact captured token is returned by local search and appears in a validated full Portable
   Brain export.
4. No daemon or listener remains running, and status explicitly reports that application-level
   encryption is not enabled for the default product.
5. The complete sequence finishes in 300 seconds or less with empty Open Brain and package caches.

The base artifact also fails acceptance if its installed dependency graph contains the Secure Node
extra or its help, status, or documentation claims encrypted custody, compartments, grants,
certified purge, fencing, or multi-client service guarantees.

## Separate Secure Node acceptance

`open-brain[secure-node]` has its own setup and conformance gates. Its installation time, key
custody, grants, service lifecycle, encrypted storage, and recovery checks do not count toward or
weaken this five-minute default test.

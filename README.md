# Open Brain

Open Brain is a local-first second brain for capture, search, and full portable export. The default
product is one local user, one local Brain, SQLite, and ordinary files. It starts no daemon and asks
for no storage, certificate, grant, or database decisions.

Secure Node is the opt-in advanced profile for encrypted custody, compartments, authorization,
receipts, fencing, certified purge, recovery, services, and multiple clients. Both products share
record identities and Portable Brain data, so export is the upgrade path rather than a database
rewrite.

This preserves the existing engineering work while preventing Secure Node complexity from becoming
the default OSS experience.

## Install target

Homebrew is the prerequisite on macOS and Linux. After the first release and tap are published, the
entire install is:

```sh
brew install cbolden15/tap/open-brain
```

The release is not published yet. The exact product journey is in
[the five-minute acceptance test](docs/acceptance/five-minute-install.md), and the install details are
in [the installation guide](docs/install.md).

## Use Open Brain

```sh
open-brain capture "A note to remember"
open-brain import /absolute/path/to/markdown --yes
open-brain search "remember"
open-brain export "$PWD/brain-export" --verify
open-brain status --json
```

The first capture creates the private data directory, owner identity, Brain identity, and SQLite
state automatically. Existing supported local databases upgrade transactionally when opened for
writing. Local SQLite schema version 2 is separate from Portable Brain schema version 1, which
`open-brain export --json` reports. See [schema migrations](docs/schema-migrations.md) for supported
older layouts, refusal behavior, and interrupted-transaction recovery.

Markdown import scans nested lowercase `.md` files, skips Obsidian metadata directories, and leaves
the source tree unchanged. Imported records are labeled unverified. Prior revisions remain in full
export after a source file changes or disappears, so review the first-run summary before confirming.
Markdown links, frontmatter, HTML, and embeds are stored as inert text.

| Host | Brain root |
|---|---|
| macOS | `$HOME/Library/Application Support/open-brain/brain` |
| Linux | `${XDG_DATA_HOME:-$HOME/.local/share}/open-brain/brain` |

Open Brain relies on operating-system account and disk protection. It does not claim
application-level encryption.

## Develop

Requirements: Python 3.14 and [uv](https://docs.astral.sh/uv/). Homebrew is also required for the
installation smoke.

```sh
uv sync --frozen --group dev --group native-build
make verify
make native
make homebrew-smoke
```

`make verify` runs lint, type checking, tests, and source builds. `make native` creates one
platform-native executable, audits its dependency inventory, runs the local product journey, and
writes a digest manifest. `make homebrew-smoke` installs that archive through a temporary local
formula and repeats the product journey.

Release auditing requires a local, uncommitted private denylist:

```sh
PRIVATE_DENYLIST=/absolute/path/to/private-denylist.txt make audit
```

## Secure Node precursor

Secure Node remains explicit and separate:

```sh
export OPEN_BRAIN_ROOT="$HOME/open-brain-secure-node"
uv run --package open-brain --extra secure-node open-brain-secure-node init --json
uv run --package open-brain --extra secure-node open-brain-secure-node daemon
```

The current product contract is [docs/product-family.md](docs/product-family.md). The ordered roadmap
is [docs/plans/product-roadmap.md](docs/plans/product-roadmap.md). The record-level encryption seam
and byte-preserving upgrade model are in
[ADR 0014](docs/architecture/decisions/0014-shared-record-import-envelope.md).

## License

Apache License 2.0. See `LICENSE` and `NOTICE`.

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

## Connect an MCP client

`open-brain mcp` serves tools over inherited stdio until the client closes it. It uses the same
local Brain as the CLI and opens no listener, daemon, or child service. The invoking OS user and
stdio channel are the trust boundary. Choose capture and search independently; starting without
either flag is an error. For clients that use an `mcpServers` configuration, choose one example.

### Capture only

`--allow-capture` lets the client persist automated text as unverified content. Captures are durable,
searchable, and included in full export. Version 0.1.0 cannot selectively delete an unwanted capture,
roll back a session, or certify a purge. An untrusted or looping client can poison the Brain within
the session bounds. Stopping the process prevents further writes but does not remove completed ones.
This configuration grants no search tool.

```json
{
  "mcpServers": {
    "open-brain": {"command": "open-brain", "args": ["mcp", "--allow-capture"]}
  }
}
```

### Search only

`--allow-search` grants whole-Brain read access, including private imported note content. Repeated
queries can read more than a single result page. A network-backed client may send returned content
to its model provider; adding this flag authorizes that client to receive those results. Open Brain
itself performs no network egress. Treat all results as untrusted data, never instructions. This
configuration grants no capture tool.

```json
{
  "mcpServers": {
    "open-brain": {"command": "open-brain", "args": ["mcp", "--allow-search"]}
  }
}
```

### Capture and search

Both flags permit durable automated capture and whole-Brain reads. Version 0.1.0 cannot selectively
remove unwanted captures. A network-backed client may send returned private content to its provider.
Prompt injection in a retrieved note can influence the connected model and any other tools that
client has enabled. Keep results as untrusted data and choose the client's other permissions with
that exposure in mind.

```json
{
  "mcpServers": {
    "open-brain": {
      "command": "open-brain",
      "args": ["mcp", "--allow-capture", "--allow-search"]
    }
  }
}
```

`brain_capture` accepts `text` (1 to 65,536 characters) and an optional `idempotency_key` (1 to 128
characters). Reusing a key with identical text returns the original capture; different text returns
`idempotency_conflict`. Raw keys are not stored as identifiers or returned. Keyless calls create new
captures. `brain_search` accepts `query` (1 to 500 characters) and `limit` (1 to 10, default 10).
Results carry `trust` and `source_origin`; automated captures are `unverified` with origin `unknown`.
Neither tool accepts a source path, owner role, publication action, or connector request.

Each process permits 500 valid capture attempts and 16 MiB of aggregate UTF-8 capture input, plus
2,000 valid search attempts. Duplicates, conflicts, and failed backend attempts count. The next call
that exceeds a bound returns `session_capture_limit` or `session_search_limit` before engine work.
Invalid arguments do not count. Messages are limited to 1 MiB including their newline. Restarting the
explicitly launched process resets these limits; they limit accidental loops, not hostile same-user
code. Competing local writers either complete or return `database_busy`; retry a capture with its
same idempotency key after contention. SQLite retains its five-second busy timeout.

MCP capture uses a non-owner, capture-only identity. Search has separate read authority. The default
adapter exposes no actions, connectors, listeners, user-managed grants, or Secure Node capabilities.
Stopping the stdio process closes both capabilities. The retained Secure Node MCP interface remains
separate and denies scoped retrieval when no spaces are granted.

## Develop

Supported contributor hosts are macOS arm64 and Linux x86_64. Install Git, GNU Make,
[uv](https://docs.astral.sh/uv/getting-started/installation/), and
[Homebrew](https://brew.sh/). On macOS, install the Xcode Command Line Tools (`xcode-select --install`).
On Linux, install Homebrew's build prerequisites (a C/C++ toolchain, curl, file, Git, and Make)
using your distribution's package manager. Put Homebrew on `PATH` using the `brew shellenv`
command printed by its installer. The workspace uses Python 3.14; uv downloads it if needed.

From the repository root in a fresh clone:

```sh
uv sync --frozen --group dev --group native-build
make contributor-check
```

`make contributor-check` runs `make verify` followed by `make homebrew-smoke`. Expect Ruff's
`All checks passed!`, MyPy's `Success: no issues found`, a passing pytest summary (some filesystem
checks can skip on unsupported hosts), successful wheel/source builds, native smoke JSON, and
`existing_product: preserved` or `existing_product: absent`. A nonzero exit means the check failed.
Both CI jobs run this same target. No credentials or private access are required.

The Homebrew check builds one native executable, audits its dependency inventory, runs the local
product journey, and writes a digest manifest. It installs a test-only, keg-only `open-brain-smoke`
formula in the temporary `open-brain-local/smoke` tap and runs its unlinked binary by absolute path.
It checks that an existing `open-brain` installation's prefix, version, link, and binary digest stay
unchanged. If no product is installed, the check leaves it absent. Each normal or interrupted exit
cleans up smoke-owned Homebrew state; the next run recovers marked tap/formula residue after a forced
termination. Do not run concurrent Homebrew smoke checks against the same Homebrew installation.

Common failures:

- `uv: command not found`, `make: command not found`, or `Homebrew is required for contributor-check`:
  install the missing prerequisite and open a shell with it on `PATH`.
- `unsupported native build platform`: use macOS arm64 or Linux x86_64.
- `reserved smoke tap is not smoke-owned; refusing cleanup` or `reserved smoke tap has another
  installed formula; refusing cleanup`: inspect `brew --repository open-brain-local/smoke` and
  `brew list --formula --full-name`. Move your own unrelated tap work to another name before retrying.
- `existing Open Brain installation changed during smoke`: check for another Homebrew operation or
  manual change during the run; the smoke intentionally fails if preservation cannot be verified.

Private release auditing is **not required for normal contributions** and is excluded from
`make contributor-check` and CI. The owner runs `make audit` and `make audit-history` separately with
an uncommitted `PRIVATE_DENYLIST`; contributors do not need that file.

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

# Open Brain

Open Brain gives your notes and AI agents one local memory: capture text, search it later, and
export the complete Brain. Your records live in SQLite and ordinary files on your computer.
The core runs in the foreground without a daemon or provider account.

## Try a synthetic note

Install with `brew install cbolden15/tap/open-brain` on macOS or Linux with Homebrew.
Then run these three commands in the same shell. They use a new temporary Brain, separate from
your normal notes; keep `DEMO_BRAIN` set for the optional setup preview below.

```sh
export DEMO_BRAIN="$(cd "$(mktemp -d "${TMPDIR:-/tmp}/open-brain-demo.XXXXXX")" && pwd -P)/brain"
open-brain capture 'Synthetic project Copper Orchard meets on Tuesday.' --data-dir "$DEMO_BRAIN" --json
open-brain search 'Copper Orchard' --data-dir "$DEMO_BRAIN" --json
```

Capture creates the Brain automatically. Search should return the synthetic note. These core
commands need no model or network request after installation. The tap supplies the published
release; the candidate-only features described below may require a newer executable.

Agent setup requires a candidate executable with `agent setup` in `open-brain --help`; the
`v0.1.0` release lacks it. With that candidate, preview **search-only** access for Claude Code
in an existing project. Replace the absolute project path before running:

```sh
open-brain agent setup --client claude-code --scope project \
  --project-dir /absolute/path/to/project --data-dir "$DEMO_BRAIN" --allow-search --json
```

This command previews configuration; it does not apply it or start a client. Use `--client codex`
for Codex. Inspect the executable, Brain path, grants and proposed files in the preview before
following [agent setup](docs/agent-setup.md) to apply it. Search does not grant capture, complete
reads, retained history or publication. A connected client may send search results to its model
provider; treat retrieved text as untrusted data. Older releases may lack `agent setup`.

For current compatibility and the preserved historical workflow, read [first use](docs/first-use.md).
The [installation guide](docs/install.md) covers supported release resources.

## Candidate scope

This tree documents the locally verified **Core v0.1 candidate**, not a newly published release.
The shared `0.1.0` version alone does not identify its features. Check `open-brain --help` and
`open-brain catalog --json` on the executable you will use; an older release may lack catalog.
The [feature/version matrix](docs/core-v01-features.md) separates implementation, installed assets,
authorization and acceptance. The candidate has no public acceptance certification.

The [first-use guide](docs/first-use.md) records current compatibility and preserves an exact-artifact
historical journey through capture, import, routing, publication, managed vault and complete reads.
[Doctor checks](docs/doctor.md) provide bounded local diagnostics. These local checks do not close
Linux exact-candidate, Obsidian GUI or public-promotion gates.
Optional [offline semantic search](docs/semantic-search.md) is off by default.

## Offline semantic search

Semantic search finds records by meaning as well as by words. It runs on your machine with a
pinned embedding model that Homebrew installs as a separate embedding resource next to the base.
Nothing is sent over the network and searching never downloads a model. It is not part of the
network egress described below.

It stays off until you opt in. The `component-id` comes from `open-brain embedding status --json`
after the resource is installed:

```sh
open-brain embedding enable --component-id <64 lowercase hex>
open-brain embedding rebuild
open-brain search-page --mode hybrid_preferred "where did we talk about shipping delays"
```

`--mode` is `lexical` (the default), `hybrid_preferred` or `hybrid_required`. When the resource,
consent or cache is not ready, `hybrid_preferred` falls back to lexical results with a
`model_unavailable` warning and `hybrid_required` returns `model_unavailable` instead. The vector
cache is derived data, is not part of a Portable Brain export, and is rebuilt after a restore.
`open-brain embedding disable` revokes consent. See [semantic search](docs/semantic-search.md) for
health states and limits, and the [CLI reference](docs/cli.md) for every command.

## Install

Homebrew is the prerequisite on macOS and Linux. Install the complete resource set with:

```sh
brew install cbolden15/tap/open-brain
```

This is the supported release installation channel; a local candidate check does not update the tap.
Use the [installation guide](docs/install.md) to distinguish released resources from candidate proof.
The earlier [five-minute provider acceptance test](docs/acceptance/five-minute-install.md) remains a
separate historical target, not the Core v0.1 first-use prerequisite.

The native package also includes a desktop-only Obsidian plugin for the managed Markdown vault and
a separately packaged structural Graphify helper. The plugin offers capture, search, source
navigation, conflict review, generated Canvas output, and semantic suggestion review. Installation,
explicit Obsidian activation, provider setup, pause controls, and safe plugin removal are documented
in [the installation guide](docs/install.md#use-the-managed-vault-in-obsidian).

Semantic refresh is opt-in network use. It supports explicit OpenAI, Anthropic, or Gemini API keys
after the owner acknowledges the eligible managed-note scope. Claude subscription remains closed
with `subscription_isolation_unproven`; Open Brain does not add privileges or broader host access to
make that transport work.

[Agent setup for Claude Code and Codex](docs/agent-setup.md) runs from the command line. Optional
source/collector implementations are source-only; continuously updating connected sources and
production provider onboarding remain outside Core v0.1. See the
[source boundary](docs/integrations/core-sources.md). See
[ADR 0017](docs/architecture/decisions/0017-desktop-companion-boundary.md) for the collector
package and permission boundaries.

The Core v0.1 candidate includes [paged search, complete reads and retained history](docs/records-and-history.md),
including owner-CLI revision relationships and publication workflows in both clients. Its
[Portable v5 exports](docs/portable-brain-v5.md) preserve source, decision, privacy-repair, and
issuer-migration evidence. The [Portable v4 document](docs/portable-brain-v4.md) remains available
for the historical format. These changes
require this candidate's surfaces; local verification does not publish a new Homebrew release.

## Use Open Brain

The historical blocks in the [first-use guide](docs/first-use.md) require their pinned artifact.
They derive IDs from actual responses; reference pages use uppercase placeholders for your own IDs.

The first capture creates the private data directory, owner identity, Brain identity, and SQLite
state automatically. Existing supported local databases upgrade transactionally when opened for
writing. This candidate uses local state schema **21**, runtime session **16**, and Portable metadata
**16**. These are separate from product version `0.1.0`, catalog schema 2, task contract `t03.v1`,
and plugin bridge protocol 1. Stop older sessions before upgrading and update clients together;
older runtimes must refuse incompatible state. See [schema migrations](docs/schema-migrations.md) for supported
older layouts, refusal behavior, and interrupted-transaction recovery.

Markdown import scans nested lowercase `.md` files, skips Obsidian metadata directories, and leaves
the source tree unchanged. Imported records are labeled unverified. Prior revisions remain in full
export after a source file changes or disappears, so review the first-run summary before confirming.
Markdown links, frontmatter, HTML, and embeds are stored as inert text.

Other owner-local features each have their own guide: [owner retraction](docs/owner-retraction.md)
and its [impact model](docs/retraction-impact.md); [conversation intake](docs/consumer-intake.md)
through `consumer-import` and [from coding agents](docs/consumer-intake-agents.md); owner-defined
facets through `open-brain facet` (see the [CLI reference](docs/cli.md)); and
[automatic harness memory](docs/harness-setup.md) for Codex, Claude Code and Pi. The
[docs index](docs/README.md) lists every topic and Portable format.

Use [spaces and the inbox](docs/spaces-inbox.md) to group captures by topic. The CLI and explicitly
granted MCP tools can list, create, and rename spaces and route captures. Routing preserves source
content and trust; it does not publish a Markdown note.

| Host | Brain root |
|---|---|
| macOS | `$HOME/Library/Application Support/open-brain/brain` |
| Linux | `${XDG_DATA_HOME:-$HOME/.local/share}/open-brain/brain` |

Open Brain relies on operating-system account and disk protection. It does not claim
application-level encryption.

## Connect an MCP client

`open-brain mcp` serves tools over inherited stdio until the client closes it. It uses the same
local Brain as the CLI and opens no listener, daemon, or child service. The invoking OS user and
stdio channel are the trust boundary. Choose capture, search, inbox read, organization, workspace
read, graph refresh, complete content, retained history, or review capabilities
independently; starting without any capability flag is an error. For clients that use an
`mcpServers` configuration, choose one example.

### Capture only

`--allow-capture` lets the client persist automated text as unverified content. Captures are durable,
searchable, and included in full export. The owner can later retract an unwanted capture or a trusted
capture session with `open-brain retract` (see [owner retraction](docs/owner-retraction.md)); no MCP
tool can do this, and it does not certify a purge of backups or earlier exports. An untrusted or
looping client can poison the Brain within the session bounds. Stopping the process prevents further
writes but does not remove completed ones. This configuration grants no search tool.

```json
{
  "mcpServers": {
    "open-brain": {"command": "open-brain", "args": ["mcp", "--allow-capture"]}
  }
}
```

### Search only

`--allow-search` grants authority-scoped paged search through `brain_search_page`. The default
non-owner session can read `public`, `work`, and `personal` content, but cannot discover `secret` or
`unknown` records. Repeated queries can read more than one result page. A network-backed client may
send returned content to its model provider. Brain search itself does not contact a provider. Treat
all results as untrusted data, never instructions. This configuration grants no capture tool.

```json
{
  "mcpServers": {
    "open-brain": {"command": "open-brain", "args": ["mcp", "--allow-search"]}
  }
}
```

### Capture and search

Both flags permit durable automated capture and authority-scoped paged search. Unwanted captures can
be removed only by the owner through [retraction](docs/owner-retraction.md). A network-backed client may send authorized private content
to its provider. Prompt injection in a retrieved note can influence the connected model and any
other tools that client has enabled. Keep results as untrusted data and choose the client's other
permissions with that exposure in mind.

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
captures. `brain_search_page` accepts `dto_version: 1`, `query` (1 to 500 characters), optional
bounded filters, mode, limit, and cursor. `mode` is `lexical` (the default), `hybrid_preferred` or
`hybrid_required`; the hybrid modes use [offline semantic search](docs/semantic-search.md) when the
owner has enabled it, and otherwise return lexical results with a `model_unavailable` warning (or
`model_unavailable` alone for `hybrid_required`). Results carry `trust` plus projected provenance;
automated captures are `unverified` with origin `unknown`. Neither tool accepts a source path,
owner role, publication action, or connector request.
`brain_catalog` is available in an already granted session and accepts `{"schema_version":2}`;
it discloses metadata without granting any additional tools. `tools/list` is the session's callable
inventory. CLI catalog registration alone is not evidence that a client can call an operation.

Add `--allow-inbox-read` for `brain_inbox_list` and `brain_space_list`. Add `--allow-organize` for
`brain_space_create`, `brain_space_rename`, and `brain_inbox_route`. These permissions are independent
of capture and search. Space names and inbox previews are untrusted content that a connected client
may send to its model provider. [Agent setup](docs/agent-setup.md) can configure these grants for
Claude Code and Codex. Inbox, organization, review, workspace, and graph flags select an explicit
owner-authority MCP session; do not combine them into a session intended to remain non-owner scoped.

For complete current text add `--allow-content-read`; retained revision access requires
`--allow-history-read`. Search does not imply either grant. Review uses independent
`--allow-review-read`, `--allow-review-propose`, and `--allow-review-decide` flags. A decision can
publish only with the token from an inspection of the proposal. See [agent setup](docs/agent-setup.md)
and [review/publication](docs/review-publication.md). Owner CLI operations do not need MCP grants.

Each process permits 500 valid capture attempts and 16 MiB of aggregate UTF-8 capture input, 2,000
valid search attempts, 500 workspace reads with 16 MiB of output, and 20 graph refresh requests with
40 model attempts and 1 MiB of selected input. Organization permits 500 reads, 500 writes, and
16 MiB of aggregate response content per process. Duplicates, conflicts, and failed backend attempts
count. Calls that exceed a bound return a bounded session-limit error.
Invalid arguments do not count. Messages are limited to 1 MiB including their newline. Restarting the
explicitly launched process resets these limits; they limit accidental loops, not hostile same-user
code. Competing local writers either complete or return `database_busy`; retry a capture with its
same idempotency key after contention. SQLite retains its five-second busy timeout.

MCP capture uses a non-owner, capture-only identity. Search and path-free workspace/graph reads have
separate authority. `--allow-graph-refresh` exposes only a refresh request; version 0.1.0 has no MCP
provider-configuration or credential operation, so it returns `provider_not_configured`. The adapter
cannot accept graph suggestions, resolve workspace conflicts, edit exclusions or invoke
connectors. Its explicitly granted review decisions can publish a canonical page after inspection;
this is separate from graph-suggestion acceptance. Stopping the stdio process closes its capabilities.

## Develop

Supported contributor hosts are macOS arm64 and Linux x86_64. Install Git, GNU Make, Node.js 24.15+ or 26+,
npm, stable Rust, [uv](https://docs.astral.sh/uv/getting-started/installation/), and
[Homebrew](https://brew.sh/). On macOS, install the Xcode Command Line Tools (`xcode-select --install`).
On Linux, install Homebrew's build prerequisites (a C/C++ toolchain, curl, file, Git, and Make)
using your distribution's package manager. Put Homebrew on `PATH` using the `brew shellenv`
command printed by its installer. The workspace uses Python 3.14; uv downloads it if needed.
CI uses the latest Node.js 24 release.

From the repository root in a fresh clone:

```sh
uv sync --frozen --group dev --group native-build
make contributor-check
```

`make contributor-check` runs `make verify` and `make native-integration-smoke`. Expect Ruff's
`All checks passed!`, MyPy's `Success: no issues found`, a passing pytest summary (some filesystem
checks can skip on unsupported hosts), successful wheel/source builds, native smoke JSON, and
`existing_product: preserved` or `existing_product: absent`. A nonzero exit means the check failed.
CI runs the same checks, with the Python suite split across parallel shards (`make test-shard`).
No credentials or private access are required.

The native integration check builds the base and Graphify executables, audits their dependency
inventories, runs the local product journey, and writes a component digest manifest. It installs a
test-only, keg-only `open-brain-smoke`
formula in the temporary `open-brain-local/smoke` tap and runs its unlinked binary by absolute path.
It checks that an existing `open-brain` installation's prefix, version, link, and binary digest stay
unchanged. If no product is installed, the check leaves it absent. Each normal exit and INT/TERM interruption
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

## Historical implementations

The prior Secure Node and predecessor implementations are quarantined in `archive/`. They are not
members of the uv workspace and are excluded from imports, builds, tests, and installed artifacts.
Any future Secure Node must use a separate package or repository, namespace, and release boundary.

The current product contract is [docs/product-family.md](docs/product-family.md). The ordered roadmap
is [docs/plans/product-roadmap.md](docs/plans/product-roadmap.md). The shared-record and
byte-preserving interoperability model is in
[ADR 0014](docs/architecture/decisions/0014-shared-record-import-envelope.md).

## License

Apache License 2.0. See `LICENSE` and `NOTICE`.

# Open Brain roadmap and archived Secure Node direction

- Status: Open Brain workstreams are retained; the in-package Secure Node direction was superseded
  by the strict archive boundary on 2026-09-11.
- Date: 2026-09-08
- Product authority: [`../product-family.md`](../product-family.md)
- Acceptance authority: [`../acceptance/five-minute-install.md`](../acceptance/five-minute-install.md)

## Current state

Completed `OB1-W0` through `OB1-W6` work gives Open Brain
a small default dependency graph, automatic private bootstrap, direct capture, ranked SQLite FTS5
search, idempotent Markdown import, guarded schema migrations, local MCP capture/search, verified
Portable Brain export, status, doctor, and a native build.

The first appliance, protocol, crypto, and semantic-kernel implementation is quarantined under
`archive/open-brain-secure-node`. It is not an active workstream or package in this repository. A
future Secure Node must start behind a separate distribution or repository boundary.

`OB1-W2` no longer builds a distribution system. It removes the custom installer, transactional
activation, receipts, clean-host matrix, notarization pipeline, attestations, and release-evidence
assembly. The replacement is a native executable in a small archive, a digest manifest, a Homebrew
formula generated from that manifest, and one product smoke on each supported architecture.

No release has been published. W7 adds one contributor command and a smoke formula that preserves
any installed product. The documented frozen setup and `make contributor-check` passed from a fresh
clone on macOS arm64. Linux and macOS CI at the exact goal-branch head remain the W7 merge gate.

The public repository is `cbolden15/open-brain`. It was transferred from
`vora-technology/open-brain` on 2026-09-08 without changing its repository identity, branches, or
open pull requests. The formula will live in `cbolden15/homebrew-tap`; creating and protecting that
tap remains a final release prerequisite, not an Open Brain product workstream.

## Product milestones

| Milestone | Outcome | Gate |
|---|---|---|
| `OB1` Open Brain | A useful local second brain installed with Homebrew | Each ordered workstream below is usable before the next starts |
| `SN1` Secure Node | Future separate encrypted-custody product | Out of scope for this repository |
| `UP1` Upgrade proof | A default export imports into a future separate product | Shared identities and bytes survive through Portable Brain |

## Ordered Open Brain workstreams

Execution plan: [2026-09-08 OB1 product completion](2026-09-08-ob1-product-completion.md).

### `OB1-W2`: reduce the release surface

Execution contract: [2026-09-08 OB1-W2 release-surface
reduction](2026-09-08-ob1-w2-release-surface-reduction.md).

Keep one native executable, one release manifest, and a Homebrew formula contract. CI runs on
`macos-latest` arm64 and `ubuntu-latest` x86_64. Each runner installs through Homebrew, captures a
record, searches for it, exports it, and exits cleanly.

Usable when the repository verification passes, the native artifact passes its product-scope audit,
the macOS executable is arm64 and signed, both Homebrew smoke jobs pass, and no removed release path
is used by active instructions or CI. Completed historical records may retain superseded paths when
they clearly point to ADR 0015.

### `OB1-W3`: design and implement search

Write the retrieval design before changing search behavior. SQLite FTS5 is the baseline. Define
tokenization, indexed fields, ranking, snippets, filters, update behavior, deletion behavior, and a
small relevance fixture. Add hybrid lexical and embedding retrieval only if it remains local,
dependency-free for the installed product, and measurably improves that fixture.

Usable when representative queries find the right records with understandable ranking and no model,
network, daemon, or Secure Node dependency.

### `OB1-W4`: import Markdown and Obsidian vaults

Import one directory tree of Markdown files. Preserve source-relative paths and content digests.
Record enough source identity to make repeated imports idempotent and to update changed files without
duplicating unchanged records.

Usable when a real synthetic vault can be imported, searched, changed, and re-imported with stable
results.

### `OB1-W5`: add schema migrations

Replace implicit schema creation with ordered, versioned SQLite migrations. Refuse unknown newer
schemas and test upgrade from every released schema version. Include the schema version in Portable
Brain exports without exporting live SQLite state.

Usable when a prior-version fixture upgrades once, retry is harmless, current data remains searchable,
and export validation reports the correct schema version.

### `OB1-W6`: expose capture and search through MCP

Add a local MCP server with capture and search tools over the same direct application path. It must
not start a daemon, import Secure Node, or add network transport to the default product.

Usable when a local MCP client can capture and retrieve a record from the same Brain and storage path
as the CLI, with explicit launch-time consent for durable automation writes and whole-Brain reads.

### `OB1-W7`: make contribution verification ordinary

Document and test build-from-source, unit/integration tests, native build, and the product smoke.
Contributors must be able to verify a pull request without signing credentials, Docker, VMs, cloud
accounts, or private infrastructure.

Usable when a new contributor can follow the checked-in commands on macOS or Linux and reproduce the
same smoke used by CI.

Local W7 evidence is recorded in [the contributor-path audit](../audits/2026-09-09-ob1-w7-contributor-path-audit.md).

## Shared record and encryption seam

The encryption seam is record-level. Open Brain stores the shared record body as plaintext in its
private local SQLite/filesystem implementation. Secure Node later places that same exact body inside
its protected record envelope and encrypts accepted storage. Attachments and blobs remain
digest-addressed. Search indexes and embeddings are derived projections, never the portable source of
truth.

This decision is already specified in
[`ADR 0014`](../architecture/decisions/0014-shared-record-import-envelope.md). Portable Brain export is
the upgrade input. Secure Node must not reinterpret or copy the Open Brain SQLite database.

## Archived Secure Node workstream mapping

Stable `M1-W*` identifiers remain valid in historical evidence. Current planning uses these names:

| Previous ID | Current name | State |
|---|---|---|
| `M1-W0` | `SN1-W0`: executable protocol freeze | Complete |
| `M1-W1` | `SN1-W1`: pure semantic kernel | Complete |
| `M1-W2` | `SN1-W2`: transactional encrypted ledger and blobs | Not started |
| `M1-W3` | `SN1-W3`: authorization, request binding, and fencing | Not started |
| `M1-W4` to `M1-W8` | `SN1-W4` to `SN1-W8`: operations, search, purge, service, closure | Not started |

These identifiers remain historical evidence only. Continuing the work requires a separate package
or repository, namespace, test suite, dependency graph, and release policy. It cannot re-enter the
Open Brain Homebrew journey or workspace as an extra.

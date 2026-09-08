# Open Brain and Secure Node roadmap

- Status: accepted product split; `OB1-W2` is complete and the `OB1-W3` to `OB1-W7`
  completion plan is READY
- Date: 2026-09-08
- Product authority: [`../product-family.md`](../product-family.md)
- Acceptance authority: [`../acceptance/five-minute-install.md`](../acceptance/five-minute-install.md)

## Current state

The product split preserves the existing engineering. Completed protocol, schema, crypto, and
semantic-kernel work belongs to Secure Node. Completed `OB1-W0` and `OB1-W1` work gives Open Brain a
small default dependency graph, automatic private bootstrap, direct capture, basic search, verified
Portable Brain export, status, doctor, and a native build.

`OB1-W2` no longer builds a distribution system. It removes the custom installer, transactional
activation, receipts, clean-host matrix, notarization pipeline, attestations, and release-evidence
assembly. The replacement is a native executable in a small archive, a digest manifest, a Homebrew
formula generated from that manifest, and one product smoke on each supported architecture.

No release has been published. No search, import, migration, or MCP feature work belongs in the
release-surface reduction change.

The public repository is `cbolden15/open-brain`. It was transferred from
`vora-technology/open-brain` on 2026-09-08 without changing its repository identity, branches, or
open pull requests. The formula will live in `cbolden15/homebrew-tap`; creating and protecting that
tap remains a final release prerequisite, not an Open Brain product workstream.

## Product milestones

| Milestone | Outcome | Gate |
|---|---|---|
| `OB1` Open Brain | A useful local second brain installed with Homebrew | Each ordered workstream below is usable before the next starts |
| `SN1` Secure Node | Advanced encrypted custody and multi-client operation | Secure Node conformance passes without changing the Open Brain default |
| `UP1` Upgrade proof | A default export imports into a fresh Secure Node | Shared identities and bytes survive; new custody claims start only after import |

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

## Shared record and encryption seam

The encryption seam is record-level. Open Brain stores the shared record body as plaintext in its
private local SQLite/filesystem implementation. Secure Node later places that same exact body inside
its protected record envelope and encrypts accepted storage. Attachments and blobs remain
digest-addressed. Search indexes and embeddings are derived projections, never the portable source of
truth.

This decision is already specified in
[`ADR 0014`](../architecture/decisions/0014-shared-record-import-envelope.md). Portable Brain export is
the upgrade input. Secure Node must not reinterpret or copy the Open Brain SQLite database.

## Secure Node workstream mapping

Stable `M1-W*` identifiers remain valid in historical evidence. Current planning uses these names:

| Previous ID | Current name | State |
|---|---|---|
| `M1-W0` | `SN1-W0`: executable protocol freeze | Complete |
| `M1-W1` | `SN1-W1`: pure semantic kernel | Complete |
| `M1-W2` | `SN1-W2`: transactional encrypted ledger and blobs | Not started |
| `M1-W3` | `SN1-W3`: authorization, request binding, and fencing | Not started |
| `M1-W4` to `M1-W8` | `SN1-W4` to `SN1-W8`: operations, search, purge, service, closure | Not started |

Secure Node work does not enter the default Homebrew journey. `UP1` begins only after the required
Secure Node persistence and authorization work exists.

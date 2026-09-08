# OB1 product completion plan

- Status: READY for `OB1-W3` implementation; all surfaced P0-P2 findings are resolved
- Date: 2026-09-08
- Branch: `goal/open-brain-five-minute-install`
- Product authority: [`../product-family.md`](../product-family.md)
- Acceptance authority: [`../acceptance/five-minute-install.md`](../acceptance/five-minute-install.md)
- Predecessor: [`2026-09-08-ob1-w2-release-surface-reduction.md`](2026-09-08-ob1-w2-release-surface-reduction.md)

## Objective

Complete the five remaining Open Brain workstreams in order: useful local search, one Markdown
import path, versioned SQLite migrations, local capture and search over MCP, and an ordinary
contributor path. Finish with one public Homebrew release and the exact five-minute acceptance run
on both supported platforms.

The work stays focused on the second-brain product. It does not restore custom installers,
notarization, release attestations, clean-host infrastructure, daemons, or Secure Node behavior.

## Starting state

`OB1-W0`, `OB1-W1`, and `OB1-W2` are complete on the goal branch. The default native executable
already performs automatic private bootstrap, direct text capture, basic substring search, verified
Portable Brain export, status, and doctor. The local Homebrew smoke passes on macOS arm64, and the
same source passed the macOS arm64 and Linux x86_64 CI jobs.

Version `0.1.0` remains unreleased. The public source repository is `cbolden15/open-brain`, and this
checkout's `origin` uses that URL. The repository was transferred from
`vora-technology/open-brain` on 2026-09-08. The future formula repository,
`cbolden15/homebrew-tap`, does not exist yet, and the goal branch has not been merged to `main`.
Therefore no external user has a released SQLite schema that must remain compatible yet.

## Completion contract

Open Brain is complete for its first public release only when:

1. `OB1-W3` through `OB1-W7` each pass their own usability gate in order.
2. The default executable still has no Secure Node, connector, model, network listener, daemon, or
   network dependency; the opt-in MCP subcommand is stdio-only.
3. A fresh checkout can run the same verification used by CI without credentials or private
   infrastructure.
4. The two final archives, combined digest manifest, and Homebrew formula bind one exact version.
5. The public acceptance block completes through verified export within 300 seconds on macOS arm64
   and Linux x86_64.

No later workstream may be used to waive an earlier gate. A failed gate is fixed in its own branch
before the next branch starts.

## Execution shape

Use one branch and one focused PR per workstream. Each branch starts from the updated
`goal/open-brain-five-minute-install` branch and merges back into it before the next branch begins.

| Order | Branch | Outcome |
|---|---|---|
| 1 | `feat/ob1-w3-search` | FTS5 retrieval and relevance proof |
| 2 | `feat/ob1-w4-markdown-import` | Idempotent Markdown and Obsidian import |
| 3 | `feat/ob1-w5-schema-migrations` | First supported, versioned local schema |
| 4 | `feat/ob1-w6-local-mcp` | On-demand stdio capture and search tools |
| 5 | `docs/ob1-w7-contributor-path` | Reproducible contributor verification |

Runtime changes, release publication, and unrelated cleanup never share a PR. Read-only review may
run concurrently, but one coordinator owns edits, git state, and the gate verdict.

## Fixed product decisions

### Search baseline

SQLite FTS5 is the first-release retrieval engine. The installed product gets no embedding model,
model download, vector database, network call, or new runtime dependency. Embeddings remain deferred
unless the completed FTS5 relevance fixture exposes a named failure class and a separate proposal
proves a local dependency-free implementation improves it.

`search_documents` remains the rebuildable document projection. A materialized FTS5 virtual table
keyed to each search result ID indexes only the public-safe title and body projection after NFC
normalization. Same-transaction engine operations keep insert, update, delete, and rebuild behavior
consistent.
Every returned title, snippet, and explanation passes through the existing
`project_public_result_text` boundary even if an index row is malformed. Regression tests seed path,
credential, protected-literal, and digest-shaped values and require that none appear in output.
Every human result also carries a bounded `owner`, `third-party`, or `unverified` label; JSON results
carry the existing `trust` plus public `source_origin`. Raw source references remain hidden. W6
preserves these fields in MCP results. Unknown automation captures and Markdown imports must
therefore be visibly unverified at the point where either a person or model consumes search output.

The live projection and FTS table both belong to `.open-brain/state/phase1.sqlite3`. The existing
`.open-brain/indexes/search.sqlite3` database is a separate disposable artifact used by retained
portable-import and Secure Node recovery paths; default Open Brain retrieval never queries or
synchronizes it. Its owning portability operation replaces it on an explicit index rebuild; otherwise
it remains a potentially stale plaintext snapshot until the entire local data directory is removed.
W3 updates default-product status and doctor to label and count the live phase1 projection as the
authoritative index and, when the disposable database exists, to report its presence, generation,
document count, and non-authoritative stale-snapshot status without exposing content or its absolute
path. W5 does not migrate the disposable database. The privacy and threat documents retain this
second plaintext index as an explicit data-at-rest surface rather than treating it as orphaned state.

FTS rows and ranking state are derived data, not Portable Brain records. Default Open Brain stores
the durable index as plaintext inside its private local data directory. Transient SQLite sorter and
FTS scratch may use OS-managed temporary storage outside that directory; Open Brain does not claim
application encryption or data-directory confinement for that surface. A future Secure Node
implementation must keep scratch in memory or its encrypted boundary and must encrypt and purge every
durable derived index; this plan does not exempt FTS data from that requirement.

### Import identity

The first import command is:

```sh
open-brain import /absolute/path/to/vault
```

It recursively considers UTF-8 `.md` files and never changes the source directory. A local-only
import-root record maps the resolved source directory to a random stable root ID. The absolute path
stays in operational SQLite state and never appears in search output or Portable Brain exports.
Portable provenance uses that root ID plus the normalized relative path, so source identity survives
export without revealing the host path.

Import opens the selected root as a directory and stores its operational device and inode identity.
The same identity reuses its existing root ID even when a case-insensitive filesystem reports a path
alias. Ancestor and descendant checks walk directory identities rather than comparing path strings.
A new overlapping root is refused with `overlapping_import_root` before any scan or write. Replacing
a directory at the same path with a different identity is refused; cross-volume root relocation and
root folding are separate future work.

Each imported file is identified inside that root by its normalized source-relative path. Exact
source bytes are retained through the existing file/blob capture boundary, and SHA-256 records the
content revision. A deterministic delivery key over root ID, relative path, and content digest makes
an unchanged rerun a no-op. Changed bytes create one new immutable capture revision and move the
active search document to it. Prior capture records remain valid history.

Import uses the existing non-owner public-job submission path with `content_origin=unknown`,
`owner_context=automation_absent`, and capture-only authority. Portable records therefore carry
`source.origin=third_party`, `provenance.content_origin=unknown`, and `trust.label=unverified`.
Imported Markdown never gains owner-authored trust or automatic publication authority, even when the
user wrote the source file.

After a successful complete scan, a previously imported path that is now missing becomes inactive
and leaves the default search projection, while its immutable capture history remains exportable. If
the same path and digest reappear, the existing capture is reactivated without creating a revision.
A rename is an inactive old path plus an active new path. Destructive source pruning and a search
flag for inactive revisions are outside the first release.

Only regular UTF-8 `.md` files are eligible. Each candidate is opened without following symlinks and
accepted only when descriptor metadata confirms the same regular file observed during enumeration
and `st_nlink == 1`; symlinks, hardlinks, FIFOs, sockets, devices, file-swap races, and dot-directories
are skipped and counted.
Before mutation, enumeration stops and refuses the import if it visits more than 100,000 filesystem
entries, selects more than 10,000 Markdown files, or observes more than 512 MiB of eligible file data.
Each read is also bounded by the existing 1 MiB capture limit. Oversized files, invalid UTF-8,
path-normalization collisions, and unreadable files are reported as failures. Markdown, frontmatter,
wiki links, HTML, and embed syntax are treated as inert text; import never follows a content link,
executes code, or loads an Obsidian plugin. Import commits one file at a time after enumeration,
returns a summary, and exits nonzero if any selected file failed. A rerun safely resumes the remaining
work.

`--allow-large-vault` is the single explicit recovery for a root over a total enumeration bound. It
bypasses only the visit, file-count, and aggregate-byte ceilings while preserving the regular-file,
hardlink, per-file, path, and race checks. A default bound refusal creates no root state and prints
the exact retry flag without echoing the directory, so the user can opt in without splitting the
vault into overlapping roots.

### Schema sequencing

W3 and W4 may extend the unreleased local Brain database while it remains pre-ledger at
`PRAGMA user_version=1`. No compatibility claim is allowed at that point. W5 freezes local schema
version 2 as the first supported external schema and must upgrade the reachable W2, W3-only, and
W4-populated layouts without losing records or searchability.

W5 extracts and reuses the checked-in `Migration` type, checksum validation, and transactional
migration algorithm from `open_brain_engine.storage.sqlite`; the existing event-schema defaults and
`inspect_event_schema` remain separate. The local Brain database owns its own migration catalog,
ledger rows, and inspector inside `.open-brain/state/phase1.sqlite3`, so its migration names cannot
collide with the event database.

Local schema version 2 has exactly two migration entries because the shared algorithm requires one
contiguous entry per version. Migration 1 idempotently creates the W2 baseline. Migration 2
idempotently creates the W3 FTS and W4 import structures, clears only the derived FTS rows, and
backfills them once from active `search_documents` rows. New installs and upgrades run this same
pair.

An existing unreleased version-1 database has no migration ledger. Before adopting it, W5 validates
that its tables and columns match a committed W2, W3-only, or W4-era fixture. Only then may the two
idempotent migrations establish the ledger and version. The inspector also recognizes the older
version-0 layout already classified as `legacy` by the current engine when it otherwise matches the
complete W2 table and column shape, and a valid version-1 ledger as supported-old state. An unknown
layout, a bad ledger or checksum, and every newer version fail before mutation.

The SQLite `PRAGMA user_version` is operational state and does not enter Portable Brain. Portable
Brain v1 already carries its own manifest `schema_version`. W5 makes export validation and CLI JSON
report that Portable version explicitly instead of adding the local database version to the shared
record contract.

### MCP surface

The single native executable gains an on-demand subcommand with explicit capability flags:

```sh
open-brain mcp --allow-capture
open-brain mcp --allow-search
```

It serves MCP over stdio until EOF. It starts no listener, daemon, or child service. `--allow-capture`
exposes `brain_capture`; `--allow-search` exposes `brain_search` after the user explicitly chooses
whole-Brain read access for the connected client. The flags may be combined. A launch with neither
flag exits with a bounded usage error instead of exposing a tool. Both tools use the same application
operations as the CLI. `brain_capture` accepts text plus an optional bounded idempotency key.
`brain_search` accepts a query and optional bounded limit. The existing Secure Node MCP command and
its authorization model remain separate.

The adapter hashes a supplied key into an MCP-specific delivery-ID namespace; raw keys never become
database identifiers or output. Calls without a key receive a random ID. Therefore an MCP key cannot
collide with CLI or importer delivery IDs.

Starting `open-brain mcp --allow-capture` opts into durable automated capture. The invoking OS user
and inherited stdio channel are the trust boundary, so the default single-user product adds no token
or grant system. The capture path uses the existing non-owner capture-only submission identity with
unknown content origin, automation-absent owner context, and quick-capture authority. It cannot
publish canonical content, invoke connectors, perform actions, or gain Secure Node capabilities.

The search path has separate read authority. It intentionally uses the same whole-Brain local
retrieval scope as the CLI because Open Brain 0.1.0 has one user, one Brain, and no compartments.
`brain_search` returns at most 10 public-safe titles and excerpts per call, but repeated queries can
read private imported note content. Connecting a network-backed MCP client is therefore the user's
explicit authorization for that client to receive and potentially send those results to its model
provider. That authorization is expressed by adding `--allow-search` to the client configuration.
The flag's CLI help and the adjacent README configuration examples state this disclosure and keep
capture and search independently selectable. The `open-brain` process itself performs no network
egress. Retrieved records remain untrusted data, not instructions, and stopping the stdio process
closes both read and write access.

Automated captures are immutable, searchable, and included in full export. Version 0.1.0 has no
selective delete, session rollback, or certified purge. The `--allow-capture` help and README example
state that an untrusted or looping client can persist unwanted content up to the session bounds and
that stopping the process prevents further writes but does not remove completed captures. This is an
accepted first-release limitation; adding an undo or local-delete workflow requires its own product
and export-semantics decision rather than hiding purge behavior inside MCP.

One MCP process accepts at most 500 `brain_capture` calls, 16 MiB of aggregate UTF-8 capture input,
and 2,000 `brain_search` calls. Every syntactically valid attempt counts, including duplicates and
conflicts. The next over-limit call fails before work with `session_capture_limit` or
`session_search_limit`; restarting the explicitly launched process resets the counters. These bounds
limit an accidental agent loop, not a hostile process already running as the same OS user, and the
threat model states that distinction.

### Contributor and release boundary

At the start of this plan, `make homebrew-smoke` still creates the fixed
`open-brain-local/smoke/open-brain` test formula and refuses to run when that formula, tap, or a public
`open-brain` formula is installed. No public release exists before W7, so W3 through W6 run it on the
two clean CI jobs and on local hosts that satisfy this stated precondition.

W7 replaces that behavior with a test-only, keg-only `open-brain-smoke` formula and runs the
`open-brain` binary from its keg. It never links over, uninstalls, or otherwise changes an existing
public `open-brain` installation. The final contributor target runs static checks, strict type
checking, the full test suite, package builds, the native build, and this isolated product smoke.
CI keeps exactly two jobs, macOS arm64 and Linux x86_64, and invokes that same target on both. No
signing credential, private denylist, cloud account, Docker runtime, or VM is required.

The smoke removes only its own deterministic test formula and tap before setup and in an
`EXIT`/`INT`/`TERM` trap. A previous interrupted smoke cannot block the next run. Concurrent smoke
runs on one Homebrew installation remain unsupported; no lock or transaction subsystem is added.

Publishing stays a short owner-operated checklist after W7. There is no release workflow, installer,
or evidence service to build.

### Public repository ownership

The canonical release repositories are `cbolden15/open-brain` and `cbolden15/homebrew-tap`. The
existing product repository was transferred rather than copied, preserving its repository ID,
history, branches, issues, pull requests, and old GitHub URL redirect. This checkout's `origin` now
uses the new URL. Do not recreate the repository at the old location because that permanently
removes the redirect. GitHub's current transfer behavior and caveats are recorded in its
[repository transfer documentation](https://docs.github.com/en/repositories/creating-and-managing-repositories/transferring-a-repository).

These are personal-account repositories. GitHub organization teams and organization 2FA enforcement
do not apply. The transfer review confirmed two-factor authentication on the `cbolden15` account,
no additional direct collaborators, preserved `main` protection, both open pull requests, and all
five workflows. GitHub disabled secret scanning and push protection during transfer; both were
restored immediately. The release gate must reconfirm these controls and owner-only release
publication before publishing an artifact or formula. Personal repositories expose owner and
collaborator roles rather than organization team roles, as described in GitHub's
[personal-repository permission model](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/repository-access-and-collaboration/permission-levels-for-a-personal-account-repository).

### Owner-only private-content checkpoint

Immediately before each W3 through W7 branch merges back into the goal branch, freeze its head and
run the following in a disposable single-branch clone that contains the candidate and its ancestors,
using the owner's untracked private denylist:

```sh
PRIVATE_DENYLIST=/absolute/path make audit
PRIVATE_DENYLIST=/absolute/path make audit-history
```

This checkpoint is part of every workstream gate but stays outside CI and `make contributor-check`;
contributors need no private denylist. If the current unmerged workstream introduced a match, remove
the private value and, with explicit owner authorization, rewrite only that workstream branch before
it merges. If the final audit finds an older match in the still-unmerged goal branch, stop publication
and use a separately approved one-time sanitation rewrite of that goal history, then rerun every
affected gate and audit. Never rewrite public `main`. If an affected object has already reached a
public remote, treat it as a disclosure, rotate any credential, follow the host's removal process,
and do not describe history rewriting alone as remediation.

The audit policy is not weakened for fixtures. Public `main` already contains the exact historical
prefix `examples/synthetic-vault/`, so W3 adds one code-level legacy fixture exception for that prefix
which suppresses only its `vault` path-part finding. Tests require content rules, forbidden suffixes,
and adjacent paths such as `examples/customer-vault/` to keep failing in both tree and archive scans.
W4 renames the current example away from the forbidden path family and creates symlinks, hardlinks,
sockets, and other special-file cases only inside temporary test directories. W5 commits deterministic
`.sql` builders under `tests/fixtures/local-schema/` and materializes database files only under the
test runner's temporary directory. No `.db`, `.sqlite`, or `.sqlite3` fixture enters the tree or Git
history, and no entry is removed from `FORBIDDEN_SUFFIXES` or `FORBIDDEN_PARTS`. Any further exact-path
fixture exception needs a separate security decision and must leave release-archive content and
suffix checks unchanged.

## `OB1-W3`: FTS5 search

### Design first

Before runtime edits, add `docs/retrieval.md` covering:

- indexed fields, the phase1 live projection, the disposable portability index, and their distinct
  ownership;
- NFC normalization before indexing and query conversion, plus the `unicode61` tokenizer with
  `remove_diacritics 2` and its lack of word segmentation for CJK script runs;
- literal query conversion so user text is never treated as raw FTS5 syntax;
- BM25 weighting, deterministic tie-breaking, snippets, and existing filters;
- bounded trust and public-origin labels in human and JSON search results, ready for W6 to preserve;
- insert, update, delete, and complete rebuild behavior, plus why embeddings are deferred or the
  exact measured evidence that reopens that decision.

The first release documents CJK mid-run word search as a known limitation and includes a fixture that
proves a complete CJK token remains retrievable. The correct multilingual options are a locally
shipped word segmenter or a measured character n-gram index. Both remain pending cross-platform
artifact, index-size, and ranking evaluation; the limitation cannot be hidden or described as full
Unicode search support.

Before search runtime edits, add `make verify` ahead of `make homebrew-smoke` in each existing CI job.
This is the minimum gate wiring needed for W3 through W6; W7 later replaces the two commands with the
single contributor target.

### Implementation

1. Add the result-ID-linked FTS5 table and same-transaction synchronization operations to the phase1
   store. Leave the disposable portability index schema and rebuild ownership unchanged, and make
   default status and doctor distinguish its snapshot metadata from the live phase1 document and FTS
   counts.
2. Replace the Python full-table scan with a bounded `MATCH` query and SQL `LIMIT`.
3. Weight title matches above body matches, then use stable record type, title, and result ID
   tie-breakers. Do not add recency or hidden personalization.
4. Generate bounded excerpts with FTS5 `snippet()` from the public-safe projection and return a
   public-safe explanation such as title, phrase, or lexical match.
5. Preserve and test the current `space_id`, `payload_family`, `record_type`, and result-limit engine
   filters. Preserve `allowed_space_ids` as an authorization predicate inside the FTS query before
   ranking and `LIMIT`; never apply it as a post-ranking Python filter. Expose only `--limit` on the
   first-release CLI; keep internal taxonomy and raw space IDs out of the default user surface until
   there is a concrete filtering workflow.

### Relevance fixture

Add a small synthetic fixture with representative notes and fixed query expectations. It must cover
exact phrases, multiple terms, title versus body ranking, case, composed and decomposed Unicode,
diacritics, complete CJK tokens, code-like tokens, filtering, no-result queries, deterministic ties,
updates, deletes, and rebuild equality. Include unbalanced quotes and parentheses, bare FTS operators,
prefix markers, column-filter syntax, NULs, and maximum-length input; each must be converted to a
literal bounded query or rejected with a stable error that does not echo the input. Add protected
values to both indexed fields and raw source material, then assert they cannot be retrieved or
exposed. Assert that owner-authored and unknown-automation records retain distinct visible trust and
public-origin labels. Assert relative ordering and excerpts, not SQLite's internal floating-point
score values.

### W3 gate

W3 is usable when the relevance fixture passes, the query path no longer scans every candidate row
in Python, rebuild reproduces the same ordered results, and default status and doctor agree with the
live result and FTS counts while separately reporting any disposable snapshot as non-authoritative
and potentially stale. An empty or non-matching `allowed_space_ids` set returns no rows through the
SQL query path, and an allowed set cannot receive a row from another space even when that row ranks
higher. Hostile or malformed query text cannot become FTS5 syntax, escape a field, crash the command,
or appear in an error. Human and JSON representations preserve the bounded trust label and public
source origin without revealing a raw source reference. `make verify`, `make homebrew-smoke`,
`actionlint .github/workflows/ci.yml`, and `git diff --check` pass. Both CI jobs must execute
`make verify` and `make homebrew-smoke` at the exact W3 head.

## `OB1-W4`: Markdown and Obsidian import

### Design first

Add `docs/import.md` describing root identity, relative-path normalization, exact-byte retention,
revision and reactivation behavior, inactive missing paths, file eligibility and race handling,
bounds and their explicit override, and the JSON summary. Keep imported source identity separate from
absolute host paths and from content-derived public record IDs. Update `docs/privacy-model.md` and
`docs/threat-model.md` for the new local filesystem read surface, hardlink refusal, and the rule that
imported markup is untrusted inert content.

### Implementation

1. Add an engine import task and local tables for import-root device/inode identity, file identities,
   active revisions, content digests, and last successful observation.
2. Add `open-brain import DIRECTORY` and the optional `--allow-large-vault` recovery without
   configuration prompts or a new importer framework. Reuse an exact filesystem identity and reject
   ancestor or descendant overlap before creating root state.
3. Enumerate and descriptor-open eligible regular files deterministically, preserve exact bounded
   bytes with `FilePayload` using `text/markdown`, and derive display titles from frontmatter title,
   first heading, or filename without rewriting source bytes. Enforce the default visit, file-count,
   aggregate, and per-file limits before the first capture.
4. Keep only active paths and their active revisions in the search projection while retaining
   immutable prior captures for Portable export and provenance. Mark missing paths inactive only
   after a complete successful scan and reactivate matching returns without another capture.
5. Return human and `--json` summaries with relative source paths plus `imported`, `updated`,
   `unchanged`, `missing`, `skipped`, and `failed` counts. Error output must not expose the absolute
   import root.

Rename `examples/synthetic-vault` to `examples/markdown-fixture` and expand it only with regular,
synthetic Markdown needed to prove nested paths, Obsidian metadata exclusion, changed files,
collisions, and failures. Construct every symlink, hardlink, socket, FIFO, and file-swap case under a
test runner temporary directory; never commit those filesystem objects.

### W4 gate

W4 is usable when first import makes every eligible note searchable, an unchanged rerun creates no
records, one changed note creates one revision and one active result, a missing note becomes
unsearchable without losing its export history, a restored identical note creates no record, special
files, hardlinks, and swap races cannot hang or escape the root, and same-directory case aliases reuse
one root ID on macOS. An overlapping root is refused without writes, each default enumeration bound
fails before writes and succeeds when explicitly retried with `--allow-large-vault`, exact source
bytes appear in verified export, and all W3 and repository checks still pass. The verified export
must also show `source.origin=third_party`, `provenance.content_origin=unknown`,
`owner_context=automation_absent`, and `trust.label=unverified` for every imported note, and search
must visibly label each active imported result as unverified.

## `OB1-W5`: versioned migrations

### Migration contract

Add `docs/schema-migrations.md` and one local migration catalog. Version 2 is the first released local
schema. Migration history is ordered, checksummed, and applied inside one SQLite transaction before
normal operations open the database.

### Implementation

1. Define exactly two migrations. Both use `CREATE ... IF NOT EXISTS` against a recognized
   pre-ledger layout; migration 2 deletes derived FTS rows before its single backfill so reruns cannot
   duplicate them.
2. Replace `_LocalStore.__init__` schema scripts, opportunistic `ALTER TABLE` helpers, and the direct
   version-1 bump in `_ensure_phase1_state_schema` with the local migration catalog. Set and inspect
   `PRAGMA user_version` only through the shared migration path.
3. Extend the local read-only inspector to distinguish absent, recognized legacy version 0,
   recognized pre-ledger version 1, supported ledger-backed version 1, current version 2, malformed,
   and newer state without mutating it. Keep every other database's migrations and `user_version`
   unchanged.
4. Add deterministic `.sql` fixture builders under `tests/fixtures/local-schema/` for the version-1
   W2, W3-only, and populated W4 layouts, plus recognized legacy version 0 and valid ledger-backed
   version 1. Tests materialize each database under `tmp_path`, upgrade it once, reopen it, rerun the
   migration harmlessly, and compare captures, imports, active search rows, FTS rows, and ordered
   search results before and after. Register the same public-text projection used by W3 as a bounded
   SQLite function so migration 2 joins each search row to its protected source reference and never
   backfills raw private text into FTS. Never commit a generated database file.
5. Keep Portable Brain `schema_version=1`, validate it during export, and include that value in
   `open-brain export --json` output. Do not export SQLite files, migration rows, or local schema
   version 2.

### W5 gate

W5 is usable when all three reachable version-1 no-ledger fixtures and both older-state cases are
recognized and upgrade without data loss, retry is a no-op, their post-upgrade record and result
counts equal the pre-upgrade counts, and their rebuilt FTS index returns the same expected results. A
malformed database and a synthetic version-3 database are refused without mutation. New databases
reach the identical version-2 schema, search and import still work, verified export reports Portable
schema version 1, and all prior checks pass.

No external release may occur before this gate is complete.

## `OB1-W6`: local MCP capture and search

### Implementation

1. Extract the smallest shared local application operations needed by CLI and MCP so neither adapter
   invokes the other and neither imports concrete SQLite internals.
2. Add a distinct default-product MCP adapter exposing only `brain_capture` and `brain_search`.
   Do not reuse or widen `EngineMcpAdapter`. Inject the existing non-owner capture-only sink for
   writes and the same whole-Brain retrieval task used by the CLI for reads; do not imply that one
   authority covers both paths.
3. Route `open-brain mcp` to the existing bounded JSON-RPC stdio transport without loading Secure
   Node composition. Add capture only for `--allow-capture` and search only for `--allow-search`;
   allow both flags together and reject a launch with neither.
4. Hash a supplied capture idempotency key into the MCP delivery namespace and bind it to the text
   payload. Reuse with identical text returns the original capture; reuse with different text returns
   a bounded conflict. Enforce the capture and search session limits before work.
5. Add protocol tests for initialize, tools/list, each capability alone, both capabilities together,
   neither capability, exact session limits, invalid arguments, oversized messages, EOF shutdown,
   redacted failures,
   and concurrent CLI/MCP access to the same Brain. Retain the existing
   `phase1_application.mcp_adapter(allowed_space_ids=frozenset())` deny-by-default contract and prove
   that its scoped search returns no results for an empty or non-matching grant after the extraction.
   Reuse the existing WAL mode and five-second SQLite busy timeout; contention must complete or return
   a bounded `database_busy` error without corruption or a hung process.

Update `README.md`, `docs/privacy-model.md`, and `docs/threat-model.md` with the OS-user/stdio trust
boundary, the capture-only write identity, the `--allow-search` whole-Brain read choice, the
`--allow-capture` durability and poisoning choice, the network-backed client egress decision,
untrusted-result handling, prompt-injection blast radius, session limits, accepted absence of
selective deletion in version 0.1.0, and explicit absence of actions, connectors, listeners,
user-managed grants, and Secure Node capabilities. Put capture-only, search-only, and combined client
configuration examples next to the README disclosures, and repeat both disclosures in
`open-brain mcp --help`.

Extend the installed-binary smoke after the required five-minute journey to run one
`--allow-capture` exchange and one `--allow-search` exchange. This remains part of the same two CI
jobs and does not change the five-minute timer.

### W6 gate

W6 is usable when an MCP client can capture and find a unique token in the same default Brain used by
the CLI, duplicate delivery is idempotent, simultaneous CLI and MCP writes either succeed or return
the bounded busy error, and each exact capture and search session boundary rejects the next call
without partial work. Each capability flag must omit and reject its unselected tool; the flags work
together, and neither flag yields a bounded usage error. The existing scoped MCP path must remain
deny-by-default for an empty grant and return no cross-space result for a non-matching grant. The
README configuration and CLI help must state at each choice point that search has whole-Brain read
authority, a network-backed client may exfiltrate returned content, automated capture is durable,
and version 0.1.0 cannot selectively remove an unwanted capture. Both CLI and MCP search results must
label MCP-written records `unverified` and carry public `source_origin=unknown` without a raw source
reference. EOF exits cleanly, no listener or runtime file remains, the native dependency audit stays
unchanged, and all prior checks pass.

## `OB1-W7`: contributor path

### Implementation

1. Add `make contributor-check` to `.PHONY` as `make verify` followed by `make homebrew-smoke`.
2. Update `README.md` and `CONTRIBUTING.md` with macOS and Linux prerequisites, exact commands,
   expected outputs, common failure messages, and confirmation that private release auditing is not
   required for normal contributions. Set the shipped package metadata URLs to
   `https://github.com/cbolden15/open-brain` and its README, issues, and changelog pages.
3. Make each of the two existing CI jobs invoke `make contributor-check`; do not add jobs, matrices,
   Docker, VM provisioning, caches outside normal GitHub Actions support, or release credentials.
4. Make the smoke generate a test-only, keg-only `open-brain-smoke` formula in its temporary tap and
   execute its unlinked binary by absolute prefix path. If a real `open-brain` formula is installed,
   snapshot its prefix, version, link, and binary digest and require them to remain unchanged. If it
   is absent, do not install a stand-in under the product name. Instead, test the smoke shell against
   a fake `brew` executable that begins with a modeled installed `open-brain`, records every command,
   and fails if the smoke links, uninstalls, or untaps that formula. Preserve startup cleanup and
   `EXIT`/`INT`/`TERM` teardown for smoke-owned state.
5. Update the product roadmap and acceptance status only after the exact commands pass from the
   checked-in documentation.

### W7 gate

W7 is usable when a new contributor can clone the repository, run the documented target, and obtain
the same successful checks as CI on macOS arm64 or Linux x86_64 without private access. The recorded
fake-Homebrew test proves the smoke never targets an existing product formula, a live installed
formula remains byte-for-byte unchanged when one is present, an interrupted-run test proves the next
run can recover, and both CI jobs pass at the exact goal-branch head.

## Final public release gate

Publication is an owner-authorized operation after W7, not another implementation workstream. GitHub
may produce its own release attestation as part of release immutability; Open Brain does not build or
require a separate attestation pipeline or verification gate. A defect or compromise is handled by
warning users, removing the affected formula from the supported install path, and publishing a new
version rather than replacing bytes under an existing version.

1. Reconfirm the `cbolden15` account's two-factor authentication, repository ownership, old URL
   redirect, collaborator access, open pull requests, Actions, secret scanning, push protection,
   branch protection, and release settings. Update the required checks on `main` to the final two CI
   jobs, keep force-push and deletion disabled, and enable release immutability. Confirm that
   `cbolden15/homebrew-tap` remains available, create it with only minimal documentation, and protect
   its default branch before any product release is published.
2. Freeze the goal-branch candidate commit. With the owner's untracked private denylist, run
   `PRIVATE_DENYLIST=/absolute/path make audit` and
   `PRIVATE_DENYLIST=/absolute/path make audit-history` from that exact commit. Only after both pass,
   close obsolete draft PR `#8`, merge the audited head to protected `main` without rebasing or
   squashing its history, and confirm both CI jobs pass on the resulting commit.
3. Build the two artifacts from that commit, ad hoc sign and verify the macOS arm64 executable before
   archiving, then archive both artifacts. Before hashing or publishing, run the artifact-aware audit
   with the same owner denylist:

   ```sh
   uv run python -m tools.open_brain_dev.release_audit \
     --root . \
     --private-denylist /absolute/path \
     --artifacts <macos-archive> <linux-archive>
   ```

   If it passes, compute the digests, assemble the exact-version manifest, and render
   `Formula/open-brain.rb`. No archive is hashed before its final executable is signed, and any
   rebuilt archive repeats this audit.
4. Create `v0.1.0` as a draft GitHub Release, attach both archives and the manifest, then publish it
   under release immutability. Download both public assets and require their SHA-256 values to equal
   both the manifest and formula values before committing the generated formula to the protected tap.
5. Run the exact public acceptance block on macOS arm64 and Linux x86_64, timing `brew install`
   through verified export and requiring each run to remain below 300 seconds. Only then change README,
   install, artifact, and acceptance statuses from unpublished target to released.

If publication reveals a formula or artifact defect, fix it in the smallest product or formula PR
and publish a new version. Do not restore the deleted release system.

## Verification matrix

Starting with W3, both CI jobs run `make verify` and `make homebrew-smoke`. W7 names that exact pair
`make contributor-check`; it does not weaken or add a separate CI path.

| Check | W3 | W4 | W5 | W6 | W7 |
|---|---:|---:|---:|---:|---:|
| Focused unit and integration tests | Required | Required | Required | Required | Required |
| Search relevance fixture | Required | Regression | Regression | Regression | Regression |
| Synthetic vault import | Not applicable | Required | Regression | Regression | Regression |
| Schema upgrade fixtures | Not applicable | Not applicable | Required | Regression | Regression |
| Installed MCP exchange | Not applicable | Not applicable | Not applicable | Required | Regression |
| Scoped MCP deny-by-default regression | Not applicable | Not applicable | Not applicable | Required | Regression |
| Native dependency audit | Required | Required | Required | Required | Required |
| `make verify` | Required | Required | Required | Required | Required |
| `make homebrew-smoke` | Required | Required | Required | Required | Via `contributor-check` |
| Two CI jobs | Required | Required | Required | Required | Required at exact head |
| `actionlint` and `git diff --check` | Required | Required | Required | Required | Required |
| Owner-only tree and history audit before merge | Required | Required | Required | Required | Required |

## Stop conditions

Stop the active workstream and revise its plan if:

- FTS5 is unavailable in either native artifact or requires a new runtime dependency;
- import cannot preserve exact source bytes without mutating the source vault;
- a migration cannot upgrade the committed fixture atomically and idempotently;
- the default MCP path loads Secure Node, opens a network listener, creates a background process, or
  exposes capture or search without its matching launch flag, or lacks a bounded outcome under
  concurrent local access;
- the public formula requires a second setup command or the verified-export journey exceeds 300
  seconds.

An embedding proposal, destructive vault pruning, source-root relocation feature, UI, hosted
service, Secure Node implementation, and release automation all require separate later decisions.
Immutable import revisions and idempotency keys have no automatic compaction in version 0.1.0; this
retention limitation is documented and measured in tests but does not add a cleanup subsystem.

## Expected effort

| Workstream | Focused estimate |
|---|---:|
| W3 search | 2 to 4 days |
| W4 import | 2 to 3 days |
| W5 migrations | 1 to 2 days |
| W6 MCP | 1 to 2 days |
| W7 contributors | 0.5 to 1 day |
| Public release gate | 0.5 to 1 day, assuming transfer permission and required personal-account features |

The expected total is 7 to 13 focused engineering days. Search relevance and import revision
semantics are the largest uncertainty. Each completed gate is independently useful, so a delay in a
later workstream does not invalidate earlier product value.

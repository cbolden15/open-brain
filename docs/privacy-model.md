# Privacy model

Open Brain trusts one local operating-system user. Every capture receives an immutable privacy
decision before persistence, but the application does not encrypt its own database or isolate data
from another process running as that user.

## Local storage

The Brain root, managed vault, SQLite databases, search indexes, Portable Brain exports, and imported
content may contain readable personal information. Open Brain uses owner-only paths where POSIX
permissions are available. Users remain responsible for account security, full-disk encryption,
backups, and physical access.

The live public-safe FTS5 projection is stored in `.open-brain/state/phase1.sqlite3`. The filename is
retained for compatibility. It does not indicate a running Phase 1 service. SQLite may use temporary
operating-system storage for sorter or FTS scratch.

## Offline outbox

The optional connectors outbox holds undelivered capture bodies as owner-only files inside an
owner-supplied directory, with owner-only permission modes where the platform supports them. It never
indexes or queries stored bodies: there is no search index, query interface, or model access, and
delivery is the only consumer. Status and drain output are metadata only, reporting states, counts,
capacity, and quarantine reasons without payload content.

## Capture privacy tiers and classification

Every capture receives one immutable privacy decision from the closed tier set `public`, `work`,
`personal`, `secret`, and `unknown` before persistence. Local capture and Markdown import keep
their existing default decision unless the owner passes an explicit tier.

The owner sets an explicit tier per capture with `open-brain capture TEXT --privacy-tier TIER` and
per import with `open-brain import DIRECTORY --privacy-tier TIER`. The flag is owner-only
authority on the local command line. A remote or destination-bound client never receives it, and
it is never merged with the startup-policy tier set that governs destination-bound submission.

One Markdown import invocation applies one fixed tier to every imported note unless the owner
supplies a per-root privacy manifest with `--privacy-manifest /absolute/path.json`. The manifest
is one JSON object of at most 65,536 bytes whose exact keys are repository-relative root paths and
whose values are tier names, for example `{"projects": "work", "journal": "personal"}` with
synthetic root names. Duplicate keys, absolute or traversing paths, overlapping roots, unknown
tiers, or an unreadable or non-object file are rejected with `invalid_privacy_manifest` before
any note is imported. Precedence is fixed: the most specific matching manifest root wins, then the
invocation tier, then the unchanged default decision.

A non-default import tier is part of the import revision identity. The delivery ID and the stored
revision digest incorporate the tier, so importing the same file again under a different tier
creates a new revision instead of a no-op. Default-tier imports keep the exact historical delivery
IDs and content digests.

## Boundary classification

The canonical admission boundary may rescan a submission and narrow its retained tier, for example
to `secret` on a detection finding or to `unknown` on a missing or invalid classification.
Narrowing is monotonic: no automatic or client-driven path widens a tier after submission. The
sole audited exception is the owner-only privacy repair ledger, which can move a fail-closed
`unknown` to a wider tier and leaves its own repair record.

The redaction-based boundary classifier, which reuses the deterministic work-tier redaction policy
as a secret signal, is opt-in and off by default. The engine wires no classifier into capture, so
default submissions keep their submitted decision unchanged.

## MCP

The owner explicitly launches `open-brain mcp` with capture, search, or both. Inherited stdio and the
invoking OS account are the trust boundary. EOF stops the process. No listener, token service,
daemon, connector, or background process is created.

Search grants the connected client whole-Brain read access. A network-backed client may send results
to its provider, even though Open Brain itself does not. Returned note content is untrusted data and
may contain prompt injection.

Capture uses a non-owner sink limited to durable, unverified text. Version 0.1.0 has no selective
deletion, session rollback, or certified purge. Session call and byte limits reduce accidental loops
but do not constrain hostile same-user code.

Workspace status, graph suggestions, and graph projection are separate read capabilities. Semantic
refresh is a separate launch capability, but version 0.1.0 returns `provider_not_configured` because
MCP exposes no provider-credential setup operation. MCP also cannot alter consent or exclusions,
accept a suggestion, resolve a conflict, or write an owner-authored revision.

## Managed vault and plugin

The dedicated `Open Brain Vault` is a human-editable sibling of the private Brain, not the canonical
store. Stable note identities and accepted revisions remain in the private Brain. Open Brain observes
and validates vault changes before accepting them. Conflicting edits preserve both candidates for
explicit review. The plugin is restricted to that vault and does not receive a SQLite handle.

The desktop plugin starts one normal-user `open-brain plugin` child and communicates through bounded
newline-framed stdio using `open-brain-client` protocol version 1. The bridge filters its inherited
environment and accepts only fixed operations with exact arguments. Disabling or unloading the
plugin closes the child. Neither side opens a listener or installs a background service.

## Semantic providers

Semantic graph refresh is the only active product path that intentionally sends note content over
the network. The owner must select OpenAI API, Anthropic API, or Google Gemini API, acknowledge that
eligible managed-vault notes may be sent, and grant current consent. Selection removes ineligible
or excluded notes and applies the cloud redaction check before credential resolution or HTTPS
dispatch. One operation uses one selected provider; failures do not fall back to another provider.

API keys may live only in the current plugin child or the operating-system credential store. The
plugin settings, managed vault, command arguments, logs, exports, and provider results do not contain
the raw key. Changing or removing provider selection revokes the matching consent. Session keys and
provider selection disappear with the plugin child. Data already sent to a provider cannot be
recalled by a later exclusion or revocation.

Claude subscription is not an active transport. The product reports
`subscription_isolation_unproven` and withholds note content because the required unprivileged,
tool-free client confinement has not been established.

## Markdown import

Import reads only the absolute source root selected by the owner, follows no links, loads no plugins,
and does not modify the source tree. Markdown, frontmatter, wiki links, embeds, HTML, and code remain
inert text. Imported records are unverified and their prior revisions remain in local history and
Portable Brain export after source changes or deletion. The import tier flags and per-root manifest
are described in [import design](import.md).

## Slack capture and recurring proposals

Slack user authorization is stored through the operating-system credential store; the collector
retains only an opaque account reference and has no plaintext fallback for unattended capture. The
owner selects channels explicitly. Activity and keyword discovery may surface pending suggestions,
but a suggestion is not a capture grant until the owner approves it.

Slack captures are local-only, third-party, unverified content. A recurring-note mapping is private
collector policy, and patch proposals contain only a bounded quoted update, source link, provenance,
target page ID, expected revision, and body-only edit operations. The collector has no review-decision
authority. A human must inspect and approve each proposal; target drift causes `review_conflict`,
and rejection does not modify the canonical page. Real-source acceptance must use a disposable Brain
and must not retain message bodies, account data, tokens, or raw receipts in repository evidence.

## Search projection

Public search applies an engine-owned projection before matching and again before representation.
It removes protected paths, credential-like values, source references, and digests while retaining
useful text. This limits accidental disclosure through search. It does not make every excerpt
non-sensitive or create compartment isolation.

## Graph representation

The Graphify helper is a separately packaged, normal-user foreground subprocess. It receives one
bounded snapshot of accepted managed notes over private stdio and returns structural links. It has no
network configuration, Brain root, SQLite access, or write operation. The app-private projection
cache and plugin-owned `Open Brain Graph.canvas` are rebuildable views. They are excluded from
semantic input selection and Portable Brain export.

Inferred suggestions record the selected source revisions, short evidence quotes, provider, and
model. Displaying an inferred edge does not edit Markdown. Only an explicit acceptance of a current
suggestion enters the normal revision flow and materializes a permanent link. Stale suggestions are
rejected.

## Excluded claims

Open Brain does not claim encrypted custody, multi-user grants, compartments, signed receipts,
fencing, cryptographic erasure, certified purge, or hostile same-user isolation. Those properties
require a separate product and conformance boundary. Archived Secure Node source does not add them
to the active distribution.

# OB1 native Obsidian and Graphify workspace

- Status: NW0-B1 closure/import proof passed with adapter constraints. NW0-C1 controls are mapped;
  the note-dispatch gate remains closed. Frozen packaging and remaining NW0 gates are unproven.
- Date: 2026-09-09.
- Baseline: `708e30c9d1e736969e2bb0aa6f0454a5f4804489` on `goal/open-brain-five-minute-install`.
- Planning branch: `docs/ob1-native-workspace-plan`.
- Scope authority: the requested native Obsidian and Graphify experience extends the existing
  product contract. This draft proposes changes; it does not silently rewrite accepted requirements.

## Outcome and scope

A desktop user installs Open Brain, opens its managed Obsidian vault, captures related but unlinked
notes, discovers an inferred Graphify connection, edits a note, retrieves the accepted edit, and creates a verified
Portable Brain export. The target is an ordinary elapsed time of at most 300 seconds on macOS arm64
and Linux x86_64 under a declared, reproducible starting state.

The full destination is a thin Obsidian desktop plugin backed by shared engine operations. A managed
vault is its foundation. Graphify produces a rebuildable view of accepted notes. Existing CLI and
MCP behavior remains usable independently of the editor. No required server or always-on engine
process is introduced.

Product roadmap confirmed by the user on 2026-09-09: Open Brain remains open-source software;
a cloud-hosted product offering is planned later. This integration milestone targets the OSS
desktop product. Cloud LLM inference in that product is separate from hosting Open Brain itself.
The future hosted offering does not make an Open Brain account or hosted backend a prerequisite
for this milestone. DECIDED by the user on 2026-09-09: both API-key and subscription access must
ship in the first release, covering OpenAI and Anthropic through their supported access paths.
The user also added Google Gemini API-key support on 2026-09-09. The first-release matrix therefore
contains five paths: three direct API adapters and two official-client subscription adapters.
Hosted-service implementation is outside this plan.

The initial planning milestone produced the plan, grounded constraints, and bounded feasibility
specification. The user's subsequent efficiency instruction authorizes applying the execution
guidelines and continuing bounded NW0 preflight within existing scope. It does not mark NW0 complete
or bypass the reviewed decision-record gate for NW1. CI pushes, application installation, release
publication, and repository-setting changes still require their applicable authorization.

### Proposed first-release decisions

| Decision | Working recommendation | Alternative and consequence |
|---|---|---|
| Native interface | Managed vault plus thin desktop plugin, with capture, search, refresh, and source navigation | A plugin-free vault and Canvas is a coherent intermediate milestone, but does not deliver the complete in-app experience. An export-only bridge does not meet this outcome. |
| Existing vaults | DECIDED by user on 2026-09-09: start with a dedicated Open Brain vault; preserve one-way import from existing vaults | Full two-way arbitrary-vault support is the strongest eventual integration, but adds relocation, deletion, duplicate identity, attachment, conflict, and sync-provider behavior. Plan it as a separate milestone rather than imply it ships. |
| First graph | DECIDED by user on 2026-09-09: inferred connections between previously unlinked notes are required from the start, using a cloud model initially | Provider setup and semantic processing belong inside the first-use acceptance boundary. Explicit links alone do not pass. Local inference through Ollama is deferred. |
| Installation clock | DECIDED by user on 2026-09-09: include installing Obsidian when missing, plugin activation, and chosen model-access setup; reuse existing installations and logins | Homebrew remains the declared prerequisite. An Obsidian-equipped-host-only measurement does not satisfy the selected first-use target. |
| Runtime packaging | Test a private self-invoked helper mode in the existing executable, with bounded JSON input/output and lazy Graphify loading | In-process integration has less process plumbing but shares failure/global state. A separate helper executable gives the strongest runtime/module boundary and requires an explicit archive-contract change. Select from NW0 evidence. |

These recommendations allow planning to proceed. Product-sensitive choices remain proposed until
resolved; no elapsed wait constitutes approval. The implementation gate is the completed NW0 record.
The first-graph decision supersedes the earlier explicit-links-first recommendation. The user also
selected cloud inference for the initial release, with local Ollama support later, and requires both
API-key and subscription access at launch. Credential onboarding and runtime packaging still need
feasibility evidence. This selects product architecture; it
does not establish the five-minute result or authorize transmitting any particular user's notes.

## Grounded starting point

The baseline contains the completed W5 migration runner, W6 local MCP adapter, W7 contributor
command, and bounded native auditing. Some older document status lines predate their merges; use
the baseline source and recorded merge evidence rather than reopening completed work.

| Existing seam | Verified behavior | Planning consequence |
|---|---|---|
| `packages/app/src/open_brain/services/local_operations.py` | Search invokes canonical reconciliation before retrieval | Extend the common operation boundary; do not implement independent plugin search semantics. |
| `packages/engine/src/open_brain_engine/engine/reconciliation.py` | Reconciliation updates search and space state from strict canonical Markdown; missing canonical pages fail validation | This is not a general vault synchronizer or immutable edit-history implementation. New revision, rename, delete, and conflict behavior needs explicit work. |
| `packages/engine/src/open_brain_engine/engine/markdown_import.py` | One-way import retains source revisions and keys files by registered root plus relative path | Preserve this contract. A rename-stable managed workspace needs a separate identity model. |
| `packages/engine/src/open_brain_engine/engine/portability.py` | Export enumerates validated `brain.toml`, `content/`, `history/`, and `sources/` material | External workspace edits must become accepted portable records before export. Local paths, locks, caches, and plugin settings are not portable content. |
| `CLAUDE.md` and native release tooling | Engine cannot import app/connector/legacy/workspace packages; release currently contains one executable | Keep domain contracts in the engine and inject external adapters at app composition. Any plugin/helper artifact changes the release contract explicitly. |

Graphify commit `3f82bf7f837a07fb0f7668fbdbd5662801906942` and package version
`graphifyy 0.9.57` were verified during read-only grounding. Wheel-to-source equivalence, dependency
closure, license-notice collection, and runtime compatibility remain NW0 checks. Do not rely on the
mutable upstream branch name as a release pin.

## Proposed architecture contract

### Dependency direction

The CLI, MCP adapter, and Obsidian plugin reach common local operations and `EngineTaskSet`.
New engine contracts describe workspace reconciliation and graph requests/results using neutral
types. Engine code owns accepted records and transaction decisions. It must not import Obsidian,
Graphify, app, connector, legacy, or a separate workspace package.

The app composition layer supplies filesystem/editor integration and a narrowly scoped Graphify
adapter. The plugin uses bounded CLI/stdio operations, never direct SQLite access. Preserve caller
authority: invoking a shared method does not convert an MCP automation capture into owner-authored
canonical content. A rejected or unverified source must retain its provenance in workspace and graph
presentation. The workspace visibility policy must define how eligible captures appear without
bypassing review or canonical publication rules.

Proposed command families are `workspace` and `graph`; exact flags and JSON schemas are defined in
NW1. Do not publish example commands as working before their adapters and contract tests exist.

### Caller capabilities and MCP limits

Shared engine methods do not imply identical exposed capabilities. Preserve the existing MCP
capture/search flags, limits, and non-owner provenance. The following new flag names are proposed
contracts, not implemented commands; NW0 freezes their names and semantics.

| Operation | Owner CLI / Obsidian plugin | MCP at launch |
|---|---|---|
| Existing capture and search | Existing owner operations | Existing `--allow-capture` / `--allow-search` behavior and budgets remain unchanged. |
| Workspace status and graph suggestions | Available for the selected Brain | Absent unless `--allow-workspace-read` is supplied; engine-owned public projection only, without raw paths, policy receipts, or credential metadata. |
| Semantic graph refresh | Requires current owner consent, provider selection, and content eligibility | Absent unless `--allow-graph-refresh` explicitly delegates refresh to that process; also requires the same current content/provider consent. Existing flags alone never authorize inference. |
| Accept links, resolve conflicts, delete or restore notes | Explicit owner actions through revision acceptance | Not exposed at launch. MCP capture cannot impersonate these owner actions. |
| Change consent, exclusions, credentials, or provider | Explicit owner configuration | Not exposed at launch. A refresh flag cannot select another provider or broaden content scope. |

Provisional new MCP process limits are 500 workspace-read attempts with 16 MiB of aggregate
serialized response data, 20 refresh requests, 40 actual model attempts, and 1 MiB of aggregate
selected note input across those attempts. Count valid failed/duplicate requests in the request
counters and dispatched retries in the model-attempt counter; invalid arguments do not count.
Check request limits before engine work and reserve input/attempt
capacity before dispatch. Byte-limit failures return a bounded error, not a partial disclosure.
NW0 may lower these starting limits from evidence; increases require an explicit recorded decision.

All callers also share a per-workspace inference queue, concurrency limit, and request/usage budget
owned by the engine. Restarting an MCP process must not reset those shared limits. NW0 freezes
their release values from initial/incremental measurements. Exhaustion pauses inference with a
visible reason; it does not switch billing modes or providers. Disabled tools must be absent from
MCP discovery and rejected if invoked by name. Test those cases and prove limit failures produce
zero provider calls, without granting MCP owner mutation authority.

### Filesystem and authority

Recommended physical layout: a dedicated managed vault beside the private Brain directory within
the product's platform-local application directory. The private Brain keeps accepted canonical
records and history. The managed vault is the editable presentation. Neither side is an
unconditional overwrite source. Persist a mapping from stable note ID to workspace-relative path,
last accepted revision, and last materialized body digest.

The default location is automatic; the introductory user flow does not ask for a storage backend or
root. Setup creates or reopens the dedicated Open Brain vault; it does not register an arbitrary
existing vault for two-way editing. One-way import preserves the existing import/provenance contract
and does not modify the source vault or establish bidirectional synchronization. This product
decision leaves the sibling-projection versus dedicated-canonical-subtree packaging question to NW0.
A human-facing open/reveal operation can show the vault location without exposing operational
paths to MCP or public search results. Validate root identity and containment on every operation.
Do not use a symlink from the vault to the private canonical tree. Preserve private permissions and
the current rejection of known synchronized/network storage until a separate policy supports those
locations; an Obsidian workspace does not itself imply Obsidian Sync compatibility.

The alternative is a dedicated canonical subtree used directly as the vault. It reduces duplicate
files but couples Obsidian metadata, flexible frontmatter, rename/delete behavior, and generated
artifacts to Portable Brain's strict namespace. NW0 must compare it against the sibling projection
before freezing placement. Opening the entire Brain root is rejected.

### Revision acceptance and recovery

Every mutating workspace request carries a stable note ID, a base revision/digest, and an idempotency
key. The engine records accepted revisions using an explicitly validated portable representation.
Operational paths and reconciliation bookkeeping remain local. Do not invent new v1 portable fields
or record families without a versioned compatibility decision.

Current automatic canonical capture allocates a new page ID and binds a proposal, decision, and
publication to its bytes. Portable v1 permits a nonempty set of capture provenance references, but
current reconciliation additionally requires exactly the search row's single capture reference.
NW0 must prove a same-page edit operation and its replay/import behavior before selecting schema:
retain the page ID, preserve each accepted edit's evidence, and update the stronger runtime
assumptions consistently. Merely recapturing edited text or changing provenance frontmatter does
not satisfy this contract. Review portable import reconstruction as well as export validation.

| Observed state | Required result |
|---|---|
| Workspace changed; engine base unchanged | Validate source identity and metadata, accept one revision, update search, mark graph stale. |
| Engine changed; workspace still matches its last materialization | Publish the new presentation only after verifying the target is still unchanged. |
| Both changed | Preserve both versions and return a conflict; no last-write-wins overwrite. |
| Duplicate identity, malformed metadata, unsafe path, or uncertain traversal | Refuse the affected mutation, return a bounded diagnostic, retain accepted data. Do not infer deletion from an incomplete scan. |
| Crash between accepted record and materialization | Resume from an explicit pending operation, bind it to the original request, and avoid duplicate revision creation. |

DECIDED by the user on 2026-09-09: when Obsidian and Open Brain both change a note, preserve both
versions and ask the user to resolve the conflict. Do not automatically merge or choose a winner
at launch. Persist the conflicting versions and their base revision so restarting cannot discard
either side. Show both versions and let the user select one or explicitly compose a resolution.
Resolving a conflict uses the normal revision-acceptance operation, checks that the reviewed
versions are still current, and records one idempotent accepted revision. Further edits require
renewed review rather than overwriting a newer version.

Limit the pending conflict to the affected note. Other notes remain usable. Search and graph views
may retain the last accepted revision with visible conflict status; the unaccepted conflicting
version must not enter semantic inference or be presented as accepted content. Store conflict
artifacts outside normal source enumeration. Export must not silently omit unresolved edits or
claim a fully reconciled workspace; retain the existing fail-visible reconciliation requirement.

Filesystem replacement and SQLite commit do not form one atomic transaction. NW1 needs a small
recoverable operation journal and defined reconciliation states. External editors do not honor the
engine's writer lease; a check followed by rename alone is not a proof against concurrent edits.
NW0 must demonstrate the chosen conflict-preserving write-back protocol under an edit during
promotion, or restrict automatic write-back until that protocol is proven.

Managed-vault rename preserves the stable note ID. A copied ID is a conflict, not a second alias for
the same writable record.

DECIDED by the user on 2026-09-09: deleting a note in Obsidian removes it from the active workspace,
search, and graph while retaining recoverable history. Permanent erasure is a separate explicit
action; an editor deletion never authorizes it. Record deletion as a durable inactive state under
the stable note identity. Reconciliation must not silently recreate the note, and stale graph jobs
must not republish it. Exclude inactive notes from semantic input and related-note context retrieval.
Distinguish a confirmed deletion from a rename, inaccessible root, partial scan, or concurrent edit;
uncertain or conflicting observations preserve data and require reconciliation.

Provide an explicit restore operation that preserves identity and retained provenance, checks for
path/identity conflicts, and returns the restored note to active search and graph processing.
Verified export/import must preserve both retained history and inactive status without resurrecting
deleted notes. NW0 must prove deletion, restart, restore, and export/import behavior in the selected
portable representation before freezing the schema. This decision does not add a permanent-erasure
implementation milestone.
Initially do not claim filesystem trash, arbitrary vault relocation, external sync conflict repair,
or attachment synchronization unless covered by an explicit acceptance test.

### Graph projection

The narrow upstream callable is `graphify.extractors.markdown.extract_markdown(Path)`, but importing
it runs the extractor package initializer, which loads other extractors. Its full-vault wiki-link
lookup depends on root context managed by the larger extraction facade. The candidate root-aware
path is `graphify.extract.extract(paths, root=..., cache_root=..., parallel=False)` with selected
Markdown paths only. It checks tree-sitter and retains a broad declared dependency closure. A direct
module import is not proof of a minimal Markdown-only installation.

Test this path in an app-owned, self-invoked private worker mode, with no inherited provider
credentials, bounded request/output, private cache, and a deadline. This separates process globals
and failure handling but does not remove Graphify modules from the executable. A separately packaged
helper is the stronger module boundary if lazy loading and inventory rules cannot preserve the
base path. Do not restore excluded legacy/connectors to reach the integration.

Reconcile eligible notes, form a consistent snapshot, and run deterministic extraction on that
snapshot. Record a snapshot digest, accepted revision references, adapter version, and selected
Graphify version. Map upstream path-derived references back to Brain IDs. Resolve links according to
the supported relative paths, wiki links, aliases, and heading anchors; ambiguous and unresolved
links remain visible diagnostics rather than guessed identities.

Private engine state, retained source history, `.obsidian`, and generated graph outputs never enter
source enumeration. A Canvas displayed inside the vault is still generated content and excluded.
Graphify's generated Obsidian-node export must not replace original notes or become import input.

Publish graph output only after a complete successful build. Retain the last successful generation
on failure and mark it stale. Bound selected file count, bytes, extraction time, output size, and
subprocess lifetime if used. Set exact graph limits from the NW0 fixture and measured packaging
results; do not silently inherit a broad upstream recursive scanner.

Render without a CDN or required HTTP server. Treat note titles, URLs, and markup as untrusted
presentation data: escape HTML, disallow executable navigation schemes, and validate local note
targets. Structural extraction needs no model. User acceptance of inferred relations needs durable
provenance outside the disposable graph cache.

Structural extraction is a baseline and fallback, not sufficient first-use acceptance. Add a semantic
extraction stage using the selected cloud-model path. Record the model/version,
input revisions, and extraction provenance; distinguish inferred relations from explicit links.
Show source evidence for a suggested connection. The same stable-ID and bounded-input rules apply.
Do not treat a missing provider or failed inference as a successful first graph. Preserve headless
capture/search and the structural fallback when inference is unavailable.

DECIDED by the user on 2026-09-09: show inferred connections in the graph as soon as a valid
semantic result is published; add permanent note links only when the user accepts a suggestion.
Clearly distinguish suggested edges from explicit note links and expose the source evidence.
Automatic refresh must never rewrite notes or silently promote a suggestion to an accepted link.

Acceptance invokes a shared engine operation with stable endpoint IDs, the reviewed suggestion
and evidence, expected note revisions, and an idempotency key. Present the intended note edit
before acceptance. Revalidate endpoints and revisions; a changed source requires renewed review
rather than applying an outdated edit. Use the normal conflict-preserving revision flow to add
the approved link without duplicating an existing link or repeated acceptance. Record the user's
acceptance and inference provenance durably, preserving the distinction between model output and
the user's decision. Accepted links and their required provenance must survive graph-cache removal
and Portable Brain export/import. NW0 must prove the portable representation and select the exact
link placement and direction before implementation.

Keep the semantic request/result contract independent of a provider SDK so a later Ollama adapter
can reuse input selection, provenance, and graph handling. Implement the required cloud access paths
in this release. Ollama installation, local model downloads, hardware sizing, and local inference
acceptance are deferred; do not add them to initial setup. Configure which selected content reaches
the provider and bind credentials to the intended adapter rather than inherit ambient credentials.
Credential storage and cloud usage controls must be resolved for each required access path.

DECIDED by the user on 2026-09-09: cloud inference includes all eligible notes in the managed
vault by default, with folder and individual-note exclusions available during setup and afterward.
Show this scope when the user selects a provider. Eligibility still requires accepted content and
the existing source/trust rules; it does not include unrelated vaults, private Brain internals,
history, or generated artifacts. Cloud exclusion does not remove a note from local saving, search,
or portability.

Apply exclusions in the shared semantic input-selection boundary for automatic and manual refresh,
including retries and related-note context retrieval. Recheck the current policy before dispatch;
an excluded record must not re-enter a request through a cached excerpt or candidate lookup.
Persist individual-note exclusions by stable identity so renaming a note does not clear them;
evaluate folder exclusions against the current managed-vault-relative path. Policy changes invalidate
pending selections and affected semantic projections. Already transmitted data cannot be recalled;
stop further dispatch of newly excluded records and reject results based on superseded policy.

Start the offline feasibility proof with Canvas and an explicit source-filename mapping. The
inspected HTML renderer references a CDN-hosted vis-network asset; package that asset locally before
claiming an offline HTML view. Upstream export imports also bring in broader graph modules, so
measure the complete import closure rather than only the function body. Include Graphify's
`LICENSE`, `LICENSE-MIT`, and `NOTICE` plus required dependency notices in the candidate inventory.

### Cloud consent and effective request policy

The existing `_privacy()` default, Markdown imports, and MCP captures record local-only authority.
Setup must create an explicit owner consent record before enabling inference; successful login or
an included folder is not that record. Record the owner, selected provider/access mode, permitted
operation, managed-vault scope including eligible existing and future notes, timestamp, and policy
generation. Note and folder exclusions remain editable and apply to every caller.

Preserve immutable capture privacy decisions. At dispatch, the engine creates a new effective
request decision backed by current owner consent and the selected source revisions; it must not
edit old records, flip their authority bits, or misuse `Authority.narrow` to broaden them.
For personal content, use the existing `PERSONAL_CONFIRMED` semantics with a resolvable
`confirmation_ref`. NW0 must prove the durable consent representation and its relationship to
existing privacy records before freezing the schema or using this boundary in production.

| Source state | Semantic dispatch rule |
|---|---|
| Accepted personal content with only the default local-only decision | Current owner consent may authorize a new request decision, including eligible imported notes; retain the original decision and source provenance. |
| Secret, unknown/ambiguous privacy, explicit local-only restriction, or insufficient source trust | Deny inference until a separate valid owner policy/review operation resolves the restriction. Setup's blanket scope does not override it. |
| Excluded, inactive, conflicting unaccepted, or revoked content | Deny selection, context retrieval, and retries; invalidate pending requests/results affected by the changed policy. |
| A request combining several notes | Every source must independently pass. Remove denied candidates before constructing the prompt; reject any final request retaining a denied source. Consent for one note never authorizes its neighbors. |
| Restored or imported policy state | Preserve restrictions and audit provenance; positive consent is inactive on the destination until its owner reviews scope and selects provider access. No automatic cloud dispatch during import. |

NW1 owns durable consent/revocation records, identity exclusions, folder rules, policy generation,
and shared budget state. Portable export must preserve stable-ID restrictions and consent audit
provenance in the NW0-approved representation. Local folder settings and credentials are not
portable; carry the effective per-note exclusions for existing notes and require destination scope
review before enabling inference for future notes. Never include API keys or official-client
sessions in exports. A policy change retains historical evidence without authorizing future calls.

All five adapters must pass the final assembled prompt, including retrieved context, through the
existing versioned cloud redaction/canary check before resolving inference credentials or launching
a completion client. Separate login/status operations carry no note content.
Reuse the behavior in `providers/base.py`; any replacement needs an explicit policy decision and
equivalent tests. A finding fails closed with a bounded diagnostic and no provider fallback.
Public MCP results still use the engine's public projection. NW0 must separately define the
owner-local graph representation; operational digests and internal references stay in bookkeeping.

### Model access and existing-code reuse

Both API-key and subscription access are first-release acceptance requirements. The earlier
recommendation to defer Claude subscription access is superseded. Use one bounded semantic
request/result contract with separate transports:

| Required access path | Integration boundary |
|---|---|
| OpenAI API key | Direct provider API adapter; usage billed to the user's API account. |
| ChatGPT subscription | Official Codex runtime with provider-owned login and credential refresh. |
| Anthropic API key | Direct provider API adapter; usage billed to the user's API account. |
| Claude subscription | Unmodified Claude Code with the user's own provider-managed sign-in, under the applicable product-integration conditions. |
| Google Gemini API key | Direct Gemini API adapter using the user's Google AI Studio API key and project quota/billing configuration. |

Gemini uses the same semantic schema, consent, redaction, exclusion, timeout, and usage contracts.
NW0 pins a supported Gemini endpoint/model and verifies its supported JSON Schema subset; validate
the returned result in Open Brain regardless of provider-side schema enforcement. Use a currently
supported key type and pass the selected key explicitly so ambient Google credentials cannot change
the account. Compare direct HTTP with the official SDK against the existing native dependency budget.
Google documents [API-key authentication](https://ai.google.dev/gemini-api/docs/api-key) and
[structured output](https://ai.google.dev/gemini-api/docs/structured-output); these are grounding
references, not proof of the candidate's native compatibility or semantic quality.

DECIDED by the user on 2026-09-09: setup detects an existing Codex or Claude Code login and offers
to use it. Query each installed official client's supported authentication-status interface with
a bounded timeout; do not inspect or copy its credential cache. Detection itself must not submit
notes or run billable inference. Show the detected provider and access mode, then let the user
choose it before transmitting selected content. When both clients are available, offer both;
keep API-key setup and fresh official-client sign-in available. Missing, expired, or indeterminate
authentication must produce an actionable setup state rather than imply the account is ready.
Do not treat successful login detection as proof of remaining quota or model availability.

Read-only inspection of the existing agent-config workflow runtime found reusable authentication
mode selection, sanitized child environments, provider readiness checks, structured output,
cancellation, attempt limits, and model attribution. Its `src/auth.ts`, `src/adapter.ts`,
`src/adapters/codex.ts`, `src/adapters/claude.ts`, and associated contract tests are extraction
candidates, not drop-in public dependencies. The current Claude adapter uses the Agent SDK;
its subscription mode alone does not demonstrate the required supported-client integration.
The custom router's default upstream forwarding retains incoming authorization headers. Do not
carry subscription-token relay or personal credential-file conventions into the public product.

Prefer a small Python implementation of these boundaries for Open Brain's native distribution.
First compare extension/composition of the existing `core/ports.py` `TextModelRequest`,
`TextModelResult`, and `Provider` contracts with the agent-config adapter patterns. Reuse their
input/output bounds and privacy boundaries where compatible; the current secret-resolver-based
ProviderService is not a drop-in subscription adapter.
The strongest shared-maintenance alternative is a separately licensed provider package used by
both projects; compare its Node/runtime packaging cost before selecting it. Neither approach
requires adopting the whole workflow orchestrator or a persistent router service.

NW0 must provide a thin proof of all five paths on both platforms using synthetic notes: explicit
access selection, valid semantic output, attribution, privacy rejection, and isolation for the
subscription paths.
NW2/NW3 own the exhaustive fresh/existing login, neither/one/both-client detection, cancellation,
expired-authentication, quota, and recovery matrices before their exits. Never silently switch from
subscription to paid API usage or to another provider. Define an overall operation deadline across
retries and fallback attempts.
Keep subscriptions owned by the official clients; Open Brain must not collect their session tokens.
Required client installation and authentication count toward the selected path's setup clock.
Runtime or policy obstacles require an explicit architecture resolution, not quietly dropping a
required access path from the release.

NW3 owns credential onboarding and lifecycle for all five paths. API keys belong in an existing
OS credential store where available, with session-only entry when it is unavailable; do not store
keys in vault files, plugin settings, process arguments, exports, or logs. NW0 verifies the concrete
macOS/Linux store interface and packaging impact before selecting it. Keep provider-specific
references in private app settings, support explicit replacement/removal, and leave official-client
login/logout to the client. Switching provider/access mode requires a fresh owner selection and
policy generation; never reuse another adapter's credentials implicitly.

### Subscription completion isolation

Run Codex and unmodified Claude Code as bounded, tool-disabled completion transports. Supply only
the selected, policy-checked note context through private stdio; do not put note text or credentials
in process arguments. Use an app-owned scratch directory outside the Brain/vault. Disable ambient
instructions, hooks, skills, plugins, MCP connections, session resumption, and persistent prompt
history. Neither model may invoke filesystem, shell, browser, network, or subagent tools. The
official runtime may use its own authentication store and provider connection; this does not grant
model/tool access to the host, home directory, Brain, or unrelated source trees.

NW0 selects and records exact client versions and supported launch controls, including managed
policy behavior, environment filtering, output bounds, process-tree cancellation, and transcript/log
retention. Claude bare mode alone is unsuitable for subscription login; safe mode plus separate
tool/MCP and persistence controls is a candidate, not a proven recipe. Verify equivalent controls
for Codex. Reject incompatible client versions/configurations before submitting note content.
Preserve all provider-supported authentication methods; do not modify clients or relay their tokens.

NW0-C1's [installed-client audit](../audits/2026-09-09-ob1-nw0-c1-client-isolation.md) found that
Codex's complete tool catalog and both clients' effective managed-policy boundaries remain open.
Claude's offline diagnostics showed system-tool context with schema mode enabled despite empty-tool
flags. The next strict tool-free candidate requests JSON text and applies the shared engine validator;
schema-valid accepted results and the existing quality requirements remain unchanged. Neither client
is approved for note dispatch by that offline result.

NW0 must use synthetic hostile notes, excluded-file canaries, and controlled ambient customizations
to prove no tool effects, unrelated context loading, or extra egress. If tool-free execution cannot
meet the boundary, test the existing staged-asset execution contract or an independently verified
equivalent with explicit readable assets and bounded network authority. Record that architecture
decision before proceeding; do not bypass confinement or defer a required subscription adapter.

Provider references checked on 2026-09-09: [Codex authentication](https://learn.chatgpt.com/docs/auth),
[Codex app-server integration](https://learn.chatgpt.com/docs/app-server), and
[Claude Code product integration and credential requirements](https://code.claude.com/docs/en/legal-and-compliance).
Launch-control references: [Claude programmatic execution](https://code.claude.com/docs/en/headless)
and [CLI controls](https://code.claude.com/docs/en/cli-reference). These document candidates; NW0
must verify the controls in the selected installed clients.

### Obsidian desktop surface

The plugin provides capture, search, graph refresh, and open-source-note actions. Invoke the resolved
Open Brain executable with argument arrays and a versioned JSON contract. Do not interpolate note
text into shell commands. Bound process time/output and make missing/incompatible binaries visible.
Close child processes and unregister listeners when the plugin unloads.

DECIDED by the user on 2026-09-09: inferred connections refresh automatically while Obsidian is
open, batching nearby edits, with pause and manual-refresh controls. After provider selection,
editor events request debounced reconciliation and enqueue semantic refresh only for accepted
changes. The engine still rescans and validates; an event is a hint, not authority. A reload or
dropped event must not lose edits. Select the debounce interval and maximum batch delay from NW0
latency and usage measurements; continuous typing must not create unbounded work or starvation.

Coalesce pending changes and bound in-flight inference per workspace. Changes arriving during a
run remain pending; an older result must not replace a graph for newer accepted revisions. Mark
the graph stale until its input revisions match the accepted state. Generated graph files must
not trigger another inference cycle.

Pause stops automatic semantic scheduling without stopping note saves, reconciliation, or search.
Persist the pause preference across plugin reloads. Manual refresh requests one bounded run using
the latest accepted state, including while paused, without re-enabling automatic refresh. Repeated
requests coalesce. Plugin unload cancels owned work and leaves any unfinished projection visibly
stale; reopening reconciles changes and resumes scheduling only when automatic refresh is enabled.
A long-lived MCP server must not hold a writer lease that prevents plugin/CLI operations, nor may
a plugin keep a session alive solely for background sync.

Plugin installation and activation must respect Obsidian's actual trust/activation flow. Stage only
owned plugin assets and preserve existing vault settings. Do not silently enable arbitrary community
plugins or assume an Obsidian URI can activate one. NW0 determines the supported distribution path
and the effect of first-run prompts on timing. Desktop support does not imply mobile support.
Set `isDesktopOnly: true` because the native-process bridge uses Node.js desktop APIs.

For the public product, plan a Community-directory plugin release with compiled `main.js`,
`manifest.json`, and optional `styles.css`, plus an explicit Open Brain compatibility range. Initial
directory approval is an external release dependency, not something a passing CI job establishes.
Manual asset staging is a development/beta path unless the public setup contract explicitly selects
and measures it. Plugin code must not self-install external dependencies; application/runtime
installation belongs to the approved product installation flow.

Test binary discovery from the GUI environment rather than assuming a terminal's `PATH`. Verify
macOS and Linux Homebrew locations, URI registration after first launch, and a manual open-vault
fallback. Register event cleanup, delay startup event handling until layout readiness, and use
Obsidian's supported atomic note processing where relevant. This reduces editor-local races but
does not make cross-process filesystem and SQLite writes atomic.

## Ordered implementation milestones

One coordinator owns edits and git state. Use one focused branch/PR per milestone, splitting a large
milestone into ordered PRs when reviewability requires it. Start subsequent work from the merged
goal branch. Read-only grounding and reviews may run in parallel. No runtime implementation starts
as part of this planning change; disposable NW0 probes are distinct from product implementation.

### Execution order and efficiency

These guidelines govern remaining work. Preserve completed W5–W7/readiness evidence, planning,
review, and the five incorporated corrections. The NW0 decision record tracks new evidence without
restarting those milestones or resetting experiment budgets.

1. Resolve the expensive risks first. Start NW0-B packaging and NW0-C official-client isolation
   before substantial UI work. NW0-A may establish the minimum snapshot, consent, and revision
   contracts in parallel with read-only investigations. Complete the thin proof for OpenAI API,
   Codex subscription, Anthropic API, Claude subscription, and Gemini API on both targets before
   NW0 exits. Limit early presentation work to the NW0-D feasibility comparison; UI polish waits
   until packaging and both subscription boundaries have passed.
2. Build one complete vertical slice: capture → infer connection → display source evidence →
   explicitly accept a permanent link → verified export. Use a deterministic fake provider against
   the shared contract during development, including consent denial, exclusions, stale revisions,
   and idempotent acceptance. Inference alone must leave note bytes unchanged. NW0 prototypes the
   flow with disposable code; NW1 implements its engine operations; early NW2 connects the minimal
   presentation and exercises real adapters through the same flow before broadening UI in NW3.
   Fake-provider success is development evidence only. All five real paths remain required.
3. Reuse existing EngineTaskSet operations, provider interfaces, privacy checks, and Portable Brain
   records before adding contracts. Inspect agent-config authentication, isolation, cancellation,
   attribution, and attempt-budget code and tests before writing equivalents; record what is reused
   and what fails this product's boundary. Keep the orchestration runtime out of the distribution.
   Use one semantic dataset with format-specific fixtures and one parameterized adapter contract
   suite across all five paths, plus focused provider-specific tests.
4. Keep the fast loop on this Mac. Use existing Linux x86_64 CI for builds/tests and the agreed UTM
   guest for Linux GUI checks. Run focused tests during iteration, then every required project
   check at milestone completion. Repeat slow desktop journeys at NW0-D, integrated NW2/NW3,
   and final NW4 checkpoints, or when a changed boundary invalidates their evidence. Label emulated
   timings and compare baselines within the same environment. Effort budgets are stop limits.
5. Keep one coordinator responsible for edits, integration, and git state. Bound parallel read-only
   investigations/reviews by question, effort, and output. Before delegating implementation, agree
   on interfaces and assign disjoint file ownership. Integrate small working changes frequently.
   Every new abstraction, dependency, or setting must name an agreed requirement or demonstrated
   constraint. Present architectural tradeoffs, including the strongest boundary option, before
   selecting a shortcut. No shortcut removes a launch path or weakens a privacy/acceptance gate.

### NW0: resolve contracts and prove feasibility

Output: one reviewed decision record, reproducible synthetic fixtures, and disposable experiment
code/evidence. The planning timebox is 32 engineer-hours, allocated below; this is a stop limit,
not a completion estimate. Stop an experiment at its limit, record what remains unproven, and
replan explicitly. Do not quietly reduce a launch requirement or expand the spike into NW1–NW3.

Track probe status, pass/fail criteria, and the smallest next experiments in
[the NW0 decision record](2026-09-09-ob1-native-workspace-nw0.md). Metadata availability, existing
base-product CI, and source inspection do not prove Graphify packaging or subscription isolation.

The coordinator owns the experiment ledger, fixtures, environment inventory, and evidence. Confirm
the actual host/VM identity, OS version, desktop session, package source, provider account access,
and network conditions before the dependent experiment. DECIDED by the user on 2026-09-09: use
the current arm64 Mac for desktop checks on both targets. Run macOS checks in an isolated test
account and Linux GUI checks in a separate UTM guest emulating an Ubuntu 24.04 LTS x86_64 GNOME
desktop; existing native Linux x86_64 CI supplies Linux builds/tests. This supersedes
the earlier native-hardware or hardware-virtualized x86_64 environment requirement. UTM supports
[x86/x64 emulation on Apple Silicon](https://mac.getutm.app/), with reduced performance.
Read-only inventory found UTM and an existing arm64 guest; the dedicated x86_64 guest has not been
created or verified. Provisioning and verifying that desktop remain entry dependencies for Linux
GUI experiments. Preserve the existing guest. Record the selected host, guest architecture, UTM version,
CPU/memory/disk configuration, desktop session, and execution location in the private experiment ledger.
Use the emulated desktop for Linux GUI evidence; label its timing results as
emulated and make no inference about native x86_64 performance from them.

Homebrew remains the Open Brain lifecycle. Test an official macOS Obsidian package/cask and the
official x86_64 Debian package on the selected Ubuntu desktop, recording installation and URI
registration behavior. Record exact official-client versions/distributions and credential-store
availability; do not assume Node/npm is required or already installed. Installation and network
actions occur only under the authorized NW0 execution scope, using synthetic notes.

| Experiment | Hypothesis and maximum effort | Required artifact and stopping point |
|---|---|---|
| NW0-A: durable state and privacy | 8 hours: the dedicated vault can preserve identity, revisions, restrictions, and owner decisions using a compatible portable representation | Compare sibling projection with a dedicated canonical subtree. Demonstrate one create/edit, edit conflict, delete/restart/restore, accepted link, and export/import sequence. Prove consent/revocation, mixed-source selection, canary rejection, and restored consent remaining inactive with local test doubles. Stop on unresolved schema/authority semantics; do not build the full editor. |
| NW0-B: packaging and startup | 8 hours: Graphify can be bounded without regressing the base executable | Pin source/wheel/dependencies; build both targets and inventory the full closure. Compare in-process, self-invoked, and separate-helper candidates. Record archive/expanded sizes, signatures, cold/warm startup samples, and failure cleanup. Stop a candidate that fails package or startup limits. |
| NW0-C: five inference paths | 8 hours: all five access paths can implement one bounded semantic contract | For each path on each platform, record exact client/API/model versions, explicit auth selection, structured-output validation, initial/incremental latency, and usage. Prove subscription isolation with hostile notes and ambient-config canaries. Stop affected dispatch on privacy, confinement, attribution, or quota failure. |
| NW0-D: Obsidian presentation and setup | 6 hours: the chosen desktop integration can activate and navigate within the product journey | Run one installation/activation/source-navigation journey per desktop. Compare Canvas and local HTML against evidence display and link acceptance; select the shipping surface(s) explicitly. Record prompts, cold versus reused setup timings, and runtime downloads. Do not substitute hosted CLI CI for GUI evidence. |
| NW0-E: decision record and coverage | 2 hours: every architecture-changing question has evidence and an implementation owner | Freeze note identity, revision/link/deletion compatibility, conflict promotion, consent/credential lifecycle, exclusions, MCP capabilities, shared budgets, package closure, startup bounds, presentation/protocol, and timed-journey scope. Map unresolved failures to a stopped experiment rather than declare NW0 passed. |

Use one bounded dataset with related unlinked notes, an unrelated distractor, cross-folder links,
and duplicate basenames. Separate hostile/redaction canaries from the eligible semantic corpus.
Start with at most 16 KiB of selected note input and 16 KiB of accepted output per model attempt,
60 seconds per attempt, two attempts and 90 seconds total per probe. Reserve capacity before each
dispatch: at most 80 model attempts across NW0, and 16 per access path, including retries. Stop on
the first exhausted limit. These are experiment limits, not advertised provider billing caps.
Credential entry, consent tests, and denied canaries must not spend an inference attempt.

For NW0-C, run three initial-graph samples and one incremental sample per path/platform, reserving
the remaining attempt budget for isolation checks and bounded recovery. Each accepted suggested
edge must resolve to input IDs and source evidence; require the known related connection in at
least two of three initial samples and no accepted edge to the unrelated distractor. Record failures
without choosing favorable runs. Freeze a broader release-quality rubric in NW0-E; exhaustive
auth failure, lifecycle, concurrency, and recovery matrices belong to NW1–NW3 exits.

For NW0-B, retain the existing 64 MiB archive/member and 256 MiB expanded audit limits. Measure five
cold and five warm base `status --json` invocations per target before and after each candidate;
record how cold state is established without altering the user's system. Provisional startup
regression budgets are at most 500 ms additional median cold latency and 200 ms warm latency,
with absolute times retained for the 300-second journey. Treat these as design budgets, not measured
results. Use native Linux CI for packaging/startup measurements and the UTM guest for desktop
journeys. Compare each candidate with its baseline under the same recorded runner or VM configuration;
UTM measurements establish regression evidence for that emulated environment only. Exceeding
the budgets triggers helper-architecture reconsideration, not removal of the benchmark.

NW0 exit requires evidence for A–E, all five access paths on both targets, both desktop environments,
and no unresolved decision affecting durable schema or first-release acceptance. A missing environment
or exhausted budget leaves the corresponding experiment incomplete. It does not authorize dropping
a provider, subscription mode, platform, or setup step. NW0 proves feasibility; NW4 alone establishes
the final integrated five-minute result with release candidates.

### Requirement ownership

| Contract | Implementation owner | Exit evidence |
|---|---|---|
| Workspace revisions, durable consent/revocation, exclusions, policy generation, and shared budgets | NW1 | Restart/export/import preserve identity and restrictions; restored consent is inactive; mixed-source and unauthorized requests fail closed. |
| Caller capability matrix and MCP process limits | NW1, with NW2 provider-call assertions | Disabled tools absent; no owner mutation via MCP; limit/revocation failures make zero provider calls. |
| OpenAI API, Codex subscription, Anthropic API, Claude subscription, and Gemini API adapters | NW2 | All five share semantic/privacy tests; both subscription clients pass isolation; no silent fallback or credential cross-use. |
| Model-client installation/discovery, API-key custody, login detection/reuse, provider and exclusion setup, credential replacement/removal | NW3 | Fresh/reused onboarding and error flows pass on both desktops for all five paths, including credential-store absence. |
| Integrated product, security documentation, and five-minute acceptance | NW4 | Exact-candidate platform/access matrix, updated threat/privacy contracts, and release evidence meet the unchanged product outcome. |

### NW1: implement the managed-workspace engine contract

1. Add neutral task contracts and explicit migrations for durable mappings/pending operations,
   consent/revocation, note/folder exclusions, policy generations, and shared inference-budget state.
   Derive migration count from the reviewed schema and milestone boundaries.
   Keep the existing schema runner small and dependency-free; preserve frozen catalog checksums.
2. Implement bounded source enumeration, stable-ID mapping, revision acceptance, conflict state,
   rename/delete semantics, replay, and crash recovery from the NW0 decision. Add suggestion
   acceptance through the same revision flow with durable provenance and idempotency. Implement
   effective privacy decisions and restriction-preserving restore through the shared engine boundary.
3. Add shared CLI/MCP operations with separate caller authority and safe result projections.
   Apply the caller capability matrix and process budgets; preserve existing capture/search flags
   and the headless path. Define eligible workspace materialization without changing caller trust.
4. Ensure accepted edits and required history survive verified export/import; regenerate local
   mappings after import without preserving host paths. Preserve inactive notes and demonstrate
   explicit restoration without accidental resurrection during reconciliation or import. Fail
   export visibly if workspace reconciliation cannot establish a consistent accepted snapshot.
5. Exercise the vertical slice's engine operations with the deterministic fake provider, including
   evidence, accepted-link provenance, and verified export. Prove semantic and recovery cases below
   through engine and adapter contract tests, then run
   `make verify`. No Obsidian-specific dependency may enter the engine.

### NW2: implement bounded Graphify projection and local presentation

1. Add the pinned Graphify adapter and all five model-access adapters: OpenAI API, Codex
   subscription, Anthropic API, Claude subscription, and Gemini API. Consume validated snapshots
   and effective privacy decisions; share schema validation, redaction, budgets, cancellation,
   attribution, and error semantics. Enforce the verified subscription launch controls.
2. Connect the smallest complete vertical slice to the presentation and run each real adapter through
   the same flow used by the fake provider. Implement link mapping, revision/version status,
   bounded execution, atomic cache publication,
   and failure/staleness receipts. Display inferred suggestions immediately with evidence and
   explicit-link distinction. Avoid a new authoritative graph database.
3. Package the NW0-selected presentation surface(s) with reviewed local assets and source-note
   links without overwriting user content or configuration. Keep generated files out of source input.
4. Exercise semantic quality/provenance, unauthorized egress refusal, model setup/failure, structural
   fallback, ambiguous links, hostile text, output exclusion, and timeout/recovery. Verify folder
   and note exclusions across automatic/manual requests, retries, context retrieval, renames, and
   policy changes during queued or in-flight work. Add final-prompt canary rejection, tool effects,
   ambient-context loading, credential cross-use, quota/timeout failure, and MCP dispatch-limit cases
   for every applicable adapter. Prove deterministic structural rebuilds separately from semantics.
5. Run `make verify`, native build, Homebrew smoke, and bounded artifact inspection. Repeat both
   target builds when the dependency closure changes; do not relax native audit limits to pass.
   NW2 cannot exit until all five adapters pass their applicable shared and provider-specific cases.

### NW3: ship the thin Obsidian plugin and setup flow

1. Add a desktop-only plugin package with a pinned build toolchain, manifest/version compatibility,
   and the approved binary discovery/protocol contract. Implement the app-owned model-client
   installation flow and plugin-facing discovery,
   provider/access selection, bounded login detection/reuse, API-key custody/replacement/removal,
   and consent/exclusion onboarding for all five paths. Keep raw keys out of plugin settings.
2. Implement capture/search/refresh/navigation, conflict comparison and explicit resolution,
   stale-graph state, automatic refresh with edit batching, persistent pause, and one-shot manual
   refresh. Add suggestion review and explicit acceptance with a preview of the permanent note edit.
   Verify unload and retry
   behavior without an always-on engine service.
3. Implement managed-vault create/open and plugin asset staging/upgrade/removal. Keep activation
   explicit where required; preserve user notes, unrelated settings, and unrelated plugins.
4. Test plugin operations against the same contract fixtures as CLI/MCP, with added adapter-specific
   process, trust, file-event, renderer, and activation cases. Cover edit bursts, changes during
   inference, stale-result rejection, generated-output exclusion, pause across reloads, manual
   refresh while paused, and cancellation on unload. Verify inference leaves note bytes unchanged,
   accepted links survive cache rebuild/export/import, repeated acceptance does not duplicate a
   link, and changed source revisions prevent stale acceptance. Verify both conflict versions
   survive restart, unrelated notes remain usable, concurrent edits reject stale resolutions,
   and repeated resolution does not create duplicate revisions. Cover missing/expired credentials,
   neither/one/both subscription clients authenticated, provider switching, quota failure, and
   session-only key entry when the OS store is unavailable.
5. Run actual Obsidian GUI journeys on both target desktops. A mocked plugin or CLI smoke cannot
   establish that the app opened, the plugin activated, or source navigation worked. NW3 cannot
   exit until fresh/reused onboarding passes for all five paths on both desktops.

### NW4: accept the revised five-minute product and prepare release

1. Update product authority, acceptance, installation, `docs/privacy-model.md`, `docs/threat-model.md`,
   artifact characterization, contributor guidance, and `CLAUDE.md` for the implemented scope.
   Document cloud authorization, client confinement, plugin IPC, and graph representation boundaries.
   Retain old milestone evidence as history.
2. Extend `make contributor-check` to compose the real plugin checks and native integration smoke.
   Keep GUI timing evidence distinct from hosted CI command-line coverage.
3. Run the timed journey below on both platforms using final candidate artifacts, with exact app,
   plugin, runtime, platform, prerequisite, network, and elapsed-time evidence.
4. Run plan-conformance and independent safety reviews, source/history/native owner audits, and
   exact-candidate CI. Preserve all existing history-only and exact-payload exceptions without
   widening them for new dependencies.
5. Prepare reviewable release assets bound to one version and recorded digests. Publishing, tap
   changes, and merging to the release branch remain separate authorized actions.

## Verification contract

### One semantic dataset, format-specific fixtures

Extend the existing semantic fixture approach with synthetic source facts, then materialize distinct
Markdown, SQLite, CLI/MCP JSON, plugin, graph, and Portable Brain fixtures. Do not force their formats
into one physical fixture. Never use personal vault content, real captures, or private host paths.
Parameterize the shared adapter suite over all five access paths. Keep schema/privacy/selection,
deadlines, cancellation, budget accounting, attribution, and stale-result assertions common;
add focused tests for each provider's transport, authentication, and output behavior. Use deterministic
fake responses for fast iteration and the same input/result contract for bounded real-provider probes.

| Case family | Observable assertion |
|---|---|
| First use and edit | Related unlinked notes appear in the managed vault; an inferred connection with source evidence resolves to stable IDs; an accepted edit is retrievable and present in verified export. |
| Identity and conflicts | Rename retains identity; duplicate IDs and concurrent body edits preserve both versions; incomplete enumeration does not erase active notes. |
| Recovery and lifecycle | Kill after each durable/promotion boundary; replay produces one accepted revision and correct pending state; disconnect/uninstall retains notes and engine history. |
| Graph and privacy | Generated output never becomes a source; stale cache remains identifiable; hostile text cannot execute; viewing needs no CDN. Inference follows the selected cloud-provider authorization policy; provider-none remains a usable fallback but does not pass semantic first-use acceptance. |
| Portability and caller contracts | Export/import preserves accepted shared IDs/bytes/provenance without paths/settings/cache; MCP cannot gain owner canonical authority through workspace/plugin adapters. |

Initial existing checks and locations to extend:

- `tests/security/test_direct_edit_reconciliation.py` and
  `packages/app/tests/integration/engine/test_local_schema.py`.
- `packages/app/tests/integration/engine/test_markdown_import.py` and
  `packages/app/tests/integration/engine/test_markdown_import_fs.py`.
- `packages/app/tests/integration/services/test_local_entrypoints.py` and
  `packages/app/tests/integration/services/test_local_mcp.py`.
- `packages/engine/tests/contract/test_portable_brain_v1.py` and
  `packages/engine/tests/contract/test_shared_portability.py`.
- `tests/release/test_native_distribution.py`, `tests/security/test_native_artifact_audit.py`,
  and `tests/security/test_architecture_imports.py`.

New workspace, Graphify, and plugin tests should exercise observable contracts, not mirror internal
functions. Select exact new test locations during implementation. Run focused tests while iterating.
Before handing off a code change and at milestone completion, run the project's real `make verify`;
after packaging changes, run `make native` and `make homebrew-smoke`, with CI
providing the other architecture. Run `git diff --check` and `actionlint .github/workflows/ci.yml`
before an implementation handoff. Documentation-only planning does not require a new runtime build.

### Timed acceptance

Record ordinary wall-clock elapsed time from the declared installation start through verified export.
Use a bounded synthetic corpus of related unlinked notes and an unrelated distractor. Include required
cloud-provider onboarding and first semantic processing in the clock. Run the journey separately
for each required provider/access path on each supported platform; a user configures only their
chosen path, not all five. DECIDED by the user on 2026-09-09: the clock includes installing Obsidian
when missing, plugin activation, and model sign-in or API-key setup. It also includes installation
of the selected official model client when required. Reuse compatible existing installations and
offer detected logins, but record those faster journeys separately from missing-app/fresh-login
acceptance. Do not pause the clock for downloads, activation prompts, or authentication steps.
Declare provider account/access prerequisites and network conditions before measurement; do not
silently move setup steps outside the clock after a failed run. Local model installation is deferred
with Ollama support.
Record Linux desktop timings with the selected UTM host/guest configuration and identify the journey as
emulated. Keep the 300-second target; a pass supports that measured environment, while a miss requires
a product decision and does not establish that native Linux would fail. Do not describe these results
as native x86_64 timing evidence.
Full-vault indexing is a separate measurement. Do not claim an arbitrary corpus completes in five minutes.

1. Install the candidate through the supported Homebrew lifecycle; install/launch Obsidian as
   needed, activate the approved plugin, and configure the chosen model access within the clock.
2. Create/open the default managed vault and capture the unlinked acceptance notes.
3. Run semantic extraction, inspect an inferred connection and its source evidence, and navigate
   from the graph to the original note in Obsidian.
4. Save an edit, preview and accept the suggested permanent link, reconcile the note revisions,
   and retrieve the accepted text through Open Brain. Confirm inference alone did not write the link.
5. Export and verify the Brain; confirm the accepted edit and preserved provenance are present.

Pass only when the journey takes at most 300 seconds and every operation succeeds. A missed time
target is evidence for a product decision, not permission to omit a required step. Closing the editor
must leave no required engine/helper service. An open Obsidian desktop process is expected and must
be distinguished from the existing no-background-runtime guarantee when updating acceptance prose.

## Rollback and data preservation

Before changing a populated development Brain, preserve a verified export using synthetic test data.
Migrations retain prior records and use the existing transaction/ledger discipline. Do not plan an
automatic schema downgrade or purge history during uninstall. Older binaries may refuse a newer
schema; rollback instructions must identify a compatible binary or restoration into a fresh root.

Graph cache removal/rebuild is safe only within the positively identified generated area. Plugin
removal deletes only its owned assets after any required user action; it does not delete the vault,
canonical records, history, or unrelated Obsidian settings. Interrupted setup can be retried without
replacing an existing user's vault.

## Primary grounding references

The following official Obsidian sources were checked during planning. Runtime behavior and actual
installation duration still require the NW0 probes.

1. [Manifest](https://docs.obsidian.md/Reference/Manifest) and
   [submission requirements](https://docs.obsidian.md/community-directory/submission-requirements-for-plugins):
   Node.js/Electron dependencies require a desktop-only plugin.
2. [Build a plugin](https://docs.obsidian.md/Plugins/Getting%20started/Build%20a%20plugin):
   local plugin placement and explicit Community-plugin activation.
3. [Submit a plugin](https://docs.obsidian.md/plugins/releasing/submit-plugin) and
   [developer policies](https://docs.obsidian.md/community-directory/developer-policies):
   Community-directory acceptance, release assets, and prohibition on self-installing/updating
   the plugin or its dependencies. Bundled JavaScript dependencies are a separate matter.
4. [Vault APIs](https://docs.obsidian.md/Plugins/Vault),
   [events](https://docs.obsidian.md/Plugins/Events), and
   [load-time guidance](https://docs.obsidian.md/plugins/guides/load-time):
   atomic in-editor note processing, event registration, and startup handling.
5. [Obsidian URI](https://help.obsidian.md/Extending%2BObsidian/Obsidian%2BURI) and
   [application downloads](https://obsidian.md/download): platform opening and distribution paths.

Graphify source anchors at the inspected pin:

- [Package and dependencies](https://github.com/Graphify-Labs/graphify/blob/3f82bf7f837a07fb0f7668fbdbd5662801906942/pyproject.toml).
- [Markdown extractor](https://github.com/Graphify-Labs/graphify/blob/3f82bf7f837a07fb0f7668fbdbd5662801906942/graphify/extractors/markdown.py)
  and [extractor initializer](https://github.com/Graphify-Labs/graphify/blob/3f82bf7f837a07fb0f7668fbdbd5662801906942/graphify/extractors/__init__.py).
- [Root-aware extraction facade](https://github.com/Graphify-Labs/graphify/blob/3f82bf7f837a07fb0f7668fbdbd5662801906942/graphify/extract.py).
- [Canvas/export APIs](https://github.com/Graphify-Labs/graphify/blob/3f82bf7f837a07fb0f7668fbdbd5662801906942/graphify/export.py).
- [HTML renderer](https://github.com/Graphify-Labs/graphify/blob/3f82bf7f837a07fb0f7668fbdbd5662801906942/graphify/exporters/html.py).

## Planning completion and next action

Planning is complete when this draft records code-grounded constraints, ordered milestones,
verification and recovery criteria, and explicitly unresolved product decisions. It is not a claim
that the architecture is implemented, packaging is viable, or the five-minute target has passed.

The pre-NW0 review of `b87e47a` produced five consolidated corrections, incorporated here on
2026-09-09. Gemini API support was added afterward at the user's request and is included in the
same contracts and milestone exits. The original review's independent model refutation covered
two of four lenses; this revision does not claim a new independent review or successful probes.

| Review correction | Coverage in this plan |
|---|---|
| Cloud consent and privacy | Effective request policy, durable restrictions, restoration semantics, final-prompt scan; NW0-A and NW1/NW2. |
| Subscription-client isolation | Tool-disabled completion, ambient-context controls, credential ownership, hostile-input proof; NW0-C and NW2. |
| MCP authority and budgets | Explicit capability matrix, new opt-ins and process/shared limits; NW0-E and NW1/NW2. |
| Implementation ownership | Requirement ownership matrix and explicit five-adapter/onboarding exit gates in NW1–NW4. |
| Bounded feasibility work | NW0-A–E effort/request limits, named desktop target and provisioning dependency, startup measurements, explicit stop conditions. |

Next action: run NW0-C2's zero-inference Codex catalog and policy preflight; C1's remaining
enforcement gaps must close before a synthetic subscription call.
Preserve completed NW0-B1 evidence and its required adapter controls; provision the agreed
UTM guest before Linux GUI checkpoints without blocking local packaging work. Run a formal review
of the completed NW0 decision record before executing NW1.

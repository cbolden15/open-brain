# OB1 native Obsidian and Graphify workspace

- Status: grounded planning draft; implementation has not started.
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
Hosted-service implementation is outside this plan.

This milestone produces the plan, grounded constraints, and a bounded feasibility specification.
It does not authorize implementation, CI pushes, application installation, release publication,
or repository-setting changes. Later implementation can use reversible work under its approved scope;
outward-facing actions require the applicable user authorization.

### Proposed first-release decisions

| Decision | Working recommendation | Alternative and consequence |
|---|---|---|
| Native interface | Managed vault plus thin desktop plugin, with capture, search, refresh, and source navigation | A plugin-free vault and Canvas is a coherent intermediate milestone, but does not deliver the complete in-app experience. An export-only bridge does not meet this outcome. |
| Existing vaults | Preserve one-way import; offer the managed vault for connected editing | Full two-way arbitrary-vault support is the strongest eventual integration, but adds relocation, deletion, duplicate identity, attachment, conflict, and sync-provider behavior. Plan it as a separate milestone rather than imply it ships. |
| First graph | DECIDED by user on 2026-09-09: inferred connections between previously unlinked notes are required from the start, using a cloud model initially | Provider setup and semantic processing belong inside the first-use acceptance boundary. Explicit links alone do not pass. Local inference through Ollama is deferred. |
| Installation clock | Include the work needed to obtain and activate the Obsidian experience; Homebrew remains the declared prerequisite | If Obsidian is a prerequisite, explicitly rename the measured claim to workspace setup on an Obsidian-equipped host. Do not report application installation as included. |
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

### Filesystem and authority

Recommended physical layout: a dedicated managed vault beside the private Brain directory within
the product's platform-local application directory. The private Brain keeps accepted canonical
records and history. The managed vault is the editable presentation. Neither side is an
unconditional overwrite source. Persist a mapping from stable note ID to workspace-relative path,
last accepted revision, and last materialized body digest.

The default location is automatic; the introductory user flow does not ask for a storage backend or
root. A human-facing open/reveal operation can show the vault location without exposing operational
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

Filesystem replacement and SQLite commit do not form one atomic transaction. NW1 needs a small
recoverable operation journal and defined reconciliation states. External editors do not honor the
engine's writer lease; a check followed by rename alone is not a proof against concurrent edits.
NW0 must demonstrate the chosen conflict-preserving write-back protocol under an edit during
promotion, or restrict automatic write-back until that protocol is proven.

Managed-vault rename preserves the stable note ID. A copied ID is a conflict, not a second alias for
the same writable record. Removing a note from the workspace is distinct from purging retained
history. NW0 must select the supported initial deletion behavior and its search/export consequences.
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
targets. Structural extraction needs no model. If users can explicitly accept inferred relations,
those decisions need durable provenance outside the disposable graph cache.

Structural extraction is a baseline and fallback, not sufficient first-use acceptance. Add a semantic
extraction stage using the selected cloud-model path. Record the model/version,
input revisions, and extraction provenance; distinguish inferred relations from explicit links.
Show source evidence for a suggested connection. The same stable-ID and bounded-input rules apply.
Do not treat a missing provider or failed inference as a successful first graph. Preserve headless
capture/search and the structural fallback when inference is unavailable.

Keep the semantic request/result contract independent of a provider SDK so a later Ollama adapter
can reuse input selection, provenance, and graph handling. Implement the required cloud access paths
in this release. Ollama installation, local model downloads, hardware sizing, and local inference
acceptance are deferred; do not add them to initial setup. Configure which selected content reaches
the provider and bind credentials to the intended adapter rather than inherit ambient credentials.
Credential storage and cloud usage controls must be resolved for each required access path.

Start the offline feasibility proof with Canvas and an explicit source-filename mapping. The
inspected HTML renderer references a CDN-hosted vis-network asset; package that asset locally before
claiming an offline HTML view. Upstream export imports also bring in broader graph modules, so
measure the complete import closure rather than only the function body. Include Graphify's
`LICENSE`, `LICENSE-MIT`, and `NOTICE` plus required dependency notices in the candidate inventory.

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
The strongest shared-maintenance alternative is a separately licensed provider package used by
both projects; compare its Node/runtime packaging cost before selecting it. Neither approach
requires adopting the whole workflow orchestrator or a persistent router service.

NW0 must prove all four paths on the supported platforms using synthetic notes, including fresh
sign-in or key setup, detection with neither/one/both clients authenticated, explicit selection,
valid semantic output, cancellation, expired authentication, quota exhaustion,
and unambiguous usage attribution. Never silently switch from subscription to paid API usage or
to another provider. Define an overall operation deadline across retries and fallback attempts.
Keep subscriptions owned by the official clients; Open Brain must not collect their session tokens.
Required client installation and authentication count toward the selected path's setup clock.
Runtime or policy obstacles require an explicit architecture resolution, not quietly dropping a
required access path from the release.

Provider references checked on 2026-09-09: [Codex authentication](https://learn.chatgpt.com/docs/auth),
[Codex app-server integration](https://learn.chatgpt.com/docs/app-server), and
[Claude Code product integration and credential requirements](https://code.claude.com/docs/en/legal-and-compliance).

### Obsidian desktop surface

The plugin provides capture, search, graph refresh, and open-source-note actions. Invoke the resolved
Open Brain executable with argument arrays and a versioned JSON contract. Do not interpolate note
text into shell commands. Bound process time/output and make missing/incompatible binaries visible.
Close child processes and unregister listeners when the plugin unloads.

Editor events may request debounced reconciliation while Obsidian is open. The engine operation
still rescans and validates; an event is a hint, not authority. A reload or dropped event must not
lose edits. Manual refresh remains available. A long-lived MCP server must not hold a writer lease
that prevents plugin/CLI operations, nor may a plugin keep a session alive solely for background sync.

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
as part of this planning change.

### NW0: resolve contracts and prove feasibility

Output: a reviewed decision record, synthetic evidence, and a runnable bounded spike. The earlier
1–2 working-day estimate covered structural graph experiments only. Re-estimate after measuring
the four required cloud access paths; semantic quality, onboarding, and first-use latency are now
required probes.

1. Compare sibling workspace projection with a dedicated canonical subtree. Prove one new note and
   one edit reach search and Portable Brain v1 with preserved identity/provenance. Determine exactly
   how accepted edit history is represented; distinguish revision history from search reindexing.
2. Pin Graphify and invoke the minimal Markdown-to-graph path. Build for both targets, inventory
   modules/native libraries, record archive and expanded sizes against existing audit limits, and
   compare in-process, self-invoked worker, and separate-helper failure behavior. Resolve Python 3.14
   wheels for both targets; measure against the current 64 MiB archive/member and 256 MiB expanded
   audit limits. Wheel publication alone does not prove PyInstaller compatibility.
3. Produce local graph HTML/Canvas with no remote asset fetch. Demonstrate source-note navigation
   using linked synthetic notes and record all runtime network attempts. Add cross-folder wiki
   links and duplicate basenames to the graph fixture; build twice with network denied and verify
   repeatable normalized structural graph JSON and Canvas. Separately prove semantic discovery on
   unlinked related notes, including an unrelated distractor; measure model provisioning and first
   inference for every required API-key and subscription path. Define an evidence-based semantic-quality rubric
   and repeated-run threshold; do not require byte-identical model output.
4. Exercise Obsidian app installation/first launch/plugin activation on the declared supported
   desktop environments. Record required prompts and ordinary elapsed time. Do not redefine
   prerequisites after seeing a slow result.
5. Freeze note identity, trust/materialization, conflict/promotion, deletion, package closure, graph
   limits, plugin protocol, and timed-journey scope in the decision record. If a claim fails, narrow
   it explicitly or change the architecture before NW1.

NW0 exit: both platform packaging probes are evidenced, the note round-trip is demonstrated, and no
unresolved decision can change the durable schema or first-release acceptance. Source inspection
alone is insufficient. CI pushes or app installation happen only under the authorized spike scope.

### NW1: implement the managed-workspace engine contract

1. Add neutral task contracts and one explicit migration for durable mappings/pending operations.
   Keep the existing schema runner small and dependency-free; preserve frozen catalog checksums.
2. Implement bounded source enumeration, stable-ID mapping, revision acceptance, conflict state,
   rename/delete semantics, replay, and crash recovery from the NW0 decision.
3. Add shared CLI/MCP operations with separate caller authority and safe result projections.
   Preserve the default headless capture/search path and define eligible workspace materialization.
4. Ensure accepted edits and required history survive verified export/import; regenerate local
   mappings after import without preserving host paths. Fail export visibly if required workspace
   reconciliation cannot establish a consistent accepted snapshot.
5. Prove semantic and recovery cases below through engine and adapter contract tests, then run
   `make verify`. No Obsidian-specific dependency may enter the engine.

### NW2: implement bounded Graphify projection and local presentation

1. Add the pinned adapter and selected semantic-model path at the app/runtime boundary; consume
   only validated snapshots and explicitly authorized model configuration.
2. Implement link mapping, revision/version status, bounded execution, atomic cache publication,
   and failure/staleness receipts. Avoid a new authoritative graph database.
3. Package reviewed local viewer assets and generate Canvas/source-note links without overwriting
   user content or configuration. Keep generated files out of every source path.
4. Exercise semantic quality/provenance, unauthorized egress refusal, model setup/failure, structural
   fallback, ambiguous links, hostile text, output exclusion, and timeout/recovery. Prove deterministic
   structural rebuilds separately from the semantic-quality acceptance rubric.
5. Run `make verify`, native build, Homebrew smoke, and bounded artifact inspection. Repeat both
   target builds when the dependency closure changes; do not relax native audit limits to pass.

### NW3: ship the thin Obsidian plugin and setup flow

1. Add a desktop-only plugin package with a pinned build toolchain, manifest/version compatibility,
   and the approved binary discovery/protocol contract.
2. Implement capture/search/refresh/navigation, conflict visibility, stale-graph state, and bounded
   debounced editor events. Verify unload and retry behavior without an always-on engine service.
3. Implement managed-vault create/open and plugin asset staging/upgrade/removal. Keep activation
   explicit where required; preserve user notes, unrelated settings, and unrelated plugins.
4. Test plugin operations against the same contract fixtures as CLI/MCP, with added adapter-specific
   process, trust, file-event, renderer, and activation cases.
5. Run actual Obsidian GUI journeys on both target desktops. A mocked plugin or CLI smoke cannot
   establish that the app opened, the plugin activated, or source navigation worked.

### NW4: accept the revised five-minute product and prepare release

1. Update product authority, acceptance, installation, privacy, artifact characterization, contributor
   guidance, and `CLAUDE.md` for the implemented scope. Retain old milestone evidence as history.
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
functions. Select exact new test locations during implementation. After code edits, run the project's
real `make verify`; after packaging changes, run `make native` and `make homebrew-smoke`, with CI
providing the other architecture. Run `git diff --check` and `actionlint .github/workflows/ci.yml`
before an implementation handoff. Documentation-only planning does not require a new runtime build.

### Timed acceptance

Record ordinary wall-clock elapsed time from the declared installation start through verified export.
Use a bounded synthetic corpus of related unlinked notes and an unrelated distractor. Include required
cloud-provider onboarding and first semantic processing in the clock. Run the journey separately
for each required provider/access path on each supported platform; a user configures only their
chosen path, not all four. Local model installation is
deferred with Ollama support.
Full-vault indexing is a separate measurement. Do not claim an arbitrary corpus completes in five minutes.

1. Install the candidate through the supported Homebrew lifecycle; obtain/launch Obsidian and
   activate the approved plugin according to the NW0 starting-state decision.
2. Create/open the default managed vault and capture the unlinked acceptance notes.
3. Run semantic extraction, inspect an inferred connection and its source evidence, and navigate
   from the graph to the original note in Obsidian.
4. Save an edit, reconcile it, and retrieve the accepted new text through Open Brain.
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

Next action: review the first-release scope and authorize only NW0's bounded feasibility work when
its product assumptions are resolved. Run a formal document review before executing NW1.

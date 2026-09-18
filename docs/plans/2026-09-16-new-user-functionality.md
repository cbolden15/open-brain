# New-user functionality implementation plan

Status: M1 T01–T02 and recovery checkpoints A–C merged in PR41 at `33a7472f06516753c5c66d76eac8e60538c2660f`. M2 T03 contracts and strict consumers are frozen in [T03-FREEZE.json](../ai/workstreams/20260917-open-brain-public-m2-0fd6e9/T03-FREEZE.json); T04 source/migration is locally verified in [T04-CHECKPOINT.json](../ai/workstreams/20260917-open-brain-public-m2-0fd6e9/T04-CHECKPOINT.json); M2 implementation and automated/native checks are recorded in [M2-CHECKPOINT.json](../ai/workstreams/20260917-open-brain-public-m2-0fd6e9/M2-CHECKPOINT.json); Obsidian GUI acceptance remains pending; desktop GUI/release work is deferred under the core-priority decision below. Broader product/release acceptance remains open.

## Objective and authority

Close every critical and non-critical finding in the [new-user assessment](../audits/2026-09-16-new-user-functional-assessment.md), except new backup/restore functionality. The target user is comfortable with AI coding tools, IDEs and terminals, but has no private Open Brain setup, prior Brain data, developer OAuth client or unpublished package. Both installation/first use and ongoing brain functionality are in scope.

Repository: `.`.

Assessment baseline: main `5cf081aa3d591e14b89245db290d4a186ff8156a`. M2 implementation baseline: merged main `33a7472f06516753c5c66d76eac8e60538c2660f`, durable schema 6 and runtime session 1. Retained M1 checks and review are recorded in [M1 evidence](../ai/workstreams/20260917-open-brain-public-m1-publication-1f7912/EVIDENCE.json); they do not establish all A01 or public release acceptance. Public Homebrew stable during assessment was v0.1.0, tag `a374e4d806bcebe396c63eab916e005813f07bda`. A source feature is not a shipped feature. The assessment reported 1,654 passing tests and five filesystem-specific skips; it did not run full release verification, clean native installation, native GUI acceptance or production OAuth acceptance. Those results are not evidence that this plan has been implemented.

This plan governs new-user remediation; it does not reactivate old appliance/control-plane plans. Preserve the foreground, unprivileged, dependency-minimal core, one OS user/one Brain, shared engine authority and separately optional connectors/collector/desktop. Do not add a core daemon, database server, network listener or required model runtime.

User decision: an optional on-device embedding model download is allowed. Capture and ordinary lexical search remain offline without that component; configured semantic inference is also local. Never download a model implicitly during capture or search.

Companion artifacts:

- Coverage: `docs/plans/2026-09-16-new-user-functionality-coverage.json`.
- Excluded follow-up: `docs/plans/2026-09-16-backup-restore-follow-up.md`.
- Workstream: `docs/ai/workstreams/20260916-open-brain-public-new-user-functionality-plan-60f7da/STATE.md`.

### Scope boundary

F3 Portable restore, scheduled backups, recovery orchestration and new revision restore/revert commands are excluded. Existing export/import/workspace-reactivation behavior and its regression tests remain protected. Read-only history, append-only corrections, source availability, retirement and selective forgetting are active work. Crash recovery for an interrupted migration or purge is internal transactional correctness, not a new user backup/restore feature. Preserving export compatibility is not permission to build a new restore workflow.

Removed Outlook, Teams, OneDrive, SharePoint and deferred Slack adapters are not reinstated. OCR, arbitrary binary archival, automatic semantic merging, hosted inference and multilingual quality claims are not added. PDF/DOCX remain bounded local extraction, with explicit owner-driven continuity operations rather than an unsolicited filesystem watcher.

### Core CLI/MCP priority (user decision, 2026-09-17)

Prioritize core CLI and MCP delivery. Preserve completed desktop code, tests and artifacts; defer further desktop-specific features and release preparation. Shared-engine verification and compatibility checks remain required. Obsidian scope is unchanged. Desktop release readiness and desktop GUI acceptance are not gates for core delivery.

This decision narrows the desktop portions of the tasks and acceptance rows below; it does not mark those deferred portions complete or defer a whole mixed-surface task.

| Tasks | Deferred desktop work | Work retained in scope |
|---|---|---|
| T07 / A05 | Further desktop features, UI polish and observed desktop GUI acceptance | Completed desktop work and compatibility checks; core CLI/MCP and Obsidian workflows |
| T11–T14 / A08–A11 | Desktop lifecycle/forget/rebind screens and document-picker/import UI | Owner CLI, engine security/lifecycle, source continuity, bounded optional document parsing/refresh/rebind and Obsidian |
| T18–T19 / A13–A14 | Desktop semantic setup/status and catalog screens | CLI/MCP/helper behavior, shared eligibility/privacy, catalog truth and Obsidian integration |
| T20–T21 / A15–A16 | Desktop installer/release packaging, macOS signing/notarization/stapling/Gatekeeper, Linux desktop AppImage/window readiness and desktop-only launch docs | Core native audit/Homebrew/clean installs, shared compatibility, relevant optional packages and CLI/MCP/Obsidian documentation |
| T23 / A17 | Desktop native GUI journeys and desktop release timing | Core CLI/MCP and Obsidian acceptance, relevant platform/runtime and provider/auth evidence |

Google OAuth “Desktop client” describes an OAuth application type; CLI/connector authentication work is not deferred by this desktop-product decision. M3 implementation and outward publication still require their separate authorization.

### Core v0.1 delivery scope (user decision, 2026-09-18)

T09 collector recovery is [locally verified](../ai/workstreams/20260918-open-brain-public-m3-source-continuity-owner-control-c63538/T09-CHECKPOINT.json). Narrow T19–T21 and T23 to core CLI/MCP,
the existing Obsidian workflow, truthful core artifacts and documentation, and
exact-candidate acceptance. T10–T18 and T22 are deferred. Their original task and
acceptance definitions below remain follow-up requirements, not completed work.

The candidate may describe immutable records and publication, lexical search,
complete reads, history, and explicit relationships according to verified
implementation. It must not claim continuously updating connected sources,
retire/correct/selective forget, Markdown-root rebind, PDF/DOCX, Linux background
service, semantic recall, desktop release readiness, or Google OAuth/provider
readiness. Preserve completed desktop compatibility and the foreground-only
core boundary. Public promotion still requires exact artifact/platform evidence;
push, PR, merge, release, live data, OAuth registration, real service installation,
and model downloads remain separately gated.

## Delivery approach

Use an orchestrator-worker workflow with independent verification. The coordinator owns coverage, shared decisions, integration and evidence. Workers own bounded changes. Shared contracts are frozen before adapters fan out. Immediate F1 remediation does not wait for a schema migration or the model work.

| Milestone | Outcome | Work units | Exit gate |
|---|---|---|---|
| M1: publication/search correctness | Multi-source approval and cumulative updates no longer break public search | T01–T02 | A01; independently releasable without a schema change |
| M2: shared records and retrieval | Immutable source revisions, complete reads, scoped/paged discovery and readable history | T03–T08 | A02–A05 and A07; contract and migration proof |
| M3: source continuity and owner control | Updates survive routing; batches progress; move/unavailability and owner lifecycle work | T09–T15 | A06 and A08–A12; destructive operations independently reviewed |
| M4: optional meaning-based recall | Opt-in local helper measurably improves paraphrase recall | T16–T18 | A13; offline and resource proof on both platforms |
| M5: usable public delivery | Exact packages, truthful catalog/docs, clean first use and public/native acceptance | T19–T23 | A14–A17; public claims require exact-artifact receipts |

M2 contract design, T09 containment design, T16 model spike, catalog design and owner-led OAuth preparation can start alongside M1. M1 can ship alone. M3 workers consume frozen contracts. M4 integration consumes the same completed lifecycle eligibility rules, not a parallel notion of active data. M5 preparation runs throughout; its final acceptance uses one exact integrated candidate. Smaller verified releases are allowed, but unfinished findings remain open in the coverage register.

Planning estimates, not delivery promises: M1 1–2 engineer-days; M2 15–25 including publication/read client workflows; M3 25–40; M4 6–10; M5 12–20 plus provider review and access to both native platforms. Rough effort is 59–97 engineer-days, not elapsed calendar time, and tasks can overlap after contract freeze. These are sizing ranges, not measured model-assisted productivity. Re-estimate after T03 and T16 using actual evidence; provider approval has no assumed completion date. A smaller verified core/publication release can precede the whole program.

## Architecture decisions and alternatives

These are recommended implementation defaults, not completed proof or authorization to act on live data. Execution starts with a contract review in T03. A material change requires a recorded decision; do not quietly shrink scope when proof fails.

| Decision | Complete architectural option | Smaller option | Plan recommendation |
|---|---|---|---|
| Changing sources | Stable logical sources, immutable revisions, source-level routing, publications pinned to exact evidence | Quarantine changes to routed sources | Complete architecture. Quarantine is early containment, not F4 closure. |
| Paging during collection | Persist bounded ranked ID/revision snapshots, TTL and access/deletion invalidation | Generation-bound keyset cursor with explicit restart on mutation | Generation-bound paging initially. It guarantees complete traversal for an unchanged generation, not uninterrupted browsing across writes. |
| Referenced-source forgetting | Preview and confirm full dependent-content removal, with crash-safe exclusion | Refuse all referenced sources | Full dependency-aware removal; refusal alone does not close F5. Independent source captures survive. |
| Full-content agent reads | Explicit new content/history grants | Silently expand existing search authority | New grants, default off for existing agents. Owner CLI remains authorized. |
| Semantic runtime | Separately packaged foreground native helper and pinned model | Isolated optional Python helper; lexical-only core | Native helper preferred, pending T16 proof; isolated helper is fallback. Lexical-only release is not completion of W1. |

Further defaults: explicit owner-authored duplicate/supersedes/contradiction relationships, no automatic merge or truth winner; logical-source retirement persists across later revisions; source-wide forget suppresses automatic recapture, while revision-only forget suppresses that revision. Owner confirmation at execution names the exact scope and collateral derived-content loss.

### Shared contracts: T03 freeze

One engine contract owner controls `packages/engine/src/open_brain_engine/engine/contracts.py`, schema migrations, task protocols, lifecycle eligibility and Portable schema compatibility. One app owner integrates `packages/app/src/open_brain/services/local_operations.py`, CLI/MCP operation registries and bridge DTOs. Collector owns queue/checkpoint contracts; connectors own normalized upstream identity and ordering. UI and model workers consume these contracts and shared fixtures.

Create a reviewed contract artifact before parallel implementation. It must define identities, operation DTOs, typed errors, grants, source-head ordering, route versions, retrieval/vector generations, mutation leases, schema cutover and compatibility fixtures. Extend task capabilities rather than making the entire engine the public adapter API. Expected error vocabulary includes `not_found`, `cursor_invalid`, `cursor_stale`, `revision_changed`, `preview_stale`, `operation_pending`, `source_revision_conflict`, `model_unavailable` and `projection_stale`; errors must not contain private paths or provider exception text.

T03 must explicitly define private cursor-signing key creation/custody/rotation and invalidation across restart, relocation and imported Brain identity; a cursor never grants access. It must also preserve untrusted-source framing in agent-facing reads/history: embedded instructions are content, not authority to run tools, widen grants, publish or delete. Add malicious-source fixtures and check that projection/serialization never promotes source text into privileged instructions. These are required contract/security proof obligations, not claims that the planning review certified them.

The merged M1 baseline used durable schema 6 and runtime session version 1. M2 now uses schema 7/session 2 under the frozen T03 contract, preserving historical schema-5 and schema-6 migration fixtures. Existing consumers sometimes reject unknown fields. New operations need negotiated capabilities and strict producer/consumer fixtures; additive JSON is not assumed compatible. Preserve old first-page `search(query, limit)` behavior. Version durable migrations and reject old writers/sessions that could corrupt new state. Keep storage schema, runtime session compatibility and transport protocol versions distinct.

### Stable sources and immutable evidence

Introduce logical source identity scoped by connector/connection/resource namespace, an immutable revision/capture identity, an explicit current head, source-level route/version, availability and lifecycle. Manual captures remain independent sources. Equal text or a common URL does not merge identity. A retry with the same revision key but different bytes fails explicitly.

Current public-job replacement updates the current SQL capture row while older source JSON survives. T04 must inventory and reconstruct history from validated durable source/history files, not merely add columns to current SQL. Preserve capture IDs, bytes, delivery aliases, current heads, routes and all exact publication memberships. Group historical revisions only when durable identity proves the grouping; otherwise preserve ungrouped history with diagnostics. Do not invent chronological order from arbitrary provider tokens.

Source-head updates use expected-head comparison and adapter ordering/predecessor evidence. Late revisions cannot silently roll the source backwards. Retain them as history or quarantine them with a typed reason. Routing follows the source across revisions. Published pages remain pinned to the reviewed capture IDs and show `source_update_available` until a new reviewed publication is approved.

Migration proof must construct schema-5 fixtures through the existing replacement path: three revisions of one delivery, metadata-only changes, identical external keys in different namespaces, routed heads and historical files no longer represented by current SQL. Record pre-migration IDs, digests and export inventories. Test collisions, malformed/missing files, symlinks, ambiguous grouping and interruption at durable/SQL commit boundaries. Reopening must finish idempotently or refuse safely, never expose mixed-schema state. Re-export retains every non-forgotten prior payload byte. No backup/restore command is a prerequisite.

### Retrieval, complete reading and agent authority

New paged search accepts query, optional space/payload-family/record-type filters, mode, bounded limit and cursor. Space is a relevance filter intersected with actual effective authorization, not a new privacy boundary. Public results distinguish source from canonical records and identify immutable revision, logical source where applicable, projected metadata, bounded provenance and actual mode/warnings.

Search cursors are opaque, versioned and integrity-protected. Bind to Brain identity, normalized query/filters, page policy, effective authorization, retrieval generation and exact deterministic last-sort tuple. Reauthorize each call. Compare generation and select in one read transaction. Any relevant mutation returns `cursor_stale`; do not silently restart. Test exact FTS-score encoding and binary tie-breakers. Do not hold a database transaction between CLI invocations.

Add full projected textual `read ID` with expected revision, bounded Unicode-safe chunks, continuation and a true completion marker. Default chunk target is 32 KiB, maximum 64 KiB; protocol/MCP byte budgets can lower it. Project/redact before splitting. Bind continuation to immutable revision and projection-policy version. All projected text must be reconstructible, including the middle/end of long sources. This is not original binary-file retrieval. Unknown and unauthorized IDs are indistinguishable.

Add CLI read/filter/cursor operations, MCP content read and paged search, and plugin/desktop consumers. Existing MCP agents do not acquire full-content or history grants automatically. History needs a separate grant and current authorization/redaction even when the stored revision is old. Negotiation and denied-operation tests are mandatory.

### History, relationships and corrections

Expose bounded history list/show for logical sources and canonical pages: immutable revision, predecessor, timestamp, reason, lifecycle, provenance and current/head marker. No revert/restore operation is added.

Add explicit `duplicate_of`, `supersedes` and `contradicts` relationships with exact revision-bound endpoints, owner decision and decision history. Prevent supersession cycles and self-edges, make symmetric relationships deterministic, and reject stale endpoint decisions. Independent identical captures remain distinct. Source/canonical results stay distinct with readable relationships; suggestions do not authorize merging or deletion.

Corrections append owner-authored evidence or a reviewed replacement page; they do not rewrite upstream evidence or imply a different author. Retirement is an explicit companion choice. Contradictory facts remain visible unless the owner deliberately changes lifecycle state; the system does not silently select truth.

### Collector progress and source availability

Persist per-item `pending`, `captured`, `duplicate` and `quarantined` outcomes under the selection/generation lock. A typed conflict on A must not block independent B. Quarantine retains a durable replayable intake and visible owner inspect/retry controls. Advance the provider checkpoint only after every item is committed or durably transferred to quarantine. An error string is not durable custody. Bound quarantine storage and apply backpressure; global disk/auth/infrastructure failures stop the batch instead of being hidden as bad content. Preserve pause/reset cancellation and acknowledgement-after-local-commit semantics.

Missing, inaccessible, retired and forgotten are separate states. A source disappearance must not erase historical evidence or pretend to prove deletion. Markdown root rebind and document-source rebind/refresh are owner preview/apply operations, preserving logical identity only when evidence and explicit selection support it. Ordinary imports keep their fail-closed identity checks. No background watcher is implied by a one-shot importer.

Markdown rebind is initially owner CLI only. Preview inventories target identity and the active imported relative-path/digest snapshot; apply reopens and revalidates under the writer lease. A same-inode move can retain the root. A new-inode copy/recreation requires an exact active snapshot match and stronger owner confirmation, including disclosure if the old root remains reachable. Reject overlap, Brain self-import, symlinks, poisoned/replaced targets, mismatched copied snapshots and stale previews. Rebind changes registration only, not captures or missing-file state; the next ordinary import owns content changes.

Optional documents retain private operational path/file bindings outside public captures, exports and renderer responses. Explicit check/refresh or owner mark-unavailable updates availability; absent/unreadable source projections leave default active search while captured history and published citations survive. Reappearance is not a user data restore operation: it updates source availability, and changed bytes require preview/consent unless that exact source was separately enabled for collection. A confirmed document move preserves the opaque source identity only after old/new binding and byte evidence are validated; an unconfirmed unrelated path remains a new source.

Linux background collection uses an optional systemd user unit, never root/system service or automatic linger. Bind ownership to exact unit/marker/executable/state preimages, revalidate before overwrite/remove, bound commands and verify process/socket/unit cleanup. Support a normal logged-in user session; collection after logout requires a separate owner/admin policy. No user supervisor means truthful foreground-only guidance. Installation alone never enables sources, installs a service, initiates OAuth or creates schedules.

### Publication clients and release truth

Extend the versioned bridge to delegate inbox/space/routing and review list/show/propose/approve/reject/edit-and-approve to the existing shared publication service. Freeze per-session operation grants, idempotency, review tokens and response limits; UI adapters must not implement a second publication engine. Unsupported operations stay disabled through handshake negotiation.

Obsidian adds a bounded Review captures for publication flow: select 1–32 captures in one space, route if needed, edit a complete draft, inspect the token-bound proposal/evidence, explicitly approve/edit-and-approve/reject, then refresh and open the managed note. Use a deterministic initial draft, with no required model/network call. Capture success says captured to inbox and offers review for vault; it does not claim a canonical vault note exists. Desktop exposes the same route/draft/inspect/approve/materialization workflow and native vault reveal. Test keyboard cancellation, empty Brain, stale tokens, redaction and unload/child cleanup. Graph suggestions stay separate from canonical publication.

Recommend a new core release identity such as 0.2.0, subject to release-owner selection, rather than materially different artifacts all labelled 0.1.0. Bind core/plugin/Graphify and independently versioned optional desktop/collector/helper artifacts to exact source/digest compatibility manifests. Core native audit continues rejecting connector/collector/parser/service modules. Optional collector packaging provides connector/document commands and licenses through a separate installer/formula. No source checkout or private environment variables are needed for supported user paths.

Catalog v2 separates implementation state, onboarding readiness, package/version, platform/surface, callable operations, one-shot/foreground/scheduled lifecycle and acceptance evidence. Build installed entries from actual registered dispatch capabilities, not every provisional descriptor. Planned entries require explicit inclusion; deferred Slack stays unavailable. Desktop catalog consumption is deferred; when resumed, it consumes this catalog instead of hard-coded source cards. Public promotion requires artifact and auth receipts, not a boolean edited in a descriptor.

Prepare a separate approval-gated release workflow for immutable platform artifacts, manifests, checksums and optional package audits. Deliberately update existing tests that currently assume only the CI workflow exists. Use one version authority rather than hard-coded divergent package/runtime versions. If desktop release work is reauthorized, public macOS desktop needs signing/notarization/stapling and real Gatekeeper launch; Linux desktop needs an exact AppImage/native-window receipt. These desktop receipts do not gate core delivery. Preparing workflow code does not authorize publishing a tag, artifact, formula or release.

### Public OAuth and native gates

Normal Gmail/Drive users must not need a developer Cloud project or client JSON. The optional package uses an owner-approved production Desktop client, system browser/loopback flow, PKCE/state checks, exact scopes and OS credential custody. A Desktop client identity cannot be treated as a confidential server secret. Keep developer overrides explicitly developer-only.

At T22 recheck primary provider policy. The delivery research found Gmail `gmail.readonly` and Drive `drive.readonly` restricted; evaluate Drive `drive.file` with a native Picker as the least-privilege option, pending proof that recurring selected-file access meets the requirements. Do not silently weaken collection behavior to avoid review. References: [Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes), [Drive scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth), [production readiness](https://developers.google.com/identity/protocols/oauth2/production-readiness/policy-compliance), [native OAuth](https://developers.google.com/identity/protocols/oauth2/native-app), [restricted-scope verification](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification).

Owner/provider work includes production project/client, verified domain and public policies, contacts/branding, exact scope justifications, retention/deletion disclosures, verification submission and any required security assessment. Explain account-wide permission versus selected capture resources, local retention, downstream agent access and disconnect versus deletion/revocation. Policy promises must match implemented F5 behavior. Synthetic engineering can proceed while approvals are pending, but public source promotion cannot.

Require an 8+ day production authorization soak or a documented provider-approved equivalent, including refresh, process reboot, revocation/reconnect, resource removal/reappearance, rate limiting and Workspace-admin restrictions. Use a disposable Brain and small owner-selected resources, only after approval. Retain sanitized receipts, not tokens/content. This is a proposed acceptance window, not evidence already collected.

Each supported platform needs exact core installation plus real Obsidian publication and cleanup, relevant OS credential prompts where advertised, and Linux user-service behavior. Optional Tauri native-window and desktop document-picker acceptance are deferred and do not gate core delivery. CLI proof flags, hosted CI and screenshots from another commit do not count as native GUI acceptance. Time the documented primary journey; if it exceeds the existing five-minute promise, improve the flow or seek an explicit promise change before publication. Do not silently relabel a failed acceptance gate.

### Owner lifecycle and selective forgetting

Retire removes active retrieval/vector eligibility but retains history and export. Correct appends new evidence and relationships. Forget removes selected content and dependent copies from the live store and future exports. External prior exports, client transcripts, unmanaged user copies, OS snapshots and physical storage remnants are outside the promise; do not claim forensic secure erase.

Owner CLI uses preview/apply, never an implicit destructive MCP grant. Desktop lifecycle UI is deferred under the core-priority decision. Preview freezes Brain identity, target revisions, dependency digest, state generations, affected counts, workspace conflicts, recapture suppression and expiry. Apply requires exact confirmation and revalidates under exclusive mutation authority. Changes invalidate the preview. Source-wide suppression tombstones must contain no forgotten text, title, raw URL or plaintext upstream key.

Inventory every store before implementing purge: durable source JSON, content/history files, raw blobs, current/historical captures, pending/rejected/approved proposals and evidence, review decisions and publication history, managed notes/revisions/conflict copies, relationships, FTS/text/vector projections, connector staging and collector pending payloads. Delete shared blobs only if no retained record owns them. Preview explains when independent identical captures keep the same bytes and offers an expanded scope, never silently deletes them.

For a cited forgotten source, initial conservative policy removes dependent canonical pages and their tainted history as complete derived artifacts. Other independent source captures survive and can be republished. No heuristic substring redaction of past pages. The owner must see this collateral removal before confirming.

Use a durable purge journal: reserved/excluded, durable files removed, projections removed, verified, complete. Reservation prevents new reads or conflicting commits, invalidates cursors and coordinates collector suppression/control epoch and pending-payload removal. Export returns `operation_pending` until completion. Recovery runs before any action that could resurrect excluded artifacts. Cross-process reader/writer leases need bounded acquisition and stale-writer rejection. A staged collector payload containing forgotten bytes means purge is not complete.

Root/path identity checks must survive symlink swaps. Do not remove arbitrary divergent owner-edited files on a stale preview. A changed managed artifact needs a new explicit preview or remains a visible blocker. Completed receipts retain only safe opaque IDs/counts/error codes. Test SQLite WAL/checkpoint/compaction limits without promising SSD/snapshot erasure. Verify with unique synthetic canaries across live stores, all projections and a newly verified export.

### Optional local semantic recall

T16 is a feasibility gate, not permission to defer W1. Candidate is a separately packaged foreground ONNX helper, local tokenizer and pinned embedding model; an isolated Python helper is a valid fallback if native packaging fails. The helper has no Brain write authority and no role in permissions or deletion. Use bounded inherited stdio, startup/close deadlines and no daemon/listener.

A primary-source review supports investigating ONNX Runtime and all-MiniLM-L6-v2; exact release ABI, tokenizer, total footprint and performance are unproven. The model card identifies English and a 256-word-piece truncation limit, so explicit chunking is required. Verify licenses/digests at implementation time. Sources: [ONNX native API](https://onnxruntime.ai/docs/get-started/with-c.html), [macOS packaging](https://onnxruntime.ai/docs/execution-providers/CoreML-ExecutionProvider.html), [model card](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2), [model artifacts](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/tree/main/onnx).

Explicit installation shows publisher, license and expected bytes, verifies pinned artifact hashes and promotes atomically with confined extraction. No auto-fetch or telemetry during inference. Prove operation with network denied; do not call ordinary runtime configuration an OS sandbox. Missing/corrupt optional assets cannot block capture or lexical search.

Engine-owned rebuildable chunks/vectors use projected content digest, model/tokenizer/chunking fingerprint and eligibility generation. Redact before embedding; exclude retired/forgotten/ineligible records. Token-aware overlapping chunks must cover long text. Foreground semantic index build supports progress, cancel/resume and atomic generation promotion. Filter lexical and semantic candidate sets before public materialization; use deterministic hybrid ranking and preserve source/canonical distinctions. Purge and route/privacy changes invalidate the same eligibility rules. Report `mode_used`; fallback is visible, and a required-hybrid request fails clearly instead of pretending semantic execution happened.

Provisional engineering targets to approve at T16: installed helper+model+tokenizer ≤250 MB; peak RSS ≤512 MB; warm query p95 ≤500 ms and cold query ≤2 seconds at 10,000 chunks; indexing that corpus ≤5 minutes. Measure on named macOS arm64 and Linux x86_64 baseline hardware. Exact scan is acceptable if measured. Failure triggers optimization or an explicit budget decision, not a silently weakened threshold or scope removal.

Commit a synthetic relevance fixture of at least 40 independently specified queries with expected IDs frozen before tuning. Include the assessment's `launch due date` versus `release deadline`, exact entities, abbreviations, distractors, changed/contradictory facts and middle-of-long-source text. Require the named miss in top five, ≥15 percentage-point paraphrase Recall@5 gain over lexical and ≤2 percentage-point exact/entity loss. Report MRR and latency as well. English is the first supported quality claim; other languages remain unclaimed pending their own evidence.

## Work packages and acceptance ownership

Paths below are ownership boundaries, not authority to edit unrelated files. Resolve actual files again at dispatch. Each task adds focused tests and returns commands/results, exact diff/commit, remaining risks and its coverage IDs. Independent reviewers verify changes before milestone integration.

### M1: fix the release blocker first

| Task | Owner/tier | Scope and dependencies | Required acceptance |
|---|---|---|---|
| T01 | Engine implementer, strong | `engine/reconciliation.py` and resolver tests. Use ordered complete `canonical_source_rows` membership inside the validation snapshot; retain identity, trust, space and path checks. No schema change. | A01: first two-source approval and later third-source update followed by public searches; negative tamper/reorder/duplicate/representative-ID cases. |
| T02 | App test implementer, mid | Composed CLI/MCP/bridge/desktop service fixtures; depends T01 interface, no parallel edit of reconciliation. Build empty-Brain route/propose/inspect/approve/search journey. | A01: related and unrelated searches survive; all provenance retained; secondary-source privacy canary absent; refresh/export/doctor still pass. |

### M2: contracts, immutable records and usable retrieval

| Task | Owner/tier | Scope and dependencies | Required acceptance |
|---|---|---|---|
| T03 | Contract architect, strong | Freeze the shared contract above, supported surface matrix and migration/Portable compatibility fixtures. Start beside M1. One owner for central schemas. | A02: version/grant/error fixtures accepted by engine/app/collector implementers; old clients reject unsupported writes safely. |
| T04 | Engine implementer, strong | Immutable source/revision storage and schema-6-to-next-version cutover with historical schema-5 coverage, route/head CAS, alias preservation, durable history inventory. Depends T03. | A03: file-only old revisions recovered without changed bytes/IDs; head/routes/publication/export parity; interruption/collision/symlink/ambiguity cases. |
| T05 | Retrieval implementer, strong | Shared filtered search pages, generation/keyset cursors and full projected source/page chunk reads. Depends T01/T03; integrate T04 identities through frozen DTOs. | A04: at least 201 results without loss/duplication, filter matrix, long Unicode exact reconstruction, stale/cross-Brain/cross-grant denial. |
| T06 | App adapter implementer, mid | CLI/MCP grants, read/filter/cursor APIs, organization/review/workspace bridge operations and protocol negotiation. Own central app registries; depends T03/T05 contracts. | A01/A05: actual publication and read sessions, denied old agents, mandatory review inspection, bounded payloads and secret-free errors. |
| T07 | Client implementers, mid | Separate Obsidian and desktop tasks for publication workflow plus read/filter/continuation, using shared bridge APIs. Depends T06; disjoint package ownership, no second app/schema owner. | A01/A05: capture→route→draft→inspect→approve→open vault; complete record reads, pagination and stale/restart states; cancellation and child cleanup. |
| T08 | Engine/history implementer, strong | History list/show, explicit relationships, decision history and stale/cycle validation. Depends T04/T05; app owner integrates surfaces in a separate slot. | A07: current/history distinction, correction lineage, independent duplicates and canonical/source separation, scoped history redaction. |

### M3: keep sources current and give owners control

| Task | Owner/tier | Scope and dependencies | Required acceptance |
|---|---|---|---|
| T09 | Collector implementer, mid | Durable per-item receipt/quarantine and inspect/retry/status. Depends T03 outcome contract; can precede full T04 cutover. | A06: blocked A does not block B; exact checkpoint custody, crash/replay/acknowledgement, quota and global-failure cases. |
| T10 | Connector/collector implementer, mid | Immutable intake integration, source ordering/predecessors, routing survives changes and publication update visibility. Depends T04/T09. | A06: routed A1 published, A2+B1 ingested, A1 remains evidence, A2 update approved, all public searches work; late/replayed/metadata-only revisions. |
| T11 | Lifecycle implementer, strong | Retire and append-only correct/supersede, including eligibility and owner-facing semantics. Depends T08/T10. | A08: distinct retire/correct outcomes, persistent retirement across source revision, retained readable/exported history and explicit review. |
| T12 | Lifecycle implementer plus independent security reviewer, strong | Exact store inventory, forget preview/apply, journal, recapture suppression and all durable/projected cleanup. Depends T04/T08–T11. Coordinate any semantic index added later. | A09: referenced/unreferenced/shared-blob purge, stale preview, divergent managed files, active collector, crash at every journal step, no unique canaries in new export. |
| T13 | Import implementer, mid | Owner preview/apply Markdown root rebind with opaque root identity, overlap/self-import/TOCTOU checks. Depends T03; integrate T04 source semantics. | A10: real move resumes same identity/history; unconfirmed replacement remains refused; unrelated/overlapping/symlink roots fail closed. |
| T14 | Document connector/client implementer, mid | Optional PDF/DOCX package discovery/install, picker/preview/import, explicit source refresh/rebind/availability. Depends T03/T04; app integration owned centrally. | A11: exact package and native picker on both platforms, changed file revision, explicitly confirmed move, missing/inaccessible file states, parser safety bounds. |
| T15 | Collector/platform implementer, mid | Optional Linux user-service lifecycle, preserving foreground operation and separate package. Depends T03 service contract. | A12: install/start/status/restart/stop/remove without root; ownership, PID/config drift, crash and cleanup; no core service side effects. |

### M4: optional meaning-based recall

| Task | Owner/tier | Scope and dependencies | Required acceptance |
|---|---|---|---|
| T16 | Bounded runtime researcher, mid; decision review strong | Two-platform helper/tokenizer/model proof using synthetic data, licenses/digests and quality/resource fixture. Start early after T03 retrieval boundary. | A13: measured feasibility receipt; choose native or isolated helper, or explicitly report blocked proof and revised proposal. |
| T17 | Retrieval/helper implementer, strong | Pinned optional installation, chunking/index build/resume and hybrid ranking, lifecycle/redaction integration. Depends T05/T11/T16. | A13: offline/missing/corrupt/interrupted cases, fresh and stale generations, deterministic rank, lifecycle concurrency and model fingerprint compatibility. |
| T18 | Client/test implementer, mid | Optional setup/index status, mode/warnings, public client integration and final benchmarks. Depends T06/T07/T12/T17. | A13: full-text middle matches, no hidden downloads, no active/retired/forgotten scope leak, resource and relevance targets on exact artifacts. |

### M5: public delivery and acceptance

| Task | Owner/tier | Scope and dependencies | Required acceptance |
|---|---|---|---|
| T19 | Catalog/app implementer, mid | One versioned joined capability/availability catalog across core, connectors, collector and desktop. Owner-certified state promotion, no boolean-only onboarding claim. Depends T03. | A14: planned/source-only/developer-accepted/public states match installed commands, packages, auth and receipt references; unsupported actions fail truthfully. |
| T20 | Release implementer, mid | Reproducible optional connector/collector/desktop/helper artifacts with platform matrices, matching engine/protocol constraints and install/removal paths. Depends package contracts; final build after M1–M4 integration. | A15: native-audit/Homebrew smoke and clean optional installs with candidate hashes on macOS arm64/Linux x86_64; no developer checkout/PYTHONPATH precondition. |
| T21 | Documentation/test implementer, economy for mapping, mid for changes | README, CLI/help, agent setup, first-use guide, source guides, doctor examples and feature/version matrix. Depends actual T19/T20 candidate surfaces. Standardize documented `doctor --check … --json`. | A16: examples executed as written; exact artifact capture/import→route→proposal→inspect→approve→vault/search/full-read; public v0.1.0 never claims source-only features. |
| T22 | Provider integration implementer, mid; owner external gate | Production Google OAuth/client/consent/scopes and long-duration auth acceptance. Prepare synthetic tests and instructions first; owner authorizes external registration/access. | A17: exact public-client refresh/restart/expiry/reauthorization/revocation receipts; developer OAuth success cannot promote public status. |
| T23 | Independent acceptance lead, strong judgment | Clean new-user journeys, exact native GUI/plugin/provider timing on both supported platforms and final coverage reconciliation. Depends all required candidate features and T22 public gates. | A01–A17: each receipt tied to source/artifact/platform; no private preseed; unresolved scope/provider/platform gates remain open. |

## Acceptance ledger

These IDs are required evidence, not currently passed results. A worker's unit tests alone cannot close a product finding.

| Evidence ID | Observable completion requirement |
|---|---|
| A01 | Empty-Brain multi-source publication, cumulative update, related/unrelated public search and full provenance; privacy/tamper negatives and existing export/doctor invariants. |
| A02 | Frozen compatible task/DTO/grant/error contract, strict old/new client fixtures and unsupported-writer refusal. |
| A03 | Schema-5 and schema-6 durable-file migration inventory parity, immutable history, unchanged heads/routes/citations, crash-safe cutover and export-byte preservation. |
| A04 | Complete projected text in bounded chunks; at least 201 deterministic paged/filter results; current authorization and cursor/revision invalidation. |
| A05 | Real CLI/MCP and bridge/client read/filter/paging behavior with explicit grants, response budgets and source/canonical distinction. |
| A06 | Routed source stays current without rewriting published evidence; independent batch items progress; durable quarantine/checkpoints/retries survive interruption. |
| A07 | History list/show and revision-bound duplicate/supersedes/contradiction relations; no hidden merge, truth selection or unauthorized historical content. |
| A08 | Retire versus correct semantics tested across source updates, search, history, vectors and existing export. |
| A09 | Owner-confirmed live forgetting removes exact dependency closure, blocks resurrection and passes durable-store/vector/new-export canary scans after interruption. |
| A10 | Explicit safe Markdown root rebind preserves source identity/history; ordinary changed-root refusal and self-import/overlap protections remain. |
| A11 | Public optional bounded PDF/DOCX import and picker; revision continuity, confirmed moves and honest missing/inaccessible state; parser/security negatives. |
| A12 | Optional Linux user service lifecycle and foreground mode proven without root; macOS ownership/cleanup regressions preserved. |
| A13 | Optional local model exact-artifact install/offline/error/lifecycle proof and frozen-fixture quality/resource thresholds on both platforms. |
| A14 | Joined versioned catalog truth tested against actual installed feature/package/auth/readiness state; public promotion requires evidence. |
| A15 | Exact reproducible core and optional artifacts with compatibility, clean installation and removal on both supported platforms. |
| A16 | Only documented supported commands needed for a fresh-user capture/import-to-canonical-vault/search/read journey; exact doctor examples pass. |
| A17 | Native GUI/plugin/provider and production OAuth receipts, platform-specific timing, credential-custody/redaction and failure/revocation coverage. |

Receipt format: task/finding IDs, source commit and dirty-state statement, artifact versions/hashes, platform/architecture and relevant hardware, commands or manual script, expected versus actual outcomes, fixture identity, timestamps, pass/fail/skip reasons and bounded sanitized evidence. Provider receipts add auth mode/scopes and expiry/refresh conditions, never tokens. Every skipped required gate remains incomplete.

## Orchestration and verification rules

The coordinator reserves at most three concurrent workers in this four-slot session; no nested dispatch. Default milestone budget is twelve total worker attempts. Split at verified milestone boundaries rather than run the entire program in one thread. A documented override is required before exceeding the budget. Actual spend, when available, governs optional review depth; worker count is not a cost measurement.

| Work | Explicit model | Reason |
|---|---|---|
| Mechanical inventory, mappings, documentation consistency | `gpt-5.6-luna`, high | Bounded extraction and checks |
| Read-only exploration, normal adapters/UI/tests, packaging and provider research | `gpt-5.6-sol`, high | Substantive but bounded implementation/research |
| Source/schema/provenance/purge architecture and implementation, critical independent review | `gpt-6-astra`, high | Cross-contract correctness and destructive-state risk |

Use governance-rendered prompt files containing goal, authoritative finding/task IDs, owned files, dependencies/frozen contract, invariants, exact tools, required tests, deadline/stop condition and report path. Research remains read-only. Use isolated task worktrees during implementation; one worker owns each central schema/registry/fixture. Escalate after a demonstrated cross-contract risk or changed-strategy failure, not simply because work is long. Two identical failures require a new approach, not a third identical retry.

Validate every report is nonempty and each claimed test has evidence before consuming it. Every substantive implementation has an independent reviewer; storage migration and purge require a strong correctness/security review. The coordinator reconciles findings, integrates serially and checks the coverage register. Documentation cannot substitute for an unimplemented requested workflow.

Run focused real tests for each changed surface. At integrated mergeable boundaries run project-required `make verify` and `git diff --check`; run workflow lint when CI changes. Native/package changes add `make native-audit` and `make homebrew-smoke`. Final contributor acceptance uses `make contributor-check` plus separate exact-candidate native GUI/provider scripts. Reconfirm commands from current repository instructions before execution. Do not rerun an unchanged full suite merely because another worker finishes.

Core regression invariants include idempotent Markdown imports, changed/missing-file handling, managed-note reactivation, source/canonical ordering, review/publication provenance and redaction, collector replay/checkpoints/rate limits/owned cleanup, bounded document parsers and verified byte-preserving exports. New migrations may version Portable metadata, but existing supported payload semantics and fail-closed compatibility remain tested. Excluding restore development is not permission to remove related regression tests.

### Working across multiple root sessions

Use one coordinator session plus two or three implementation sessions, each in its own Git worktree and task branch. This is preferable to several sessions editing one checkout. A lighter option is one writer plus read-only research/review sessions in the same checkout; multiple simultaneous writers in one checkout are not the recommended model. Worktree isolation prevents file interference, not incompatible design decisions.

Before starting workers, commit the approved plan, coverage map and frozen contract artifacts to a local coordination branch, or distribute a verified immutable plan bundle. New worktrees do not inherit untracked/uncommitted files. At the end of this planning turn the new plan files are untracked; they must be made available before a separate worktree is asked to use them. No remote push is necessary for sessions on this machine.

The coordinator alone updates the program assignment ledger and master coverage map. Each assignment records task IDs, exclusive write paths, prerequisite commits, contract version/hash, base commit, worktree/branch, model, acceptance commands, state and report location. States distinguish assigned, active, returned, integrated and verified; a worker saying done does not mean the program task is verified. Reserve file ownership before dispatch. Another session cannot silently take over a live assignment or change a frozen interface.

Each root session gets its own generated workstream directory and STATE/HANDOFF packet. It may read the coordinator plan but writes only its owned code/tests and its own workstream state. Never share one worker HANDOFF file or let every session rewrite the master plan. The coordinator can publish updated plan/contract commits at explicit synchronization points; workers pin and acknowledge the new version instead of discovering incompatible changes halfway through an implementation.

| Parallel wave | Session A | Session B | Session C |
|---|---|---|---|
| Initial | M1 T01–T02 correctness fix and composed tests | T03 contract design and compatibility fixtures | Read-only model/platform research and synthetic relevance fixture design; no engine integration |
| After contract freeze | T04–T05 engine identity/migration/retrieval, with one schema owner | T06 app/bridge adapters against frozen fixtures | T09 collector containment; T16 isolated helper spike once its boundary is frozen |
| After shared APIs land | One bounded import/lifecycle task | T07 Obsidian client | T07 desktop client, or a separate collector task if desktop is not ready |

These are dispatch examples, not three permanent giant branches. Split T07 into explicit package-owned subtasks and select only ready tasks from the dependency graph. Do not run T12 purge concurrently with another owner changing lifecycle/schema contracts. If adjacent tasks need the same central file, serialize that part or have the designated owner integrate both changes.

Return a small commit with focused test results, exact base/HEAD, finding/task IDs, changed files, unresolved risks and a validated handoff. No worker pushes, merges the integration branch or marks global findings closed. The coordinator reviews and integrates one returned change at a time, reruns the real integrated checks, updates the ledger, then tells dependent sessions which commit to adopt. Rebase or cherry-pick only known owned work; do not reset another session's worktree. Retest after conflict resolution. Shared account/model concurrency and spend limits apply across sessions; opening another root thread does not reset the coordinator's dispatch budget.

### Planning review record

The installed doc-review pipeline reviewed the draft with coherence, feasibility and security lenses. Initial coherence timed out; a single retry completed with independent refutation and a ready verdict. Its one retained P3 was a 200-result boundary inconsistency, corrected to at least 201 results in T05 and A04. The initial feasibility/security findings were rejected for unlocatable evidence, so they do not certify those risk areas. Strong contract, migration and purge reviews remain mandatory implementation gates.

Verbatim reports and disposition: `docs/audits/2026-09-16-new-user-plan-review.md`; structured results: `docs/audits/2026-09-16-new-user-plan-review.json`. The multi-session section and explicit T03 security proof obligations were coordinator additions after the reviewed draft; they were checked for consistency, not rerun through the independent pipeline. Coverage/structure validation is separate from product verification. No implementation or product tests were performed in this planning milestone.

### Authority and stop conditions

This milestone creates and reviews the plan only. It does not authorize product implementation, live data migration/deletion, connected-account access, service installation, model downloads, public OAuth registration, credential changes, pushes, remote merges or release publication. A later implementation instruction authorizes scoped synthetic/local work; real destructive confirmation and outward-facing actions still have explicit gates.

Completion of the implementation program means every active coverage entry has passing evidence from the supported exact candidate, or the user has explicitly approved a scope change. A blocked model/provider/platform gate remains open. Only the backup/restore follow-up is excluded by the current request. Before starting a new root thread, checkpoint actual branch/HEAD, tests, outstanding gates and one next action in the workstream handoff.

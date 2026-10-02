# Saved Markdown lifecycle contract

Status: checkpoint A design, October 1, 2026. This document specifies work for
checkpoints B and C. It does not implement or enable withdrawal or sharing.
The inspected baseline is `58281d1dafad5e73265bd74b6f1bae27ba130972`, with local
schema 10, runtime session 5, and Portable 5. Product version `0.1.0` is not a
compatibility test. A changes this document only.

## Boundary and terminology

The engine owns logical sources, revision order, lifecycle decisions, approvals,
copy links, authorization, and recovery. The optional connector reads selected
files; the collector owns bounded scans and delivery custody. The application
exposes owner decisions. None of this adds a scheduler, network listener, or
service dependency to the foreground core.

A **source** is one proven logical item. A **revision** is an immutable capture
within that source. **Withdrawal** retires the source from current retrieval.
An **approval** permits one exact revision to have a separate managed public
copy for an explicit provider set. **Rejection** records a negative decision
before copy admission. **Revocation** disables an approved copy for subsequent
external access. None of these operations deletes original evidence.

Existing `ReviewTasks`/`ReviewPublicationService` approve canonical Markdown
pages. They do not grant external sharing. Their source-bound review tokens,
page heads, and publication records remain separate from sharing approvals.
Routing likewise changes assignment, not privacy or publication. Source
withdrawal does not delete or silently rewrite a reviewed canonical page;
external access to a page or evidence derived from a managed source must still
pass the applicable source/sharing checks below.

Only saved-Markdown items admitted through the new bounded revision binding and
copies created through the new sharing task acquire this managed policy. Do not
invent approvals for unrelated captures or alter their retained privacy.
Historical adoption requires a separately reconciled receipt and the existing
engine adoption contract. Equal bodies, URLs, paths in another root, or names
are not adoption proof. Private source selection, installed bindings, receipts,
schedules, and deployment remain outside this contract.

## Operations and authority

Names marked **B** or **C** are selected new interfaces, not available commands.
Paths in the tables are package-relative: engine files live under
`packages/engine/src/open_brain_engine/engine/`, app service files under
`packages/app/src/open_brain/services/`. New DTOs are closed frozen values with
strict types and version 1; unknown fields, duplicate JSON keys, non-finite
numbers, invalid UTF-8, and oversized values are rejected before reservation.
Engine tasks own validation even when the CLI already validated a request.

### Existing operations retained

| Engine operation and DTO | Owner and app surface | Authority, binding, and replay |
| --- | --- | --- |
| `SourceTasks.submit_revision(SourceRevisionSubmission) -> SourceRevisionReceipt` | `sources.py`, `source_intake.py`; currently a Python task, not a saved-Markdown CLI command | Validated `PUBLIC_JOB` capture/profile; namespace, revision key, capture request digest, expected head, order, control epoch. Existing terminal replay is keyed by namespace/revision and capture request digest. It does **not** prove equality of the whole retry envelope. B wraps it with the stricter binding below. |
| `SourceTasks.fence_intake(expected_epoch, authority) -> int` | `sources.py`; trusted owner coordination hook | `authority.owner`; epoch CAS. Advances the fence and quarantines stale unfinished intake. It is not withdrawal and does not provide operation-ID replay. |
| `SourceTasks.route(SourceRouteRequest) -> SourceRouteResponse` | `sources.py`, `t03_contracts.py`; existing `source route` owner surface | `organize`, current source/destination space scope; operation ID, expected head and route version. Same request returns retained receipt; changed request under that ID refuses. |
| `HistoryTasks.list_history(HistoryListRequest)` / `read_history(RecordReadRequest)` | `history.py`, `records.py`, `paging.py`; `history list/show`, existing negotiated history reads | `history-read` and current scope/privacy. Exact revision membership is checked. Today inactive/unavailable sources fail even for owners; B supplies the narrowly scoped retained-owner-history exception. |
| `RetrievalTasks.search_page(SearchPageRequest)` / `read_record(RecordReadRequest)` | `retrieval.py`, `paging.py`, `records.py`; `search-page`, `read`, negotiated CLI/plugin/MCP readers | `search` or `content-read`; trusted session authority, expected revision, bounded cursor. Current source reads resolve the head; an old expected head refuses. |
| `ReviewTasks.propose/show/decide`, `ProposalDraft`, `ReviewProposal`, `DecisionOutcome`, `DecisionRecord` | `review.py`, `review_bound.py`; `ReviewPublicationService`, `review propose/show/approve/reject/edit-and-approve` | Existing owner or explicitly injected review grants, exact review digest and delivery identity. Canonical-page approval, rejection, and replay retain their current meaning; none creates a sharing approval. |

### Selected lifecycle and sharing operations

All new owner operations require `authority.owner` **and** owner-local egress
mode, the current Brain/issuer identity, and a trusted local runtime. Ordinary
agent grants, including `review-decide`, cannot invoke them. No new MCP sharing
mutation, plugin wire operation, or agent grant is introduced for the pilot.

| Stage, task, and DTO | Owning module and app/CLI surface | Exact request and result contract |
| --- | --- | --- |
| **B** `SourceTasks.public_revision_sink(binding, context) -> PublicJobRevisionSink` | `sources.py`, `source_intake.py`; injected into collector `EngineRevisionSink` in `lifecycle.py` | Engine validates `SourceRevisionBinding` and `PublicJobCaptureContext`. The sink exposes only `inspect_head(item_id)` and `submit(SourceRevisionDelivery)`. No owner routing, fencing, withdrawal, or sharing tasks escape to the collector. |
| **B** sink `inspect_head -> SourceRevisionHead` / `submit -> SourceRevisionDeliveryReceipt` | Engine `source_intake.py`; collector `saved_markdown.py`, `custody.py` | Head response contains bound item/source IDs, capture/revision key, lifecycle/version, control epoch, and destination. Delivery and terminal receipt bind the complete envelope digest and destination. Pending is explicit and cannot complete custody. |
| **B** `SourceTasks.inspect(SourceInspectRequest) -> SourceInspection` | New `source_lifecycle.py` and `source_lifecycle_contracts.py`, delegated by `SourceTasks`; new app `source_lifecycle.py`; `source inspect SOURCE_ID --json` | Owner-only bounded metadata: head, head/route/lifecycle versions, lifecycle/availability, destination, and retained withdrawal receipt. Optional absence evidence is opaque metadata, not a path or body. No mutation or approval token is implied. |
| **B** `SourceTasks.withdraw(SourceWithdrawRequest) -> SourceWithdrawReceipt` | Same owners; `source withdraw --request-file FILE --json` | DTO binds operation ID, source ID, expected head, expected lifecycle version, Brain/issuer, reason code, and optional complete-scan evidence digest. Atomically retires the source and increments lifecycle version. Identical replay returns the original receipt even after later state changes; altered ID reuse refuses, stale new requests return `revision_changed`. |
| **C** `SharingTasks.preview(SharingPreviewRequest) -> SharingPreview` | New `sharing.py`, `sharing_contracts.py`, wired through `EngineTaskSet`; new app `saved_markdown_sharing.py`; `sharing preview --request-file FILE --json` | Request binds operation ID, source/head/lifecycle/route versions, Brain/issuer, and explicit nonempty provider set. Engine freezes an immutable preview, exact copy payload, all evidence digests, and engine-issued expiry. Same ID/exact request returns the same preview; changes refuse. Preview confers no egress. |
| **C** `SharingTasks.inspect(SharingInspectRequest) -> SharingInspection` | Same owners; `sharing inspect PREVIEW_OR_APPROVAL_ID --json` | Owner-only full exact preview text or bounded refusal, binding digest, expiry, decision/copy state, and immutable receipts. A terminal-safe human display is visibly escaped; its JSON payload and digest remain exact. |
| **C** `SharingTasks.decide(SharingDecisionRequest) -> SharingDecisionReceipt` | Same owners; `sharing approve/reject --request-file FILE --json` | Operation ID, preview ID/digest, destination, expected decision version 0, and decision `approve` or `reject`. Approve revalidates all preview bindings and admits exactly one copy. Reject writes a terminal rejection with no capture admission. Repeated exact request returns the same receipt or the same pending operation. Conflicting decisions/new IDs on a decided preview refuse. |
| **C** `SharingTasks.revoke(SharingRevokeRequest) -> SharingRevokeReceipt` | Same owners; `sharing revoke --request-file FILE --json` | Operation ID, approval ID, expected approval version, Brain/issuer, and bounded reason. Appends immutable revocation, updates the current approval version, and closes external visibility atomically. Same request replays; altered/stale requests refuse. It works for pending as well as completed approved copies. |

Every mutation receipt includes its operation ID, request digest, Brain/issuer,
subject IDs, resulting version/state, and a retained receipt digest. Approval
receipts additionally bind preview, approval, and copy delivery IDs and, only
when terminal, the capture ID. `pending` is never disguised as `approved` or
`captured`. Request files are owner-local inputs, not an alternate authority.
The service reserves response capacity before mutation and emits the effective
operation ID before a retry can become ambiguous.

Rejected approvals are terminal. A later attempt needs a fresh preview and a
new decision. Revoking an approval never changes its original decision or the
copy's immutable privacy record. No reactivation, undo-revocation, or source
deletion operation is selected in this phase. Returning files remain withheld
until a separately designed explicit owner transition is authorized.

## Identity, ordering, custody, and scans

### Identity and order decisions

Use canonical JSON with domain-separated SHA-256, not delimiter concatenation,
for the new identities. `SourceRecordKey.delivery_id()` remains unchanged for
other connectors. The saved-Markdown revision adapter uses its own delivery
envelope rather than changing that shared method's semantics.

| Identity | Selected binding |
| --- | --- |
| Logical item | `saved-markdown-item.v1`, Brain ID/issuer epoch, accepted source identity, and normalized root-relative path. Normalize separators and Unicode NFC, reject absolute paths, traversal, controls, and normalization collisions. Preserve case; do not merge differently spelled items. Root device/inode and physical path are private operational bindings, not global item identity. |
| Engine namespace | Existing exact keys `connector_name`, `connection_id`, `resource_id`, `external_id`; `saved_markdown`, opaque accepted-source binding, opaque destination binding, and the stable item digest respectively. Body, policy, and delivery IDs never enter `external_id`. |
| Version fingerprint | Original file SHA-256, exact transformed UTF-8 SHA-256, normalization version, complete privacy-policy digest/version, and normalized capture request digest. Capture normalization can change NFC/line endings; retain both adapter and admitted-payload digests rather than assuming equality. |
| Revision occurrence | Logical item, version fingerprint, predecessor capture and revision key, with null predecessor for initial admission. Reverting A → B → A creates a new occurrence rather than replaying the first A. Observing unchanged bytes/policy at the same accepted head is a no-op. |
| Delivery | Revision occurrence, complete capture request/delivery value, expected head, order evidence, engine control epoch, destination, and accepted binding. An uncertain retry preserves this identity even if the file, head, or policy subsequently changes. |

Select existing **predecessor ordering plus CAS**. The first observation uses
`ordering={"kind":"unordered"}` and `expected_head=null`; later observations
use `{"kind":"predecessor","revision_key":PREVIOUS_KEY}` and the proven
accepted head. One item has at most one unresolved delivery. Filesystem mtime
is read-stability evidence only, never provider order. A source-owned monotonic
epoch/sequence is an architecturally valid alternative, but would require a
durable order authority and reset semantics that this adapter does not need.

Stale head/control/lifecycle observations refuse. Equal revision identity with
different bytes is a conflict retained in custody/quarantine. A stale request
is never silently rebased. A new observation is created only after the prior
request has a verified terminal result or an explicit owner resolution.
Renames are a new candidate plus a possible disappearance candidate until
identity reconciliation is proved. Baseline adoption remains a private receipt
reconciliation step; B does not auto-adopt Phase 2 or imported captures.

### Closed delivery envelope

`SourceRevisionDelivery` version 1 freezes these fields before the first call:

| Field group | Required values |
| --- | --- |
| Destination and capability | Brain ID, issuer epoch, opaque sink/root fingerprint, accepted source ID, binding generation, collector control epoch, and validated public-job context commitment. |
| Observation | Stable item digest, revision occurrence, raw and transformed digests, normalization and privacy-policy versions/digests, complete normalized intake including title/reference/provenance/privacy, and admitted-payload digest. |
| Engine request | Exact `SourceRevisionSubmission.custody_bytes()`: namespace, revision key, canonical capture-request SHA-256, expected head, order, engine control epoch, full capture request, capture delivery ID, and file bytes if applicable. Also bind expected lifecycle version for the managed source. |
| Replay | Envelope contract version, canonical envelope bytes/digest, stable delivery ID, and expected terminal-receipt binding. Store full payload only in protected pending custody; retained terminal evidence keeps hashes/IDs/receipts. |

The engine persists a managed-delivery reservation keyed by delivery ID and
whole-envelope digest before delegating to existing source admission. This
closes two current gaps: `SourceRevisionReceipt` has no destination/request
digest, and existing replay compares the capture request digest without
rechecking all order/head fields. Do not change existing DTO version 1 in
place. The new receipt wraps the durable source result and exact managed
reservation; the collector verifies both before advancing its accepted head.

`operation_pending`, a journal custody receipt, lost response, or transport
failure retains the same envelope. `captured` advances the accepted head only
after source/capture/request/destination verification. `history_only` and
`quarantined` are explicit terminal source results, never evidence of a new
current head; quarantine retains its envelope for owner resolution. The chosen
predecessor path should not normally produce `history_only`, but must reject
an unexpected receipt rather than infer promotion. Collector page/checkpoint
progress requires verified terminal resolution for every staged item.

Preserve the current pause/disable cancellation barrier: no new admission after
its acknowledgement. Cancellation may discard an unsubmitted observation, but
must retain an uncertain or engine-accepted revision envelope for reconciliation.
Do not call a successful pause proof of completed capture or change its terminal
receipt race handling without the existing lifecycle regressions.

### Full inventory is separate from delivery

Add `SavedMarkdownScanEpoch`, `SavedMarkdownScanPage`, and
`SavedMarkdownAbsenceCandidate` in connector `saved_markdown.py`, with durable
scan state owned by collector `saved_markdown.py` and its private store.
One run still delivers at most 25 items. Inventory pages visit at most 256
directory entries and read at most 25 candidate bodies; unchanged/refused items
advance the inventory cursor. Each run remains bounded by its time/byte budget.

An epoch persists an opaque ID, root device/inode binding, selection digest,
policy/normalization versions, destination and control bindings, deterministic
directory traversal continuation, seen item IDs with presence/refusal state,
directory membership fingerprints, and accumulated errors. Continuation is
server-owned state with a digest, not an arbitrary client filesystem path.
Resume its saved directory worklist after restart; do not restart at page one.
If the bounded state quota is exhausted, retain an incomplete epoch and report
the bound rather than declare completion.

Complete traversal must be followed by bounded validation of recorded directory
membership/read-stability evidence under the same binding. Changed directory
membership, unstable reads, permission/traversal errors (including `os.walk`
errors), changed root/selection/policy/destination, or interrupted validation
invalidate completion. Budget exhaustion is resumable incomplete work. Presence
with a normalization refusal is still presence and cannot become disappearance.
Local filesystems do not supply an atomic tree snapshot: the resulting evidence
is a verified observed scan, not a claim about future filesystem state.

Only a completed epoch can emit a metadata-only absence candidate containing
item/source IDs, last terminal head, lifecycle version, epoch/evidence digest,
destination, and selection generation. The candidate does not call the engine.
Owner inspection rechecks current source state and candidate freshness; a known
return invalidates that candidate. Withdrawal remains an explicit operation and
CAS even if a file appears after the scan. Partial scans can deliver valid
present items, but cannot provide absence evidence. No candidate contains source
text, root paths, or provider URLs in logs.

## Approval and external eligibility

Preview is for an accepted engine revision, not unobserved current filesystem
bytes. It requires an active managed source with available retained evidence,
known supported normalization/policy, and a public-tier third-party payload.
Unknown, malformed, unsupported, or secret-bearing material remains withheld.
The explicit owner decision supplies publication authority; normalization and
folder location supply none.

The engine freezes the original source/capture IDs, head/lifecycle/route versions,
exact retained capture and provenance digests, adapter and admitted-payload
digests, normalization version, complete original privacy-policy digest, proposed
copy privacy, Brain/issuer, sorted unique provider IDs, and expiry. Expiry is
15 minutes after preview creation using the engine clock. The provider set must
contain 1–16 explicit provider IDs validated against the existing provider-ID
syntax and the application's supported configured providers; there is no
default, wildcard, or provider fallback.

The preview returns the entire exact copy text within the current 65,536-byte
pilot bound, plus its UTF-8 digest. If exact representation or the response
budget cannot fit, refuse. Do not approve an excerpt. Derive the copy payload
from the retained normalized body and supported provenance; if additional
projection/redaction would change the text, refuse and require a separately
inspected payload. Approval rechecks the actual immutable source bytes and every
binding under the writer fence immediately before reservation.

The copy uses `CaptureSubmission.for_public_job`, `TextPayload` containing the
exact approved body, third-party provenance, quick-capture action, and a
validated engine-bound public-job identity. This is a third-party public-job
text capture, not an owner-authored fallback. Select the supported
`source_reference` and `Provenance.source_ref` value
`urn:open-brain:sharing-copy:v1:PREVIEW_UUID:ORIGINAL_CAPTURE_UUID`. Allocate the
preview UUID before hashing the preview, so there is no recursive digest. Its
single decision links the eventual approval. The original source reference and
capture remain linked in the approval evidence. A text payload
avoids prepending a reference URL to the approved body during retrieval.

The copy has public privacy with `cloud=false` and `external_egress=true` under
this exact approval; original cloud/egress flags never change. Cloud inference
remains separately gated by the managed-consent and ancestry checks below. The
link sidecar, not an invented field in a v1 capture, durably binds original
revision, approval, exact copy capture, submission digest, and destination. Do
not embed private actor IDs, role IDs, or deployment paths in production code.
The engine allocates and persists one non-owner sharing job context per Brain
using ordinary UUID4 actor/role/claim IDs and only `capture.accept` capability.
`PublicJobCaptureContext.create` validates it against the current profile.
Approval evidence carries this context so clean restore and replay use the same
capture request identity; collector admission never receives it.

Copy delivery is `sharing-copy.v1` plus the immutable approval ID and submission
digest. Repeating approval with a new operation ID cannot create another copy
for the same preview. Changing a provider set requires a new preview/approval;
it never edits an existing approval. Head advancement, source withdrawal,
revocation, route divergence from the approval, or invalid/missing evidence
makes that copy ineligible. A later source revision does not inherit approval.
An already delivered payload cannot be recalled from a provider.

### Mandatory access checks by surface

For a managed linked copy, external eligibility is the intersection of valid
link and approval, approved current source head/lifecycle/route, exact copy bytes
and privacy, matching Brain/issuer, explicitly allowed provider, current trusted
consent/generation, permitted tier/space, and the operation's capability. Missing
evidence denies before reading or emitting body/title/excerpt. An original
managed local-only revision is never made externally readable by approval of
its copy. Apply the same check to all retained provenance members when a result
or prompt can expose their content.

| Surface and current owner | Required B/C behavior |
| --- | --- |
| Trusted sessions: app `session_authority.py`, `session_consent.py`, `launcher_policy.py`, `local_entrypoints.py`, `local_mcp.py` | Keep loading and revalidating current consent/policy per request and immediately before output. Provider/destination/owner fields come from trusted startup, never tool arguments. Sharing does not grant a session or broaden an existing one. |
| Current paged reads: engine `paging.py`, `records.py` | B hides retired sources for everyone. C gates managed copies and relevant provenance before ranking, excerpting, projection, or chunking. SQL candidate filtering and final projector checks must agree; a filtered result cannot affect visible rank, counts, or boundaries. |
| Legacy `search/fetch/read_page/scoped`: engine `retrieval.py`; inbox/organization projections in `spaces.py` | B removes retired sources from current materialized search/inbox and rechecks eligibility at reads. C excludes incomplete/disabled managed copies from these projections. These older APIs lack provider authority and remain owner-local only; external sessions must use authority-bearing paged APIs or refuse, never fall back to legacy retrieval. |
| Exact source/canonical history: `history.py`, `records.py`, `paging.py` | B allows owner-local retained source history despite retirement/unavailability, while verifying exact membership and actual retained bytes. Missing/corrupt bytes still refuse. C retains originals/copies for owner history; external history remains constrained by current link eligibility. A `history-read` grant does not resurrect a stale copy or expose its original. |
| Relationship and decision evidence: `relationships.py`, canonical member projection in `records.py` | Revalidate both endpoints and every content-bearing provenance member. Omit unauthorized edges/evidence and identifiers that disclose hidden members. Owner history can inspect retained decisions; external relationships cannot bypass source withdrawal or revocation. |
| Review evidence and inbox previews: app `review_publication.py`, engine `review_bound.py`, MCP injected callbacks | Canonical review remains independent. External sessions cannot receive managed original/copy bodies through review/inbox callbacks unless each member passes the same policy. These callbacks currently lack the complete provider-aware engine projector; keep them unavailable externally until they can enforce it. Existing local review grants remain unchanged. |
| Managed semantic egress: `managed_inference.py` selection, retained ancestry, `prepare` and `release`; app `managed_providers.py` | C checks every retained source ancestor and the exact selected provider at both preparation and release. Generic managed consent must not promote a managed original or stale/revoked copy to eligible cloud input. Existing `_effective_privacy` can derive cloud authority for public/work records, so stored cloud=false alone is insufficient. Copy ancestry carries its original linkage for verification without granting read access to the original body. Unknown ancestry fails closed. |
| Cursor generation/bindings: `paging.py`, `cursors.py`, `history.py` | Keep current authority binding dimensions. Include managed eligibility in provider-visible digests and projection policy generation. Source advancement/withdrawal/revocation invalidates affected search/read/history cursors before more bytes. Consent changes invalidate the trusted session. Hidden unrelated writes retain the existing scoped-cursor behavior. |
| Startup, rebuild, reconciliation, managed vault, Portable: `local.py`, `source_store.py`, `portable_index.py`, `reconciliation.py`, `managed_workspace.py` | Derive views only from validated durable state. Rebuild/restart/import cannot republish disabled copies. A local managed-vault file is not an external grant; semantic export of it rechecks retained ancestry. Portable export is owner-only and preserves evidence, not provider/session permission. |

Put the reusable managed-eligibility predicate in engine `sharing.py`; use it
from projection and semantic paths. Owner history and external retrieval are
different modes with explicit checks, not a global `history=True` bypass.

## Version and migration decisions

A leaves every format and compatibility constant unchanged. B and C are
independently reviewable checkpoints, so each receives the smallest new schema,
runtime floor, and Portable format needed for its actual durable semantics.
Pre-creating unused C tables/sidecars in B would couple the checkpoints;
adding closed metadata to an unchanged Portable version would be incompatible.

| Stage | Local schema / minimum runtime session / Portable output | New metadata and format owners |
| --- | --- | --- |
| Baseline/A | 10 / 5 / 5 | Existing migration SQL/checksums, `JournalEnvelope` v1, `SourceRevisionSubmission` v1, T03 DTO v1, capture/publication schemas, and Portable 1–5 remain frozen. |
| B | 11 / 6 / 6 | Append migration 11 in `local_schema_catalog.py` via the existing exclusive migration coordinator. Add `source_lifecycle`/immutable lifecycle operations and managed source binding/delivery evidence; preserve existing `logical_sources` columns and source sidecar v1 shape. New required `history/sources/lifecycle-v1.json` and `history/sources/admission-v1.json` carry lifecycle versions/receipts and terminal revision binding/order/replay evidence. New `portable/v6.py` validates a closed catalog; engine v6 evidence/restore helpers own SQL mapping. |
| C | 12 / 7 / 7 | Append migration 12 with immutable preview/decision/link/revocation records, the bounded sharing job identity, current approval projection, and copy-operation custody. New required `history/sharing/approvals-v1.json` carries job identity, previews, decisions, exact links, revocations, and terminal operation receipts. `portable/v7.py` extends the frozen v6 inventory with its own catalog; engine v7 evidence/restore helpers own mapping. |

New owner CLI DTOs use distinct contract names/version 1. Existing T03 request,
response, error schema and plugin protocol do not gain fields. Existing external
reads gain stricter eligibility behind the same read DTOs; raise the persisted
projection-policy generation. New owner operations use their own bounded error
enum (`invalid_arguments`, `unsupported_capability`, `not_found`,
`revision_changed`, `operation_pending`, `preview_expired`, `binding_mismatch`,
`response_too_large`) rather than extending an unchanged closed T03 error enum.

The local classifier, coordinator, read-only opener, runtime session registry,
app catalog/handshake, and desktop native runtime admission must all agree on
each new schema/floor. An older client must refuse before recovery or writes;
the shared product version does not make it compatible. Update their fixtures
and generated declarations in B/C only. Native admission/packaging changes
require the project native audit and Homebrew smoke checks as well as ordinary
verification. No dependency change is selected.

### Durable metadata mapping and old evidence

| Evidence | Migration, validation, export/import requirement |
| --- | --- |
| Existing capture, publication, source, privacy and issuer evidence | Preserve exact historical bytes, IDs, privacy storage values, migration checksums and current supported old-format behavior. Existing versioned validators recognize Portable 1–5; the application has a specific standalone-v4 import refusal. Preserve that distinction and its exact regression fixtures. |
| Lifecycle | New rows start at lifecycle version 0 and preserve the actual existing lifecycle/availability. No migration invents an owner withdrawal receipt for an already retired source. Each new transition binds prior/result state, request, actor, destination, sequence, and exact receipt. Sidecar state must agree with the unchanged source metadata. |
| Revision admission | Include path-free managed namespace/binding, per-revision occurrence/key/order/request digests, stable delivery ID and terminal receipt, with cross-references to retained captures. Existing v5 restore fills request/key/order columns with NULL and does not prove provider continuity; imports without this evidence remain unbound until explicit reconciliation. Never fabricate it from sequence or timestamp. |
| Sharing | Preserve immutable preview bytes/digests, policy/provider/destination bindings, approval/rejection/revocation sequence and receipts, copy membership, and submission digest. Validate referential integrity, unique copy per approval, linear versions, exact byte hashes, source membership, and derived active eligibility. Recompute derived state instead of trusting exported `active=true`. |
| Local operational state | Filesystem root bindings, active sessions, consent files, scan continuations, cursor keys, and pending collector envelopes are not Portable authority. Fresh restore requires new local bindings and consent. Imported previews remain inspectable; executing an undecided preview requires a fresh local preview. |

New exporters use one writer fence plus one SQLite read snapshot for all
authoritative evidence. Validators consume captured snapshot bytes and reject
missing/extra sidecars, tampering, inconsistent links, invalid types, unsupported
versions, and recomputed manifests over semantically invalid evidence. Import
restores into a hidden stage, validates semantic state and index before promotion,
and repeats that audit on same-import-ID retry, following the v5 boundary.

B/C support existing accepted old imports through their current migration paths;
they do not silently enable the currently refused standalone-v4 path. New output
is always the current version. Never downgrade an archive by dropping lifecycle
or sharing sidecars. V6 and V7 retain unchanged v1–v5 payloads where those formats
already require exact preservation. New historical source records without a
proven managed binding keep their prior policy rather than becoming approved.

Preserve the existing `ingestion_pending` export refusal for retained pending or
quarantined journal payloads. Extend it to unresolved managed delivery or copy
operations that have no complete canonical linkage. Pending-state acceptance
tests prove refusal and successful export after reconciliation, not an unsafe
Portable serialization of operational queues. Private backup restoration of
collector custody remains separate work.

## Transaction and crash contract

Use the engine's shared-writer fence, SQLite transactions, durable ingress
journal, and staged materializer. Do not hold a transaction across user review.
Filesystem evidence may be written between transactions; DB operation state is
the recovery authority, and a missing/mismatched sidecar blocks export until
engine recovery regenerates it. Never expose a public copy solely because its
capture stage is terminal.

| Boundary / crash point | Durable state and required recovery |
| --- | --- |
| Before collector custody save / after save before delivery | Before save there is no admitted work. After save, replay the same envelope without reading a newer file, head, policy, or destination. Failure to persist means do not call the engine. |
| Managed revision reservation / capture journal admission | In one DB transaction reserve exact envelope and expected source/lifecycle/control state. Use the existing source intake and capture journal thereafter. Recovery sees the reservation before any capture materialization, checks withdrawal/fence state, and either completes the same delivery or quarantines it; never a new capture ID on retry. |
| Source capture/source-file/blob materialization / stage-three linkage | Retain existing `register_intake` atomic source revision/head linkage with capture stage three. Add managed receipt linkage and eligibility invalidation in that commit. Source advancement invalidates prior approvals before any current read can see the new head and old eligible copy together. |
| Owner withdrawal | Validate destination, ID replay, head/lifecycle CAS and pending intake under the writer fence. Unresolved intake returns pending until reconciled, or is explicitly quarantined before retirement. One transaction writes decision, lifecycle version/state, receipt and current-view invalidation. A returning/queued submission cannot set lifecycle active. |
| Preview / approval validation | Preview is durable but non-egress. In the approval transaction revalidate exact bytes/head/route/policy/providers/destination and unexpired preview; reserve decision, immutable approval, copy delivery/envelope and mandatory pending-copy marker together. Rejection reserves only its terminal negative decision. |
| Copy journal admission / accepted capture before link | The copy is managed and externally ineligible from reservation, before enqueue. Its immutable capture contains a domain-bound public-job provenance commitment identifying this managed operation through supported fields. Engine inventory and rebuild must recognize missing linkage and refuse, rather than treating an unlinked capture as an unrelated public capture. Lost responses retain the exact copy envelope and replay key. |
| Link materialization / index update | Validate capture receipt, exact request, destination and current approval/source state. Atomically materialize link, terminal operation receipt and eligible current projection. If source/approval became stale meanwhile, retain a linked historical copy with no external eligibility. Index is derived; rebuild must give the same result. |
| Receipt committed / response lost | Replay returns the exact stored receipt. Collector may release payload custody only after checking that receipt and durably updating its checkpoint. Acknowledgement loss does not create another delivery or approval. |
| Revocation during pending copy / after completed copy | Atomically record revocation and invalidate current eligibility/cursors. Recovery may finish already accepted evidence as history but cannot reactivate it. Replaying original approval returns its historical outcome, never repeats the side effect or removes revocation. |
| Startup or clean restore | Validate lifecycle/approval/link evidence before constructing current search, inbox or semantic views. Preserve history, derive eligibility, rotate local session/cursor bindings, and refuse incomplete evidence. Restored consent and physical source bindings cannot be inferred from an approval. |

The mandatory managed-copy marker is the reserved URN above, retained unchanged
in v1 capture source/provenance fields. Existing `Provenance.create` and
`CaptureSubmission` accept these text fields, and `capture.py` preserves them.
Do not add transformation-receipt fields to the Python `Provenance` DTO, whose
shape is closed. Only the sharing task may admit the reserved URN namespace;
ordinary public-job capture attempts using it refuse. Missing, malformed, or
mismatched approval/link evidence for such a marker fails closed at reads,
rebuild, recovery, and Portable validation. C must test this marker end to end,
including accepted captures with the DB link deliberately removed.

## Evidence and implementation acceptance

The baseline's recorded full Python run passed **3,391 tests**, with five
filesystem-specific skips: one non-UTF-8 filename case and four NFC/NFD name
distinction cases. Ruff and mypy passed (409 source files), plugin tests passed
291 cases, desktop tests passed 259 cases plus one Python test, and Rust passed
26 tests. These are baseline evidence, not proof of the proposed operations.

The following exact existing tests ground the current claims. Node IDs below
use repository-relative paths so they can be passed directly to pytest.

| Current claim | Exact existing test evidence |
| --- | --- |
| Revision CAS/order, same-request replay, stale control fence and retained late revision | `packages/app/tests/unit/engine/test_source_tasks.py::test_explicit_revision_order_replay_history_route_and_control` |
| Fenced incomplete capture remains in custody without new source writes; export refuses pending ingestion | `packages/app/tests/unit/engine/test_source_tasks.py::test_fenced_reservation_enters_custody_without_new_source_writes_or_startup_poison` (three faults) |
| Exact alias adoption preserves capture identity; old imports preserve evidence | `packages/app/tests/unit/engine/test_source_tasks.py::test_migrated_alias_adoption_and_automatic_publication_preserve_identity`; `::test_schema_seven_imports_legacy_portable_without_changing_evidence` |
| Route CAS and existing Portable export; exact standalone-v4 application refusal | `packages/app/tests/unit/engine/test_source_tasks.py::test_source_route_cas_preserves_capture_and_exports_v5`; `::test_standalone_v4_import_refusal_uses_valid_v4_fixture` |
| Effective tier/egress intersection and cursor policy binding | `packages/app/tests/unit/engine/test_paging.py::test_paged_search_enforces_the_principal_tier_matrix_and_owner_access`; `::test_external_provider_intersects_stored_egress_for_search_read_and_cursors`; `::test_cursor_authority_binding_names_and_binds_every_policy_dimension` |
| Exact retained revision, history grants/current scope, current anchor resolution | `packages/app/tests/unit/engine/test_history.py::test_source_history_exact_revision_grant_and_current_scope`; `packages/app/tests/unit/engine/test_paging.py::test_source_anchor_resolves_current_head_after_authorization` |
| Durable consent changes and malformed/foreign/stale consent refusal | `packages/app/tests/unit/test_session_consent.py::test_owner_lifecycle_is_durable_atomic_and_idempotent`; `::test_absent_malformed_foreign_and_stale_state_fail_closed` |
| Canonical review freezes sources, requires exact digest and current route, replays crashes | `packages/app/tests/integration/engine/test_review_publication_engine.py::test_bound_proposal_freezes_multi_source_review_context`; `::test_bound_decision_requires_exact_review_digest`; `::test_bound_decision_refuses_changed_source_route`; `::test_bound_review_replays_early_fault_boundaries` |
| Shared app review operations retain their token/retry binding | `packages/app/tests/integration/services/test_review_publication.py::test_shared_service_runs_multi_source_create_inspect_and_decide`; `::test_decisions_bind_review_token_and_generated_retry_key` |
| Normalization, version-bound identity and limits | `packages/connectors/tests/contract/test_saved_markdown.py::test_normalizer_removes_owner_context_but_keeps_fenced_and_later_content`; `::test_selected_root_binds_destination_policy_and_bytes`; `::test_selected_root_refuses_symlinks_escapes_limits_and_unstable_reads` |
| Local-only third-party capture, exact staged replay, refusal isolation | `packages/collector/tests/integration/test_saved_markdown.py::test_saved_markdown_public_job_admission_is_local_only_and_third_party`; `::test_saved_markdown_replays_exact_staged_intake_after_lost_response`; `::test_saved_markdown_refusals_do_not_enter_collector_custody` |
| Pause acknowledgement and terminal-receipt cancellation race | `packages/collector/tests/integration/test_lifecycle.py::test_pause_acknowledgement_prevents_scheduled_import_until_resume`; `::test_pause_during_multi_record_page_stops_remaining_imports`; `::test_cancel_after_terminal_receipt_does_not_read_discarded_custody` |
| Journal exact envelope and crash recovery | `packages/app/tests/unit/engine/test_ingestion_journal.py::test_journal_v1_round_trips_exact_normalized_submission`; `::test_journal_crash_boundaries_recover_once_without_pending_content`; `::test_portable_export_refuses_active_journal_payload` |
| Frozen Portable sidecars, round-trip bytes, exact refusal and promotion recovery | `packages/engine/tests/contract/test_portable_brain_v5.py::test_sidecars_are_required_canonical_and_closed`; `packages/app/tests/integration/engine/test_portability_v5.py::test_fresh_export_import_reexport`; `::test_standalone_v4_refusal_is_exact`; `::test_import_promotion_fault_matrix`; `::test_export_promotion_fault_matrix` |
| Restore rollback, exact retained privacy and immutable snapshot audit | `packages/engine/tests/unit/engine/test_portable_v5_restore.py::test_restore_transaction_rolls_back_all_base_rows`; `::test_exact_retained_values_and_markers_survive_reopen`; `::test_snapshot_audit_rejects_content_drift_without_repair` |

Source inspection, rather than an existing positive test, establishes these
gaps: saved-Markdown `external_id` currently includes version digests; its scan
stops after 25 accepted candidates and rejects continuation; source replay lacks
whole-envelope equality; no withdrawal/sharing task exists; history currently
denies retired sources; v5 restore omits admission order; managed semantic
consent does not supply per-copy/provider approval. No existing passing test is
claimed to prove their future behavior.

### Future tests required in B and C

These are acceptance names for future tests, not skipped or failing A fixtures.
Each new operation must cover exact replay, altered replay, stale state,
unauthorized callers, injected crash boundaries, and clean restore.

| Stage and proposed test family | Required assertions / implementation owners |
| --- | --- |
| B `test_saved_markdown_stable_item_predecessor_revision_and_revert` | New/changed/unchanged/A→B→A observations; stable namespace, distinct revision occurrences, predecessor CAS, Unicode collision and rename refusal. Connector and revision sink. |
| B `test_revision_delivery_exact_envelope_pending_receipt_and_destination` | Change each envelope field under one delivery ID; refuse. Lost response/queued journal/`operation_pending` preserves exact custody through restart. Wrong destination/head/receipt cannot advance checkpoint; fenced replay remains explicit. Engine sink and collector custody. |
| B `test_full_scan_continues_beyond_25_across_restart` | At least 60 files with unchanged early pages; every selected path visited. Inject unreadable directories, membership changes, unstable reads, root/policy changes, quota and time exhaustion, malformed-present files. None yields false absence; unchanged files do not starve later ones. |
| B `test_withdrawal_cas_replay_owner_history_and_return` | Owner inspect/withdraw; stale/altered/foreign/unauthorized request refusal; current paged/legacy/search/inbox omission; exact retained owner history; no source deletion; return and queued revision cannot reactivate. Include history/read/search cursors, startup and index rebuild. |
| B `test_schema11_runtime6_portable6_lifecycle_admission_roundtrip` | Independent schema-10 fixture and frozen checksums; exclusive migration crash/rollback/retry; old runtime refusal before writes. V6 complete sidecar validation, preserved old-format behavior, active/retired history, admission identity/order/terminal replay after clean restore, fresh physical binding requirement, pending export refusal. |
| C `test_sharing_preview_exact_bytes_and_decision_binding` | Full inspected payload/digest or refusal; Unicode/byte bound and hostile text; changed head/route/policy/provider/destination/expiry rejected; unauthorized owner claims fail; reject produces zero copies; canonical-page approve grants no egress. |
| C `test_sharing_provider_consent_surface_matrix` | Two allowed/disallowed providers, absent/revoked/replaced consent, tier/space/destination restrictions; original remains local-only; verify search, full chunks, historical chunks, canonical evidence, relationships, review/inbox callbacks, semantic prepare/release and retained ancestry. No legacy fallback. |
| C `test_sharing_copy_journal_link_crash_matrix` | Fault before/after admission, capture acceptance, link materialization, projection/index write and receipt response. Same approval/envelope creates one copy. Accepted unlinked or damaged-link capture stays hidden through restart/rebuild; reject admits none. |
| C `test_sharing_head_change_withdrawal_revocation_and_old_cursors` | Head change, route change, source withdrawal and revoke each close existing external visibility, invalidate relevant cursors, and block pending semantic release. New versions remain unapproved. Owner history and immutable original privacy/copy evidence remain intact. |
| C `test_schema12_runtime7_portable7_sharing_roundtrip` | Independent schema-11 fixture, runtime floor, active/superseded/rejected/revoked previews/approvals/copies, exact receipt replay, missing or forged links/providers, semantic tampering despite recomputed manifests, promotion crash/duplicate audit, and pending export refusal. Restore never creates consent or revives a stale approval. |

### Checkpoint A checks

The required A gate, unchanged:

```sh
uv run --frozen pytest -q packages/app/tests/unit/engine packages/app/tests/unit/test_session_consent.py packages/app/tests/integration/engine/test_review_publication_engine.py packages/app/tests/integration/services/test_review_publication.py packages/connectors/tests/contract/test_saved_markdown.py packages/collector/tests/integration/test_saved_markdown.py packages/collector/tests/integration/test_lifecycle.py && make lint typecheck && git diff --check && actionlint .github/workflows/ci.yml
```

Additional existing Portable checks run during A:

```sh
uv run --frozen pytest -q packages/app/tests/integration/engine/test_portability_v5.py packages/engine/tests/contract/test_portable_brain_v5.py packages/engine/tests/unit/engine/test_portable_v5_evidence.py packages/engine/tests/unit/engine/test_portable_v5_restore.py
```

Result: **207 passed in 40.37s**, zero failures or skips, against the inspected
baseline production code. The enforced gate result and final checked-tree
identity belong in the checkpoint execution evidence. Passing A establishes a
reviewable contract and current baseline only; B/C implementation, installed
compatibility, provider acceptance, deployment, and historical adoption remain
unproved.

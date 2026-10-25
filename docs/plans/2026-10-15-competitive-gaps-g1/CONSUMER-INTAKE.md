# Engine consumer intake implementation contract

Goal110; existing competitive-gap branch and PR86. Baseline295fb28, schema19/runtime14/Portable14. The implemented development candidate uses schema20/runtime15/Portable15 and the reviewed successor contract below. Full goal acceptance still requires final review and exact-candidate gates. Provider archive formats remain inactive. Engine APIs are tested with explicitly injected synthetic profile records, never a production bypass flag.

## Decision record and authority

Use one successor shared lifecycle replay authority (journal v2) for old owner lifecycle and new consumer operations. Preserve original request/event/envelope bytes and old journal v1/Portable1-14 validators. Reject a parallel importer current-head/tombstone ledger: retraction and read eligibility must observe one complete membership state. The predecessor owner segment stays owner-only; a new consumer segment contract carries public-job provenance and stable source bindings.

Allocate the reviewed successor coordinates20/15/15 together, including migration/classifier/admission, recovery, Portable catalogs and client/native handshakes. Migration1-19 statement/checksum bytes stay exact. Migration20 advances the journal identity contract by an explicit migration; old request/event bytes and chain remain exact. New typed replay accepts legacy operations unchanged and consumer intent references/outcomes explicitly. An all-unchanged/no-text operation may finish without adding groups/captures/session membership; its typed request still binds its complete durable outcomes.

## Exact identities and plan construction

Raw provider IDs are exact UTF-8 scalar sequences. The prior prose defined identity hashing using NFC-normalizing portable canonical JSON while separately forbidding normalization of provider IDs. Resolve this conflict explicitly: identity hashes encode exact provider ID UTF-8 bytes as hexadecimal within the domain-separated canonical input; body/title normalization remains separate. This is an unshipped normalized-contract correction, not a change to existing capture IDs.

An internal import plan fixes a canonical UUID operation_id before preview. Preview binds it, and commit rejects a different ID. This resolves full-envelope/delivery binding without a mutable preview intent. The existing CaptureSubmission.request_value excludes delivery_id, so the specified delivery digest over capture_request_sha256 is acyclic and remains usable. The engine constructs capture-only PublicJobCaptureContext and quick TextPayload submissions; parser/caller transcript role labels cannot provide owner identity, authority, publication, routing, privacy override or session membership.

## Durable evidence and bounds

Retain content-addressed per-conversation intent bytes (including normalized messages, exact identifier-byte evidence, complete envelopes and source preimages) in immutable consumer intent custody. The shared request binds their hashes, proposed account registration/archive binding, selected outcomes and group manifests. Do not embed the whole64MiB operation body in the existing16MiB canonical journal request. Every custodied object is bounded, hashed and inventory-referenced; reject orphan, duplicate, noncanonical, mismatching or missing objects. The source-of-truth operations/events remain the shared journal, not the operational preview table.

Operational expiring previews are not Portable evidence. Durable account scopes/namespaces/archive bindings, accepted intents, terminal outcomes/receipts and source/membership evidence are versioned Portable evidence; projections must reconstruct exactly from checked authority. Pending imports block export without recovery mutation. Terminal halt retains required custody as noncurrent history.

## Staging and stable sources

Materialize consumer captures with original immutable public-job/privacy/provenance envelopes, engine-issued reservations and typed lifecycle custody. Add a group-aware source registration seam: each segment keeps the documented four-part consumer namespace and stable source_id. Register new source revisions without promoting an existing logical source head; new-source pending heads stay historical-only. Do not invoke ordinary SourceTasks.submit_revision unchanged because it independently journals and promotes sources.

Activation, under the canonical writer fence and a single database transaction, revalidates every at-most128 complete segment receipt, exact source head/lifecycle/control/privacy coordinates, account/archive binding, group revision, session barrier and tombstones. Then atomically publish all source heads, complete group membership, one outcome/receipt and newly admitted session membership plus generations. Shorter replacements retire old tail eligibility without deleting immutable source history. Missing conversations have no effect. Unchanged reimports retain the original activation receipt reference and return an operation-bound unchanged outcome receipt; they never join a new session.

Current group eligibility additionally requires every required segment source to remain active, available, at its admitted head, and eligible under effective privacy. A withdrawal/unavailability of one current member withholds the complete current conversation. Activation source preimages include route_version and effective privacy, in addition to source ID/head/lifecycle/control versions. Re-adding a previously removed ordinal tail is an explicit new revision on the same stable source namespace, never source-ID reset or withdrawal reversal.

The operation-owned eligibility generation/session advance may be reconciled from its exact preceding activation receipts. Unrelated generation changes require selected-preimage revalidation, not unconditional stale refusal. Actual selected head/privacy/lifecycle changes refuse activation and remain explicitly recoverable/haltable. Status and preview-page APIs are read-only; no ordinary startup recovery is implied by those reads. Old issuer unfinished work may be halted/read under current owner authority but not resumed.

## Successor validation

Portable15 validates mixed shared journal replay and consumer source/receipt/intents before destination writes. It composes the frozen pre-lifecycle base semantics plus explicit successor lifecycle and owner approval validation; it must not relax Portable13/14 owner-only validators to accept consumer envelopes. Preserve isolated v1 fixtures and evidence meanings. Restore installs validated journal/intents/approvals before deriving current source/membership projections, and refuses any disagreement with exported projections.

Consumer profiles stay inactive in production. Synthetic tests inject explicit in-memory reviewed profile records through test fixtures; no CLI/config flag activates an unverified format. This milestone implements engine task APIs, not archive parsing or product import-chat CLI.

## Acceptance

Goal110 completion checklist is authoritative: strict normalized contracts for both providers; durable engine preview/commit/status/recover/halt; identity/provenance/privacy admission; unchanged/replace/mixed/no-text outcomes; crash/lost-ack/process-exit and concurrency; all-surface staging exclusion; retraction non-resurrection; Portable/corruption/migration/old-reader proof; independent final review; exact-candidate full/native checks and green PR CI. Push scoped verified commits to PR86, leaving it open and auto-merge disabled. No claim of complete G1/end-user importer.

## Pre-freeze review resolutions

Independent source/design review confirms the successor direction. Per-object custody limits: normalized conversation DTO32MiB encoded maximum (8MiB normalized message UTF-8,10000 messages,128 segment descriptors); segment envelope1MiB maximum with each text payload below65536 scalars; operation root16MiB with references, not embedded bodies; complete aggregate normalized transcript bytes64MiB,10000 conversations and100000 messages per plan. Canonical encoding/byte limits are checked before reservation. Intent objects are typed and content-addressed with exact reachability; envelope/body bytes are separate references to avoid text duplication in one intent object.

Migration20 preserves predecessor nonterminal operations under the same issuer; the successor dispatcher must support their original recovery/halt semantics. It does not automatically activate or terminalize them during migration. Changing issuer permits current-owner status/halt but prohibits resume, for either owner or consumer operations. All materializer/read/retraction/export/restore dispatch follows the current journal identity, not guessed envelope shape.

Scope registration and the first archive binding commit atomically with the first new conversation activation. A zero-admission operation retains proposed-scope evidence but creates no empty registered scope. An unchanged operation against an existing registered scope may add a previously unseen repackaged archive binding atomically with its terminal outcome; it cannot rebind a known digest to another scope. Mixed operations share one checked scope and archive binding. Both accepted and conflicting evidence are explicit operation outcomes, never inferred registration.

Concrete storage roles to freeze with journal2 implementation: immutable `consumer_intent_objects(sha256 PRIMARY KEY,kind,object_bytes)`; immutable accepted approval binding keyed by shared journal operation_id; operational expiring preview rows; rebuildable scopes/namespace/archive bindings, stable segment-source mapping and typed outcome projections. Scope/namespace IDs are engine-issued UUID4 identities; provider IDs are retained only as exact UTF-8-byte evidence. Indexes and full DDL/golden hashes must land together in the successor schema module before coordinate registration. No separate barrier/current-membership authority is introduced.

Repeated preview first reconciles existing accepted operation by exact immutable input binding, then existing unexpired preview for that operation, before allocating reservation UUIDs/timestamps. Accepted operation replay/status survives preview expiry; changed input under the same operation_id always conflicts. A not-yet-accepted expired preview can be rebuilt only as a new bound preview; its old token is never silently renewed.

The normalized builder and consumer custody both accept only quick action and an optional reference intent (None or REFERENCE), never arbitrary intent, capture reason or routing. Both require exactly capture.accept capabilities and no requested/admitted cloud or external egress; selected privacy tier does not grant egress. Admission jointly verifies normalized capture-request descriptors against retained envelope.request_sha256, body, ordinal/key, normalized object hash, exact protected provider identities and registered namespace before reservation and on restore. A metadata DTO alone cannot certify its capture request preimage.

## Concrete request and event discriminator

Shared journal2 preserves the generic frozen LifecycleRequest byte schema. A new
consumer request uses exact payload keys `groups` and `consumer`; the latter has
`contract:ob.consumer-intake.v1`, `intent_sha256`, and ordered unique `items`.
Items are exact `group_key`, nullable `revision_sha256`, `planned_outcome`, and
nullable `reference_receipt_sha256`. Planned outcomes are new, replace_current,
unchanged, conflict, retracted or no_text. Only new/replacement items contribute
GroupIntents. The marker is explicit; legacy payloads keep predecessor replay.

The new consumer_outcome event contains exact group_key/outcome/receipt_sha256.
Committed outcomes derive from retained group_activated events; remaining halted
outcomes derive from operation_halted. Receipts bind the complete request and
manifest/session or typed terminal item/reference. All-unchanged/no_text requests
finish with no new capture/session. Exact retained steps reconcile only for open
or committed items; refusal/halted items cannot stage, custody or activate.

Prepared per-conversation roots reference separate hashed profile, normalized
DTO, exact-provider-ID and segment-envelope objects. Maximum raw provider IDs
(10000 messages at1024 UTF-8 bytes each) exceed16MiB after hexadecimal encoding,
so the protected identity object has its own32MiB bound and root hash. This keeps
the16MiB operation/journal root and1MiB segment-envelope limits intact without
silently reducing accepted identity bounds. Joint validation proves normalized
capture-request hashes against actual immutable envelopes and engine identity.

Concrete DDL and golden fixtures are now in consumer_schema.py and the engine
test fixtures consumer-schema20.json and consumer-lifecycle-journal-v2.json.
They are registered in the successor catalog together with schema20/runtime15/Portable15 admission, migration, recovery and client/native metadata.

## Engine operation root and input binding

Engine-facing ConsumerImportPlan contains only exact provider message/conversation IDs and normalized accepted text/omissions, provider/profile/archive/operation identity, exactly one explicit existing scope or proposed label, privacy tier and replacement choice. Its root references bounded per-conversation input objects; identifier bytes are hex-protected. Empty message input is explicit no_text evidence, never an empty capture.

The accepted operation root contract ob.consumer-operation-intent.v1 has exact operation_id,brain_id,issuer_epoch,principal_id,provider,profile_sha256,input_sha256,scope,archive_sha256,privacy_tier,replace_current,session_id,eligibility_generation,control_epoch,items. Scope is exact scope_id,namespace_id,label,proposed; scope IDs use scope_UUID4, namespace IDs use UUID4. Item is exact item_token,group_key,revision_sha256,planned_outcome,reference_receipt_sha256,intent_sha256,input_sha256,expected_revision_sha256. Root items sort by group_key; item tokens are random opaque identifiers retained in the root. The shared request derives all admitted GroupIntents from referenced prepared conversation segments and expected group revisions, and binds the root digest. No cyclic root/request digest exists; the accepted approval separately binds both exact bytes.

Root validation reconstructs the complete hash-referenced parser input and all prepared conversation intents, compares exact protected IDs, normalized messages/title/omissions/profile/namespace, requested privacy and deterministic delivery identities, and validates one complete reachable object union with exact kinds. No-text items reference only their protected empty conversation input. Prospective envelopes for unchanged/conflicted/retracted preview bodies remain evidence but never become staged captures. Reservation/source/receipt/delivery identities are unique across the operation. Acceptance requires the engine owner principal and actual capture-only context; retained profile evidence cannot activate an inactive profile.

Live orchestration persists preview/root/objects under the writer fence, then reserves shared operation plus immutable approval/objects/bindings before custody. Each segment is separately custodied then materialized through the real capture stage machine. Activation rechecks group/source/barrier/scope/archive/current privacy and publishes scope/archive registration, stable heads, complete membership, outcome/receipt and generations in one transaction. Status reads durable journal truth; recover uses retained approval only; halt seals unfinished groups without materializing or activating them. Old issuer can read/halt but never resume. The current development candidate advances schema/runtime/Portable together to20/15/15; no engine schema bypass flag is introduced.


## Concrete integration review resolutions

Writer-fenced acceptance repeats exact-operation reconciliation after the optimistic read. Concurrent commits cannot reserve twice. A segment retry reconciles its own completed group before checking the old source preimage, so another recovery runner's activation does not look like foreign drift. Halt and recovery serialize each durable step under the same writer fence; a halt between steps cannot authorize further staging or activation.

A normalized body match with a different selected privacy tier is a conflict, not unchanged. A previously retained noncurrent revision also reports conflict, even with replacement selected, rather than claiming the journal can readmit a known revision. These decisions preserve original privacy and revision evidence. Whole-conversation current reads check every member's effective privacy and route against the caller authority; a readable first segment cannot expose a conversation whose required sibling is withheld.

Input-plan objects separate exact-ID evidence and normalized text as well as separating the prepared normalized DTO. Maximum accepted IDs plus highly escaped text otherwise exceed a single32MiB object despite each independent budget being valid. Roots retain references and exact kind/reachability checks.

Portable15 handles terminal halted stage0–2 materializer projections through validated export-only normalization. It verifies exact rows, retained reservations/envelopes and any partial file bytes, omits only those unfinished projections from a disposable snapshot, and preserves all intent/custody. The source Brain is unchanged. Restore leaves them unmaterialized while preserving status and semantic re-export. Stage3 history remains; damaged or unknown partial state refuses. This avoids materializing on halt and keeps frozen pre-lifecycle Portable semantics intact. Details are in `$REPO/.worktrees/competitive-gap-reconciliation/docs/portable-brain-v15.md`.


## Final review boundary corrections

Generic owner lifecycle mutations reject the explicit consumer request discriminator. Consumer intake alone composes its accepted approval with staging, activation, scope/archive publication and outcomes. Its private stage revalidates the retained root/request, active profile, issuer and scope under the writer fence; private activation additionally requires the exact bound session and registered scope/archive. A generic lifecycle call cannot substitute a session, bypass profile deactivation or create an active conversation without registered scope/outcome evidence. Legacy groups-only owner requests keep predecessor behavior.

Exact predecessor restore retries at current schema20 reject every unexpected consumer evidence/projection/preview table, including orphan objects. Otherwise an older serializer could miss successor state and report a successful duplicate restore while the next Portable15 export fails. Recovery-closure tests pin all predecessor producers through Portable14 and exercise a populated consumer Portable15 baseline.

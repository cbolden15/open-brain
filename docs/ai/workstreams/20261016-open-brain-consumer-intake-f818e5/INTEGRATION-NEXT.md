# Consumer integration continuation notes

Goal110 remains active; PR86 open/unmerged; production19/14/14. Foundation unit passed full make verify with frozen inputs; later workstream documentation changes are bookkeeping only. Ledger active0/3,total3/6; existing worker lineages finished read-only integration maps.

## Required next integration seams

1. Explicit live journal dispatcher by admitted schema20/identityv2, and segment decoder by typed consumer request marker; no shape guessing. Preserve owner-only v1 envelope and same-issuer pending owner recovery. Update every materializer, read, retraction and recovery caller together with20/15/15.
2. Source registration hook before ordinary automatic register_completed_captures: stable namespace/source revision without head promotion; new pending source placeholder reconciles against original absent-source preimage. Guard consumer_chat namespace against ordinary SourceRevisionSubmission paths. Withdrawal remains allowed during staging and makes activation stale; never reverse it.
3. Consumer activation transaction rechecks source identity/head/head+route+lifecycle versions, availability/lifecycle/space/effective privacy/control epoch, complete group/barriers/scope/archive. New pending placeholders are operation-owned exceptions, not bypasses. Retire eligibility of shortened tail while retaining immutable stable namespace/source for later re-add.
4. Shared reader group closure must include every required current member source and authority-dependent effective privacy. Check all release surfaces; a boolean static group check alone may miss per-authority privacy restrictions. Pending never becomes history-visible.
5. Portable15 composes frozen Portable12 base plus explicitly allowlisted mixed journal2/consumer/approval families, not frozen13/14 validator inheritance. Add explicit successor approval validator preserving approval_record/lifecycle_request/receipt/session-membership semantics. V15RestoreBundle derives fromV12 and installs journal2+immutable consumer/owner approval authority before source/search projections. Extend deeper restore factories, replay baseline catalogs, semantic audits and allowlists together.

## Operation contract decisions still to implement and verify

Root operation object must bind operation/Brain/issuer/principal, provider/profile, scope proposed or existing identity, archive/input hash, privacy/replacement, selected group/source preimages, eligibility/control, engine-owned session, sorted complete per-conversation object refs and typed outcomes. Request binds root digest, so root cannot bind whole request digest cyclically; approval row binds both independently. Validate complete reachable union and exact object kinds; no orphans or duplicate source/reservation/delivery IDs across conversations.

Public internal plan should carry only normalized parser values plus exact provider identifier byte evidence, provider/profile/opid/archive hash, explicit scope selector and owner privacy/replacement choice. Engine resolves scope/namespace and builds envelopes/reservations. Initial inactive profile check precedes any DB mutation. Repeated preview checks accepted operation and existing preview by exact immutable input before allocating IDs/timestamps; commit reconciles accepted operation before expiry. Body preview is bounded explicit output; ordinary preview/status remains body-free.

no_text needs explicit protected identity/omission evidence because current PreparedConsumerConversation requires at least one admitted segment. Do not fake empty captures or broaden frozen nonempty conversation DTO. Zero-admission proposed scope remains unregistered. Existing-scope unchanged repackaging may atomically bind new archive with terminal outcome.

## Source read anchors

engine/capture_lifecycle.py::_journal,_segments,require_lifecycle_materialization,CaptureLifecycleTasks.activate_group
engine/source_store.py::register_completed_captures
engine/source_intake.py::SourceRevisionSubmission,register_intake
engine/source_lifecycle.py::withdraw
engine/mcp_eligibility.py::lifecycle_capture_visible,capture_visible
engine/records.py::RecordProjector.source
engine/portable_v14_evidence.py::serialize_portable_v13_successor_state
portable/retraction_v14.py; engine/owner_retraction_contracts.py::validate_approval_records hardcodes v1
engine/portable_v13_restore.py;engine/portable_v14_restore.py
engine/capture_replay.py::_baseline_manifest supports8-14
engine/capture_recovery.py::emit_owner_capture_plan is deliberately owner-only

## Required tests after wiring

Both synthetic providers through realengine APIs; stable/duplicate/replacement/no_text/conflict/retracted/halted; lostack/process exit at every step; own activation generations vs selected/unrelated races; source privacy/withdrawal and ordinary namespace impersonation; group-wide current visibility and all staging surfaces; oldissuer halt/status-only; owner predecessor receipts; mixed Portable15 export/restore/re-export equality, haltedcustody,pendingrefusal, projection and semantic tamper before destination writes, oldreaders, migrationclone/fencing/interruption.

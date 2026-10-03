-- Synthetic schema-11/runtime-6 SQLite dump from source c66382883b8ff4b907c5fdbfcb8428003e43fe12.
BEGIN TRANSACTION;
CREATE TABLE brain_identity (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    tenant_id TEXT NOT NULL UNIQUE CHECK (length(tenant_id) > 0),
    brain_id TEXT NOT NULL UNIQUE CHECK (length(brain_id) > 0),
    issuer_epoch INTEGER NOT NULL CHECK (issuer_epoch > 0),
    legacy_issuer_epoch INTEGER CHECK (legacy_issuer_epoch IS NULL OR legacy_issuer_epoch > 0),
    recorded_at TEXT NOT NULL CHECK (length(recorded_at) > 0),
    CHECK (legacy_issuer_epoch IS NULL OR legacy_issuer_epoch < issuer_epoch)
);
INSERT INTO "brain_identity" VALUES(1,'tenant_1d5dab51-a820-461a-8021-f435109064c9','brn_dvo2wuniebdbvabb6q2rbedeze',1,NULL,'2026-10-02T23:09:26.742451Z');
CREATE TABLE canonical_revision_members (
 revision_id TEXT NOT NULL, page_id TEXT NOT NULL, publication_id TEXT NOT NULL,
 ordinal INTEGER NOT NULL CHECK(ordinal>=0),
 capture_id TEXT NOT NULL REFERENCES source_revisions(capture_id),
 PRIMARY KEY(revision_id,ordinal), UNIQUE(revision_id,capture_id)
);
CREATE TABLE canonical_revision_privacy (
    revision_id TEXT PRIMARY KEY,
    effective_privacy_json TEXT NOT NULL CHECK (
        typeof(effective_privacy_json) = 'text'
        AND CASE
            WHEN json_valid(effective_privacy_json)
                THEN json_type(effective_privacy_json) = 'object'
            ELSE 0
        END
    )
);
CREATE TABLE capture_ingestion_events (
    event_sequence INTEGER PRIMARY KEY AUTOINCREMENT CHECK (event_sequence > 0),
    delivery_id TEXT NOT NULL REFERENCES capture_ingestion_items(delivery_id) ON DELETE CASCADE,
    event_kind TEXT NOT NULL CHECK (
        event_kind IN (
            'queued', 'attempt_failed', 'accepted', 'duplicate', 'quarantined', 'discarded'
        )
    ),
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 0),
    receipt_json TEXT NOT NULL CHECK (
        typeof(receipt_json) = 'text' AND json_valid(receipt_json)
        AND json_type(receipt_json) = 'object'
    ),
    recorded_at TEXT NOT NULL CHECK (length(recorded_at) > 0)
);
CREATE TABLE capture_ingestion_items (
    journal_sequence INTEGER PRIMARY KEY AUTOINCREMENT CHECK (journal_sequence > 0),
    ingestion_id TEXT NOT NULL UNIQUE CHECK (length(ingestion_id) > 0),
    delivery_id TEXT NOT NULL UNIQUE CHECK (length(delivery_id) > 0),
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    envelope_sha256 TEXT NOT NULL CHECK (length(envelope_sha256) = 64),
    submission_path TEXT NOT NULL CHECK (
        submission_path IN ('owner', 'public_job', 'destination_bound')
    ),
    byte_count INTEGER NOT NULL CHECK (byte_count > 0),
    queued_at TEXT NOT NULL CHECK (length(queued_at) > 0)
);
CREATE TABLE capture_ingestion_payloads (
    delivery_id TEXT PRIMARY KEY REFERENCES capture_ingestion_items(delivery_id) ON DELETE CASCADE,
    envelope_bytes BLOB NOT NULL CHECK (
        typeof(envelope_bytes) = 'blob' AND length(envelope_bytes) > 0
    )
);
CREATE TABLE capture_ingestion_tombstones (
    delivery_id TEXT PRIMARY KEY CHECK (length(delivery_id) > 0),
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    result_json TEXT NOT NULL CHECK (
        typeof(result_json) = 'text' AND json_valid(result_json)
        AND json_type(result_json) = 'object'
    ),
    decided_at TEXT NOT NULL CHECK (length(decided_at) > 0)
);
CREATE TABLE captures (
    delivery_id TEXT PRIMARY KEY,
    request_sha256 TEXT NOT NULL,
    capture_id TEXT NOT NULL UNIQUE,
    accepted_receipt_id TEXT NOT NULL UNIQUE,
    payload_family TEXT NOT NULL,
    payload_json BLOB NOT NULL,
    search_text TEXT NOT NULL,
    file_bytes BLOB,
    source_origin TEXT NOT NULL,
    source_reference TEXT NOT NULL,
    space_id TEXT,
    intent TEXT,
    capture_why TEXT,
    action TEXT NOT NULL,
    title TEXT,
    accepted_at TEXT NOT NULL,
    stage INTEGER NOT NULL DEFAULT 0,
    source_path TEXT,
    canonical_path TEXT,
    auto_proposal_id TEXT UNIQUE,
    auto_proposal_receipt_id TEXT UNIQUE,
    auto_decision_id TEXT UNIQUE,
    auto_decision_receipt_id TEXT UNIQUE,
    page_id TEXT UNIQUE,
    publication_id TEXT UNIQUE,
    publication_path TEXT,
    enrichment_state TEXT NOT NULL DEFAULT 'pending_enrichment',
    actor_id TEXT,
    role_claim_json TEXT,
    privacy_json TEXT,
    provenance_json TEXT,
    submission_path TEXT
);
CREATE TABLE decisions (
    delivery_id TEXT PRIMARY KEY,
    request_sha256 TEXT NOT NULL,
    decision_id TEXT NOT NULL UNIQUE,
    decision_receipt_id TEXT NOT NULL UNIQUE,
    proposal_id TEXT NOT NULL UNIQUE,
    outcome TEXT NOT NULL,
    effective_bytes BLOB,
    recorded_at TEXT NOT NULL,
    page_id TEXT,
    publication_id TEXT UNIQUE,
    canonical_path TEXT,
    publication_path TEXT,
    stage INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE engine_generations (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), incarnation TEXT NOT NULL,
 retrieval_generation INTEGER NOT NULL CHECK(retrieval_generation>=0),
 authorization_epoch INTEGER NOT NULL CHECK(authorization_epoch>=0),
 projection_policy_version INTEGER NOT NULL CHECK(projection_policy_version>=1),
 control_epoch INTEGER NOT NULL CHECK(control_epoch>=0),
 fencing_epoch INTEGER NOT NULL CHECK(fencing_epoch>=0),
 vector_generation INTEGER NOT NULL CHECK(vector_generation>=0)
);
INSERT INTO "engine_generations" VALUES(1,'c9b0879d-eab9-42f8-ab93-e885e4fbacf6',0,0,1,0,0,0);
CREATE TABLE issuer_migration_marker (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    source_manifest_bytes BLOB NOT NULL CHECK (
        typeof(source_manifest_bytes) = 'blob' AND length(source_manifest_bytes) > 0
    ),
    source_manifest_sha256 TEXT NOT NULL CHECK (length(source_manifest_sha256) = 64),
    brain_id TEXT NOT NULL CHECK (length(brain_id) > 0),
    legacy_issuer_epoch INTEGER NOT NULL CHECK (legacy_issuer_epoch > 0),
    current_issuer_epoch INTEGER NOT NULL CHECK (current_issuer_epoch > 0),
    legacy_binding_manifest_sha256 TEXT NOT NULL CHECK (
        length(legacy_binding_manifest_sha256) = 64
    ),
    recorded_at TEXT NOT NULL CHECK (length(recorded_at) > 0),
    CHECK (legacy_issuer_epoch < current_issuer_epoch)
);
CREATE TABLE legacy_issuer_bindings (
    artifact_path TEXT NOT NULL CHECK (length(artifact_path) > 0),
    jsonl_ordinal INTEGER CHECK (jsonl_ordinal IS NULL OR jsonl_ordinal >= 0),
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    issuer_epoch INTEGER NOT NULL CHECK (issuer_epoch > 0)
);
CREATE TABLE logical_sources (
 source_id TEXT PRIMARY KEY, head_capture_id TEXT NOT NULL,
 historical_only INTEGER NOT NULL CHECK(historical_only IN (0,1)),
 space_id TEXT REFERENCES spaces(space_id),
 route_version INTEGER NOT NULL CHECK(route_version >= 0),
 head_version INTEGER NOT NULL CHECK(head_version >= 1),
 lifecycle TEXT NOT NULL CHECK(lifecycle IN ('active','retired')),
 availability TEXT NOT NULL CHECK(availability IN ('available','missing','inaccessible','unknown')),
 FOREIGN KEY(source_id,head_capture_id)
 REFERENCES source_revisions(source_id,capture_id) DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE managed_conflicts (
    conflict_id TEXT PRIMARY KEY,
    note_id TEXT NOT NULL REFERENCES managed_notes(note_id),
    base_revision_id TEXT NOT NULL,
    accepted_revision_id TEXT NOT NULL,
    candidate_body_bytes BLOB NOT NULL,
    candidate_sha256 TEXT NOT NULL CHECK (length(candidate_sha256) = 64),
    status TEXT NOT NULL CHECK (status IN ('open', 'resolved')),
    resolution_revision_id TEXT,
    detected_at TEXT NOT NULL,
    resolved_at TEXT,
    CHECK (
        (status = 'open' AND resolution_revision_id IS NULL AND resolved_at IS NULL)
        OR
        (status = 'resolved' AND resolution_revision_id IS NOT NULL AND resolved_at IS NOT NULL)
    ),
    FOREIGN KEY (note_id, base_revision_id)
        REFERENCES managed_note_revisions(note_id, revision_id),
    FOREIGN KEY (note_id, accepted_revision_id)
        REFERENCES managed_note_revisions(note_id, revision_id),
    FOREIGN KEY (note_id, resolution_revision_id)
        REFERENCES managed_note_revisions(note_id, revision_id)
);
CREATE TABLE managed_consents (
    consent_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES managed_workspaces(workspace_id),
    provider TEXT NOT NULL,
    access_mode TEXT NOT NULL,
    operation TEXT NOT NULL,
    note_scope TEXT NOT NULL,
    owner_actor_id TEXT NOT NULL,
    granted_generation INTEGER NOT NULL CHECK (granted_generation >= 0),
    active INTEGER NOT NULL CHECK (active IN (0, 1)),
    granted_at TEXT NOT NULL,
    revoked_at TEXT,
    CHECK (
        (active = 1 AND revoked_at IS NULL)
        OR
        (active = 0 AND revoked_at IS NOT NULL)
    )
);
CREATE TABLE managed_exclusions (
    exclusion_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES managed_workspaces(workspace_id),
    kind TEXT NOT NULL CHECK (kind IN ('note', 'folder', 'portable_set')),
    subject TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    policy_generation INTEGER NOT NULL CHECK (policy_generation >= 0),
    recorded_at TEXT NOT NULL,
    UNIQUE (workspace_id, kind, subject)
);
CREATE TABLE managed_inference_budgets (
    workspace_id TEXT NOT NULL REFERENCES managed_workspaces(workspace_id),
    provider TEXT NOT NULL,
    window_key TEXT NOT NULL,
    request_limit INTEGER NOT NULL CHECK (request_limit >= 0),
    byte_limit INTEGER NOT NULL CHECK (byte_limit >= 0),
    used_requests INTEGER NOT NULL DEFAULT 0 CHECK (used_requests >= 0),
    used_bytes INTEGER NOT NULL DEFAULT 0 CHECK (used_bytes >= 0),
    reserved_requests INTEGER NOT NULL DEFAULT 0 CHECK (reserved_requests >= 0),
    reserved_bytes INTEGER NOT NULL DEFAULT 0 CHECK (reserved_bytes >= 0),
    uncertain_requests INTEGER NOT NULL DEFAULT 0 CHECK (uncertain_requests >= 0),
    uncertain_bytes INTEGER NOT NULL DEFAULT 0 CHECK (uncertain_bytes >= 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, provider, window_key),
    CHECK (used_requests + reserved_requests + uncertain_requests <= request_limit),
    CHECK (used_bytes + reserved_bytes + uncertain_bytes <= byte_limit)
);
CREATE TABLE managed_inference_requests (
    request_id TEXT PRIMARY KEY,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    workspace_id TEXT NOT NULL REFERENCES managed_workspaces(workspace_id),
    provider TEXT NOT NULL,
    access_mode TEXT NOT NULL,
    operation TEXT NOT NULL,
    adapter_identity TEXT NOT NULL,
    policy_generation INTEGER NOT NULL CHECK (policy_generation >= 0),
    selections_json TEXT NOT NULL,
    prompt_bytes BLOB NOT NULL,
    prompt_sha256 TEXT NOT NULL CHECK (length(prompt_sha256) = 64),
    effective_privacy_json TEXT NOT NULL,
    input_bytes INTEGER NOT NULL CHECK (input_bytes > 0),
    max_output_bytes INTEGER NOT NULL CHECK (max_output_bytes > 0),
    timeout_seconds INTEGER NOT NULL CHECK (timeout_seconds > 0),
    status TEXT NOT NULL CHECK (
        status IN ('reserved', 'dispatching', 'succeeded', 'failed', 'uncertain',
                   'superseded', 'cancelled')
    ),
    output_bytes INTEGER CHECK (output_bytes IS NULL OR output_bytes >= 0),
    created_at TEXT NOT NULL,
    completed_at TEXT,
    CHECK (
        (status IN ('reserved', 'dispatching') AND completed_at IS NULL)
        OR
        (status IN ('succeeded', 'failed', 'uncertain', 'superseded', 'cancelled')
         AND completed_at IS NOT NULL)
    )
);
CREATE TABLE managed_links (
    link_id TEXT PRIMARY KEY,
    source_note_id TEXT NOT NULL REFERENCES managed_notes(note_id),
    source_revision_id TEXT NOT NULL,
    target_note_id TEXT NOT NULL REFERENCES managed_notes(note_id),
    target_revision_id TEXT NOT NULL,
    source_quote TEXT NOT NULL,
    target_quote TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    accepted_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    UNIQUE (source_revision_id, target_note_id),
    CHECK (source_note_id != target_note_id),
    FOREIGN KEY (source_note_id, source_revision_id)
        REFERENCES managed_note_revisions(note_id, revision_id),
    FOREIGN KEY (target_note_id, target_revision_id)
        REFERENCES managed_note_revisions(note_id, revision_id)
);
CREATE TABLE managed_note_observations (
    workspace_id TEXT NOT NULL REFERENCES managed_workspaces(workspace_id),
    generation INTEGER NOT NULL CHECK (generation > 0),
    note_id TEXT NOT NULL REFERENCES managed_notes(note_id),
    relative_path TEXT NOT NULL,
    accepted_revision_id TEXT NOT NULL,
    observed_sha256 TEXT NOT NULL CHECK (length(observed_sha256) = 64),
    materialized_sha256 TEXT CHECK (
        materialized_sha256 IS NULL OR length(materialized_sha256) = 64
    ),
    fingerprint_json TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, generation, note_id),
    UNIQUE (workspace_id, generation, relative_path),
    FOREIGN KEY (note_id, accepted_revision_id)
        REFERENCES managed_note_revisions(note_id, revision_id)
);
CREATE TABLE managed_note_revisions (
    revision_id TEXT PRIMARY KEY,
    note_id TEXT NOT NULL,
    parent_revision_id TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('setup', 'edit', 'link', 'merge', 'restore')),
    body_bytes BLOB NOT NULL,
    body_sha256 TEXT NOT NULL CHECK (length(body_sha256) = 64),
    accepted_by_actor_id TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    privacy_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    operation_id TEXT NOT NULL UNIQUE,
    UNIQUE (note_id, revision_id),
    FOREIGN KEY (note_id) REFERENCES managed_notes(note_id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (note_id, parent_revision_id)
        REFERENCES managed_note_revisions(note_id, revision_id)
        DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE managed_notes (
    note_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES managed_workspaces(workspace_id),
    relative_path TEXT,
    accepted_revision_id TEXT NOT NULL,
    materialized_revision_id TEXT,
    materialized_sha256 TEXT CHECK (
        materialized_sha256 IS NULL OR length(materialized_sha256) = 64
    ),
    write_base_sha256 TEXT CHECK (
        write_base_sha256 IS NULL OR length(write_base_sha256) = 64
    ),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (workspace_id, relative_path),
    CHECK (
        (materialized_revision_id IS NULL AND materialized_sha256 IS NULL)
        OR
        (materialized_revision_id IS NOT NULL AND materialized_sha256 IS NOT NULL)
    ),
    CHECK (materialized_revision_id IS NOT NULL OR write_base_sha256 IS NULL),
    FOREIGN KEY (note_id, accepted_revision_id)
        REFERENCES managed_note_revisions(note_id, revision_id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (note_id, materialized_revision_id)
        REFERENCES managed_note_revisions(note_id, revision_id)
        DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE managed_operations (
    operation_id TEXT PRIMARY KEY,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    workspace_id TEXT NOT NULL REFERENCES managed_workspaces(workspace_id),
    note_id TEXT REFERENCES managed_notes(note_id),
    kind TEXT NOT NULL CHECK (
        kind IN ('setup', 'refresh', 'accept_revision', 'materialize', 'deactivate', 'restore',
                 'resolve', 'grant_consent', 'revoke_consent', 'set_exclusion', 'accept_link')
    ),
    caller_actor_id TEXT NOT NULL,
    target_relative_path TEXT,
    expected_revision_id TEXT,
    expected_target_sha256 TEXT CHECK (
        expected_target_sha256 IS NULL OR length(expected_target_sha256) = 64
    ),
    body_bytes BLOB,
    body_sha256 TEXT CHECK (body_sha256 IS NULL OR length(body_sha256) = 64),
    status TEXT NOT NULL CHECK (
        status IN ('prepared', 'writing', 'promoted', 'completed', 'conflict', 'cancelled')
    ),
    stage INTEGER NOT NULL DEFAULT 0 CHECK (stage >= 0),
    created_at TEXT NOT NULL,
    completed_at TEXT,
    CHECK (
        (body_bytes IS NULL AND body_sha256 IS NULL)
        OR
        (body_bytes IS NOT NULL AND body_sha256 IS NOT NULL)
    ),
    CHECK (
        (status IN ('prepared', 'writing', 'promoted') AND completed_at IS NULL)
        OR
        (status IN ('completed', 'conflict', 'cancelled') AND completed_at IS NOT NULL)
    )
);
CREATE TABLE managed_recovery_decisions (
    recovery_request_id TEXT PRIMARY KEY,
    target_operation_id TEXT NOT NULL UNIQUE REFERENCES managed_operations(operation_id),
    actor_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL REFERENCES managed_workspaces(workspace_id),
    preview_sha256 TEXT NOT NULL CHECK (
        typeof(preview_sha256) = 'text' AND length(preview_sha256) = 64
    ),
    request_sha256 TEXT NOT NULL CHECK (
        typeof(request_sha256) = 'text' AND length(request_sha256) = 64
    ),
    snapshot_json TEXT NOT NULL CHECK (
        typeof(snapshot_json) = 'text'
        AND length(CAST(snapshot_json AS BLOB)) <= 16384
        AND CASE
            WHEN json_valid(snapshot_json) THEN json_type(snapshot_json) = 'object'
            ELSE 0
        END
    ),
    decision TEXT NOT NULL CHECK (
        decision = 'owner_abandon_unverifiable_legacy_write'
    ),
    completed_at TEXT NOT NULL
);
CREATE TABLE managed_source_deliveries (
 delivery_id TEXT PRIMARY KEY,
 envelope_sha256 TEXT NOT NULL CHECK(length(envelope_sha256)=64),
 envelope_bytes BLOB NOT NULL,
 source_id TEXT REFERENCES logical_sources(source_id),
 destination_brain_id TEXT NOT NULL,
 issuer_epoch INTEGER NOT NULL CHECK(issuer_epoch>0),
 expected_head TEXT,
 expected_lifecycle_version INTEGER NOT NULL CHECK(expected_lifecycle_version>=0),
 source_delivery_id TEXT NOT NULL UNIQUE,
 receipt_json TEXT
);
CREATE TABLE managed_suggestions (
    suggestion_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES managed_inference_requests(request_id),
    workspace_id TEXT NOT NULL REFERENCES managed_workspaces(workspace_id),
    source_note_id TEXT NOT NULL REFERENCES managed_notes(note_id),
    source_revision_id TEXT NOT NULL,
    target_note_id TEXT NOT NULL REFERENCES managed_notes(note_id),
    target_revision_id TEXT NOT NULL,
    source_quote TEXT NOT NULL,
    target_quote TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    output_sha256 TEXT NOT NULL CHECK (length(output_sha256) = 64),
    status TEXT NOT NULL CHECK (status IN ('pending', 'accepted', 'invalidated')),
    issued_at TEXT NOT NULL,
    accepted_at TEXT,
    CHECK (source_note_id != target_note_id),
    CHECK (
        (status = 'accepted' AND accepted_at IS NOT NULL)
        OR
        (status != 'accepted' AND accepted_at IS NULL)
    ),
    FOREIGN KEY (source_note_id, source_revision_id)
        REFERENCES managed_note_revisions(note_id, revision_id),
    FOREIGN KEY (target_note_id, target_revision_id)
        REFERENCES managed_note_revisions(note_id, revision_id)
);
CREATE TABLE managed_workspaces (
    workspace_id TEXT PRIMARY KEY,
    root_path TEXT UNIQUE,
    device TEXT,
    inode TEXT,
    owner_actor_id TEXT NOT NULL,
    origin_owner_actor_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    observation_generation INTEGER NOT NULL DEFAULT 0 CHECK (observation_generation >= 0),
    policy_generation INTEGER NOT NULL DEFAULT 0 CHECK (policy_generation >= 0),
    UNIQUE (device, inode),
    CHECK (
        (root_path IS NULL AND device IS NULL AND inode IS NULL)
        OR
        (root_path IS NOT NULL AND device IS NOT NULL AND inode IS NOT NULL)
    )
);
CREATE TABLE managed_write_authority (
    operation_id TEXT PRIMARY KEY REFERENCES managed_operations(operation_id),
    authority_version INTEGER NOT NULL CHECK (authority_version IN (0, 1)),
    descriptor_json TEXT,
    descriptor_sha256 TEXT,
    CHECK (
        (authority_version = 0 AND descriptor_json IS NULL AND descriptor_sha256 IS NULL)
        OR
        (authority_version = 1
         AND typeof(descriptor_json) = 'text'
         AND CASE
             WHEN json_valid(descriptor_json) THEN json_type(descriptor_json) = 'object'
             ELSE 0
         END
         AND typeof(descriptor_sha256) = 'text'
         AND length(descriptor_sha256) = 64)
    )
);
CREATE TABLE markdown_import_files (
    file_id TEXT PRIMARY KEY,
    root_id TEXT NOT NULL REFERENCES markdown_import_roots(root_id),
    relative_path TEXT NOT NULL,
    active_revision_id TEXT,
    last_observed_scan_id TEXT NOT NULL,
    last_observed_device TEXT,
    last_observed_inode TEXT,
    UNIQUE (root_id, relative_path),
    UNIQUE (file_id, active_revision_id),
    CHECK (
        (last_observed_device IS NULL AND last_observed_inode IS NULL)
        OR
        (last_observed_device IS NOT NULL AND last_observed_inode IS NOT NULL)
    ),
    FOREIGN KEY (file_id, active_revision_id)
        REFERENCES markdown_import_revisions(file_id, revision_id)
        DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE markdown_import_revisions (
    revision_id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL REFERENCES markdown_import_files(file_id),
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    delivery_id TEXT NOT NULL UNIQUE,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    capture_id TEXT UNIQUE REFERENCES captures(capture_id),
    first_observed_at TEXT NOT NULL,
    UNIQUE (file_id, content_sha256),
    UNIQUE (file_id, revision_id)
);
CREATE TABLE markdown_import_roots (
    root_id TEXT PRIMARY KEY,
    canonical_path TEXT NOT NULL UNIQUE,
    device TEXT NOT NULL,
    inode TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_complete_scan_id TEXT,
    last_complete_scan_at TEXT,
    UNIQUE (device, inode),
    CHECK (
        (last_complete_scan_id IS NULL AND last_complete_scan_at IS NULL)
        OR
        (last_complete_scan_id IS NOT NULL AND last_complete_scan_at IS NOT NULL)
    )
);
CREATE TABLE privacy_invalid_evidence (
    target_kind TEXT NOT NULL CHECK (
        target_kind IN ('source_revision','canonical_revision','search_document')
    ),
    target_id TEXT NOT NULL,
    invalid_reason TEXT NOT NULL CHECK (
        invalid_reason IN ('missing','malformed','inconsistent')
    ),
    invalid_evidence_sha256 TEXT NOT NULL CHECK (length(invalid_evidence_sha256) = 64),
    PRIMARY KEY (target_kind, target_id, invalid_evidence_sha256)
);
CREATE TABLE privacy_repair_ledger (
    repair_id TEXT PRIMARY KEY,
    repair_sequence INTEGER NOT NULL UNIQUE CHECK (repair_sequence > 0),
    target_kind TEXT NOT NULL CHECK (
        target_kind IN ('source_revision','canonical_revision')
    ),
    target_id TEXT NOT NULL,
    invalid_evidence_sha256 TEXT NOT NULL CHECK (length(invalid_evidence_sha256) = 64),
    owner_actor_id TEXT NOT NULL CHECK (length(owner_actor_id) > 0),
    replacement_privacy_json TEXT NOT NULL CHECK (
        typeof(replacement_privacy_json) = 'text'
        AND CASE
            WHEN json_valid(replacement_privacy_json)
            THEN json_type(replacement_privacy_json) = 'object'
            ELSE 0
        END
    ),
    operation_id TEXT NOT NULL UNIQUE,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    issuer_epoch INTEGER NOT NULL CHECK (issuer_epoch > 0),
    receipt_json TEXT NOT NULL CHECK (
        typeof(receipt_json) = 'text'
        AND CASE
            WHEN json_valid(receipt_json)
            THEN json_type(receipt_json) = 'object'
            ELSE 0
        END
    ),
    recorded_at TEXT NOT NULL CHECK (length(recorded_at) > 0),
    supersedes_repair_id TEXT REFERENCES privacy_repair_ledger(repair_id),
    FOREIGN KEY (target_kind, target_id, invalid_evidence_sha256)
        REFERENCES privacy_invalid_evidence(target_kind, target_id, invalid_evidence_sha256)
);
CREATE TABLE proposal_sets (
    delivery_id TEXT PRIMARY KEY,
    request_sha256 TEXT NOT NULL,
    capture_id TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    stage INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE proposals (
    proposal_id TEXT PRIMARY KEY,
    set_delivery_id TEXT NOT NULL,
    capture_id TEXT NOT NULL,
    proposed_kind TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    proposed_bytes BLOB NOT NULL,
    supplied_reason TEXT,
    space_id TEXT,
    receipt_id TEXT NOT NULL UNIQUE,
    page_id TEXT,
    canonical_path TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    terminal_decision_id TEXT UNIQUE
);
CREATE TABLE relationship_decisions (
 decision_id TEXT PRIMARY KEY,
 relationship_id TEXT NOT NULL REFERENCES revision_relationships(relationship_id),
 sequence INTEGER NOT NULL UNIQUE, operation_id TEXT NOT NULL UNIQUE,
 request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
 decision TEXT NOT NULL CHECK(decision IN ('accept','reject','remove')),
 relationship_version INTEGER NOT NULL CHECK(relationship_version>=1),
 recorded_at TEXT NOT NULL, actor_id TEXT NOT NULL, receipt_json TEXT NOT NULL
);
CREATE TABLE review_contexts (
    proposal_id TEXT PRIMARY KEY REFERENCES proposals(proposal_id),
    binding_json BLOB NOT NULL,
    proposal_json BLOB NOT NULL
);
CREATE TABLE review_page_heads (
    page_id TEXT PRIMARY KEY,
    publication_id TEXT NOT NULL UNIQUE,
    proposal_id TEXT NOT NULL REFERENCES proposals(proposal_id),
    capture_id TEXT NOT NULL REFERENCES captures(capture_id),
    canonical_path TEXT NOT NULL UNIQUE,
    published_sha256 TEXT NOT NULL CHECK (length(published_sha256) = 64)
);
CREATE TABLE review_sources (
    proposal_id TEXT NOT NULL REFERENCES proposals(proposal_id),
    capture_id TEXT NOT NULL REFERENCES captures(capture_id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0 AND ordinal < 32),
    PRIMARY KEY (proposal_id, capture_id),
    UNIQUE (proposal_id, ordinal)
);
CREATE TABLE revision_relationships (
 relationship_id TEXT PRIMARY KEY, left_record_id TEXT NOT NULL, left_revision_id TEXT NOT NULL,
 right_record_id TEXT NOT NULL, right_revision_id TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('duplicate_of','supersedes','contradicts')),
 decision TEXT NOT NULL CHECK(decision IN ('accept','reject','remove')),
 version INTEGER NOT NULL CHECK(version>=1),
 UNIQUE(left_record_id,left_revision_id,right_record_id,right_revision_id,kind)
);
CREATE TABLE route_operations (
    delivery_id TEXT PRIMARY KEY,
    request_sha256 TEXT NOT NULL,
    route_id TEXT NOT NULL UNIQUE,
    supersedes_route_id TEXT,
    capture_id TEXT NOT NULL,
    space_id TEXT NOT NULL,
    receipt_id TEXT NOT NULL UNIQUE,
    recorded_at TEXT NOT NULL,
    stage INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE runtime_compatibility (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 minimum_runtime_session_version INTEGER NOT NULL CHECK(minimum_runtime_session_version=6),
 state_schema_version INTEGER NOT NULL CHECK(state_schema_version=11)
);
INSERT INTO "runtime_compatibility" VALUES(1,6,11);
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY CHECK (version > 0),
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
INSERT INTO "schema_migrations" VALUES(1,'local_baseline','7c59f87406a0386d523a49b9d1df1b1996432260c27987f8df81271bb5ffd525','2026-10-02T23:09:26.730915Z');
INSERT INTO "schema_migrations" VALUES(2,'local_search_and_import','b1bac4a7791ba1a3f9eea782df714dfee7027c0c9a4b296e98246b8e1c4ccb33','2026-10-02T23:09:26.732341Z');
INSERT INTO "schema_migrations" VALUES(3,'managed_workspace','03fd749e7ef1afc11e0acb9636bd83b1cd41eca0e44824c2b7708348d01a1686','2026-10-02T23:09:26.733284Z');
INSERT INTO "schema_migrations" VALUES(4,'runtime_compatibility','46626bc341838cbd2970408a3256b0301f8afd3d7d74788ec92242eb7ba93811','2026-10-02T23:09:26.733436Z');
INSERT INTO "schema_migrations" VALUES(5,'review_publication','42596b9f92af6b00a5eaafeafb23ed93945c21fa2682855a345102a435e4ab96','2026-10-02T23:09:26.733758Z');
INSERT INTO "schema_migrations" VALUES(6,'managed_recovery_authority','4b15ce56af0cd7c40663c5fae188ac43e02953ed534f48c04b4103211d65a3e8','2026-10-02T23:09:26.734067Z');
INSERT INTO "schema_migrations" VALUES(7,'immutable_source_history','ed19cb8b96810e91a3eaa3020e38af95c2dae6893a48c4a560f2ceeaa29b1d44','2026-10-02T23:09:26.735145Z');
INSERT INTO "schema_migrations" VALUES(8,'effective_privacy_projection','e85ce7ea1168d934809da58b15bb21ff8a4645c4ab13c7e8e313c5f04103e04f','2026-10-02T23:09:26.738702Z');
INSERT INTO "schema_migrations" VALUES(9,'issuer_identity_and_owner_repair','759b438f01247314e9e646d6bbe9318699fe1a8173531c724be355b08d436c8f','2026-10-02T23:09:26.741087Z');
INSERT INTO "schema_migrations" VALUES(10,'durable_capture_ingestion_journal','4af102664eef6baa0b361600d9db8776761ae3445c78f496149b9f7fc5dad67d','2026-10-02T23:09:26.741869Z');
INSERT INTO "schema_migrations" VALUES(11,'saved_markdown_lifecycle','460e092527223dca89f109d85ff322959f7f594d28dc39a5d3f80b737678b740','2026-10-02T23:09:26.742253Z');
CREATE TABLE "search_documents" (
    result_id TEXT PRIMARY KEY NOT NULL,
    capture_id TEXT NOT NULL,
    record_type TEXT NOT NULL,
    payload_family TEXT NOT NULL,
    space_id TEXT,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    trust TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    canonical_path TEXT,
    updated_at TEXT NOT NULL
, effective_tier TEXT NOT NULL
    CHECK (effective_tier IN ('public','work','personal','secret','unknown'))
    DEFAULT 'unknown', effective_cloud INTEGER NOT NULL
    CHECK (effective_cloud IN (0, 1)) DEFAULT 0, effective_external_egress INTEGER NOT NULL
    CHECK (effective_external_egress IN (0, 1)) DEFAULT 0, invalid_evidence_reason TEXT CHECK (
    invalid_evidence_reason IS NULL
    OR invalid_evidence_reason IN ('missing','malformed','inconsistent')
), invalid_evidence_sha256 TEXT CHECK (
    invalid_evidence_sha256 IS NULL OR length(invalid_evidence_sha256) = 64
), applied_repair_id TEXT, applied_repair_sequence INTEGER CHECK (
    applied_repair_sequence IS NULL OR applied_repair_sequence > 0
));
PRAGMA writable_schema=ON;
INSERT INTO sqlite_master(type,name,tbl_name,rootpage,sql)VALUES('table','search_documents_fts','search_documents_fts',0,'CREATE VIRTUAL TABLE search_documents_fts USING fts5(
        result_id UNINDEXED,
        title,
        body,
        tokenize=''unicode61 remove_diacritics 2''
    )');
CREATE TABLE 'search_documents_fts_config'(k PRIMARY KEY, v) WITHOUT ROWID;
INSERT INTO "search_documents_fts_config" VALUES('version',4);
CREATE TABLE 'search_documents_fts_content'(id INTEGER PRIMARY KEY, c0, c1, c2);
CREATE TABLE 'search_documents_fts_data'(id INTEGER PRIMARY KEY, block BLOB);
INSERT INTO "search_documents_fts_data" VALUES(1,X'');
INSERT INTO "search_documents_fts_data" VALUES(10,X'00000000000000');
CREATE TABLE 'search_documents_fts_docsize'(id INTEGER PRIMARY KEY, sz BLOB);
CREATE TABLE 'search_documents_fts_idx'(segid, term, pgno, PRIMARY KEY(segid, term)) WITHOUT ROWID;
CREATE TABLE search_fts_identity (
        fts_rowid INTEGER PRIMARY KEY,
        result_id TEXT NOT NULL UNIQUE
    );
CREATE TABLE source_aliases (
 delivery_id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES logical_sources(source_id),
 evidence_sha256 TEXT NOT NULL CHECK(length(evidence_sha256)=64)
);
CREATE TABLE source_intakes (
 namespace_sha256 TEXT NOT NULL, revision_key TEXT NOT NULL,
 source_id TEXT NOT NULL, request_sha256 TEXT NOT NULL,
 delivery_id TEXT NOT NULL UNIQUE, plan_json TEXT NOT NULL,
 submission_json BLOB NOT NULL, receipt_json TEXT,
 PRIMARY KEY(namespace_sha256,revision_key)
);
CREATE TABLE source_lifecycle_operations (
 operation_id TEXT PRIMARY KEY,
 request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
 source_id TEXT NOT NULL REFERENCES logical_sources(source_id),
 expected_head TEXT NOT NULL REFERENCES source_revisions(capture_id),
 expected_lifecycle_version INTEGER NOT NULL CHECK(expected_lifecycle_version>=0),
 resulting_lifecycle_version INTEGER NOT NULL CHECK(resulting_lifecycle_version>=1),
 reason_code TEXT NOT NULL CHECK(length(reason_code)>0 AND length(reason_code)<=96),
 absence_evidence_digest TEXT CHECK(
     absence_evidence_digest IS NULL OR length(absence_evidence_digest)=64
 ),
 sequence INTEGER NOT NULL UNIQUE CHECK(sequence>=1),
 recorded_at TEXT NOT NULL,
 receipt_json TEXT NOT NULL,
 UNIQUE(source_id, resulting_lifecycle_version)
);
CREATE TABLE source_lifecycle_state (
 source_id TEXT PRIMARY KEY REFERENCES logical_sources(source_id),
 lifecycle_version INTEGER NOT NULL CHECK(lifecycle_version>=0)
);
CREATE TABLE source_namespaces (
 namespace_sha256 TEXT PRIMARY KEY CHECK(length(namespace_sha256)=64),
 namespace_json TEXT NOT NULL UNIQUE,
 source_id TEXT NOT NULL UNIQUE REFERENCES logical_sources(source_id)
);
CREATE TABLE source_operations (
 operation_id TEXT PRIMARY KEY, request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
 source_id TEXT NOT NULL REFERENCES logical_sources(source_id), receipt_json TEXT NOT NULL
);
CREATE TABLE source_quarantine (
 custody_id TEXT PRIMARY KEY, source_id TEXT REFERENCES logical_sources(source_id),
 revision_key TEXT NOT NULL, request_sha256 TEXT NOT NULL, submission_json BLOB NOT NULL,
 recorded_at TEXT NOT NULL
);
CREATE TABLE source_revision_privacy (
    capture_id TEXT PRIMARY KEY REFERENCES source_revisions(capture_id),
    effective_privacy_json TEXT NOT NULL CHECK (
        typeof(effective_privacy_json) = 'text'
        AND CASE
            WHEN json_valid(effective_privacy_json)
                THEN json_type(effective_privacy_json) = 'object'
            ELSE 0
        END
    )
);
CREATE TABLE source_revisions (
 capture_id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES logical_sources(source_id)
 DEFERRABLE INITIALLY DEFERRED,
 sequence INTEGER NOT NULL CHECK(sequence >= 1), predecessor_capture_id TEXT,
 source_path TEXT NOT NULL UNIQUE, source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
 source_bytes BLOB NOT NULL, request_sha256 TEXT, revision_key TEXT,
 ordering_json TEXT,
 recorded_at TEXT NOT NULL, diagnostic TEXT,
 UNIQUE(source_id,sequence), UNIQUE(source_id,revision_key), UNIQUE(source_id,capture_id),
 FOREIGN KEY(source_id,predecessor_capture_id) REFERENCES source_revisions(source_id,capture_id)
);
CREATE TABLE space_operations (
    delivery_id TEXT PRIMARY KEY,
    request_sha256 TEXT NOT NULL,
    operation TEXT NOT NULL,
    space_id TEXT NOT NULL,
    name TEXT NOT NULL,
    receipt_id TEXT NOT NULL UNIQUE,
    recorded_at TEXT NOT NULL,
    stage INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE spaces (
    space_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX route_identity_idx ON route_operations (route_id);
CREATE INDEX markdown_import_files_finalize_idx
ON markdown_import_files(root_id, last_observed_scan_id)
WHERE active_revision_id IS NOT NULL;
CREATE INDEX search_capture_idx ON search_documents (capture_id);
CREATE TRIGGER search_documents_result_id_immutable
    BEFORE UPDATE OF result_id ON search_documents
    WHEN new.result_id IS NOT old.result_id
    BEGIN
        SELECT RAISE(ABORT, 'search result identity is immutable');
    END;
CREATE TRIGGER search_documents_fts_insert
    AFTER INSERT ON search_documents
    BEGIN
        INSERT INTO search_fts_identity(result_id) VALUES (new.result_id);
        INSERT INTO search_documents_fts(rowid, result_id, title, body)
        SELECT fts_rowid, new.result_id, new.title, new.body
        FROM search_fts_identity
        WHERE result_id = new.result_id;
    END;
CREATE TRIGGER search_documents_fts_update
    AFTER UPDATE OF title, body ON search_documents
    BEGIN
        DELETE FROM search_documents_fts
        WHERE rowid = (
            SELECT fts_rowid FROM search_fts_identity WHERE result_id = old.result_id
        );
        INSERT INTO search_documents_fts(rowid, result_id, title, body)
        SELECT fts_rowid, new.result_id, new.title, new.body
        FROM search_fts_identity
        WHERE result_id = new.result_id;
    END;
CREATE TRIGGER search_documents_fts_delete
    AFTER DELETE ON search_documents
    BEGIN
        DELETE FROM search_documents_fts
        WHERE rowid = (
            SELECT fts_rowid FROM search_fts_identity WHERE result_id = old.result_id
        );
        DELETE FROM search_fts_identity WHERE result_id = old.result_id;
    END;
CREATE UNIQUE INDEX managed_open_conflict_idx
ON managed_conflicts(note_id) WHERE status = 'open';
CREATE UNIQUE INDEX managed_active_consent_idx
ON managed_consents(workspace_id, provider, access_mode, operation, note_scope)
WHERE active = 1;
CREATE INDEX managed_operations_recovery_idx
ON managed_operations(workspace_id, status, created_at)
WHERE status IN ('prepared', 'writing', 'promoted');
CREATE INDEX review_sources_capture_idx ON review_sources (capture_id, proposal_id);
CREATE TRIGGER generation_search_documents_insert AFTER INSERT ON search_documents
 BEGIN UPDATE engine_generations
 SET retrieval_generation=retrieval_generation+1 WHERE singleton=1; END;
CREATE TRIGGER generation_search_documents_update AFTER UPDATE ON search_documents
 BEGIN UPDATE engine_generations
 SET retrieval_generation=retrieval_generation+1 WHERE singleton=1; END;
CREATE TRIGGER generation_search_documents_delete AFTER DELETE ON search_documents
 BEGIN UPDATE engine_generations
 SET retrieval_generation=retrieval_generation+1 WHERE singleton=1; END;
CREATE TRIGGER generation_logical_sources_insert AFTER INSERT ON logical_sources
 BEGIN UPDATE engine_generations
 SET retrieval_generation=retrieval_generation+1 WHERE singleton=1; END;
CREATE TRIGGER generation_logical_sources_update AFTER UPDATE ON logical_sources
 BEGIN UPDATE engine_generations
 SET retrieval_generation=retrieval_generation+1 WHERE singleton=1; END;
CREATE TRIGGER generation_logical_sources_delete AFTER DELETE ON logical_sources
 BEGIN UPDATE engine_generations
 SET retrieval_generation=retrieval_generation+1 WHERE singleton=1; END;
CREATE TRIGGER generation_source_revisions_insert AFTER INSERT ON source_revisions
 BEGIN UPDATE engine_generations
 SET retrieval_generation=retrieval_generation+1 WHERE singleton=1; END;
CREATE TRIGGER generation_source_revisions_update AFTER UPDATE ON source_revisions
 BEGIN UPDATE engine_generations
 SET retrieval_generation=retrieval_generation+1 WHERE singleton=1; END;
CREATE TRIGGER generation_source_revisions_delete AFTER DELETE ON source_revisions
 BEGIN UPDATE engine_generations
 SET retrieval_generation=retrieval_generation+1 WHERE singleton=1; END;
CREATE TRIGGER generation_review_page_heads_insert AFTER INSERT ON review_page_heads
 BEGIN UPDATE engine_generations
 SET retrieval_generation=retrieval_generation+1 WHERE singleton=1; END;
CREATE TRIGGER generation_review_page_heads_update AFTER UPDATE ON review_page_heads
 BEGIN UPDATE engine_generations
 SET retrieval_generation=retrieval_generation+1 WHERE singleton=1; END;
CREATE TRIGGER generation_review_page_heads_delete AFTER DELETE ON review_page_heads
 BEGIN UPDATE engine_generations
 SET retrieval_generation=retrieval_generation+1 WHERE singleton=1; END;
CREATE TRIGGER generation_canonical_revision_members_insert
 AFTER INSERT ON canonical_revision_members
 BEGIN UPDATE engine_generations
 SET retrieval_generation=retrieval_generation+1 WHERE singleton=1; END;
CREATE TRIGGER generation_canonical_revision_members_update
 AFTER UPDATE ON canonical_revision_members
 BEGIN UPDATE engine_generations
 SET retrieval_generation=retrieval_generation+1 WHERE singleton=1; END;
CREATE TRIGGER generation_canonical_revision_members_delete
 AFTER DELETE ON canonical_revision_members
 BEGIN UPDATE engine_generations
 SET retrieval_generation=retrieval_generation+1 WHERE singleton=1; END;
CREATE TRIGGER privacy_invalid_evidence_update_immutable
BEFORE UPDATE ON privacy_invalid_evidence
BEGIN
    SELECT RAISE(ABORT, 'privacy invalid evidence is append-only');
END;
CREATE TRIGGER privacy_invalid_evidence_delete_immutable
BEFORE DELETE ON privacy_invalid_evidence
BEGIN
    SELECT RAISE(ABORT, 'privacy invalid evidence is append-only');
END;
CREATE TRIGGER brain_identity_update_immutable BEFORE UPDATE ON brain_identity
BEGIN
    SELECT RAISE(ABORT, 'brain identity is durable');
END;
CREATE TRIGGER brain_identity_delete_immutable BEFORE DELETE ON brain_identity
BEGIN
    SELECT RAISE(ABORT, 'brain identity is durable');
END;
CREATE UNIQUE INDEX legacy_issuer_binding_identity_idx
ON legacy_issuer_bindings(artifact_path, coalesce(jsonl_ordinal, -1));
CREATE TRIGGER legacy_issuer_bindings_update_immutable
BEFORE UPDATE ON legacy_issuer_bindings
BEGIN
    SELECT RAISE(ABORT, 'legacy issuer bindings are append-only');
END;
CREATE TRIGGER legacy_issuer_bindings_delete_immutable
BEFORE DELETE ON legacy_issuer_bindings
BEGIN
    SELECT RAISE(ABORT, 'legacy issuer bindings are append-only');
END;
CREATE TRIGGER issuer_migration_marker_update_immutable
BEFORE UPDATE ON issuer_migration_marker
BEGIN
    SELECT RAISE(ABORT, 'issuer migration marker is append-only');
END;
CREATE TRIGGER issuer_migration_marker_delete_immutable
BEFORE DELETE ON issuer_migration_marker
BEGIN
    SELECT RAISE(ABORT, 'issuer migration marker is append-only');
END;
CREATE UNIQUE INDEX privacy_repair_supersession_idx
ON privacy_repair_ledger(supersedes_repair_id) WHERE supersedes_repair_id IS NOT NULL;
CREATE TRIGGER privacy_repair_ledger_update_immutable BEFORE UPDATE ON privacy_repair_ledger
BEGIN
    SELECT RAISE(ABORT, 'privacy repair ledger is append-only');
END;
CREATE TRIGGER privacy_repair_ledger_delete_immutable BEFORE DELETE ON privacy_repair_ledger
BEGIN
    SELECT RAISE(ABORT, 'privacy repair ledger is append-only');
END;
CREATE TRIGGER search_documents_privacy_insert_shape BEFORE INSERT ON search_documents
BEGIN
    SELECT CASE
        WHEN (NEW.invalid_evidence_reason IS NULL) != (NEW.invalid_evidence_sha256 IS NULL)
        THEN RAISE(ABORT, 'invalid search privacy evidence pairing')
        WHEN (NEW.applied_repair_id IS NULL) != (NEW.applied_repair_sequence IS NULL)
        THEN RAISE(ABORT, 'invalid search privacy repair pairing')
        WHEN NEW.effective_tier IN ('secret','unknown')
        AND (NEW.effective_cloud != 0 OR NEW.effective_external_egress != 0)
        THEN RAISE(ABORT, 'invalid search privacy authority')
        WHEN NEW.applied_repair_id IS NOT NULL AND NEW.invalid_evidence_reason IS NULL
        THEN RAISE(ABORT, 'invalid search privacy repair lineage')
        WHEN NEW.applied_repair_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM privacy_repair_ledger AS repair
            WHERE repair.repair_id = NEW.applied_repair_id
            AND repair.repair_sequence = NEW.applied_repair_sequence
            AND repair.invalid_evidence_sha256 = NEW.invalid_evidence_sha256
            AND (repair.target_kind != 'source_revision' OR repair.target_id = NEW.capture_id)
        )
        THEN RAISE(ABORT, 'invalid search privacy repair binding')
        WHEN NEW.invalid_evidence_reason IS NOT NULL
        AND (NEW.effective_tier != 'unknown' OR NEW.effective_cloud != 0
        OR NEW.effective_external_egress != 0)
        AND NEW.applied_repair_id IS NULL
        THEN RAISE(ABORT, 'invalid search privacy failure closure')
    END;
END;
CREATE TRIGGER search_documents_privacy_update_shape BEFORE UPDATE ON search_documents
BEGIN
    SELECT CASE
        WHEN (NEW.invalid_evidence_reason IS NULL) != (NEW.invalid_evidence_sha256 IS NULL)
        THEN RAISE(ABORT, 'invalid search privacy evidence pairing')
        WHEN (NEW.applied_repair_id IS NULL) != (NEW.applied_repair_sequence IS NULL)
        THEN RAISE(ABORT, 'invalid search privacy repair pairing')
        WHEN NEW.effective_tier IN ('secret','unknown')
        AND (NEW.effective_cloud != 0 OR NEW.effective_external_egress != 0)
        THEN RAISE(ABORT, 'invalid search privacy authority')
        WHEN NEW.applied_repair_id IS NOT NULL AND NEW.invalid_evidence_reason IS NULL
        THEN RAISE(ABORT, 'invalid search privacy repair lineage')
        WHEN NEW.applied_repair_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM privacy_repair_ledger AS repair
            WHERE repair.repair_id = NEW.applied_repair_id
            AND repair.repair_sequence = NEW.applied_repair_sequence
            AND repair.invalid_evidence_sha256 = NEW.invalid_evidence_sha256
            AND (repair.target_kind != 'source_revision' OR repair.target_id = NEW.capture_id)
        )
        THEN RAISE(ABORT, 'invalid search privacy repair binding')
        WHEN NEW.invalid_evidence_reason IS NOT NULL
        AND (NEW.effective_tier != 'unknown' OR NEW.effective_cloud != 0
        OR NEW.effective_external_egress != 0)
        AND NEW.applied_repair_id IS NULL
        THEN RAISE(ABORT, 'invalid search privacy failure closure')
    END;
END;
CREATE INDEX capture_ingestion_items_pending_order_idx
ON capture_ingestion_items(journal_sequence);
CREATE INDEX capture_ingestion_events_delivery_idx
ON capture_ingestion_events(delivery_id, event_sequence DESC);
CREATE TRIGGER capture_ingestion_items_update_immutable
BEFORE UPDATE ON capture_ingestion_items
BEGIN
    SELECT RAISE(ABORT, 'ingestion item is immutable');
END;
CREATE TRIGGER capture_ingestion_payloads_update_immutable
BEFORE UPDATE ON capture_ingestion_payloads
BEGIN
    SELECT RAISE(ABORT, 'ingestion payload is immutable');
END;
CREATE TRIGGER capture_ingestion_events_update_immutable
BEFORE UPDATE ON capture_ingestion_events
BEGIN
    SELECT RAISE(ABORT, 'ingestion event is append-only');
END;
CREATE TRIGGER capture_ingestion_tombstones_update_immutable
BEFORE UPDATE ON capture_ingestion_tombstones
BEGIN
    SELECT RAISE(ABORT, 'ingestion tombstone is immutable');
END;
CREATE TRIGGER capture_ingestion_tombstones_delete_immutable
BEFORE DELETE ON capture_ingestion_tombstones
BEGIN
    SELECT RAISE(ABORT, 'ingestion tombstone is immutable');
END;
CREATE TRIGGER capture_ingestion_payloads_delete_guarded
BEFORE DELETE ON capture_ingestion_payloads
WHEN NOT EXISTS (
    SELECT 1 FROM capture_ingestion_events
    WHERE delivery_id = OLD.delivery_id
    AND event_kind IN ('accepted', 'duplicate', 'discarded')
)
BEGIN
    SELECT RAISE(ABORT, 'ingestion payload requires a terminal event');
END;
CREATE TRIGGER capture_ingestion_items_delete_guarded
BEFORE DELETE ON capture_ingestion_items
WHEN NOT (
    EXISTS (
        SELECT 1 FROM capture_ingestion_events
        WHERE delivery_id = OLD.delivery_id AND event_kind IN ('accepted', 'duplicate')
    )
    AND EXISTS (
        SELECT 1 FROM captures
        WHERE delivery_id = OLD.delivery_id AND request_sha256 = OLD.request_sha256
    )
) AND NOT EXISTS (
    SELECT 1 FROM capture_ingestion_tombstones
    WHERE delivery_id = OLD.delivery_id AND request_sha256 = OLD.request_sha256
)
BEGIN
    SELECT RAISE(ABORT, 'ingestion item requires durable terminal replay');
END;
CREATE VIEW capture_ingestion_pending AS
SELECT item.journal_sequence, item.ingestion_id, item.delivery_id, item.request_sha256,
       item.envelope_sha256, item.submission_path, item.byte_count, item.queued_at
FROM capture_ingestion_items AS item
JOIN capture_ingestion_payloads AS payload USING (delivery_id)
WHERE COALESCE(
    (SELECT event.event_kind FROM capture_ingestion_events AS event
     WHERE event.delivery_id = item.delivery_id
     ORDER BY event.event_sequence DESC LIMIT 1),
    'queued'
) NOT IN ('accepted', 'duplicate', 'discarded');
CREATE TRIGGER source_lifecycle_state_seed AFTER INSERT ON logical_sources
BEGIN INSERT INTO source_lifecycle_state(source_id,lifecycle_version) VALUES(NEW.source_id,0); END;
PRAGMA writable_schema=OFF;
DELETE FROM "sqlite_sequence";
COMMIT;
PRAGMA user_version=11;

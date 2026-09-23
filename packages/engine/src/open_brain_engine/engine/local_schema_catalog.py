"""Frozen SQL for the local schema migrations; independent of event storage."""

from open_brain_engine.storage.migrations import _migration

from .source_schema import SOURCE_HISTORY_SCHEMA

BASELINE = (
    """
CREATE TABLE IF NOT EXISTS captures (
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
)
    """.strip(),
    """
CREATE TABLE IF NOT EXISTS spaces (
    space_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    updated_at TEXT NOT NULL
)
    """.strip(),
    """
CREATE TABLE IF NOT EXISTS space_operations (
    delivery_id TEXT PRIMARY KEY,
    request_sha256 TEXT NOT NULL,
    operation TEXT NOT NULL,
    space_id TEXT NOT NULL,
    name TEXT NOT NULL,
    receipt_id TEXT NOT NULL UNIQUE,
    recorded_at TEXT NOT NULL,
    stage INTEGER NOT NULL DEFAULT 0
)
    """.strip(),
    """
CREATE TABLE IF NOT EXISTS route_operations (
    delivery_id TEXT PRIMARY KEY,
    request_sha256 TEXT NOT NULL,
    route_id TEXT NOT NULL UNIQUE,
    supersedes_route_id TEXT,
    capture_id TEXT NOT NULL,
    space_id TEXT NOT NULL,
    receipt_id TEXT NOT NULL UNIQUE,
    recorded_at TEXT NOT NULL,
    stage INTEGER NOT NULL DEFAULT 0
)
    """.strip(),
    """
CREATE TABLE IF NOT EXISTS proposal_sets (
    delivery_id TEXT PRIMARY KEY,
    request_sha256 TEXT NOT NULL,
    capture_id TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    stage INTEGER NOT NULL DEFAULT 0
)
    """.strip(),
    """
CREATE TABLE IF NOT EXISTS proposals (
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
)
    """.strip(),
    """
CREATE TABLE IF NOT EXISTS decisions (
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
)
    """.strip(),
    """
CREATE TABLE IF NOT EXISTS search_documents (
    result_id TEXT PRIMARY KEY,
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
)
    """.strip(),
    """
CREATE INDEX IF NOT EXISTS search_capture_idx ON search_documents (capture_id)
    """.strip(),
    """
CREATE UNIQUE INDEX IF NOT EXISTS route_identity_idx ON route_operations (route_id)
    """.strip(),
)

IMPORT_SCHEMA = (
    """
CREATE TABLE IF NOT EXISTS markdown_import_roots (
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
)
    """.strip(),
    """
CREATE TABLE IF NOT EXISTS markdown_import_files (
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
)
    """.strip(),
    """
CREATE TABLE IF NOT EXISTS markdown_import_revisions (
    revision_id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL REFERENCES markdown_import_files(file_id),
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    delivery_id TEXT NOT NULL UNIQUE,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    capture_id TEXT UNIQUE REFERENCES captures(capture_id),
    first_observed_at TEXT NOT NULL,
    UNIQUE (file_id, content_sha256),
    UNIQUE (file_id, revision_id)
)
    """.strip(),
    """
CREATE INDEX IF NOT EXISTS markdown_import_files_finalize_idx
ON markdown_import_files(root_id, last_observed_scan_id)
WHERE active_revision_id IS NOT NULL
    """.strip(),
)

LIVE_SEARCH_SCHEMA = (
    """
CREATE TABLE IF NOT EXISTS search_fts_identity (
        fts_rowid INTEGER PRIMARY KEY,
        result_id TEXT NOT NULL UNIQUE
    )
    """.strip(),
    """
CREATE VIRTUAL TABLE IF NOT EXISTS search_documents_fts USING fts5(
        result_id UNINDEXED,
        title,
        body,
        tokenize='unicode61 remove_diacritics 2'
    )
    """.strip(),
    """
CREATE TRIGGER IF NOT EXISTS search_documents_result_id_immutable
    BEFORE UPDATE OF result_id ON search_documents
    WHEN new.result_id IS NOT old.result_id
    BEGIN
        SELECT RAISE(ABORT, 'search result identity is immutable');
    END
    """.strip(),
    """
CREATE TRIGGER IF NOT EXISTS search_documents_fts_insert
    AFTER INSERT ON search_documents
    BEGIN
        INSERT INTO search_fts_identity(result_id) VALUES (new.result_id);
        INSERT INTO search_documents_fts(rowid, result_id, title, body)
        SELECT fts_rowid, new.result_id, new.title, new.body
        FROM search_fts_identity
        WHERE result_id = new.result_id;
    END
    """.strip(),
    """
CREATE TRIGGER IF NOT EXISTS search_documents_fts_update
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
    END
    """.strip(),
    """
CREATE TRIGGER IF NOT EXISTS search_documents_fts_delete
    AFTER DELETE ON search_documents
    BEGIN
        DELETE FROM search_documents_fts
        WHERE rowid = (
            SELECT fts_rowid FROM search_fts_identity WHERE result_id = old.result_id
        );
        DELETE FROM search_fts_identity WHERE result_id = old.result_id;
    END
    """.strip(),
)

SEARCH_SCHEMA = (
    """
CREATE TABLE IF NOT EXISTS search_documents (
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
)
    """.strip(),
)

_MIGRATION_2 = (
    IMPORT_SCHEMA
    + LIVE_SEARCH_SCHEMA[:2]
    + (
        """
DROP TRIGGER IF EXISTS search_documents_result_id_immutable
    """.strip(),
        """
DROP TRIGGER IF EXISTS search_documents_fts_insert
    """.strip(),
        """
DROP TRIGGER IF EXISTS search_documents_fts_update
    """.strip(),
        """
DROP TRIGGER IF EXISTS search_documents_fts_delete
    """.strip(),
        """
DELETE FROM search_documents_fts
    """.strip(),
        """
DELETE FROM search_fts_identity
    """.strip(),
        """
CREATE TABLE IF NOT EXISTS local_migration_search_documents (
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
)
    """.strip(),
        """
INSERT INTO local_migration_search_documents (
    result_id, capture_id, record_type, payload_family, space_id, title, body,
    trust, provenance_json, canonical_path, updated_at
)
SELECT d.result_id, d.capture_id, d.record_type, d.payload_family, d.space_id,
    local_public_search_text(d.title, c.source_reference),
    local_public_search_text(d.body, c.source_reference),
    d.trust, d.provenance_json, d.canonical_path, d.updated_at
FROM search_documents AS d JOIN captures AS c ON c.capture_id = d.capture_id
ORDER BY d.result_id
    """.strip(),
        """
DROP TABLE search_documents
    """.strip(),
        """
ALTER TABLE local_migration_search_documents RENAME TO search_documents
    """.strip(),
        """
CREATE INDEX IF NOT EXISTS search_capture_idx ON search_documents (capture_id)
    """.strip(),
        """
INSERT INTO search_fts_identity(result_id) SELECT result_id FROM search_documents ORDER BY result_id
    """.strip(),
        """
INSERT INTO search_documents_fts(rowid, result_id, title, body)
SELECT i.fts_rowid, d.result_id, d.title, d.body
FROM search_documents AS d JOIN search_fts_identity AS i USING (result_id)
ORDER BY i.fts_rowid
    """.strip(),
    )
    + LIVE_SEARCH_SCHEMA[2:]
)

MANAGED_WORKSPACE_SCHEMA = (
    """
CREATE TABLE IF NOT EXISTS managed_workspaces (
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
)
    """.strip(),
    """
CREATE TABLE IF NOT EXISTS managed_note_revisions (
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
)
    """.strip(),
    """
CREATE TABLE IF NOT EXISTS managed_notes (
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
)
    """.strip(),
    """
CREATE TABLE IF NOT EXISTS managed_note_observations (
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
)
    """.strip(),
    """
CREATE TABLE IF NOT EXISTS managed_links (
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
)
    """.strip(),
    """
CREATE TABLE IF NOT EXISTS managed_conflicts (
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
)
    """.strip(),
    """
CREATE UNIQUE INDEX IF NOT EXISTS managed_open_conflict_idx
ON managed_conflicts(note_id) WHERE status = 'open'
    """.strip(),
    """
CREATE TABLE IF NOT EXISTS managed_consents (
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
)
    """.strip(),
    """
CREATE UNIQUE INDEX IF NOT EXISTS managed_active_consent_idx
ON managed_consents(workspace_id, provider, access_mode, operation, note_scope)
WHERE active = 1
    """.strip(),
    """
CREATE TABLE IF NOT EXISTS managed_exclusions (
    exclusion_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES managed_workspaces(workspace_id),
    kind TEXT NOT NULL CHECK (kind IN ('note', 'folder', 'portable_set')),
    subject TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    policy_generation INTEGER NOT NULL CHECK (policy_generation >= 0),
    recorded_at TEXT NOT NULL,
    UNIQUE (workspace_id, kind, subject)
)
    """.strip(),
    """
CREATE TABLE IF NOT EXISTS managed_inference_budgets (
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
)
    """.strip(),
    """
CREATE TABLE IF NOT EXISTS managed_inference_requests (
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
)
    """.strip(),
    """
CREATE TABLE IF NOT EXISTS managed_suggestions (
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
)
    """.strip(),
    """
CREATE TABLE IF NOT EXISTS managed_operations (
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
)
    """.strip(),
    """
CREATE INDEX IF NOT EXISTS managed_operations_recovery_idx
ON managed_operations(workspace_id, status, created_at)
WHERE status IN ('prepared', 'writing', 'promoted')
    """.strip(),
)

RUNTIME_COMPATIBILITY_SCHEMA = (
    """
CREATE TABLE IF NOT EXISTS runtime_compatibility (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    minimum_runtime_session_version INTEGER NOT NULL CHECK (
        minimum_runtime_session_version = 1
    ),
    state_schema_version INTEGER NOT NULL CHECK (state_schema_version = 4)
)
    """.strip(),
    """
INSERT OR IGNORE INTO runtime_compatibility (
    singleton, minimum_runtime_session_version, state_schema_version
) VALUES (1, 1, 4)
    """.strip(),
)

REVIEW_SCHEMA = (
    """
CREATE TABLE review_contexts (
    proposal_id TEXT PRIMARY KEY REFERENCES proposals(proposal_id),
    binding_json BLOB NOT NULL,
    proposal_json BLOB NOT NULL
)
    """.strip(),
    """
CREATE TABLE review_sources (
    proposal_id TEXT NOT NULL REFERENCES proposals(proposal_id),
    capture_id TEXT NOT NULL REFERENCES captures(capture_id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0 AND ordinal < 32),
    PRIMARY KEY (proposal_id, capture_id),
    UNIQUE (proposal_id, ordinal)
)
    """.strip(),
    """
CREATE INDEX review_sources_capture_idx ON review_sources (capture_id, proposal_id)
    """.strip(),
    """
CREATE TABLE review_page_heads (
    page_id TEXT PRIMARY KEY,
    publication_id TEXT NOT NULL UNIQUE,
    proposal_id TEXT NOT NULL REFERENCES proposals(proposal_id),
    capture_id TEXT NOT NULL REFERENCES captures(capture_id),
    canonical_path TEXT NOT NULL UNIQUE,
    published_sha256 TEXT NOT NULL CHECK (length(published_sha256) = 64)
)
    """.strip(),
    "DROP TABLE runtime_compatibility",
    """
CREATE TABLE runtime_compatibility (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    minimum_runtime_session_version INTEGER NOT NULL CHECK (
        minimum_runtime_session_version = 1
    ),
    state_schema_version INTEGER NOT NULL CHECK (state_schema_version = 5)
)
    """.strip(),
    """
INSERT INTO runtime_compatibility (
    singleton, minimum_runtime_session_version, state_schema_version
) VALUES (1, 1, 5)
    """.strip(),
)

MANAGED_RECOVERY_SCHEMA = (
    """
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
)
    """.strip(),
    """
INSERT INTO managed_write_authority (
    operation_id, authority_version, descriptor_json, descriptor_sha256
)
SELECT operation_id, 0, NULL, NULL
FROM managed_operations
WHERE kind IN ('setup', 'materialize')
    """.strip(),
    """
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
)
    """.strip(),
    "DROP TABLE runtime_compatibility",
    """
CREATE TABLE runtime_compatibility (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    minimum_runtime_session_version INTEGER NOT NULL CHECK (
        minimum_runtime_session_version = 1
    ),
    state_schema_version INTEGER NOT NULL CHECK (state_schema_version = 6)
)
    """.strip(),
    """
INSERT INTO runtime_compatibility (
    singleton, minimum_runtime_session_version, state_schema_version
) VALUES (1, 1, 6)
    """.strip(),
)

PRIVACY_SCHEMA = (
    """
ALTER TABLE search_documents ADD COLUMN effective_tier TEXT NOT NULL
    CHECK (effective_tier IN ('public','work','personal','secret','unknown'))
    DEFAULT 'unknown'
    """.strip(),
    """
ALTER TABLE search_documents ADD COLUMN effective_cloud INTEGER NOT NULL
    CHECK (effective_cloud IN (0, 1)) DEFAULT 0
    """.strip(),
    """
ALTER TABLE search_documents ADD COLUMN effective_external_egress INTEGER NOT NULL
    CHECK (effective_external_egress IN (0, 1)) DEFAULT 0
    """.strip(),
    """
ALTER TABLE search_documents ADD COLUMN invalid_evidence_reason TEXT CHECK (
    invalid_evidence_reason IS NULL
    OR invalid_evidence_reason IN ('missing','malformed','inconsistent')
)
    """.strip(),
    """
ALTER TABLE search_documents ADD COLUMN invalid_evidence_sha256 TEXT CHECK (
    invalid_evidence_sha256 IS NULL OR length(invalid_evidence_sha256) = 64
)
    """.strip(),
    """
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
)
    """.strip(),
    """
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
)
    """.strip(),
    """
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
)
    """.strip(),
    """
CREATE TRIGGER privacy_invalid_evidence_update_immutable
BEFORE UPDATE ON privacy_invalid_evidence
BEGIN
    SELECT RAISE(ABORT, 'privacy invalid evidence is append-only');
END
    """.strip(),
    """
CREATE TRIGGER privacy_invalid_evidence_delete_immutable
BEFORE DELETE ON privacy_invalid_evidence
BEGIN
    SELECT RAISE(ABORT, 'privacy invalid evidence is append-only');
END
    """.strip(),
    """
CREATE TABLE privacy_repair_ledger (
    repair_id TEXT PRIMARY KEY,
    target_kind TEXT NOT NULL CHECK (
        target_kind IN ('source_revision','canonical_revision','search_document')
    ),
    target_id TEXT NOT NULL,
    invalid_evidence_sha256 TEXT NOT NULL CHECK (length(invalid_evidence_sha256) = 64),
    owner_actor_id TEXT NOT NULL,
    replacement_privacy_json TEXT NOT NULL CHECK (
        typeof(replacement_privacy_json) = 'text'
        AND CASE
            WHEN json_valid(replacement_privacy_json)
            THEN json_type(replacement_privacy_json) = 'object'
            ELSE 0
        END
    ),
    operation_id TEXT NOT NULL UNIQUE,
    recorded_at TEXT NOT NULL,
    supersedes_repair_id TEXT REFERENCES privacy_repair_ledger(repair_id),
    UNIQUE (target_kind, target_id, invalid_evidence_sha256, operation_id)
)
    """.strip(),
    """
CREATE TRIGGER privacy_repair_ledger_update_immutable BEFORE UPDATE ON privacy_repair_ledger
BEGIN
    SELECT RAISE(ABORT, 'privacy repair ledger is append-only');
END
    """.strip(),
    """
CREATE TRIGGER privacy_repair_ledger_delete_immutable BEFORE DELETE ON privacy_repair_ledger
BEGIN
    SELECT RAISE(ABORT, 'privacy repair ledger is append-only');
END
    """.strip(),
    """
CREATE TRIGGER search_documents_privacy_insert_shape BEFORE INSERT ON search_documents
BEGIN
    SELECT CASE
        WHEN (NEW.invalid_evidence_reason IS NULL) != (NEW.invalid_evidence_sha256 IS NULL)
        THEN RAISE(ABORT, 'invalid search privacy evidence pairing')
        WHEN NEW.effective_tier IN ('secret','unknown')
        AND (NEW.effective_cloud != 0 OR NEW.effective_external_egress != 0)
        THEN RAISE(ABORT, 'invalid search privacy authority')
        WHEN NEW.invalid_evidence_reason IS NOT NULL
        AND (NEW.effective_tier != 'unknown' OR NEW.effective_cloud != 0
        OR NEW.effective_external_egress != 0)
        THEN RAISE(ABORT, 'invalid search privacy failure closure')
    END;
END
    """.strip(),
    """
CREATE TRIGGER search_documents_privacy_update_shape BEFORE UPDATE ON search_documents
BEGIN
    SELECT CASE
        WHEN (NEW.invalid_evidence_reason IS NULL) != (NEW.invalid_evidence_sha256 IS NULL)
        THEN RAISE(ABORT, 'invalid search privacy evidence pairing')
        WHEN NEW.effective_tier IN ('secret','unknown')
        AND (NEW.effective_cloud != 0 OR NEW.effective_external_egress != 0)
        THEN RAISE(ABORT, 'invalid search privacy authority')
        WHEN NEW.invalid_evidence_reason IS NOT NULL
        AND (NEW.effective_tier != 'unknown' OR NEW.effective_cloud != 0
        OR NEW.effective_external_egress != 0)
        THEN RAISE(ABORT, 'invalid search privacy failure closure')
    END;
END
    """.strip(),
    "DROP TABLE runtime_compatibility",
    """
CREATE TABLE runtime_compatibility (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    minimum_runtime_session_version INTEGER NOT NULL CHECK (
        minimum_runtime_session_version = 3
    ),
    state_schema_version INTEGER NOT NULL CHECK (state_schema_version = 8)
)
    """.strip(),
    """
INSERT INTO runtime_compatibility (
    singleton, minimum_runtime_session_version, state_schema_version
) VALUES (1, 3, 8)
    """.strip(),
)

IDENTITY_AND_REPAIR_SCHEMA: tuple[str, ...] = (
    """
CREATE TABLE brain_identity (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    tenant_id TEXT NOT NULL UNIQUE CHECK (length(tenant_id) > 0),
    brain_id TEXT NOT NULL UNIQUE CHECK (length(brain_id) > 0),
    issuer_epoch INTEGER NOT NULL CHECK (issuer_epoch > 0),
    legacy_issuer_epoch INTEGER CHECK (legacy_issuer_epoch IS NULL OR legacy_issuer_epoch > 0),
    recorded_at TEXT NOT NULL CHECK (length(recorded_at) > 0),
    CHECK (legacy_issuer_epoch IS NULL OR legacy_issuer_epoch < issuer_epoch)
)
    """.strip(),
)

INGESTION_JOURNAL_SCHEMA = (
    """
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
)
    """.strip(),
    """
CREATE TABLE capture_ingestion_payloads (
    delivery_id TEXT PRIMARY KEY REFERENCES capture_ingestion_items(delivery_id) ON DELETE CASCADE,
    envelope_bytes BLOB NOT NULL CHECK (
        typeof(envelope_bytes) = 'blob' AND length(envelope_bytes) > 0
    )
)
    """.strip(),
    """
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
)
    """.strip(),
    """
CREATE TABLE capture_ingestion_tombstones (
    delivery_id TEXT PRIMARY KEY CHECK (length(delivery_id) > 0),
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    result_json TEXT NOT NULL CHECK (
        typeof(result_json) = 'text' AND json_valid(result_json)
        AND json_type(result_json) = 'object'
    ),
    decided_at TEXT NOT NULL CHECK (length(decided_at) > 0)
)
    """.strip(),
    """
CREATE INDEX capture_ingestion_items_pending_order_idx
ON capture_ingestion_items(journal_sequence)
    """.strip(),
    """
CREATE INDEX capture_ingestion_events_delivery_idx
ON capture_ingestion_events(delivery_id, event_sequence DESC)
    """.strip(),
    """
CREATE TRIGGER capture_ingestion_items_update_immutable
BEFORE UPDATE ON capture_ingestion_items
BEGIN
    SELECT RAISE(ABORT, 'ingestion item is immutable');
END
    """.strip(),
    """
CREATE TRIGGER capture_ingestion_payloads_update_immutable
BEFORE UPDATE ON capture_ingestion_payloads
BEGIN
    SELECT RAISE(ABORT, 'ingestion payload is immutable');
END
    """.strip(),
    """
CREATE TRIGGER capture_ingestion_events_update_immutable
BEFORE UPDATE ON capture_ingestion_events
BEGIN
    SELECT RAISE(ABORT, 'ingestion event is append-only');
END
    """.strip(),
    """
CREATE TRIGGER capture_ingestion_tombstones_update_immutable
BEFORE UPDATE ON capture_ingestion_tombstones
BEGIN
    SELECT RAISE(ABORT, 'ingestion tombstone is immutable');
END
    """.strip(),
    """
CREATE TRIGGER capture_ingestion_tombstones_delete_immutable
BEFORE DELETE ON capture_ingestion_tombstones
BEGIN
    SELECT RAISE(ABORT, 'ingestion tombstone is immutable');
END
    """.strip(),
    """
CREATE TRIGGER capture_ingestion_payloads_delete_guarded
BEFORE DELETE ON capture_ingestion_payloads
WHEN NOT EXISTS (
    SELECT 1 FROM capture_ingestion_events
    WHERE delivery_id = OLD.delivery_id
    AND event_kind IN ('accepted', 'duplicate', 'discarded')
)
BEGIN
    SELECT RAISE(ABORT, 'ingestion payload requires a terminal event');
END
    """.strip(),
    """
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
END
    """.strip(),
    """
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
) NOT IN ('accepted', 'duplicate', 'discarded')
    """.strip(),
    "DROP TABLE runtime_compatibility",
    """
CREATE TABLE runtime_compatibility (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    minimum_runtime_session_version INTEGER NOT NULL CHECK (
        minimum_runtime_session_version = 5
    ),
    state_schema_version INTEGER NOT NULL CHECK (state_schema_version = 10)
)
    """.strip(),
    """
INSERT INTO runtime_compatibility (
    singleton, minimum_runtime_session_version, state_schema_version
) VALUES (1, 5, 10)
    """.strip(),
)

IDENTITY_AND_REPAIR_SCHEMA += (
    """
CREATE TRIGGER brain_identity_update_immutable BEFORE UPDATE ON brain_identity
BEGIN
    SELECT RAISE(ABORT, 'brain identity is durable');
END
    """.strip(),
    """
CREATE TRIGGER brain_identity_delete_immutable BEFORE DELETE ON brain_identity
BEGIN
    SELECT RAISE(ABORT, 'brain identity is durable');
END
    """.strip(),
    """
CREATE TABLE legacy_issuer_bindings (
    artifact_path TEXT NOT NULL CHECK (length(artifact_path) > 0),
    jsonl_ordinal INTEGER CHECK (jsonl_ordinal IS NULL OR jsonl_ordinal >= 0),
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    issuer_epoch INTEGER NOT NULL CHECK (issuer_epoch > 0)
)
    """.strip(),
    """
CREATE UNIQUE INDEX legacy_issuer_binding_identity_idx
ON legacy_issuer_bindings(artifact_path, coalesce(jsonl_ordinal, -1))
    """.strip(),
    """
CREATE TRIGGER legacy_issuer_bindings_update_immutable
BEFORE UPDATE ON legacy_issuer_bindings
BEGIN
    SELECT RAISE(ABORT, 'legacy issuer bindings are append-only');
END
    """.strip(),
    """
CREATE TRIGGER legacy_issuer_bindings_delete_immutable
BEFORE DELETE ON legacy_issuer_bindings
BEGIN
    SELECT RAISE(ABORT, 'legacy issuer bindings are append-only');
END
    """.strip(),
    """
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
)
    """.strip(),
    """
CREATE TRIGGER issuer_migration_marker_update_immutable
BEFORE UPDATE ON issuer_migration_marker
BEGIN
    SELECT RAISE(ABORT, 'issuer migration marker is append-only');
END
    """.strip(),
    """
CREATE TRIGGER issuer_migration_marker_delete_immutable
BEFORE DELETE ON issuer_migration_marker
BEGIN
    SELECT RAISE(ABORT, 'issuer migration marker is append-only');
END
    """.strip(),
    "DROP TABLE privacy_repair_ledger",
    """
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
)
    """.strip(),
    """
CREATE UNIQUE INDEX privacy_repair_supersession_idx
ON privacy_repair_ledger(supersedes_repair_id) WHERE supersedes_repair_id IS NOT NULL
    """.strip(),
    """
CREATE TRIGGER privacy_repair_ledger_update_immutable BEFORE UPDATE ON privacy_repair_ledger
BEGIN
    SELECT RAISE(ABORT, 'privacy repair ledger is append-only');
END
    """.strip(),
    """
CREATE TRIGGER privacy_repair_ledger_delete_immutable BEFORE DELETE ON privacy_repair_ledger
BEGIN
    SELECT RAISE(ABORT, 'privacy repair ledger is append-only');
END
    """.strip(),
    """
ALTER TABLE search_documents ADD COLUMN applied_repair_id TEXT
    """.strip(),
    """
ALTER TABLE search_documents ADD COLUMN applied_repair_sequence INTEGER CHECK (
    applied_repair_sequence IS NULL OR applied_repair_sequence > 0
)
    """.strip(),
    "DROP TRIGGER search_documents_privacy_insert_shape",
    "DROP TRIGGER search_documents_privacy_update_shape",
    """
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
END
    """.strip(),
    """
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
END
    """.strip(),
    "DROP TABLE runtime_compatibility",
    """
CREATE TABLE runtime_compatibility (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    minimum_runtime_session_version INTEGER NOT NULL CHECK (
        minimum_runtime_session_version = 4
    ),
    state_schema_version INTEGER NOT NULL CHECK (state_schema_version = 9)
)
    """.strip(),
    """
INSERT INTO runtime_compatibility (
    singleton, minimum_runtime_session_version, state_schema_version
) VALUES (1, 4, 9)
    """.strip(),
)

LOCAL_MIGRATIONS = (
    _migration(1, "local_baseline", BASELINE),
    _migration(2, "local_search_and_import", _MIGRATION_2),
    _migration(3, "managed_workspace", MANAGED_WORKSPACE_SCHEMA),
    _migration(4, "runtime_compatibility", RUNTIME_COMPATIBILITY_SCHEMA),
    _migration(5, "review_publication", REVIEW_SCHEMA),
    _migration(6, "managed_recovery_authority", MANAGED_RECOVERY_SCHEMA),
    _migration(7, "immutable_source_history", SOURCE_HISTORY_SCHEMA),
    _migration(8, "effective_privacy_projection", PRIVACY_SCHEMA),
    _migration(9, "issuer_identity_and_owner_repair", IDENTITY_AND_REPAIR_SCHEMA),
    _migration(10, "durable_capture_ingestion_journal", INGESTION_JOURNAL_SCHEMA),
)

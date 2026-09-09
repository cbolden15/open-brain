-- Synthetic schema builder frozen from 5a51797 local_store.py.

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
);
CREATE TABLE IF NOT EXISTS spaces (
    space_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS space_operations (
    delivery_id TEXT PRIMARY KEY,
    request_sha256 TEXT NOT NULL,
    operation TEXT NOT NULL,
    space_id TEXT NOT NULL,
    name TEXT NOT NULL,
    receipt_id TEXT NOT NULL UNIQUE,
    recorded_at TEXT NOT NULL,
    stage INTEGER NOT NULL DEFAULT 0
);
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
);
CREATE TABLE IF NOT EXISTS proposal_sets (
    delivery_id TEXT PRIMARY KEY,
    request_sha256 TEXT NOT NULL,
    capture_id TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    stage INTEGER NOT NULL DEFAULT 0
);
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
);
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
);
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
);
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
);
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
);
CREATE INDEX IF NOT EXISTS markdown_import_files_finalize_idx
ON markdown_import_files(root_id, last_observed_scan_id)
WHERE active_revision_id IS NOT NULL;
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
);
CREATE INDEX IF NOT EXISTS search_capture_idx ON search_documents (capture_id);

CREATE UNIQUE INDEX IF NOT EXISTS route_identity_idx ON route_operations (route_id);
CREATE TABLE search_fts_identity (
        fts_rowid INTEGER PRIMARY KEY,
        result_id TEXT NOT NULL UNIQUE
    );
CREATE VIRTUAL TABLE search_documents_fts USING fts5(
        result_id UNINDEXED,
        title,
        body,
        tokenize='unicode61 remove_diacritics 2'
    );
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
PRAGMA user_version = 1;

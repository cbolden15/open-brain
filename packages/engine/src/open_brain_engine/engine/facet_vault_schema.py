"""Additive local vault custody DDL for the coordinator's schema-11 registration.

These tables are operational state. They must not enter Portable facet evidence.
"""

FACET_VAULT_SCHEMA = (
    """
CREATE TABLE IF NOT EXISTS facet_projection_state (
    workspace_id TEXT NOT NULL REFERENCES managed_workspaces(workspace_id),
    file_kind TEXT NOT NULL CHECK (file_kind IN ('record', 'index')),
    file_id TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    subject_kind TEXT CHECK (subject_kind IN ('source', 'canonical')),
    subject_id TEXT,
    content_revision_id TEXT,
    emitted_facets_json TEXT,
    id_name_mapping_json TEXT,
    materialized_sha256 TEXT NOT NULL CHECK (length(materialized_sha256) = 64),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, file_kind, file_id),
    UNIQUE (workspace_id, relative_path),
    CHECK (
        (file_kind = 'record' AND subject_kind IS NOT NULL AND subject_id IS NOT NULL
         AND content_revision_id IS NOT NULL AND emitted_facets_json IS NOT NULL
         AND id_name_mapping_json IS NOT NULL)
        OR
        (file_kind = 'index' AND subject_kind IS NULL AND subject_id IS NULL
         AND content_revision_id IS NULL AND emitted_facets_json IS NULL
         AND id_name_mapping_json IS NULL)
    )
)
    """.strip(),
    """
CREATE TABLE IF NOT EXISTS facet_vault_previews (
    preview_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES managed_workspaces(workspace_id),
    file_id TEXT NOT NULL REFERENCES managed_notes(note_id),
    relative_path TEXT NOT NULL,
    root_device TEXT NOT NULL,
    root_inode TEXT NOT NULL,
    subject_kind TEXT NOT NULL CHECK (subject_kind IN ('source', 'canonical')),
    subject_id TEXT NOT NULL,
    content_revision_id TEXT NOT NULL,
    file_sha256 TEXT NOT NULL CHECK (length(file_sha256) = 64),
    previous_facets_json TEXT NOT NULL,
    previous_mapping_json TEXT NOT NULL,
    expected_version INTEGER NOT NULL CHECK (expected_version >= 0),
    add_value_ids_json TEXT NOT NULL,
    remove_value_ids_json TEXT NOT NULL,
    catalogue_versions_json TEXT NOT NULL,
    authority_sha256 TEXT NOT NULL CHECK (length(authority_sha256) = 64),
    conflicts_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
)
    """.strip(),
    """
CREATE TABLE IF NOT EXISTS facet_vault_apply_operations (
    operation_id TEXT PRIMARY KEY,
    preview_id TEXT NOT NULL REFERENCES facet_vault_previews(preview_id),
    principal_id TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    semantic_operation_id TEXT NOT NULL UNIQUE,
    receipt_json TEXT NOT NULL,
    committed_at TEXT NOT NULL
)
    """.strip(),
    """
CREATE INDEX IF NOT EXISTS facet_vault_previews_expiry
ON facet_vault_previews(expires_at)
    """.strip(),
)

"""Schema-7 additions, including the reserved M2 history and relationship storage."""

SOURCE_HISTORY_SCHEMA = (
    """
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
)
    """.strip(),
    """
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
)
    """.strip(),
    """
CREATE TABLE source_namespaces (
 namespace_sha256 TEXT PRIMARY KEY CHECK(length(namespace_sha256)=64),
 namespace_json TEXT NOT NULL UNIQUE,
 source_id TEXT NOT NULL UNIQUE REFERENCES logical_sources(source_id)
)
    """.strip(),
    """
CREATE TABLE source_aliases (
 delivery_id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES logical_sources(source_id),
 evidence_sha256 TEXT NOT NULL CHECK(length(evidence_sha256)=64)
)
    """.strip(),
    """
CREATE TABLE source_operations (
 operation_id TEXT PRIMARY KEY, request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
 source_id TEXT NOT NULL REFERENCES logical_sources(source_id), receipt_json TEXT NOT NULL
)
    """.strip(),
    """
CREATE TABLE source_quarantine (
 custody_id TEXT PRIMARY KEY, source_id TEXT REFERENCES logical_sources(source_id),
 revision_key TEXT NOT NULL, request_sha256 TEXT NOT NULL, submission_json BLOB NOT NULL,
 recorded_at TEXT NOT NULL
)
    """.strip(),
    """
CREATE TABLE source_intakes (
 namespace_sha256 TEXT NOT NULL, revision_key TEXT NOT NULL,
 source_id TEXT NOT NULL, request_sha256 TEXT NOT NULL,
 delivery_id TEXT NOT NULL UNIQUE, plan_json TEXT NOT NULL,
 submission_json BLOB NOT NULL, receipt_json TEXT,
 PRIMARY KEY(namespace_sha256,revision_key)
)
    """.strip(),
    """
CREATE TABLE engine_generations (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), incarnation TEXT NOT NULL,
 retrieval_generation INTEGER NOT NULL CHECK(retrieval_generation>=0),
 authorization_epoch INTEGER NOT NULL CHECK(authorization_epoch>=0),
 projection_policy_version INTEGER NOT NULL CHECK(projection_policy_version>=1),
 control_epoch INTEGER NOT NULL CHECK(control_epoch>=0),
 fencing_epoch INTEGER NOT NULL CHECK(fencing_epoch>=0),
 vector_generation INTEGER NOT NULL CHECK(vector_generation>=0)
)
    """.strip(),
    """
CREATE TABLE revision_relationships (
 relationship_id TEXT PRIMARY KEY, left_record_id TEXT NOT NULL, left_revision_id TEXT NOT NULL,
 right_record_id TEXT NOT NULL, right_revision_id TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('duplicate_of','supersedes','contradicts')),
 decision TEXT NOT NULL CHECK(decision IN ('accept','reject','remove')),
 version INTEGER NOT NULL CHECK(version>=1),
 UNIQUE(left_record_id,left_revision_id,right_record_id,right_revision_id,kind)
)
    """.strip(),
    """
CREATE TABLE relationship_decisions (
 decision_id TEXT PRIMARY KEY,
 relationship_id TEXT NOT NULL REFERENCES revision_relationships(relationship_id),
 sequence INTEGER NOT NULL UNIQUE, operation_id TEXT NOT NULL UNIQUE,
 request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
 decision TEXT NOT NULL CHECK(decision IN ('accept','reject','remove')),
 relationship_version INTEGER NOT NULL CHECK(relationship_version>=1),
 recorded_at TEXT NOT NULL, actor_id TEXT NOT NULL, receipt_json TEXT NOT NULL
)
    """.strip(),
    """
CREATE TABLE canonical_revision_members (
 revision_id TEXT NOT NULL, page_id TEXT NOT NULL, publication_id TEXT NOT NULL,
 ordinal INTEGER NOT NULL CHECK(ordinal>=0),
 capture_id TEXT NOT NULL REFERENCES source_revisions(capture_id),
 PRIMARY KEY(revision_id,ordinal), UNIQUE(revision_id,capture_id)
)
    """.strip(),
    """
DROP TABLE runtime_compatibility
    """.strip(),
    """
CREATE TABLE runtime_compatibility (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 minimum_runtime_session_version INTEGER NOT NULL CHECK(minimum_runtime_session_version=2),
 state_schema_version INTEGER NOT NULL CHECK(state_schema_version=7)
)
    """.strip(),
    """
INSERT INTO runtime_compatibility VALUES(1,2,7)
    """.strip(),
    """
CREATE TRIGGER generation_search_documents_insert AFTER INSERT ON search_documents
 BEGIN UPDATE engine_generations
 SET retrieval_generation=retrieval_generation+1 WHERE singleton=1; END
    """.strip(),
    """
CREATE TRIGGER generation_search_documents_update AFTER UPDATE ON search_documents
 BEGIN UPDATE engine_generations
 SET retrieval_generation=retrieval_generation+1 WHERE singleton=1; END
    """.strip(),
    """
CREATE TRIGGER generation_search_documents_delete AFTER DELETE ON search_documents
 BEGIN UPDATE engine_generations
 SET retrieval_generation=retrieval_generation+1 WHERE singleton=1; END
    """.strip(),
    """
CREATE TRIGGER generation_logical_sources_insert AFTER INSERT ON logical_sources
 BEGIN UPDATE engine_generations
 SET retrieval_generation=retrieval_generation+1 WHERE singleton=1; END
    """.strip(),
    """
CREATE TRIGGER generation_logical_sources_update AFTER UPDATE ON logical_sources
 BEGIN UPDATE engine_generations
 SET retrieval_generation=retrieval_generation+1 WHERE singleton=1; END
    """.strip(),
    """
CREATE TRIGGER generation_logical_sources_delete AFTER DELETE ON logical_sources
 BEGIN UPDATE engine_generations
 SET retrieval_generation=retrieval_generation+1 WHERE singleton=1; END
    """.strip(),
    """
CREATE TRIGGER generation_source_revisions_insert AFTER INSERT ON source_revisions
 BEGIN UPDATE engine_generations
 SET retrieval_generation=retrieval_generation+1 WHERE singleton=1; END
    """.strip(),
    """
CREATE TRIGGER generation_source_revisions_update AFTER UPDATE ON source_revisions
 BEGIN UPDATE engine_generations
 SET retrieval_generation=retrieval_generation+1 WHERE singleton=1; END
    """.strip(),
    """
CREATE TRIGGER generation_source_revisions_delete AFTER DELETE ON source_revisions
 BEGIN UPDATE engine_generations
 SET retrieval_generation=retrieval_generation+1 WHERE singleton=1; END
    """.strip(),
    """
CREATE TRIGGER generation_review_page_heads_insert AFTER INSERT ON review_page_heads
 BEGIN UPDATE engine_generations
 SET retrieval_generation=retrieval_generation+1 WHERE singleton=1; END
    """.strip(),
    """
CREATE TRIGGER generation_review_page_heads_update AFTER UPDATE ON review_page_heads
 BEGIN UPDATE engine_generations
 SET retrieval_generation=retrieval_generation+1 WHERE singleton=1; END
    """.strip(),
    """
CREATE TRIGGER generation_review_page_heads_delete AFTER DELETE ON review_page_heads
 BEGIN UPDATE engine_generations
 SET retrieval_generation=retrieval_generation+1 WHERE singleton=1; END
    """.strip(),
    """
CREATE TRIGGER generation_canonical_revision_members_insert
 AFTER INSERT ON canonical_revision_members
 BEGIN UPDATE engine_generations
 SET retrieval_generation=retrieval_generation+1 WHERE singleton=1; END
    """.strip(),
    """
CREATE TRIGGER generation_canonical_revision_members_update
 AFTER UPDATE ON canonical_revision_members
 BEGIN UPDATE engine_generations
 SET retrieval_generation=retrieval_generation+1 WHERE singleton=1; END
    """.strip(),
    """
CREATE TRIGGER generation_canonical_revision_members_delete
 AFTER DELETE ON canonical_revision_members
 BEGIN UPDATE engine_generations
 SET retrieval_generation=retrieval_generation+1 WHERE singleton=1; END
    """.strip(),
)

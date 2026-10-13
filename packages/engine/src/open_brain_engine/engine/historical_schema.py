"""Append-only schema-13 reconciliation facts; no captures or fake approvals."""

HISTORICAL_AUTHORITY_SCHEMA = (
    (
        """
CREATE TABLE historical_registry_state (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 brain_id TEXT NOT NULL, issuer_epoch INTEGER NOT NULL CHECK(issuer_epoch>=1),
 generation INTEGER NOT NULL CHECK(generation>=0),
 registry_sha256 TEXT NOT NULL CHECK(length(registry_sha256)=64)
)
    """.strip(),
        """
CREATE TABLE historical_operations (
 operation_id TEXT PRIMARY KEY,
 kind TEXT NOT NULL CHECK(kind IN ('baseline','claim','relation','revocation')),
 request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
 request_json BLOB NOT NULL CHECK(length(request_json)<=65536 AND json_valid(request_json)),
 receipt_json BLOB NOT NULL CHECK(length(receipt_json)<=4096 AND json_valid(receipt_json)),
 transition_sha256 TEXT NOT NULL CHECK(length(transition_sha256)=64),
 registry_generation INTEGER NOT NULL UNIQUE CHECK(registry_generation>=1)
)
    """.strip(),
        """
CREATE TABLE historical_claims (
 capture_id TEXT PRIMARY KEY REFERENCES source_revisions(capture_id),
 source_id TEXT NOT NULL REFERENCES logical_sources(source_id),
 capture_source_id TEXT NOT NULL REFERENCES logical_sources(source_id),
 claim_role TEXT NOT NULL CHECK(claim_role IN ('baseline_original','historical_copy')),
 registered_operation_id TEXT NOT NULL REFERENCES historical_operations(operation_id),
 CHECK(claim_role!='baseline_original' OR source_id=capture_source_id)
)
    """.strip(),
        """
CREATE TABLE historical_baselines (
 operation_id TEXT PRIMARY KEY REFERENCES historical_operations(operation_id),
 source_id TEXT NOT NULL UNIQUE REFERENCES logical_sources(source_id),
 original_capture_id TEXT NOT NULL REFERENCES historical_claims(capture_id),
 namespace_sha256 TEXT NOT NULL UNIQUE CHECK(length(namespace_sha256)=64),
 revision_key TEXT NOT NULL,
 envelope_sha256 TEXT NOT NULL CHECK(length(envelope_sha256)=64),
 envelope_bytes BLOB NOT NULL CHECK(length(envelope_bytes)<=65536 AND json_valid(envelope_bytes))
)
    """.strip(),
        """
CREATE TABLE historical_relations (
 operation_id TEXT PRIMARY KEY REFERENCES historical_operations(operation_id),
 baseline_operation_id TEXT NOT NULL REFERENCES historical_baselines(operation_id),
 copy_claim_operation_id TEXT NOT NULL REFERENCES historical_operations(operation_id),
 original_capture_id TEXT NOT NULL REFERENCES historical_claims(capture_id),
 copy_capture_id TEXT NOT NULL UNIQUE REFERENCES historical_claims(capture_id),
 original_source_id TEXT NOT NULL REFERENCES logical_sources(source_id),
 copy_source_id TEXT NOT NULL REFERENCES logical_sources(source_id),
 approval_evidence_sha256 TEXT NOT NULL CHECK(length(approval_evidence_sha256)=64),
 provider_ids_json TEXT NOT NULL CHECK(json_valid(provider_ids_json)),
 relation_version INTEGER NOT NULL CHECK(relation_version=1),
 CHECK(original_capture_id!=copy_capture_id)
)
    """.strip(),
        """
CREATE TABLE historical_revocations (
 operation_id TEXT PRIMARY KEY REFERENCES historical_operations(operation_id),
 relation_operation_id TEXT NOT NULL UNIQUE REFERENCES historical_relations(operation_id),
 relation_version INTEGER NOT NULL CHECK(relation_version=2)
)
    """.strip(),
        """
CREATE TRIGGER historical_registry_state_update_guard BEFORE UPDATE ON historical_registry_state
WHEN NEW.singleton!=OLD.singleton OR NEW.brain_id!=OLD.brain_id
 OR NEW.issuer_epoch!=OLD.issuer_epoch OR NEW.generation!=OLD.generation+1
BEGIN SELECT RAISE(ABORT,'historical registry identity/generation is immutable'); END
    """.strip(),
        """
CREATE TRIGGER historical_registry_state_delete_guard BEFORE DELETE ON historical_registry_state
BEGIN SELECT RAISE(ABORT,'historical registry state cannot be deleted'); END
    """.strip(),
    )
    + tuple(
        f"CREATE TRIGGER {table}_{action.lower()}_immutable BEFORE {action} ON {table} "
        "BEGIN SELECT RAISE(ABORT,'historical authority evidence is immutable'); END"
        for table in (
            "historical_operations",
            "historical_claims",
            "historical_baselines",
            "historical_relations",
            "historical_revocations",
        )
        for action in ("UPDATE", "DELETE")
    )
    + (
        "UPDATE engine_generations SET projection_policy_version=projection_policy_version+1",
        "DROP TABLE runtime_compatibility",
        """
CREATE TABLE runtime_compatibility (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 minimum_runtime_session_version INTEGER NOT NULL CHECK(minimum_runtime_session_version=8),
 state_schema_version INTEGER NOT NULL CHECK(state_schema_version=13)
)
    """.strip(),
        "INSERT INTO runtime_compatibility VALUES(1,8,13)",
    )
)

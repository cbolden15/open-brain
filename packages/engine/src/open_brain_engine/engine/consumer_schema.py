"""Concrete schema20 consumer evidence and projection design.

The catalog cutover is coordinated with journal2/runtime15/Portable15. Merely
importing this module does not register or apply a migration to any Brain.
"""

from open_brain_engine.storage.migrations import _migration

from .capture_lifecycle_journal import JOURNAL_SCHEMA

CONSUMER_STORAGE_SCHEMA: tuple[str, ...] = (
    """CREATE TABLE consumer_intent_objects (
 sha256 TEXT PRIMARY KEY CHECK(length(sha256)=64),
 kind TEXT NOT NULL CHECK(kind IN
 ('operation','conversation','normalized','segment','profile','identity')),
 object_bytes BLOB NOT NULL CHECK(length(object_bytes)>0 AND length(object_bytes)<=33554432)
)""",
    """CREATE TABLE consumer_previews (
 preview_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE,
 principal_id TEXT NOT NULL, brain_id TEXT NOT NULL, issuer_epoch INTEGER NOT NULL,
 created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
 input_sha256 TEXT NOT NULL CHECK(length(input_sha256)=64),
 root_sha256 TEXT NOT NULL CHECK(length(root_sha256)=64),
 root_bytes BLOB NOT NULL CHECK(length(root_bytes)>0 AND length(root_bytes)<=16777216)
)""",
    """CREATE TABLE consumer_preview_objects (
 preview_id TEXT NOT NULL REFERENCES consumer_previews(preview_id) ON DELETE CASCADE,
 sha256 TEXT NOT NULL CHECK(length(sha256)=64), kind TEXT NOT NULL,
 object_bytes BLOB NOT NULL CHECK(length(object_bytes)>0 AND length(object_bytes)<=33554432),
 PRIMARY KEY(preview_id,sha256)
)""",
    """CREATE TABLE consumer_approvals (
 operation_id TEXT PRIMARY KEY REFERENCES lifecycle_journal_requests(operation_id),
 root_sha256 TEXT NOT NULL REFERENCES consumer_intent_objects(sha256),
 request_bytes BLOB NOT NULL, request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64)
)""",
    """CREATE TABLE consumer_scopes (
 scope_id TEXT PRIMARY KEY, provider TEXT NOT NULL CHECK(provider IN ('chatgpt','claude')),
 label TEXT NOT NULL, namespace_id TEXT NOT NULL UNIQUE,
 created_operation_id TEXT NOT NULL REFERENCES lifecycle_journal_requests(operation_id),
 UNIQUE(provider,label)
)""",
    """CREATE TABLE consumer_archive_bindings (
 provider TEXT NOT NULL CHECK(provider IN ('chatgpt','claude')),
 archive_sha256 TEXT NOT NULL CHECK(length(archive_sha256)=64),
 scope_id TEXT NOT NULL REFERENCES consumer_scopes(scope_id),
 operation_id TEXT NOT NULL REFERENCES lifecycle_journal_requests(operation_id),
 PRIMARY KEY(provider,archive_sha256)
)""",
    """CREATE TABLE consumer_segment_bindings (
 capture_id TEXT PRIMARY KEY,
 operation_id TEXT NOT NULL REFERENCES lifecycle_journal_requests(operation_id),
 group_key TEXT NOT NULL CHECK(length(group_key)=64),
 revision_sha256 TEXT NOT NULL CHECK(length(revision_sha256)=64),
 segment_index INTEGER NOT NULL CHECK(segment_index>=0 AND segment_index<128),
 source_id TEXT NOT NULL, namespace_bytes BLOB NOT NULL,
 expected_source_bytes BLOB NOT NULL,
 normalized_sha256 TEXT NOT NULL REFERENCES consumer_intent_objects(sha256),
 envelope_sha256 TEXT NOT NULL REFERENCES consumer_intent_objects(sha256),
 UNIQUE(operation_id,group_key,segment_index)
)""",
    """CREATE TABLE consumer_outcomes (
 operation_id TEXT NOT NULL REFERENCES lifecycle_journal_requests(operation_id),
 group_key TEXT NOT NULL CHECK(length(group_key)=64),
 ordinal INTEGER NOT NULL CHECK(ordinal>=0 AND ordinal<10000),
 outcome TEXT NOT NULL CHECK(outcome IN
 ('committed','unchanged','conflict','retracted','no_text','halted')),
 receipt_bytes BLOB NOT NULL,
 receipt_sha256 TEXT NOT NULL CHECK(length(receipt_sha256)=64),
 PRIMARY KEY(operation_id,group_key), UNIQUE(operation_id,ordinal)
)""",
    "CREATE INDEX consumer_segment_bindings_source ON consumer_segment_bindings(source_id)",
    "CREATE INDEX consumer_segment_bindings_group "
    "ON consumer_segment_bindings(group_key,revision_sha256)",
)

CONSUMER_STORAGE_SCHEMA += tuple(
    f"CREATE TRIGGER {table}_{verb.lower()}_immutable BEFORE {verb} ON {table} "
    "BEGIN SELECT RAISE(ABORT, 'consumer evidence is immutable'); END"
    for table in ("consumer_intent_objects", "consumer_approvals", "consumer_segment_bindings")
    for verb in ("UPDATE", "DELETE")
)

# Existing identity bytes explicitly change contract in this successor migration;
# retained requests/events/payloads and every predecessor DDL statement do not.
_IDENTITY_UPDATE_TRIGGER = next(
    statement for statement in JOURNAL_SCHEMA
    if statement.startswith("CREATE TRIGGER lifecycle_journal_identity_update ")
)
CONSUMER_INTAKE_SCHEMA = CONSUMER_STORAGE_SCHEMA + (
    "DROP TRIGGER lifecycle_journal_identity_update",
    "UPDATE lifecycle_journal_identity SET contract='ob.capture-lifecycle-journal.v2' "
    "WHERE singleton=1 AND contract='ob.capture-lifecycle-journal.v1'",
    _IDENTITY_UPDATE_TRIGGER,
    "UPDATE engine_generations SET projection_policy_version=projection_policy_version+1",
    "DROP TABLE runtime_compatibility",
    """CREATE TABLE runtime_compatibility (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 minimum_runtime_session_version INTEGER NOT NULL CHECK(minimum_runtime_session_version=15),
 state_schema_version INTEGER NOT NULL CHECK(state_schema_version=20)
)""",
    "INSERT INTO runtime_compatibility VALUES(1,15,20)",
)
CONSUMER_INTAKE_MIGRATION = _migration(20, "consumer-conversation-intake", CONSUMER_INTAKE_SCHEMA)

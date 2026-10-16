"""Portable15 composes the frozen Portable12 base with explicit successor contracts."""

from hashlib import sha256

from open_brain_engine.core.ids import portable_canonical_json_bytes

from .v12_catalog import PORTABLE_V12_SCHEMA_CATALOG_DIGEST

PORTABLE_V15_SCHEMA_CATALOG = {
    "base_catalog_sha256": PORTABLE_V12_SCHEMA_CATALOG_DIGEST,
    "schema_version": 15,
    "state_schema_version": 20,
    "minimum_runtime_session_version": 15,
    "migration20_sha256": "369ba280ba4cd5622213ea7ed00fbbb2b3a5bcc160faf1679d1b2183e7b78128",
    "capture_lifecycle_journal": 2,
    "capture_lifecycle_schema_sha256": (
        "90ca8c4fe7205bc679e2cf1422bf523a6bb5565da242ad8505ad3b48f581657c"
    ),
    "consumer_intake_evidence": 1,
    "consumer_operation_intent": 1,
    "owner_retraction_approval": 1,
}
PORTABLE_V15_SCHEMA_CATALOG_DIGEST = sha256(
    portable_canonical_json_bytes(PORTABLE_V15_SCHEMA_CATALOG)
).hexdigest()

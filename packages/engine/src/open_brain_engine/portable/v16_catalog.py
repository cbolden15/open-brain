"""Portable16 composes the exact Portable15 catalog with owner embedding policy evidence."""

from hashlib import sha256

from open_brain_engine.core.ids import portable_canonical_json_bytes

from .v15_catalog import PORTABLE_V15_SCHEMA_CATALOG_DIGEST

PORTABLE_V16_SCHEMA_CATALOG = {
    "base_catalog_sha256": PORTABLE_V15_SCHEMA_CATALOG_DIGEST,
    "schema_version": 16,
    "state_schema_version": 21,
    "minimum_runtime_session_version": 16,
    "migration21_sha256": "bdd6d3164b0cf5f4e4f1304f0c761765e72b0f673de0d2e2ef61a3a8b6537c74",
    "embedding_policy_evidence": 1,
}
PORTABLE_V16_SCHEMA_CATALOG_DIGEST = sha256(
    portable_canonical_json_bytes(PORTABLE_V16_SCHEMA_CATALOG)
).hexdigest()

"""Portable17 composes the exact Portable16 catalog with owner egress policy evidence."""

from hashlib import sha256

from open_brain_engine.core.ids import portable_canonical_json_bytes

from .v16_catalog import PORTABLE_V16_SCHEMA_CATALOG_DIGEST

PORTABLE_V17_SCHEMA_CATALOG = {
    "base_catalog_sha256": PORTABLE_V16_SCHEMA_CATALOG_DIGEST,
    "schema_version": 17,
    "state_schema_version": 22,
    "minimum_runtime_session_version": 16,
    "migration22_sha256": "8bbc9f7b69a89c9a1d93e136a25119538d6458e0a0e7f0c6047eab71bee92afe",
    "owner_egress_policy_evidence": 1,
}
PORTABLE_V17_SCHEMA_CATALOG_DIGEST = sha256(
    portable_canonical_json_bytes(PORTABLE_V17_SCHEMA_CATALOG)
).hexdigest()

"""Portable18 composes the exact Portable17 catalog with follow-up evidence."""

from hashlib import sha256

from open_brain_engine.core.ids import portable_canonical_json_bytes

from .v17_catalog import PORTABLE_V17_SCHEMA_CATALOG_DIGEST

PORTABLE_V18_SCHEMA_CATALOG = {
    "base_catalog_sha256": PORTABLE_V17_SCHEMA_CATALOG_DIGEST,
    "schema_version": 18,
    "state_schema_version": 23,
    "minimum_runtime_session_version": 16,
    "migration23_sha256": "d24a103131bd5e2addf3ce266faf0a52c99fddeeb9f70b5d89d0c47f41d6975a",
    "followups_evidence": 1,
}
PORTABLE_V18_SCHEMA_CATALOG_DIGEST = sha256(
    portable_canonical_json_bytes(PORTABLE_V18_SCHEMA_CATALOG)
).hexdigest()

"""Portable19 composes the exact Portable18 catalog with saved-item source identities."""

from hashlib import sha256

from open_brain_engine.core.ids import portable_canonical_json_bytes

from .v18_catalog import PORTABLE_V18_SCHEMA_CATALOG_DIGEST

PORTABLE_V19_SCHEMA_CATALOG = {
    "base_catalog_sha256": PORTABLE_V18_SCHEMA_CATALOG_DIGEST,
    "schema_version": 19,
    "state_schema_version": 24,
    "minimum_runtime_session_version": 16,
    "migration24_sha256": "ff519101d6539cb6db844ae9d370f648e0efb3c1bd28ba1a6b8a5712d95f4942",
    "saved_item_sources": 1,
}
PORTABLE_V19_SCHEMA_CATALOG_DIGEST = sha256(
    portable_canonical_json_bytes(PORTABLE_V19_SCHEMA_CATALOG)
).hexdigest()

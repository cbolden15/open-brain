"""Portable21 combines the Portable20 catalog with browser annotation evidence."""

from hashlib import sha256

from open_brain_engine.core.ids import portable_canonical_json_bytes

from .v20_catalog import PORTABLE_V20_SCHEMA_CATALOG_DIGEST

PORTABLE_V21_SCHEMA_CATALOG = {
    "base_catalog_sha256": PORTABLE_V20_SCHEMA_CATALOG_DIGEST,
    "schema_version": 21,
    "state_schema_version": 26,
    "minimum_runtime_session_version": 16,
    "migration26_sha256": "4141eaa2d9ab34ae74aeead88b946833bdc83aca767806af27692cf5973863dd",
    "browser_annotations": 1,
}
PORTABLE_V21_SCHEMA_CATALOG_DIGEST = sha256(
    portable_canonical_json_bytes(PORTABLE_V21_SCHEMA_CATALOG)
).hexdigest()

"""Portable22 combines the Portable21 catalog with browser revisit evidence."""

from hashlib import sha256

from open_brain_engine.core.ids import portable_canonical_json_bytes

from .v21_catalog import PORTABLE_V21_SCHEMA_CATALOG_DIGEST

PORTABLE_V22_SCHEMA_CATALOG = {
    "base_catalog_sha256": PORTABLE_V21_SCHEMA_CATALOG_DIGEST,
    "schema_version": 22,
    "state_schema_version": 27,
    "minimum_runtime_session_version": 16,
    "migration27_sha256": "525cb534d758513beb5d37c2be01623b88e619e0f55777dc1e923eee3ead05fe",
    "browser_revisits": 1,
}
PORTABLE_V22_SCHEMA_CATALOG_DIGEST = sha256(
    portable_canonical_json_bytes(PORTABLE_V22_SCHEMA_CATALOG)
).hexdigest()

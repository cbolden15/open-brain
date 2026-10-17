"""Portable14 binds owner approval contract1 to the frozen Portable13 catalog."""

from hashlib import sha256

from open_brain_engine.core.ids import portable_canonical_json_bytes

from .v13_catalog import PORTABLE_V13_SCHEMA_CATALOG_DIGEST

PORTABLE_V14_SCHEMA_CATALOG = {
    "base_catalog_sha256": PORTABLE_V13_SCHEMA_CATALOG_DIGEST,
    "schema_version": 14,
    "owner_retraction_approval": 1,
}
PORTABLE_V14_SCHEMA_CATALOG_DIGEST = sha256(
    portable_canonical_json_bytes(PORTABLE_V14_SCHEMA_CATALOG)
).hexdigest()

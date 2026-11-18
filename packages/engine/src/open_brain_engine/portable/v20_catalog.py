"""Portable20 combines the Portable19 catalog with saved-item admission evidence."""

from hashlib import sha256

from open_brain_engine.core.ids import portable_canonical_json_bytes

from .v19_catalog import PORTABLE_V19_SCHEMA_CATALOG_DIGEST

PORTABLE_V20_SCHEMA_CATALOG = {
    "base_catalog_sha256": PORTABLE_V19_SCHEMA_CATALOG_DIGEST,
    "schema_version": 20,
    "state_schema_version": 25,
    "minimum_runtime_session_version": 16,
    "migration25_sha256": "57a354951979650d19b35608200f7646b4f9178e82110c7ee44787ac7a2afffd",
    "saved_item_admission": 1,
}
PORTABLE_V20_SCHEMA_CATALOG_DIGEST = sha256(
    portable_canonical_json_bytes(PORTABLE_V20_SCHEMA_CATALOG)
).hexdigest()

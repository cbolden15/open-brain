"""Portable20 combines the Portable19 catalog with saved-item admission evidence."""

from hashlib import sha256

from open_brain_engine.core.ids import portable_canonical_json_bytes

from .v19_catalog import PORTABLE_V19_SCHEMA_CATALOG_DIGEST

PORTABLE_V20_SCHEMA_CATALOG = {
    "base_catalog_sha256": PORTABLE_V19_SCHEMA_CATALOG_DIGEST,
    "schema_version": 20,
    "state_schema_version": 25,
    "minimum_runtime_session_version": 16,
    "migration25_sha256": "799f4522a502e0184d91e7d6193226ae597105a4d81a6e9f5ec2e9f6345679d7",
    "saved_item_admission": 1,
}
PORTABLE_V20_SCHEMA_CATALOG_DIGEST = sha256(
    portable_canonical_json_bytes(PORTABLE_V20_SCHEMA_CATALOG)
).hexdigest()

"""Portable11 derived evidence catalog; older catalogs retain their meanings."""

from hashlib import sha256

from open_brain_engine.core.ids import portable_canonical_json_bytes

from .v10_catalog import PORTABLE_V10_SCHEMA_CATALOG_DIGEST

PORTABLE_V11_SCHEMA_CATALOG = {
    "base_catalog_sha256": PORTABLE_V10_SCHEMA_CATALOG_DIGEST,
    "development_evidence": 1,
    "append_operations": 1,
    "approval_dependencies": 1,
    "schema_version": 11,
}
PORTABLE_V11_SCHEMA_CATALOG_DIGEST = sha256(
    portable_canonical_json_bytes(PORTABLE_V11_SCHEMA_CATALOG)
).hexdigest()

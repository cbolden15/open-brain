"""Portable v6 catalog, frozen by stage-one integrated acceptance and review."""

from hashlib import sha256

from open_brain_engine.core.ids import portable_canonical_json_bytes

from .v5 import PORTABLE_V5_SCHEMA_CATALOG_DIGEST

PORTABLE_V6_SCHEMA_CATALOG = {
    "base_catalog_sha256": PORTABLE_V5_SCHEMA_CATALOG_DIGEST,
    "facet_events": 1,
    "facet_operations": 1,
    "facet_state": 1,
    "review_binding": 5,
    "schema_version": 6,
}
PORTABLE_V6_SCHEMA_CATALOG_DIGEST = sha256(
    portable_canonical_json_bytes(PORTABLE_V6_SCHEMA_CATALOG)
).hexdigest()

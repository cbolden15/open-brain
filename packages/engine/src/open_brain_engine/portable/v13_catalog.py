"""Portable13 catalog binds lifecycle evidence to the frozen Portable12 base."""

from hashlib import sha256

from open_brain_engine.core.ids import portable_canonical_json_bytes

from .v12_catalog import PORTABLE_V12_SCHEMA_CATALOG_DIGEST

# Frozen independently so importing Portable schemas never initializes the engine.
# Contract tests bind this catalog value to the actual immutable journal DDL.
LIFECYCLE_JOURNAL_SCHEMA_SHA256 = "90ca8c4fe7205bc679e2cf1422bf523a6bb5565da242ad8505ad3b48f581657c"

PORTABLE_V13_SCHEMA_CATALOG = {
    "base_catalog_sha256": PORTABLE_V12_SCHEMA_CATALOG_DIGEST,
    "schema_version": 13,
    "capture_lifecycle_journal": 1,
    "capture_lifecycle_payload_layout": 1,
    "capture_lifecycle_schema_sha256": LIFECYCLE_JOURNAL_SCHEMA_SHA256,
}
PORTABLE_V13_SCHEMA_CATALOG_DIGEST = sha256(
    portable_canonical_json_bytes(PORTABLE_V13_SCHEMA_CATALOG)
).hexdigest()

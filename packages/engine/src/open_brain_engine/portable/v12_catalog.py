"""Portable12 Slack Work sharing catalog, chained to frozen Portable11."""

from hashlib import sha256

from open_brain_engine.core.ids import portable_canonical_json_bytes

from .v11_catalog import PORTABLE_V11_SCHEMA_CATALOG_DIGEST

PORTABLE_V12_SCHEMA_CATALOG = {
    "base_catalog_sha256": PORTABLE_V11_SCHEMA_CATALOG_DIGEST,
    "schema_version": 12,
    "sharing_evidence": 2,
    "slack_recall_selections": 1,
    "slack_recall_envelope": 1,
}
PORTABLE_V12_SCHEMA_CATALOG_DIGEST = sha256(
    portable_canonical_json_bytes(PORTABLE_V12_SCHEMA_CATALOG)
).hexdigest()

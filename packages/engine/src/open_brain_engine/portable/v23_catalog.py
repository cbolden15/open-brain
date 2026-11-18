"""Portable23 combines the Portable22 catalog with browser filing witnesses."""

from hashlib import sha256

from open_brain_engine.core.ids import portable_canonical_json_bytes

from .v22_catalog import PORTABLE_V22_SCHEMA_CATALOG_DIGEST

PORTABLE_V23_SCHEMA_CATALOG = {
    "base_catalog_sha256": PORTABLE_V22_SCHEMA_CATALOG_DIGEST,
    "schema_version": 23,
    "state_schema_version": 28,
    "minimum_runtime_session_version": 16,
    "migration28_sha256": "f870690f5e2317ccd6e8a927ea6830cde06a4146a724701242ea99c927e90005",
    "browser_filing_witnesses": 1,
}
PORTABLE_V23_SCHEMA_CATALOG_DIGEST = sha256(
    portable_canonical_json_bytes(PORTABLE_V23_SCHEMA_CATALOG)
).hexdigest()

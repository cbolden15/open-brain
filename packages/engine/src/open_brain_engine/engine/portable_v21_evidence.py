"""Snapshot browser annotation evidence with Portable20 state."""

import sqlite3
import unicodedata
from hashlib import sha256
from types import MappingProxyType

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.portable.browser_annotations_v21 import (
    ANNOTATION_COLUMNS,
    ANNOTATION_CONTENT,
    ANNOTATION_KEY,
    BROWSER_ANNOTATIONS_CONTRACT,
    BROWSER_ANNOTATIONS_PATH,
    annotation_request_digest,
)
from open_brain_engine.portable.v1 import PortableValidationError

from .contracts import LocalEngineContext
from .portable_v5_evidence import PortableV5StateEvidence
from .portable_v20_evidence import serialize_portable_v20_state


def serialize_portable_v21_state(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
    *,
    relationship_sidecar_present: bool,
    _expected_schema: int = 26,
) -> PortableV5StateEvidence:
    base = serialize_portable_v20_state(
        connection,
        profile,
        relationship_sidecar_present=relationship_sidecar_present,
        _expected_schema=_expected_schema,
    )
    rows = connection.execute(
        f"SELECT {', '.join(ANNOTATION_COLUMNS)} FROM browser_annotations "
        f"ORDER BY {', '.join(ANNOTATION_KEY)}"
    )
    annotations = []
    for row in rows:
        record = dict(row)
        if record["state"] == "accepted":
            normalized = {
                name: unicodedata.normalize("NFC", value) if value is not None else None
                for name in ANNOTATION_CONTENT
                for value in (record[name],)
            }
            if any(normalized[name] != record[name] for name in ANNOTATION_CONTENT):
                # Early rows hashed raw DOM code points before the wire normalized them.
                # Verify the original digest before projecting its NFC equivalent.
                if annotation_request_digest(record) != record["request_sha256"]:
                    raise PortableValidationError("browser annotation request digest mismatch")
                record.update(normalized)
                record["request_sha256"] = annotation_request_digest(record)
        annotations.append(record)
    payload = {"contract": BROWSER_ANNOTATIONS_CONTRACT, "annotations": annotations}
    semantic = {**base.semantic_state, "browser_annotations": payload}
    return PortableV5StateEvidence(
        MappingProxyType({**base.sidecars, BROWSER_ANNOTATIONS_PATH: canonical(payload)}),
        MappingProxyType(semantic),
        sha256(canonical(semantic)).hexdigest(),
    )

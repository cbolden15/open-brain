"""Portable19 evidence: exact Portable18 state plus saved-item source identities."""

import sqlite3
from hashlib import sha256
from types import MappingProxyType

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.portable.saved_item_sources_v19 import (
    SAVED_ITEM_SOURCES_PATH,
    saved_item_sources_payload,
)

from .contracts import LocalEngineContext
from .portable_v5_evidence import PortableV5StateEvidence
from .portable_v18_evidence import serialize_portable_v18_state
from .saved_item_identity import SavedItemSource


def serialize_portable_v19_state(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
    *,
    relationship_sidecar_present: bool,
    _expected_schema: int = 24,
) -> PortableV5StateEvidence:
    base = serialize_portable_v18_state(
        connection, profile, relationship_sidecar_present=relationship_sidecar_present,
        _expected_schema=_expected_schema,
    )
    sources = tuple(
        SavedItemSource(*row)
        for row in connection.execute(
            "SELECT connector_name, connection_id, resource_id "
            "FROM saved_item_sources ORDER BY connector_name, connection_id, resource_id"
        )
    )
    payload = saved_item_sources_payload(sources)
    semantic = {**base.semantic_state, "saved_item_sources": payload}
    return PortableV5StateEvidence(
        MappingProxyType({**base.sidecars, SAVED_ITEM_SOURCES_PATH: canonical(payload)}),
        MappingProxyType(semantic),
        sha256(canonical(semantic)).hexdigest(),
    )

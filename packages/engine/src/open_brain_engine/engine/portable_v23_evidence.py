"""Snapshot retained browser filing evidence with Portable22 state."""

import sqlite3
from hashlib import sha256
from types import MappingProxyType

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.portable.browser_filing_v23 import (
    BROWSER_FILING_CONTRACT,
    BROWSER_FILING_PATH,
    FILING_WITNESS_COLUMNS,
)

from .contracts import LocalEngineContext
from .portable_v5_evidence import PortableV5StateEvidence
from .portable_v22_evidence import serialize_portable_v22_state


def serialize_portable_v23_state(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
    *,
    relationship_sidecar_present: bool,
) -> PortableV5StateEvidence:
    base = serialize_portable_v22_state(
        connection,
        profile,
        relationship_sidecar_present=relationship_sidecar_present,
        _expected_schema=28,
    )
    rows = connection.execute(
        f"SELECT {', '.join(FILING_WITNESS_COLUMNS)} "
        "FROM browser_filing_witnesses ORDER BY route_id"
    )
    payload = {"contract": BROWSER_FILING_CONTRACT, "witnesses": [dict(row) for row in rows]}
    semantic = {**base.semantic_state, "browser_filing_witnesses": payload}
    return PortableV5StateEvidence(
        MappingProxyType({**base.sidecars, BROWSER_FILING_PATH: canonical(payload)}),
        MappingProxyType(semantic),
        sha256(canonical(semantic)).hexdigest(),
    )

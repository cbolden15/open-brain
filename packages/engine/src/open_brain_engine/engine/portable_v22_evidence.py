"""Snapshot calendar-day revisit evidence with Portable21 state."""

import sqlite3
from hashlib import sha256
from types import MappingProxyType

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.portable.browser_revisits_v22 import (
    BROWSER_REVISITS_CONTRACT,
    BROWSER_REVISITS_PATH,
    REVISIT_COLUMNS,
)

from .contracts import LocalEngineContext
from .portable_v5_evidence import PortableV5StateEvidence
from .portable_v21_evidence import serialize_portable_v21_state


def serialize_portable_v22_state(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
    *,
    relationship_sidecar_present: bool,
) -> PortableV5StateEvidence:
    base = serialize_portable_v21_state(
        connection,
        profile,
        relationship_sidecar_present=relationship_sidecar_present,
        _expected_schema=27,
    )
    rows = connection.execute(
        f"SELECT {', '.join(REVISIT_COLUMNS)} FROM browser_revisits ORDER BY sequence"
    )
    payload = {"contract": BROWSER_REVISITS_CONTRACT, "revisits": [dict(row) for row in rows]}
    semantic = {**base.semantic_state, "browser_revisits": payload}
    return PortableV5StateEvidence(
        MappingProxyType({**base.sidecars, BROWSER_REVISITS_PATH: canonical(payload)}),
        MappingProxyType(semantic),
        sha256(canonical(semantic)).hexdigest(),
    )

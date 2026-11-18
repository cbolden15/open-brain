"""Snapshot saved-item admission and browser operation evidence with Portable19 state."""

import sqlite3
from hashlib import sha256
from types import MappingProxyType

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.portable.saved_item_admission_v20 import (
    SAVED_ITEM_ADMISSION_CONTRACT,
    SAVED_ITEM_ADMISSION_PATH,
    TABLE_COLUMNS,
    TABLE_KEYS,
)

from .contracts import LocalEngineContext
from .portable_v5_evidence import PortableV5StateEvidence
from .portable_v19_evidence import serialize_portable_v19_state


def serialize_portable_v20_state(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
    *,
    relationship_sidecar_present: bool,
) -> PortableV5StateEvidence:
    base = serialize_portable_v19_state(
        connection,
        profile,
        relationship_sidecar_present=relationship_sidecar_present,
        _expected_schema=25,
    )
    payload = {
        "contract": SAVED_ITEM_ADMISSION_CONTRACT,
        **{
            table: [
                dict(row)
                for row in connection.execute(
                    f"SELECT {', '.join(columns)} FROM {table} "
                    f"ORDER BY {', '.join(TABLE_KEYS[table])}"
                )
            ]
            for table, columns in TABLE_COLUMNS.items()
        },
    }
    semantic = {**base.semantic_state, "saved_item_admission": payload}
    return PortableV5StateEvidence(
        MappingProxyType({**base.sidecars, SAVED_ITEM_ADMISSION_PATH: canonical(payload)}),
        MappingProxyType(semantic),
        sha256(canonical(semantic)).hexdigest(),
    )

"""Portable12 evidence cut from one schema17 snapshot."""

from __future__ import annotations

import base64
import json
import sqlite3
from hashlib import sha256
from types import MappingProxyType
from typing import Any

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.portable.slack_v12 import SELECTION_COLUMNS, SELECTIONS_PATH

from .contracts import LocalEngineContext
from .portable_v5_evidence import PortableV5StateEvidence
from .portable_v11_evidence import serialize_portable_v11_state


def selection_sidecar(connection: sqlite3.Connection) -> bytes:
    rows: list[dict[str, Any]] = []
    names = [column.split(":")[0] for column in SELECTION_COLUMNS]
    for row in connection.execute(
        "SELECT " + ",".join(names) + " FROM slack_recall_selections ORDER BY selection_id"
    ):
        value = dict(row)
        for key in ("request_bytes", "selection_bytes"):
            value[key] = base64.b64encode(value[key]).decode("ascii")
        rows.append(value)
    return canonical({"schema_version": 1, "selections": rows})


def serialize_portable_v12_state(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
    *,
    relationship_sidecar_present: bool,
    _expected_schema: int = 17,
) -> PortableV5StateEvidence:
    base = serialize_portable_v11_state(
        connection,
        profile,
        relationship_sidecar_present=relationship_sidecar_present,
        _expected_schema=_expected_schema,
    )
    selections = selection_sidecar(connection)
    sidecars = dict(base.sidecars)
    sidecars[SELECTIONS_PATH] = selections
    semantic = dict(base.semantic_state)
    semantic["slack_recall_selections"] = json.loads(selections)
    return PortableV5StateEvidence(
        sidecars=MappingProxyType(sidecars),
        semantic_state=MappingProxyType(semantic),
        semantic_state_sha256=sha256(canonical(semantic)).hexdigest(),
    )

"""Schema16 snapshot: unchanged inherited evidence plus canonical developments."""

from __future__ import annotations

import json
import sqlite3
from hashlib import sha256
from types import MappingProxyType

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical

from .contracts import LocalEngineContext
from .developments import (
    DEVELOPMENT_APPEND_PATH,
    DEVELOPMENT_EVIDENCE_PATH,
    append_sidecar,
    evidence_sidecar,
)
from .portable_v5_evidence import PortableV5StateEvidence
from .portable_v10_evidence import serialize_portable_v10_state


def serialize_portable_v11_state(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
    *,
    relationship_sidecar_present: bool,
) -> PortableV5StateEvidence:
    base = serialize_portable_v10_state(
        connection,
        profile,
        relationship_sidecar_present=relationship_sidecar_present,
        expected_schema=16,
    )
    sidecars = dict(base.sidecars)
    evidence = evidence_sidecar(connection)
    sidecars[DEVELOPMENT_EVIDENCE_PATH] = evidence
    append = append_sidecar(connection)
    sidecars[DEVELOPMENT_APPEND_PATH] = append
    semantic = dict(base.semantic_state)
    semantic["development_append_operations"] = json.loads(append)
    semantic["development_evidence"] = json.loads(evidence)
    return PortableV5StateEvidence(
        sidecars=MappingProxyType(sidecars),
        semantic_state=MappingProxyType(semantic),
        semantic_state_sha256=sha256(canonical(semantic)).hexdigest(),
    )

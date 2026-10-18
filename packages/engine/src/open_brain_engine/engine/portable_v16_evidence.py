"""Portable16 evidence: exact Portable15 state plus owner embedding policy history."""

from __future__ import annotations

import sqlite3
from hashlib import sha256
from types import MappingProxyType

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.portable.embedding_policy_v16 import (
    EMBEDDING_POLICY_PATH,
    embedding_policy_sidecar,
    validate_embedding_policy_evidence,
)

from .contracts import LocalEngineContext
from .embedding_policy import history_rows
from .portable_v5_evidence import PortableV5StateEvidence
from .portable_v15_evidence import serialize_portable_v15_state


def serialize_portable_v16_state(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
    *,
    relationship_sidecar_present: bool,
) -> PortableV5StateEvidence:
    base = serialize_portable_v15_state(
        connection,
        profile,
        relationship_sidecar_present=relationship_sidecar_present,
        _expected_schema=21,
    )
    # Policy state, vectors and caches are host-local; only history is evidence.
    raw = embedding_policy_sidecar(history_rows(connection))
    semantic = {
        **base.semantic_state,
        "embedding_policy_history": validate_embedding_policy_evidence(
            {EMBEDDING_POLICY_PATH: raw}
        ),
    }
    return PortableV5StateEvidence(
        MappingProxyType({**base.sidecars, EMBEDDING_POLICY_PATH: raw}),
        MappingProxyType(semantic),
        sha256(canonical(semantic)).hexdigest(),
    )

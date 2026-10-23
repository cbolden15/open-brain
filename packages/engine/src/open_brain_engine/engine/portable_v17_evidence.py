"""Portable17 evidence: exact Portable16 state plus owner egress policy history."""

from __future__ import annotations

import sqlite3
from hashlib import sha256
from types import MappingProxyType

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.portable.egress_policy_v17 import (
    OWNER_EGRESS_POLICY_PATH,
    owner_egress_policy_sidecar,
    validate_owner_egress_policy_evidence,
)

from .contracts import LocalEngineContext
from .owner_egress_policy import history_rows
from .portable_v5_evidence import PortableV5StateEvidence
from .portable_v16_evidence import serialize_portable_v16_state


def serialize_portable_v17_state(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
    *,
    relationship_sidecar_present: bool,
) -> PortableV5StateEvidence:
    base = serialize_portable_v16_state(
        connection,
        profile,
        relationship_sidecar_present=relationship_sidecar_present,
        _expected_schema=22,
    )
    raw = owner_egress_policy_sidecar(history_rows(connection))
    semantic = {
        **base.semantic_state,
        "owner_egress_policy_history": validate_owner_egress_policy_evidence(
            {OWNER_EGRESS_POLICY_PATH: raw}
        ),
    }
    return PortableV5StateEvidence(
        MappingProxyType({**base.sidecars, OWNER_EGRESS_POLICY_PATH: raw}),
        MappingProxyType(semantic),
        sha256(canonical(semantic)).hexdigest(),
    )

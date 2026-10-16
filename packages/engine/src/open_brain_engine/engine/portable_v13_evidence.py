"""Portable13 evidence from the same schema18 snapshot as inherited evidence."""

from __future__ import annotations

import sqlite3
from hashlib import sha256
from types import MappingProxyType

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.portable.lifecycle_v13 import lifecycle_evidence_sidecars

from .capture_lifecycle_journal import LifecycleJournal
from .contracts import LocalEngineContext
from .portable_v5_evidence import PortableV5StateEvidence
from .portable_v12_evidence import serialize_portable_v12_state


def serialize_portable_v13_state(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
    *,
    relationship_sidecar_present: bool,
) -> PortableV5StateEvidence:
    base = serialize_portable_v12_state(
        connection, profile,
        relationship_sidecar_present=relationship_sidecar_present,
        _expected_schema=18,
    )
    identity = connection.execute("SELECT brain_id,issuer_epoch FROM brain_identity").fetchone()
    journal = LifecycleJournal.for_engine(
        connection, brain_id=identity[0], issuer_epoch=identity[1]
    ).export_evidence_value()
    sidecars = dict(base.sidecars)
    sidecars.update(lifecycle_evidence_sidecars(journal))
    semantic = dict(base.semantic_state)
    semantic["capture_lifecycle_journal"] = journal
    return PortableV5StateEvidence(
        sidecars=MappingProxyType(sidecars),
        semantic_state=MappingProxyType(semantic),
        semantic_state_sha256=sha256(canonical(semantic)).hexdigest(),
    )

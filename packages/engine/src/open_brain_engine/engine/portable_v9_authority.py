"""Export exact settled historical facts from one schema-14 snapshot."""

import base64
import json
import sqlite3
from hashlib import sha256
from types import MappingProxyType
from typing import Any, cast

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.portable.v7 import SHARING_APPROVALS_PATH
from open_brain_engine.portable.v8_capture_metadata import (
    CAPTURE_METADATA_PATH,
    capture_metadata_bytes,
)
from open_brain_engine.portable.v8_custody import (
    CUSTODY_PATH,
    JOURNAL_TABLES,
    SEQUENCE_TABLES,
    custody_bytes,
)
from open_brain_engine.portable.v9 import HISTORICAL_AUTHORITY_PATH, historical_authority_bytes

from .contracts import LocalEngineContext
from .historical_contracts import HistoricalDestination
from .historical_fence import HistoricalPendingFence
from .historical_projection import verify_versioned_historical_projection
from .historical_registry import HistoricalRegistryStore
from .portable_v5_evidence import PortableV5StateEvidence, _serialize_portable_state
from .portable_v6_authority import _source_authority_metadata, _source_authority_sidecars
from .portable_v7_authority import _sharing_authority_metadata


def _journal_snapshot(connection: sqlite3.Connection) -> dict[str, Any]:
    """Same frozen custody rows, admitted only at the new runtime schema boundary."""
    if (
        not connection.in_transaction
        or connection.execute("PRAGMA user_version").fetchone()[0] != 14
    ):
        raise ValueError("capture custody requires one schema14 read snapshot")
    state: dict[str, Any] = {"schema_version": 1}
    for table, columns in JOURNAL_TABLES.items():
        state[table] = [
            dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY {columns[0]}")
        ]
    for row in state["capture_ingestion_payloads"]:
        row["envelope_bytes"] = base64.b64encode(row["envelope_bytes"]).decode("ascii")
    state["sequences"] = {
        row["name"]: row["seq"]
        for row in connection.execute("SELECT name,seq FROM sqlite_sequence")
        if row["name"] in SEQUENCE_TABLES
    }
    return state


def historical_authority_sidecar(
    connection: sqlite3.Connection, profile: LocalEngineContext
) -> tuple[str, bytes]:
    if (
        not connection.in_transaction
        or connection.execute("PRAGMA user_version").fetchone()[0] != 14
    ):
        raise ValueError("Portable9 requires one active schema-14 snapshot")
    row = connection.execute("SELECT brain_id,issuer_epoch FROM brain_identity").fetchone()
    if row is None:
        raise ValueError("Portable9 requires an existing Brain identity")
    destination = HistoricalDestination(brain_id=row[0], issuer_epoch=row[1])
    registry = HistoricalRegistryStore(profile.root, profile.root_identity).read(destination)
    HistoricalPendingFence(profile.root, profile.root_identity).assert_settled(registry)
    records = verify_versioned_historical_projection(connection, profile, registry)
    return HISTORICAL_AUTHORITY_PATH, historical_authority_bytes(registry, records)


def _serialize_historical_state(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
    *,
    relationship_sidecar_present: bool,
    portable_version: int,
) -> PortableV5StateEvidence:
    """New version boundary, not widened v5/v6/v7 serializer admission."""
    if type(relationship_sidecar_present) is not bool:
        raise ValueError("relationship sidecar presence must be a bool")
    path, historical = historical_authority_sidecar(connection, profile)
    if portable_version == 8:
        from open_brain_engine.portable.v8 import (
            HISTORICAL_AUTHORITY_PATH as old_path,
        )
        from open_brain_engine.portable.v8 import (
            historical_authority_bytes as old_bytes,
        )

        from .historical_transition import HistoricalTransition

        row = connection.execute("SELECT brain_id,issuer_epoch FROM brain_identity").fetchone()
        registry = HistoricalRegistryStore(profile.root, profile.root_identity).read(
            HistoricalDestination(brain_id=row[0], issuer_epoch=row[1])
        )
        records = verify_versioned_historical_projection(connection, profile, registry)
        if any(type(record) is not HistoricalTransition for record in records):
            raise ValueError("Portable8 restore cannot contain V2 history")
        path, historical = old_path, old_bytes(registry, cast(Any, records))
    source_authority = _source_authority_metadata(connection)
    sharing = _sharing_authority_metadata(connection)
    base = _serialize_portable_state(
        connection,
        tenant_id=profile.tenant_id,
        relationship_sidecar_present=relationship_sidecar_present,
        source_authority=source_authority,
    )
    sidecars = dict(base.sidecars)
    sidecars.update(_source_authority_sidecars(source_authority))
    sidecars[SHARING_APPROVALS_PATH] = canonical(sharing)
    sidecars[path] = historical
    capture_metadata = capture_metadata_bytes(
        [dict(row) for row in connection.execute("SELECT * FROM captures ORDER BY capture_id")]
    )
    sidecars[CAPTURE_METADATA_PATH] = capture_metadata
    custody = custody_bytes(_journal_snapshot(connection))
    sidecars[CUSTODY_PATH] = custody
    semantic = dict(base.semantic_state)
    semantic["sharing_authority"] = sharing
    semantic["historical_authority"] = json.loads(historical)
    semantic["original_capture_metadata"] = json.loads(capture_metadata)
    semantic["capture_custody"] = json.loads(custody)
    return PortableV5StateEvidence(
        sidecars=MappingProxyType(sidecars),
        semantic_state=MappingProxyType(semantic),
        semantic_state_sha256=sha256(canonical(semantic)).hexdigest(),
    )


def serialize_portable_v9_state(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
    *,
    relationship_sidecar_present: bool,
) -> PortableV5StateEvidence:
    return _serialize_historical_state(
        connection,
        profile,
        relationship_sidecar_present=relationship_sidecar_present,
        portable_version=9,
    )


def serialize_restored_v8_state(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
    *,
    relationship_sidecar_present: bool,
) -> PortableV5StateEvidence:
    """Audit old snapshot semantics at schema14; never widen the frozen V8 exporter."""
    return _serialize_historical_state(
        connection,
        profile,
        relationship_sidecar_present=relationship_sidecar_present,
        portable_version=8,
    )

"""Export exact settled historical facts from one schema-13 snapshot."""

import json
import sqlite3
from hashlib import sha256
from types import MappingProxyType

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.portable.v7 import SHARING_APPROVALS_PATH
from open_brain_engine.portable.v8 import HISTORICAL_AUTHORITY_PATH, historical_authority_bytes

from .contracts import LocalEngineContext
from .historical_contracts import HistoricalDestination
from .historical_fence import HistoricalPendingFence
from .historical_projection import verify_historical_projection
from .historical_registry import HistoricalRegistryStore
from .portable_v5_evidence import PortableV5StateEvidence, _serialize_portable_state
from .portable_v6_authority import _source_authority_metadata, _source_authority_sidecars
from .portable_v7_authority import _sharing_authority_metadata


def historical_authority_sidecar(
    connection: sqlite3.Connection, profile: LocalEngineContext
) -> tuple[str, bytes]:
    if (
        not connection.in_transaction
        or connection.execute("PRAGMA user_version").fetchone()[0] != 13
    ):
        raise ValueError("Portable8 requires one active schema-13 snapshot")
    row = connection.execute("SELECT brain_id,issuer_epoch FROM brain_identity").fetchone()
    if row is None:
        raise ValueError("Portable8 requires an existing Brain identity")
    destination = HistoricalDestination(brain_id=row[0], issuer_epoch=row[1])
    registry = HistoricalRegistryStore(profile.root, profile.root_identity).read(destination)
    HistoricalPendingFence(profile.root, profile.root_identity).assert_settled(registry)
    records = verify_historical_projection(connection, profile, registry)
    return HISTORICAL_AUTHORITY_PATH, historical_authority_bytes(registry, records)


def serialize_portable_v8_state(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
    *,
    relationship_sidecar_present: bool,
) -> PortableV5StateEvidence:
    """New version boundary, not widened v5/v6/v7 serializer admission."""
    if type(relationship_sidecar_present) is not bool:
        raise ValueError("relationship sidecar presence must be a bool")
    path, historical = historical_authority_sidecar(connection, profile)
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
    semantic = dict(base.semantic_state)
    semantic["sharing_authority"] = sharing
    semantic["historical_authority"] = json.loads(historical)
    return PortableV5StateEvidence(
        sidecars=MappingProxyType(sidecars),
        semantic_state=MappingProxyType(semantic),
        semantic_state_sha256=sha256(canonical(semantic)).hexdigest(),
    )

"""Export durable approvals and frozen lifecycle evidence from one schema19 view."""

from __future__ import annotations

import json
import sqlite3
from hashlib import sha256
from types import MappingProxyType
from typing import cast

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.portable.lifecycle_v13 import lifecycle_evidence_sidecars
from open_brain_engine.portable.retraction_v14 import (
    APPROVALS_PATH,
    retraction_evidence_sidecar,
)
from open_brain_engine.portable.v1 import PortableValidationError

from .capture_lifecycle_journal import LifecycleJournal
from .contracts import LocalEngineContext
from .owner_retraction_contracts import approval_record
from .portable_v5_evidence import PortableV5StateEvidence
from .portable_v12_evidence import serialize_portable_v12_state


def serialize_portable_v13_successor_state(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
    *,
    relationship_sidecar_present: bool,
) -> PortableV5StateEvidence:
    """Audit unchanged v13 semantics after restore into the schema19 successor."""
    base = serialize_portable_v12_state(
        connection, profile,
        relationship_sidecar_present=relationship_sidecar_present,
        _expected_schema=19,
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


def approval_records(connection: sqlite3.Connection) -> list[dict[str, object]]:
    """Refuse corrupt stored canonical bytes or hashes instead of repairing them."""
    records = []
    for operation, request_raw, request_hash, receipt_raw, receipt_hash in connection.execute(
        "SELECT operation_id,request_bytes,request_sha256,receipt_bytes,receipt_sha256 "
        "FROM retraction_approvals ORDER BY operation_id"
    ):
        try:
            request, receipt = json.loads(request_raw), json.loads(receipt_raw)
            record = approval_record(request, receipt)
            if (
                canonical(request) != request_raw
                or canonical(receipt) != receipt_raw
                or sha256(request_raw).hexdigest() != request_hash
                or sha256(receipt_raw).hexdigest() != receipt_hash
                or request["operation_id"] != operation
            ):
                raise ValueError("invalid retained approval bytes")
            records.append(record)
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as error:
            raise PortableValidationError("retained owner retraction evidence invalid") from error
    return records


def serialize_portable_v14_state(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
    *,
    relationship_sidecar_present: bool,
) -> PortableV5StateEvidence:
    base = serialize_portable_v13_successor_state(
        connection, profile, relationship_sidecar_present=relationship_sidecar_present
    )
    records = approval_records(connection)
    journal = cast(dict[str, object], base.semantic_state["capture_lifecycle_journal"])
    raw = retraction_evidence_sidecar(records, journal)
    sidecars = {**base.sidecars, APPROVALS_PATH: raw}
    semantic = {**base.semantic_state, "owner_retraction_approvals": json.loads(raw)}
    return PortableV5StateEvidence(
        sidecars=MappingProxyType(sidecars),
        semantic_state=MappingProxyType(semantic),
        semantic_state_sha256=sha256(canonical(semantic)).hexdigest(),
    )

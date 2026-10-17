"""Portable15 evidence and explicit predecessor-only snapshot audit projection."""

from __future__ import annotations

import sqlite3
from hashlib import sha256
from types import MappingProxyType
from typing import cast

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.portable.consumer_v15 import (
    CONSUMER_CONTRACT,
    CONSUMER_OBJECT_PREFIX,
    CONSUMER_PATH,
    PROJECTION_COLUMNS,
    projection_row,
    rebuild_projections,
)
from open_brain_engine.portable.lifecycle_v15 import lifecycle_evidence_sidecars
from open_brain_engine.portable.retraction_v14 import APPROVALS_PATH
from open_brain_engine.portable.retraction_v15 import retraction_evidence_sidecar
from open_brain_engine.portable.v1 import PortableValidationError

from .capture_lifecycle import _matching_row, decode_lifecycle_segment
from .capture_lifecycle_journal import JOURNAL_CONTRACT, restore_lifecycle_journal_value
from .consumer_intake import _load
from .consumer_journal import LifecycleJournalV2, consumer_items
from .consumer_operations import PreparedConsumerOperation
from .consumer_segments import ConsumerSegmentEnvelope
from .contracts import LocalEngineContext
from .portable_v5_evidence import PortableV5StateEvidence
from .portable_v12_evidence import serialize_portable_v12_state
from .portable_v14_evidence import approval_records


def incomplete_consumer_capture_rows(connection: sqlite3.Connection) -> tuple[sqlite3.Row, ...]:
    """Normalize only halted, unadmitted materializer projections, preserving custody.

    A partial capture is not Portable12 capture history. Its exact accepted intent,
    reservation and custodied envelope/body remain Portable15 authority; clean
    restore deliberately leaves that halted projection unmaterialized.
    """
    identity = connection.execute("SELECT brain_id,issuer_epoch FROM brain_identity").fetchone()
    journal = LifecycleJournalV2.for_engine(
        connection, brain_id=identity[0], issuer_epoch=identity[1]
    )
    journal.export_evidence_value()  # Nonterminal operations still block export.
    state = journal._state()
    result: list[sqlite3.Row] = []
    for row in connection.execute("SELECT * FROM captures WHERE stage != 3 ORDER BY capture_id"):
        binding = connection.execute(
            "SELECT operation_id,group_key,envelope_sha256 FROM consumer_segment_bindings "
            "WHERE capture_id=?",
            (row["capture_id"],),
        ).fetchone()
        if binding is None:
            raise PortableValidationError("incomplete capture is not halted consumer custody")
        operation, group, digest = binding
        if (
            type(row["stage"]) is not int
            or row["stage"] not in {0, 1, 2}
            or state.outcomes.get((operation, group), (None,))[0] != "halted"
            or (operation, group, row["capture_id"]) not in state.custodied
            or row["canonical_path"] is not None
            or row["publication_path"] is not None
        ):
            raise PortableValidationError("invalid incomplete consumer projection")
        segment = decode_lifecycle_segment(journal, operation, state.blobs[digest])
        if not isinstance(segment, ConsumerSegmentEnvelope):
            raise PortableValidationError("invalid incomplete consumer envelope")
        _matching_row(row, segment, journal)
        if any(
            connection.execute(sql, (row["capture_id"],)).fetchone() is not None
            for sql in (
                "SELECT 1 FROM source_revisions WHERE capture_id=?",
                "SELECT 1 FROM route_operations WHERE capture_id=?",
            )
        ):
            raise PortableValidationError("incomplete consumer capture has admitted history")
        result.append(row)
    return tuple(result)


def consumer_evidence_sidecars(
    connection: sqlite3.Connection,
    journal: LifecycleJournalV2,
    profile: LocalEngineContext,
) -> tuple[dict[str, bytes], dict[str, object]]:
    operations: dict[str, PreparedConsumerOperation] = {}
    approvals: list[dict[str, object]] = []
    inventory: dict[str, tuple[str, bytes]] = {}
    for row in connection.execute("SELECT * FROM consumer_approvals ORDER BY operation_id"):
        operation = row["operation_id"]
        prepared = _load(journal, operation)
        if prepared is None:
            raise PortableValidationError("consumer approval missing")
        prepared.validate_profile(profile)
        operations[operation] = prepared
        approvals.append(
            {
                "operation_id": operation,
                "root_sha256": prepared.intent_sha256,
                "request_sha256": prepared.request.request_sha256,
            }
        )
        for key, value in prepared.inventory().items():
            if key in inventory and inventory[key] != value:
                raise PortableValidationError("consumer object kind collision")
            inventory[key] = value
    actual = {
        key: (kind, bytes(raw))
        for key, kind, raw in connection.execute(
            "SELECT sha256,kind,object_bytes FROM consumer_intent_objects ORDER BY sha256"
        )
    }
    typed = {
        key
        for key, request in journal._state().requests.items()
        if consumer_items(request) is not None
    }
    if actual != inventory or set(operations) != typed:
        raise PortableValidationError("consumer retained object closure mismatch")
    metadata: dict[str, object] = {
        "contract": CONSUMER_CONTRACT,
        "approvals": approvals,
        "objects": [{"sha256": key, "kind": value[0]} for key, value in sorted(inventory.items())],
    }
    expected = rebuild_projections(operations, journal)
    for table, columns in PROJECTION_COLUMNS.items():
        order = ",".join(
            columns[:2]
            if table in {"consumer_archive_bindings", "consumer_outcomes"}
            else columns[:1]
        )
        rows = [
            projection_row(dict(row))
            for row in connection.execute(
                f"SELECT {','.join(columns)} FROM {table} ORDER BY {order}"
            )
        ]
        if rows != expected[table]:
            raise PortableValidationError("consumer retained projection mismatch")
        metadata[table] = rows
    files = {CONSUMER_OBJECT_PREFIX + key: raw for key, (_, raw) in inventory.items()}
    files[CONSUMER_PATH] = canonical(metadata)
    return files, metadata


def serialize_portable_v15_state(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
    *,
    relationship_sidecar_present: bool,
) -> PortableV5StateEvidence:
    identity = connection.execute("SELECT brain_id,issuer_epoch FROM brain_identity").fetchone()
    journal = LifecycleJournalV2.for_engine(
        connection, brain_id=identity[0], issuer_epoch=identity[1]
    )
    evidence = (
        journal.export_evidence_value()
    )  # Pending operations refuse before any export mutation.
    consumer_files, consumer = consumer_evidence_sidecars(connection, journal, profile)
    incomplete = incomplete_consumer_capture_rows(connection)
    staging = sqlite3.connect(":memory:") if incomplete else None
    try:
        projected = connection
        if staging is not None:
            # Copy this same fenced read snapshot; never mutate the live database.
            connection.backup(staging)
            staging.row_factory = sqlite3.Row
            staging.executemany(
                "DELETE FROM captures WHERE capture_id=?",
                [(row["capture_id"],) for row in incomplete],
            )
            staging.commit()
            staging.execute("BEGIN")
            projected = staging
        base = serialize_portable_v12_state(
            projected,
            profile,
            relationship_sidecar_present=relationship_sidecar_present,
            _expected_schema=20,
        )
    finally:
        if staging is not None:
            staging.close()
    approval_raw = retraction_evidence_sidecar(approval_records(connection), evidence)
    sidecars = {
        **base.sidecars,
        **lifecycle_evidence_sidecars(evidence),
        **consumer_files,
        APPROVALS_PATH: approval_raw,
    }
    import json

    semantic = {
        **base.semantic_state,
        "capture_lifecycle_journal": evidence,
        "consumer_intake": consumer,
        "owner_retraction_approvals": json.loads(approval_raw),
    }
    return PortableV5StateEvidence(
        MappingProxyType(sidecars),
        MappingProxyType(semantic),
        sha256(canonical(semantic)).hexdigest(),
    )


def serialize_legacy_successor_state(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
    *,
    relationship_sidecar_present: bool,
    portable_version: int,
) -> PortableV5StateEvidence:
    """Project only legacy journal2 requests into independently validated frozen v1 bytes."""
    from open_brain_engine.portable.lifecycle_v13 import (
        lifecycle_evidence_sidecars as legacy_sidecars,
    )
    from open_brain_engine.portable.retraction_v14 import (
        retraction_evidence_sidecar as legacy_approvals,
    )

    if portable_version not in {13, 14}:
        raise PortableValidationError("invalid predecessor audit version")
    base = serialize_portable_v12_state(
        connection,
        profile,
        relationship_sidecar_present=relationship_sidecar_present,
        _expected_schema=20,
    )
    identity = connection.execute("SELECT brain_id,issuer_epoch FROM brain_identity").fetchone()
    journal = LifecycleJournalV2.for_engine(
        connection, brain_id=identity[0], issuer_epoch=identity[1]
    )
    if any(
        consumer_items(request) is not None for request in journal._state().requests.values()
    ) or any(
        connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None
        for table in ("consumer_intent_objects", "consumer_approvals", *PROJECTION_COLUMNS)
    ):
        raise PortableValidationError("consumer evidence cannot enter predecessor format")
    evidence = {**journal.export_evidence_value(), "contract": JOURNAL_CONTRACT}
    staging = sqlite3.connect(":memory:")
    try:
        staging.execute("BEGIN")
        checked = restore_lifecycle_journal_value(
            staging, evidence, brain_id=identity[0], issuer_epoch=identity[1]
        )
        if checked.export_evidence_value() != evidence:
            raise PortableValidationError("predecessor audit projection mismatch")
    finally:
        staging.close()
    sidecars = {**base.sidecars, **legacy_sidecars(evidence)}
    semantic = {**base.semantic_state, "capture_lifecycle_journal": evidence}
    if portable_version == 14:
        import json

        raw = legacy_approvals(approval_records(connection), evidence)
        sidecars[APPROVALS_PATH] = raw
        semantic["owner_retraction_approvals"] = cast(object, json.loads(raw))
    return PortableV5StateEvidence(
        MappingProxyType(sidecars),
        MappingProxyType(semantic),
        sha256(canonical(semantic)).hexdigest(),
    )

"""Facet evidence cut from one caller-owned schema-15 read transaction.

The export coordinator owns the canonical fence, ingress gate, base v9 evidence,
managed projection checks and manifest/catalog composition.
"""

from __future__ import annotations

import json
import sqlite3
import unicodedata
from collections.abc import Mapping
from hashlib import sha256
from types import MappingProxyType
from typing import Any

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.portable.facets_v10 import (
    FACET_EVENTS_PATH,
    FACET_OPERATIONS_PATH,
    FACET_STATE_PATH,
    FacetPortableReferences,
    PortableFacetBundle,
    decode_portable_facets,
)
from open_brain_engine.portable.v1 import PortableValidationError

from .contracts import LocalEngineContext
from .facet_validation import parse
from .portable_v5_evidence import PortableV5StateEvidence

FACET_TABLES = (
    "facet_dimensions",
    "facet_values",
    "facet_subject_versions",
    "facet_assignments",
    "facet_queries",
    "facet_page_queries",
    "facet_events",
    "facet_operations",
)


def require_facet_snapshot(connection: sqlite3.Connection, *, brain_id: str) -> None:
    if not connection.in_transaction:
        raise PortableValidationError("Portable v10 facets require an active transaction")
    if connection.execute("PRAGMA user_version").fetchone()[0] != 15:
        raise PortableValidationError("Portable v10 facets require schema 15")
    row = connection.execute("SELECT brain_id FROM brain_identity WHERE singleton=1").fetchone()
    if row is None or row[0] != brain_id:
        raise PortableValidationError("Portable v10 facets require the admitted Brain identity")


def facet_references_from_connection(
    connection: sqlite3.Connection,
    *,
    principal_actors: Mapping[str, str] | None = None,
    owner_actor_id: str | None = None,
) -> FacetPortableReferences:
    """Read identities from the same snapshot, including retained canonical pages."""
    if not connection.in_transaction:
        raise PortableValidationError("facet identity indexes require an active transaction")
    capture_sources = dict(connection.execute("SELECT capture_id,source_id FROM source_revisions"))
    revision_pages = dict(
        connection.execute("SELECT DISTINCT revision_id,page_id FROM canonical_revision_members")
    )
    pages = frozenset(
        row[0]
        for row in connection.execute(
            "SELECT result_id FROM search_documents WHERE record_type='canonical' "
            "UNION SELECT page_id FROM review_page_heads "
            "UNION SELECT page_id FROM canonical_revision_members"
        )
    )
    if principal_actors is None:
        # Local stage-one actors are explicitly mapped through the trusted owner context.
        principal_actors = {
            row[0]: owner_actor_id if owner_actor_id is not None else row[0]
            for row in connection.execute("SELECT DISTINCT principal_id FROM facet_operations")
        }
    return FacetPortableReferences(
        frozenset(row[0] for row in connection.execute("SELECT source_id FROM logical_sources")),
        pages,
        capture_sources,
        revision_pages,
        frozenset(row[0] for row in connection.execute("SELECT space_id FROM spaces")),
        frozenset(
            row[0]
            for row in connection.execute(
                "SELECT publication_id FROM captures WHERE publication_id IS NOT NULL "
                "UNION SELECT publication_id FROM decisions WHERE publication_id IS NOT NULL"
            )
        ),
        principal_actors,
    )


def _rows(
    connection: sqlite3.Connection, table: str, columns: tuple[str, ...], order: str
) -> list[dict[str, Any]]:
    return [
        dict(zip(columns, row, strict=True))
        for row in connection.execute(f"SELECT {','.join(columns)} FROM {table} ORDER BY {order}")
    ]


def _canonical_stored(value: object, label: str) -> dict[str, Any]:
    if type(value) is str:
        payload = value.encode("utf-8")
    elif type(value) is bytes:
        payload = value
    else:
        raise PortableValidationError(f"facet {label} has an invalid storage class")
    try:
        decoded = parse(payload)
        if type(decoded) is not dict or canonical(decoded) != payload:
            raise ValueError("not a canonical object")
        return decoded
    except (ValueError, TypeError, UnicodeError) as error:
        raise PortableValidationError(f"facet {label} is not canonical") from error


def serialize_portable_v10_facets(
    connection: sqlite3.Connection,
    *,
    brain_id: str,
    principal_actors: Mapping[str, str] | None = None,
    owner_actor_id: str | None = None,
) -> PortableFacetBundle:
    """Serialize exact events/operations and assert their stored projection agrees."""
    require_facet_snapshot(connection, brain_id=brain_id)
    state: dict[str, Any] = {"schema_version": 1, "brain_id": brain_id}
    metadata = ("name", "archived", "version", "created_at", "updated_at")
    specs = (
        ("dimensions", "facet_dimensions", ("dimension_id", *metadata), "dimension_id"),
        ("values", "facet_values", ("value_id", "dimension_id", *metadata), "value_id"),
        (
            "subjects",
            "facet_subject_versions",
            ("subject_kind", "subject_id", "version"),
            "subject_kind,subject_id",
        ),
        (
            "assignments",
            "facet_assignments",
            (
                "subject_kind",
                "subject_id",
                "value_id",
                "present",
                "membership_version",
                "last_event_sequence",
            ),
            "subject_kind,subject_id,value_id",
        ),
        (
            "queries",
            "facet_queries",
            (
                "query_id",
                "name",
                "selector_json",
                "archived",
                "version",
                "selection_version",
                "created_at",
                "updated_at",
            ),
            "query_id",
        ),
        ("page_queries", "facet_page_queries", ("page_id", "query_id", "version"), "page_id"),
    )
    for key, table, columns, order in specs:
        values = _rows(connection, table, columns, order)
        for row in values:
            for flag in ("present", "archived"):
                if flag in row:
                    if type(row[flag]) is not int or row[flag] not in {0, 1}:
                        raise PortableValidationError("facet projection boolean storage invalid")
                    row[flag] = bool(row[flag])
            if "selector_json" in row:
                row["selector"] = _canonical_stored(row.pop("selector_json"), "selector")
        state[key] = values
    # name_key is derived but must agree in the store; never export it as evidence.
    for table in ("facet_dimensions", "facet_values", "facet_queries"):
        for name, name_key in connection.execute(f"SELECT name,name_key FROM {table}"):
            if unicodedata.normalize("NFC", name.casefold()) != name_key:
                raise PortableValidationError("facet derived name key drift")
    events = []
    for row in _rows(
        connection,
        "facet_events",
        ("event_id", "sequence", "operation_id", "actor_id", "recorded_at", "kind", "event_json"),
        "sequence",
    ):
        event = _canonical_stored(row.pop("event_json"), "event")
        if any(event.get(key) != value for key, value in row.items()):
            raise PortableValidationError("facet event columns disagree with retained bytes")
        events.append(event)
    operations = _rows(
        connection,
        "facet_operations",
        (
            "operation_id",
            "principal_id",
            "request_sha256",
            "request_json",
            "receipt_json",
            "event_id",
        ),
        "operation_id",
    )
    for row in operations:
        for field in ("request_json", "receipt_json"):
            row[field] = _canonical_stored(row[field], field)
    files = {
        FACET_EVENTS_PATH: canonical({"schema_version": 1, "brain_id": brain_id, "events": events}),
        FACET_OPERATIONS_PATH: canonical(
            {"schema_version": 1, "brain_id": brain_id, "operations": operations}
        ),
        FACET_STATE_PATH: canonical(state),
    }
    references = facet_references_from_connection(
        connection,
        principal_actors=principal_actors,
        owner_actor_id=owner_actor_id,
    )
    return decode_portable_facets(files, brain_id=brain_id, references=references)


def verify_portable_v10_facets(
    connection: sqlite3.Connection,
    *,
    bundle: PortableFacetBundle,
    principal_actors: Mapping[str, str] | None = None,
    owner_actor_id: str | None = None,
) -> PortableFacetBundle:
    """Audit replay/projection/receipts after staged restore or reopen."""
    actual = serialize_portable_v10_facets(
        connection,
        brain_id=bundle.brain_id,
        principal_actors=principal_actors,
        owner_actor_id=owner_actor_id,
    )
    if actual.sidecars != bundle.sidecars:
        raise PortableValidationError("Portable v10 facet restored evidence mismatch")
    return actual


__all__ = [
    "FACET_TABLES",
    "facet_references_from_connection",
    "require_facet_snapshot",
    "serialize_portable_v10_facets",
    "serialize_portable_v10_state",
    "verify_portable_v10_facets",
]


def serialize_portable_v10_state(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
    *,
    relationship_sidecar_present: bool,
) -> PortableV5StateEvidence:
    """Cut inherited historical/custody evidence and facets from one schema15 snapshot."""
    from .portable_v9_authority import _serialize_historical_state

    identity = connection.execute(
        "SELECT brain_id FROM brain_identity WHERE singleton=1"
    ).fetchone()
    if identity is None:
        raise PortableValidationError("Portable v10 requires the admitted Brain identity")
    require_facet_snapshot(connection, brain_id=identity[0])
    base = _serialize_historical_state(
        connection,
        profile,
        relationship_sidecar_present=relationship_sidecar_present,
        portable_version=9,
        expected_schema=15,
    )
    facets = serialize_portable_v10_facets(
        connection, brain_id=identity[0], owner_actor_id=profile.owner_actor_id
    )
    sidecars = dict(base.sidecars)
    sidecars.update(facets.sidecars)
    semantic = dict(base.semantic_state)
    semantic["facets"] = {path: json.loads(raw) for path, raw in facets.sidecars.items()}
    return PortableV5StateEvidence(
        sidecars=MappingProxyType(sidecars),
        semantic_state=MappingProxyType(semantic),
        semantic_state_sha256=sha256(canonical(semantic)).hexdigest(),
    )

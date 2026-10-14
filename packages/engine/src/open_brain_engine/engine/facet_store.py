"""Semantic facet row and ledger helpers, always inside the caller's transaction."""

from __future__ import annotations

import json
import sqlite3
import unicodedata
from hashlib import sha256
from typing import Any, cast
from uuid import uuid4

from open_brain_engine.core.ids import portable_canonical_json_bytes

from .facet_contracts import FacetError
from .facet_evidence import validate_evidence
from .t03_contracts import EffectiveAuthority

ROW_TABLES = {
    "dimension": ("facet_dimensions", ("dimension_id",)),
    "value": ("facet_values", ("value_id",)),
    "subject": ("facet_subject_versions", ("subject_kind", "subject_id")),
    "assignment": ("facet_assignments", ("subject_kind", "subject_id", "value_id")),
    "query": ("facet_queries", ("query_id",)),
    "attachment": ("facet_page_queries", ("page_id",)),
}


def name_key(name: str) -> str:
    return unicodedata.normalize("NFC", name.casefold())


def semantic_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    value = dict(row)
    value.pop("name_key", None)
    if "selector_json" in value:
        value["selector"] = json.loads(value.pop("selector_json"))
    for key in ("archived", "present"):
        if key in value:
            value[key] = bool(value[key])
    return value


def row_key(kind: str, row: dict[str, Any]) -> object:
    if kind == "subject":
        return {"kind": row["subject_kind"], "id": row["subject_id"]}
    if kind == "assignment":
        return {key: row[key] for key in ROW_TABLES[kind][1]}
    return row[ROW_TABLES[kind][1][0]]


def get_row(connection: sqlite3.Connection, kind: str, identity: object) -> dict[str, Any] | None:
    table, keys = ROW_TABLES[kind]
    parameters: tuple[object, ...]
    if kind == "subject":
        assert isinstance(identity, dict)
        parameters = (identity["kind"], identity["id"])
    elif kind == "assignment":
        assert isinstance(identity, dict)
        parameters = tuple(identity[key] for key in keys)
    else:
        parameters = (identity,)
    row = connection.execute(
        f"SELECT * FROM {table} WHERE " + " AND ".join(f"{key}=?" for key in keys),
        parameters,
    ).fetchone()
    return None if row is None else semantic_row(row)


def write_row(connection: sqlite3.Connection, kind: str, row: dict[str, Any]) -> None:
    """Project complete validated semantic fields; derive lookup keys on every write."""
    table, keys = ROW_TABLES[kind]
    value = dict(row)
    if "name" in value:
        value["name_key"] = name_key(value["name"])
    if "selector" in value:
        value["selector_json"] = portable_canonical_json_bytes(value.pop("selector")).decode()
    columns = tuple(value)
    updates = ",".join(f"{key}=excluded.{key}" for key in columns if key not in keys)
    connection.execute(
        f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) "
        f"ON CONFLICT({','.join(keys)}) DO UPDATE SET {updates}",
        tuple(value.values()),
    )


def change(kind: str, before: dict[str, Any] | None, after: dict[str, Any]) -> dict[str, Any]:
    return {"entity": {"kind": kind, "id": row_key(kind, after)}, "before": before, "after": after}


def next_sequence(connection: sqlite3.Connection) -> int:
    sequence = int(
        connection.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM facet_events").fetchone()[0]
    )
    if sequence > 2**53 - 1:
        raise FacetError("limit_exceeded")
    return sequence


def replay_receipt(
    connection: sqlite3.Connection,
    *,
    method: str,
    arguments: dict[str, Any],
    principal_id: str,
) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM facet_operations WHERE operation_id=?",
        (arguments["operation_id"],),
    ).fetchone()
    if row is None:
        return None
    digest = sha256(
        portable_canonical_json_bytes({"method": method, "arguments": arguments})
    ).hexdigest()
    if row["principal_id"] != principal_id or row["request_sha256"] != digest:
        raise FacetError("idempotency_conflict")
    return cast(dict[str, Any], json.loads(row["receipt_json"]))


def commit_mutation(
    connection: sqlite3.Connection,
    *,
    brain_id: str,
    method: str,
    arguments: dict[str, Any],
    authority: EffectiveAuthority,
    actor_id: str,
    recorded_at: str,
    changes: list[dict[str, Any]],
    subject_id: str,
    version: int,
) -> dict[str, Any]:
    """Append state/event/receipt together; a no-op persists only its receipt."""
    event_id = "facetevent_" + str(uuid4()) if changes else None
    receipt = {
        "dto_version": 1,
        "status": "applied" if changes else "unchanged",
        "operation_id": arguments["operation_id"],
        "subject_id": subject_id,
        "version": version,
        "event_id": event_id,
    }
    if changes:
        sequence = next_sequence(connection)
        event = {
            "schema_version": 1,
            "brain_id": brain_id,
            "event_id": event_id,
            "actor_id": actor_id,
            "operation_id": arguments["operation_id"],
            "sequence": sequence,
            "recorded_at": recorded_at,
            "kind": "query.attach"
            if method == "facet.query.attach.publication"
            else method.removeprefix("facet."),
            "changes": changes,
        }
        validate_evidence("event", event)
        for item in changes:
            write_row(connection, item["entity"]["kind"], item["after"])
        connection.execute(
            "INSERT INTO facet_events VALUES(?,?,?,?,?,?,?)",
            (
                event_id,
                sequence,
                arguments["operation_id"],
                actor_id,
                recorded_at,
                event["kind"],
                portable_canonical_json_bytes(event).decode(),
            ),
        )
        connection.execute(
            "UPDATE engine_generations SET retrieval_generation=retrieval_generation+1 "
            "WHERE singleton=1"
        )
    request = {"method": method, "arguments": arguments}
    operation = {
        "operation_id": arguments["operation_id"],
        "principal_id": authority.principal_id,
        "request_sha256": sha256(portable_canonical_json_bytes(request)).hexdigest(),
        "request_json": request,
        "receipt_json": receipt,
        "event_id": event_id,
    }
    validate_evidence("operation", operation)
    connection.execute(
        "INSERT INTO facet_operations VALUES(?,?,?,?,?,?)",
        (
            arguments["operation_id"],
            authority.principal_id,
            operation["request_sha256"],
            portable_canonical_json_bytes(request).decode(),
            portable_canonical_json_bytes(receipt).decode(),
            event_id,
        ),
    )
    return receipt


def semantic_state(connection: sqlite3.Connection, brain_id: str) -> dict[str, Any]:
    value: dict[str, Any] = {"schema_version": 1, "brain_id": brain_id}
    for kind, target in (
        ("dimension", "dimensions"),
        ("value", "values"),
        ("subject", "subjects"),
        ("assignment", "assignments"),
        ("query", "queries"),
        ("attachment", "page_queries"),
    ):
        table, keys = ROW_TABLES[kind]
        value[target] = [
            semantic_row(row)
            for row in connection.execute(f"SELECT * FROM {table} ORDER BY {','.join(keys)}")
        ]
    return value


def events_sidecar(connection: sqlite3.Connection, brain_id: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "brain_id": brain_id,
        "events": [
            json.loads(row[0])
            for row in connection.execute("SELECT event_json FROM facet_events ORDER BY sequence")
        ],
    }


def operations_sidecar(connection: sqlite3.Connection, brain_id: str) -> dict[str, Any]:
    operations = []
    for row in connection.execute("SELECT * FROM facet_operations ORDER BY operation_id"):
        value = dict(row)
        for key in ("request_json", "receipt_json"):
            value[key] = json.loads(value[key])
        operations.append(value)
    return {"schema_version": 1, "brain_id": brain_id, "operations": operations}


def attach_for_publication(
    connection: sqlite3.Connection,
    *,
    brain_id: str,
    authority: EffectiveAuthority,
    actor_id: str,
    recorded_at: str,
    publication_id: str,
    page_id: str,
    query_id: str,
    query_selection_version: int,
) -> dict[str, Any]:
    """Internal P stage seam. Caller already owns the admitted publication transaction.

    This is deliberately absent from FacetTasks and every public wire registry.
    The publication stage must prove its page/draft/destination/authority bindings.
    """
    operation_id = "publication-facet-" + sha256(publication_id.encode()).hexdigest()
    arguments = {
        "operation_id": operation_id,
        "page_id": page_id,
        "query_id": query_id,
        "query_selection_version": query_selection_version,
        "publication_id": publication_id,
    }
    receipt = replay_receipt(
        connection,
        method="facet.query.attach.publication",
        arguments=arguments,
        principal_id=authority.principal_id,
    )
    if receipt is not None:
        return receipt
    query = get_row(connection, "query", query_id)
    if query is None or query["archived"] or query["selection_version"] != query_selection_version:
        raise FacetError("review_conflict")
    if get_row(connection, "attachment", page_id) is not None:
        raise FacetError("review_conflict")
    if (
        connection.execute(
            "SELECT 1 FROM decisions WHERE publication_id=? AND page_id=? "
            "AND outcome IN ('approved','edited')",
            (publication_id, page_id),
        ).fetchone()
        is None
    ):
        raise FacetError("review_conflict")
    after = {"page_id": page_id, "query_id": query_id, "version": 1}
    return commit_mutation(
        connection,
        brain_id=brain_id,
        method="facet.query.attach.publication",
        arguments=arguments,
        authority=authority,
        actor_id=actor_id,
        recorded_at=recorded_at,
        changes=[change("attachment", None, after)],
        subject_id=page_id,
        version=1,
    )

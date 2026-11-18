"""Recover only the requesting workflow's currently visible browser intents."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from itertools import islice

from .browser_annotations import uuid4_id
from .browser_filing import filing_row, recover_filing_operation
from .browser_pages import BrowserPages, _operation_key
from .local_schema import open_local_database_read_only

_SCOPE = (
    "brain_id=? AND workflow_principal=? AND connector_name=? AND connection_id=? AND resource_id=?"
)
_ROUTES = "route_operations"


def _families(schema: int) -> list[str]:
    families = ["browser_operations"]
    if schema >= 26:
        families.append("browser_annotations")
    if schema >= 27:
        families.append("browser_revisits")
    if schema >= 26:
        families.append(_ROUTES)
    return families


def _position(
    db: sqlite3.Connection, grant: sqlite3.Row, families: list[str], after: str
) -> tuple[int, str] | None:
    scope = _operation_key(grant, after)
    for index, family in enumerate(families):
        if family == _ROUTES:
            row = filing_row(db, grant, after)
            if row:
                return index, row["route_id"]
        elif db.execute(
            f"SELECT 1 FROM {family} WHERE {_SCOPE} AND operation_id=?",
            scope,
        ).fetchone():
            return index, after
    return None


def _route_candidates(db: sqlite3.Connection, grant: sqlite3.Row, key: str) -> Iterator[str]:
    # Original browser route deliveries contain the route ID in lexical order.
    # Restores reconstruct delivery IDs; only that case needs the route-ID index
    # across imported history. Skip unrelated local owner routes in either case.
    imported = db.execute(
        "SELECT 1 FROM route_operations WHERE delivery_id GLOB 'import.route.*' LIMIT 1"
    ).fetchone()
    if imported:
        order = "r.route_id"
        index_hint = "INDEXED BY route_identity_idx"
        predicate = (
            "r.route_id>? AND (r.delivery_id GLOB 'browser.file.route_*' "
            "OR r.delivery_id GLOB 'import.route.*')"
        )
    else:
        order = "r.delivery_id"
        index_hint = ""
        predicate = "r.delivery_id>? AND r.delivery_id GLOB 'browser.file.route_*'"
        key = "browser.file." + key
    source = (grant["connector_name"], grant["connection_id"], grant["resource_id"])
    sql = (
        f"SELECT r.*,c.delivery_id AS parent_delivery FROM route_operations r {index_hint} "
        "JOIN captures c ON r.capture_id=c.capture_id WHERE "
        + predicate
        + " AND c.delivery_id GLOB 'connector.saved_item.*' AND c.stage=3 "
        "AND c.source_origin='third_party' AND c.submission_path='public_job' "
        "AND NOT EXISTS (SELECT 1 FROM saved_item_local_witnesses w "
        "WHERE w.delivery_id=c.delivery_id "
        "AND (w.connector_name,w.connection_id,w.resource_id)!=(?,?,?)) "
        "AND NOT EXISTS (SELECT 1 FROM browser_operations o WHERE o.delivery_id=c.delivery_id "
        "AND (o.connector_name,o.connection_id,o.resource_id)!=(?,?,?)) "
        "ORDER BY " + order
    )
    for row in db.execute(sql, (key, *source, *source)):
        operation = recover_filing_operation(grant, row, row["parent_delivery"])
        if operation is not None:
            yield operation


def _candidates(
    db: sqlite3.Connection, grant: sqlite3.Row, families: list[str], position: tuple[int, str]
) -> Iterator[str]:
    family_index, key = position
    scope = _operation_key(grant, "")[:-1]
    for family in families[family_index:]:
        if family == _ROUTES:
            yield from _route_candidates(db, grant, key)
        else:
            states = (
                "('accepted','pending')"
                if family == "browser_operations" and grant["complete_domain"]
                else "('accepted')"
            )
            # Read the composite operation-key index lazily. Invalid historical
            # identifiers did not originate in the Chrome v4 mutation grammar.
            for row in db.execute(
                f"SELECT operation_id FROM {family} WHERE {_SCOPE} AND operation_id>? "
                f"AND state IN {states} ORDER BY operation_id",
                (*scope, key),
            ):
                try:
                    operation = uuid4_id(row[0])
                except ValueError, TypeError, AttributeError:
                    continue
                yield operation
        key = ""


def list_intents(
    pages: BrowserPages, token: str, after: str | None, limit: int
) -> dict[str, object]:
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("invalid recovery page")
    if after is not None:
        uuid4_id(after)
    with closing(open_local_database_read_only(pages.profile)) as db:
        if not db.in_transaction:
            db.execute("BEGIN")
        grant = pages._grant(db, token, "status")
        families = _families(db.execute("PRAGMA user_version").fetchone()[0])
        position = (0, "")
        if after is not None:
            found = _position(db, grant, families, after)
            if found is None or pages._result_in_snapshot(db, grant, after, "status").state not in {
                "accepted",
                "pending",
            }:
                return {"intents": [], "next_after": None}
            position = found

        def visible() -> Iterator[dict[str, str]]:
            for op in _candidates(db, grant, families, position):
                result = pages._result_in_snapshot(db, grant, op, "status")
                if result.state in {"accepted", "pending"}:
                    yield {"id": op, "state": result.state}

        rows = list(islice(visible(), limit + 1))
        return {
            "intents": rows[:limit],
            "next_after": rows[limit - 1]["id"] if len(rows) > limit else None,
        }

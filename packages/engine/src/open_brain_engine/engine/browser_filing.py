"""Scoped browser filing using the existing portable route and receipt records."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
from typing import cast
from uuid import UUID

from .browser_annotations import operation_row, uuid4_id
from .browser_pages import BrowserPages, BrowserResult, _operation, _operation_key
from .contracts import project_public_result_text
from .local_schema import open_local_database_read_only
from .normalization import _portable_id
from .saved_item_identity import SavedItemSource


def _source(grant: sqlite3.Row) -> SavedItemSource:
    return SavedItemSource(grant["connector_name"], grant["connection_id"], grant["resource_id"])


def _id(kind: str, key: tuple[str, ...]) -> str:
    # Historical Portable route IDs require the v4 UUID layout. Domain separation
    # keeps the two IDs distinct and leaves the workflow principal and token private.
    data = json.dumps(("browser.file.v1", kind, *key), separators=(",", ":")).encode()
    return f"{kind}_{UUID(bytes=sha256(data).digest()[:16], version=4)}"


def _receipt_uuid(
    value: str, scope: tuple[str, ...], delivery: str, capture: str, space: str
) -> str:
    # XOR over the variable UUID bits is reversible. A separately derived route
    # ID verifies the recovered operation for this exact source and principal.
    mask = UUID(_id("receipt", (*scope, delivery, capture, space)).removeprefix("receipt_"))
    return str(UUID(int=UUID(value).int ^ mask.int, version=4))


def recover_filing_operation(
    grant: sqlite3.Row, row: sqlite3.Row, parent_delivery: str
) -> str | None:
    """Recover a candidate ID from portable history. Call filing_status before exposing it."""
    scope = _operation_key(grant, "")[:-1]
    op = _receipt_uuid(
        row["receipt_id"].removeprefix("receipt_"),
        scope,
        parent_delivery,
        row["capture_id"],
        row["space_id"],
    )
    return op if row["route_id"] == _id("route", (*scope, op)) else None


def _permits(grant: sqlite3.Row, space: str) -> bool:
    return bool(grant["complete_domain"] or space in json.loads(grant["spaces_json"]))


def filing_row(db: sqlite3.Connection, grant: sqlite3.Row, operation_id: str) -> sqlite3.Row | None:
    row = db.execute(
        "SELECT * FROM route_operations WHERE route_id=?",
        (_id("route", _operation_key(grant, operation_id)),),
    ).fetchone()
    return cast(sqlite3.Row | None, row)


@dataclass(frozen=True)
class BrowserRouteAdmission:
    pages: BrowserPages
    token: str
    operation_id: str
    item_key: str

    def admit(
        self,
        db: sqlite3.Connection,
        capture_id: str,
        space_id: str,
    ) -> tuple[str, str]:
        grant = self.pages._grant(db, self.token, "save")
        source = _source(grant)
        delivery = source.delivery_id(self.item_key)
        page = self.pages._visible(db, grant, delivery)
        if (
            page is None
            or page.capture_id != capture_id
            or not _permits(grant, space_id)
            or not db.execute("SELECT 1 FROM spaces WHERE space_id=?", (space_id,)).fetchone()
        ):
            raise PermissionError("browser_operation_denied")
        if _operation(db, grant, self.operation_id) or operation_row(db, grant, self.operation_id):
            raise ValueError("browser_operation_conflict")
        source.witness(db, self.item_key)
        key = _operation_key(grant, self.operation_id)
        return _id("route", key), "receipt_" + _receipt_uuid(
            self.operation_id, key[:-1], delivery, capture_id, space_id
        )


def filing_status(
    pages: BrowserPages, db: sqlite3.Connection, grant: sqlite3.Row, operation_id: str
) -> BrowserResult | None:
    row = filing_row(db, grant, operation_id)
    if row is None:
        return None
    space, capture = row["space_id"], row["capture_id"]
    if not _permits(grant, space):
        return BrowserResult("refused")
    parent = db.execute(
        "SELECT source_reference,delivery_id FROM captures WHERE capture_id=?", (capture,)
    ).fetchone()
    if parent is None:
        return BrowserResult("refused")
    delivery = parent["delivery_id"]
    if recover_filing_operation(grant, row, delivery) != operation_id:
        return BrowserResult("refused")
    source = _source(grant)
    # A legacy fragment may be absent from the stored URL. Its source can also
    # be proved by a local witness or a portable page/annotation receipt.
    item_keys = db.execute(
        "SELECT item_key FROM saved_item_local_witnesses WHERE delivery_id=? "
        "UNION SELECT item_key FROM browser_operations WHERE delivery_id=? "
        "UNION SELECT item_key FROM browser_annotations WHERE capture_id=?",
        (delivery, delivery, capture),
    )
    fallback = sha256(parent["source_reference"].encode()).hexdigest()[:32]
    if source.delivery_id(fallback) != delivery and not any(
        source.delivery_id(item[0]) == delivery for item in item_keys
    ):
        return BrowserResult("refused")
    page = pages._visible(db, grant, delivery)
    if page is None or page.capture_id != capture:
        return BrowserResult("refused")
    if row["stage"] != 2:
        return BrowserResult("pending")
    return BrowserResult("accepted", capture_id=capture, space_id=space)


class BrowserFiling(BrowserPages):
    def file(
        self, token: str, operation_id: str, item_key: str, capture_id: str, space_id: str
    ) -> BrowserResult:
        from .local import BrainEngine

        _portable_id(space_id, "space")
        admission = BrowserRouteAdmission(self, token, uuid4_id(operation_id), item_key)
        engine = BrainEngine.open(self.profile, recover_on_open=False)
        denied: PermissionError | None = None
        with engine._writer_lease.acquire_shared_writer():
            try:
                engine._route_capture(capture_id, space_id, operation_id, browser=admission)
            except PermissionError as error:
                # Filesystem leases wrap OSError; retain the verified grant refusal.
                denied = error
        if denied is not None:
            raise denied
        with closing(open_local_database_read_only(self.profile)) as db:
            result = filing_status(self, db, self._grant(db, token, "save"), operation_id)
            return result or BrowserResult("refused")

    def spaces(
        self, token: str, item_key: str, capture_id: str, *, limit: int, offset: int
    ) -> dict[str, object]:
        if (
            type(limit) is not int
            or not 1 <= limit <= 100
            or type(offset) is not int
            or not 0 <= offset < 2**31
        ):
            raise ValueError("invalid browser page")
        with closing(open_local_database_read_only(self.profile)) as db:
            grant = self._grant(db, token, "lookup")
            page = self._visible(db, grant, _source(grant).delivery_id(item_key))
            if page is None or page.capture_id != capture_id:
                raise PermissionError("browser_operation_denied")
            rows = db.execute(
                "SELECT space_id,name FROM spaces WHERE ? OR space_id IN "
                "(SELECT value FROM json_each(?)) ORDER BY name,space_id LIMIT ? OFFSET ?",
                (grant["complete_domain"], grant["spaces_json"], limit + 1, offset),
            ).fetchall()
            current = None
            if page.space_id is not None:
                row = db.execute(
                    "SELECT name FROM spaces WHERE space_id=?", (page.space_id,)
                ).fetchone()
                if row:
                    current = {
                        "space_id": page.space_id,
                        "name": project_public_result_text(row[0]),
                    }
            return {
                "spaces": [
                    {"space_id": r[0], "name": project_public_result_text(r[1])}
                    for r in rows[:limit]
                ],
                "next_offset": offset + limit if len(rows) > limit else None,
                "current_space": current,
            }

"""Calendar-day revisit intents, scoped to each saved page's current visibility."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing
from datetime import date, datetime
from hashlib import sha256
from typing import cast
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .browser_annotations import operation_row, uuid4_id
from .browser_filing import filing_row
from .browser_mutation import require_child_write
from .browser_pages import BrowserPages, BrowserResult, _operation, _operation_key
from .contracts import LocalEngineContext, project_public_result_text
from .local_schema import open_local_database_read_only
from .local_store import _LocalStore
from .records import RecordProjector
from .saved_item_identity import SavedItemSource
from .t03_contracts import EffectiveAuthority, T03Error


def safe_revisit_url(url: object) -> bool:
    if not isinstance(url, str) or not url.startswith("https://") or "#" in url or "\\" in url:
        return False
    try:
        parts = urlsplit(url)
        return bool(
            parts.hostname
            and parts.username is None
            and parts.password is None
            and len(json.dumps(url)) <= 4000
            and project_public_result_text(url) == url
            and all(not c.isspace() and ord(c) >= 32 for c in url)
        )
    except ValueError:
        return False


def validate_calendar(day: str, zone: str, *, require_installed: bool) -> None:
    if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
        raise ValueError("invalid revisit date")
    if (
        not isinstance(zone, str)
        or len(zone) > 255
        or re.fullmatch(r"[A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+-]+)*", zone) is None
        or zone.split("/")[0] in {"posix", "right", "localtime", "posixrules"}
    ):
        raise ValueError("invalid revisit zone")
    if require_installed:
        try:
            ZoneInfo(zone)
        except ZoneInfoNotFoundError:
            raise ValueError("unavailable revisit zone") from None


def date_is_due(day: str, zone: str, now: datetime) -> bool:
    if now.tzinfo is None:
        raise ValueError("an aware clock is required")
    try:
        local = now.astimezone(ZoneInfo(zone))
    except ZoneInfoNotFoundError:
        return False
    return day <= local.date().isoformat()


def revisit_row(
    db: sqlite3.Connection, grant: sqlite3.Row, operation_id: str
) -> sqlite3.Row | None:
    if db.execute("PRAGMA user_version").fetchone()[0] < 27:
        return None
    row = db.execute(
        "SELECT * FROM browser_revisits WHERE brain_id=? AND workflow_principal=? AND "
        "connector_name=? AND connection_id=? AND resource_id=? AND operation_id=?",
        _operation_key(grant, operation_id),
    ).fetchone()
    return cast(sqlite3.Row | None, row)


def result_for(
    pages: BrowserPages, db: sqlite3.Connection, grant: sqlite3.Row, row: sqlite3.Row
) -> BrowserResult:
    expected = SavedItemSource(
        grant["connector_name"], grant["connection_id"], grant["resource_id"]
    )
    if row["state"] == "accepted" and expected.delivery_id(row["item_key"]) == row["delivery_id"]:
        page = pages._visible(db, grant, row["delivery_id"])
        if page and page.capture_id == row["capture_id"]:
            return BrowserResult(
                "accepted", capture_id=page.capture_id, revisit_action=row["action"]
            )
    return BrowserResult("refused")


def _due_rows(
    db: sqlite3.Connection,
    now: datetime,
    visible: Callable[[sqlite3.Row], bool],
    *,
    capture_id: str | None = None,
    source: SavedItemSource | None = None,
) -> Iterator[dict[str, str]]:
    if db.execute("PRAGMA user_version").fetchone()[0] < 27:
        return
    clauses = [
        "r.action='set'",
        "r.state='accepted'",
        "s.lifecycle='active'",
        "s.availability='available'",
        "s.historical_only=0",
        "NOT EXISTS (SELECT 1 FROM browser_revisits n WHERE n.capture_id=r.capture_id "
        "AND n.sequence>r.sequence)",
    ]
    params: list[str] = []
    if capture_id is not None:
        clauses.append("r.capture_id=?")
        params.append(capture_id)
    if source is not None:
        clauses.append("(r.connector_name,r.connection_id,r.resource_id)=(?,?,?)")
        params.extend(source.values())
    sql = (
        "SELECT r.*,s.space_id FROM browser_revisits r JOIN logical_sources s "
        "ON s.head_capture_id=r.capture_id WHERE "
        + " AND ".join(clauses)
        + " ORDER BY r.local_date,r.capture_id"
    )
    for row in db.execute(sql, params):
        if date_is_due(row["local_date"], row["zone"], now) and visible(row):
            yield {key: row[key] for key in ("capture_id", "local_date", "zone")}


def due_for_authority(
    profile: LocalEngineContext,
    db: sqlite3.Connection,
    authority: EffectiveAuthority,
    *,
    now: datetime,
    limit: int,
    space_ids: frozenset[str] | None = None,
    capture_id: str | None = None,
) -> list[dict[str, str]]:
    from itertools import islice

    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("invalid revisit limit")
    projector = RecordProjector(profile, db, authority)

    def visible(row: sqlite3.Row) -> bool:
        if space_ids is not None and row["space_id"] not in space_ids:
            return False
        try:
            projector.source(row["capture_id"], expected=row["capture_id"])
        except T03Error as error:
            if error.code == "not_found":
                return False
            raise
        return True

    return list(islice(_due_rows(db, now, visible, capture_id=capture_id), limit))


class BrowserRevisits(BrowserPages):
    @staticmethod
    def _source(grant: sqlite3.Row) -> SavedItemSource:
        return SavedItemSource(
            grant["connector_name"], grant["connection_id"], grant["resource_id"]
        )

    def mutate(
        self,
        token: str,
        operation_id: str,
        item_key: str,
        capture_id: str,
        action: str,
        *,
        local_date: str | None = None,
        zone: str | None = None,
    ) -> BrowserResult:
        if action not in {"set", "dismiss", "clear"}:
            raise ValueError("invalid revisit action")
        if action == "set":
            if local_date is None or zone is None:
                raise ValueError("missing revisit calendar")
            validate_calendar(local_date, zone, require_installed=False)
        elif local_date is not None or zone is not None:
            raise ValueError("unexpected revisit calendar")
        uuid4_id(operation_id)
        with _LocalStore(self.profile, initialize=False).transaction() as db:
            grant = self._grant(db, token, "save")
            require_child_write(db, grant)
            delivery = self._source(grant).delivery_id(item_key)
            page = self._visible(db, grant, delivery)
            if page is None or page.capture_id != capture_id:
                raise PermissionError("browser_operation_denied")
            digest = sha256(
                json.dumps(
                    {
                        "action": action,
                        "item_key": item_key,
                        "capture_id": capture_id,
                        "local_date": local_date,
                        "zone": zone,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            prior = revisit_row(db, grant, operation_id)
            if prior is not None:
                if prior["request_sha256"] != digest:
                    raise ValueError("browser_operation_conflict")
                return result_for(self, db, grant, prior)
            if (
                _operation(db, grant, operation_id)
                or operation_row(db, grant, operation_id)
                or filing_row(db, grant, operation_id)
            ):
                raise ValueError("browser_operation_conflict")
            if action == "set":
                assert local_date is not None and zone is not None
                validate_calendar(local_date, zone, require_installed=True)
            db.execute(
                "INSERT INTO browser_revisits VALUES(NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,'accepted')",
                (
                    *_operation_key(grant, operation_id),
                    digest,
                    delivery,
                    item_key,
                    capture_id,
                    action,
                    local_date,
                    zone,
                ),
            )
            return BrowserResult("accepted", capture_id=capture_id, revisit_action=action)

    def get(self, token: str, item_key: str, capture_id: str) -> dict[str, object]:
        with closing(open_local_database_read_only(self.profile)) as db:
            grant = self._grant(db, token, "lookup")
            page = self._visible(db, grant, self._source(grant).delivery_id(item_key))
            if page is None or page.capture_id != capture_id:
                raise PermissionError("browser_operation_denied")
            current = db.execute(
                "SELECT action,state FROM browser_revisits WHERE capture_id=? "
                "ORDER BY sequence DESC LIMIT 1",
                (capture_id,),
            ).fetchone()
            if not current or current["action"] == "clear" or current["state"] != "accepted":
                return {"revisit": None}
            calendar = db.execute(
                "SELECT action,local_date,zone,state FROM browser_revisits WHERE capture_id=? "
                "AND action IN ('set','clear') ORDER BY sequence DESC LIMIT 1",
                (capture_id,),
            ).fetchone()
            if not calendar or calendar["action"] != "set" or calendar["state"] != "accepted":
                return {"revisit": None}
            state = "dismissed" if current["action"] == "dismiss" else "scheduled"
            try:
                ZoneInfo(calendar["zone"])
            except ZoneInfoNotFoundError:
                state = "zone_unavailable"
            return {
                "revisit": {
                    "local_date": calendar["local_date"],
                    "zone": calendar["zone"],
                    "state": state,
                }
            }

    def due(self, token: str, *, now: datetime, limit: int, offset: int) -> dict[str, object]:
        from itertools import islice

        if (
            type(limit) is not int
            or not 1 <= limit <= 20
            or type(offset) is not int
            or not 0 <= offset < 2**31
        ):
            raise ValueError("invalid revisit page")
        with closing(open_local_database_read_only(self.profile)) as db:
            grant = self._grant(db, token, "lookup")
            source = self._source(grant)

            def visible(row: sqlite3.Row) -> bool:
                if source.delivery_id(row["item_key"]) != row["delivery_id"]:
                    return False
                page = self._visible(db, grant, row["delivery_id"])
                return page is not None and page.capture_id == row["capture_id"]

            rows = list(
                islice(_due_rows(db, now, visible, source=source), offset, offset + limit + 1)
            )
            for row in rows[:limit]:
                title, reference = db.execute(
                    "SELECT title,source_reference FROM captures WHERE capture_id=?",
                    (row["capture_id"],),
                ).fetchone()
                url = str(reference).partition("#")[0]
                row["title"] = (
                    project_public_result_text(title or url.split("?", 1)[0])[:300] or "Saved page"
                )
                if safe_revisit_url(url):
                    row["url"] = url
            return {
                "revisits": rows[:limit],
                "next_offset": offset + limit if len(rows) > limit else None,
            }

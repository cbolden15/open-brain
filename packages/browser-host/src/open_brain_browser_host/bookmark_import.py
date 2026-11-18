"""Owner-private staging and replayable, bounded saved-item bookmark imports."""

from __future__ import annotations

import base64
import binascii
import os
import re
import sqlite3
import stat
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

from open_brain_connectors.runtime.saved_item import MAX_WHY_CHARS, SavedItemRefusal, parse_record

from .policy import Refused, _home_directory, encoded

_OPERATIONS = {
    "import.begin": {"format", "reason", "bytes", "digest", "chunks"},
    "import.chunk": {"batch_id", "index", "data"},
    "import.preview": {"batch_id", "offset", "limit"},
    "import.confirm": {"batch_id", "digest"},
    "import.step": {"batch_id"},
    "import.status": {"batch_id", "offset", "limit"},
    "import.cancel": {"batch_id"},
}
OPERATIONS = frozenset(_OPERATIONS)
CHUNK_BYTES = 48 * 1024
MAX_BYTES = 1024 * 1024
MAX_ROWS = 1_000
_MAX_BATCHES = 4
_IDLE = 30 * 60
_LIFETIME = 60 * 60
_STEP_ROWS = 4
_STAGING = (".open-brain-browser", "imports")
_DB_NAME = "bookmarks.sqlite3"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
 batch_id TEXT PRIMARY KEY, binding_id TEXT NOT NULL, principal TEXT NOT NULL,
 state TEXT NOT NULL, format TEXT NOT NULL, reason TEXT, declared_size INTEGER NOT NULL,
 upload_digest TEXT NOT NULL, chunk_count INTEGER NOT NULL, preview_digest TEXT,
 created INTEGER NOT NULL, touched INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS imports_quota ON batches(principal,state);
CREATE TABLE IF NOT EXISTS chunks (
 batch_id TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE, idx INTEGER NOT NULL,
 digest TEXT NOT NULL, body BLOB, PRIMARY KEY(batch_id,idx)
);
CREATE TABLE IF NOT EXISTS rows (
 batch_id TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE, idx INTEGER NOT NULL,
 operation_id TEXT NOT NULL, url TEXT, display TEXT, state TEXT NOT NULL,
 PRIMARY KEY(batch_id,idx)
);
"""


def _number(value: object, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


def _digest(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def parse_arguments(operation: str, args: dict[str, Any]) -> dict[str, Any]:
    if set(args) != _OPERATIONS[operation]:
        raise Refused("invalid_request")
    if operation == "import.begin":
        if type(args["bytes"]) is int and args["bytes"] > MAX_BYTES:
            raise Refused("import_too_large")
        if not _number(args["bytes"], 1, MAX_BYTES):
            raise Refused("invalid_request")
        expected = (args["bytes"] + CHUNK_BYTES - 1) // CHUNK_BYTES
        reason = args["reason"]
        if (
            args["format"] not in ("html", "urls")
            or not isinstance(reason, str)
            or not reason.strip()
            or len(reason) > MAX_WHY_CHARS
            or "\x00" in reason
            or not _digest(args["digest"])
            or not _number(args["chunks"], expected, expected)
        ):
            raise Refused("invalid_request")
        try:
            reason.encode("utf-8")
        except UnicodeError:
            raise Refused("invalid_request") from None
        return args
    batch = args["batch_id"]
    try:
        if not isinstance(batch, str) or str(UUID(batch)) != batch or UUID(batch).version != 4:
            raise ValueError
    except ValueError:
        raise Refused("invalid_request") from None
    if operation == "import.chunk":
        data = args["data"]
        if (
            not isinstance(data, str)
            or len(data) > 4 * ((CHUNK_BYTES + 2) // 3)
            or not _number(args["index"], 0, (MAX_BYTES - 1) // CHUNK_BYTES)
        ):
            raise Refused("invalid_request")
        try:
            body = base64.b64decode(data, validate=True)
        except ValueError, binascii.Error:
            raise Refused("invalid_request") from None
        if not 0 < len(body) <= CHUNK_BYTES or base64.b64encode(body).decode("ascii") != data:
            raise Refused("invalid_request")
        return {**args, "data": body}
    if operation in {"import.preview", "import.status"} and (
        not _number(args["offset"], 0, MAX_ROWS) or not _number(args["limit"], 1, 30)
    ):
        raise Refused("invalid_request")
    if operation == "import.confirm" and not _digest(args["digest"]):
        raise Refused("invalid_request")
    return args


class _Bookmarks(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.urls.append(next((value or "" for name, value in attrs if name == "href"), ""))
            if len(self.urls) > MAX_ROWS:
                raise Refused("import_too_large")


def _input_rows(raw: bytes, format: str, reason: str) -> list[tuple[str | None, str, str]]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError:
        raise Refused("import_invalid_encoding") from None
    if format == "html":
        parser = _Bookmarks()
        parser.feed(text)
        parser.close()
        urls = parser.urls
    else:
        urls = [line.strip() for line in text.splitlines() if line.strip()]
        if len(urls) > MAX_ROWS:
            raise Refused("import_too_large")
    rows: list[tuple[str | None, str, str]] = []
    observed: set[str] = set()
    for url in urls:
        try:
            record = parse_record(
                encoded(
                    {
                        "schema_version": 1,
                        "url": url,
                        "why": reason,
                        "received_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                        "fetch_status": "not_attempted",
                    }
                )
            )
        except SavedItemRefusal:
            rows.append((None, "", "skipped"))
            continue
        if record.identity_url in observed:
            rows.append((None, "", "duplicate"))
            continue
        observed.add(record.identity_url)
        parts = urlsplit(record.stored_url)
        display = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))[:512]
        rows.append((record.identity_url, display, "ready"))
    return rows


@contextmanager
def _staging() -> Iterator[sqlite3.Connection]:
    with _home_directory(_STAGING, create=True, private=True) as directory:
        fd = os.open(
            _DB_NAME,
            os.O_NONBLOCK | os.O_NOFOLLOW | os.O_RDWR | os.O_CREAT,
            0o600,
            dir_fd=directory,
        )
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o077
                or info.st_nlink != 1
            ):
                raise Refused("import_staging_unavailable")
            with closing(
                sqlite3.connect(Path.home().joinpath(*_STAGING, _DB_NAME), timeout=2)
            ) as db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA secure_delete=ON")
                db.execute("PRAGMA foreign_keys=ON")
                db.executescript(_SCHEMA)
                yield db
        finally:
            os.close(fd)


def _finalize(db: sqlite3.Connection, batch: sqlite3.Row, chunks: list[sqlite3.Row]) -> None:
    raw = b"".join(chunk["body"] for chunk in chunks)
    if len(raw) != batch["declared_size"] or sha256(raw).hexdigest() != batch["upload_digest"]:
        raise Refused("import_digest_mismatch")
    rows = _input_rows(raw, batch["format"], batch["reason"])
    batch_id = batch["batch_id"]
    manifest = {
        "batch_id": batch_id,
        "binding_id": batch["binding_id"],
        "reason": batch["reason"],
        "input_sha256": batch["upload_digest"],
        "rows": rows,
    }
    db.executemany(
        "INSERT INTO rows VALUES (?, ?, ?, ?, ?, ?)",
        ((batch_id, index, str(uuid4()), *row) for index, row in enumerate(rows)),
    )
    db.execute("UPDATE chunks SET body=NULL WHERE batch_id=?", (batch_id,))
    db.execute(
        "UPDATE batches SET state='preview',preview_digest=? WHERE batch_id=?",
        (sha256(encoded(manifest)).hexdigest(), batch_id),
    )


def _put_chunk(db: sqlite3.Connection, batch: sqlite3.Row, index: int, data: bytes) -> None:
    batch_id = batch["batch_id"]
    count = batch["chunk_count"]
    if index >= count:
        raise Refused("invalid_request")
    expected = min(CHUNK_BYTES, batch["declared_size"] - index * CHUNK_BYTES)
    if len(data) != expected:
        raise Refused("invalid_request")
    digest = sha256(data).hexdigest()
    existing = db.execute(
        "SELECT digest FROM chunks WHERE batch_id=? AND idx=?", (batch_id, index)
    ).fetchone()
    if existing is not None:
        if existing["digest"] != digest:
            raise Refused("import_chunk_conflict")
        return
    if batch["state"] != "upload":
        raise Refused("import_not_ready")
    db.execute("INSERT INTO chunks VALUES (?, ?, ?, ?)", (batch_id, index, digest, data))
    chunks = db.execute(
        "SELECT body FROM chunks WHERE batch_id=? ORDER BY idx", (batch_id,)
    ).fetchall()
    if len(chunks) == count:
        _finalize(db, batch, chunks)


def _progress(db: sqlite3.Connection, batch_id: str) -> dict[str, object]:
    state = db.execute("SELECT state FROM batches WHERE batch_id=?", (batch_id,)).fetchone()[0]
    processed, total = db.execute(
        "SELECT count(*) FILTER(WHERE state NOT IN ('ready','pending')),count(*) "
        "FROM rows WHERE batch_id=?",
        (batch_id,),
    ).fetchone()
    return {"state": state, "processed": processed, "total": total}


def _page(
    db: sqlite3.Connection, batch_id: str, offset: int, limit: int, *, preview: bool
) -> dict[str, object]:
    rows = db.execute(
        "SELECT idx,state,display FROM rows WHERE batch_id=? ORDER BY idx LIMIT ? OFFSET ?",
        (batch_id, limit + 1, offset),
    ).fetchall()
    items = [
        {
            "index": row["idx"],
            "state": row["state"],
            **({"url": row["display"] or ""} if preview else {}),
        }
        for row in rows[:limit]
    ]
    return {"rows": items, "next_offset": offset + limit if len(rows) > limit else None}


def _clear_content(db: sqlite3.Connection, batch_id: str, state: str) -> None:
    db.execute("DELETE FROM chunks WHERE batch_id=?", (batch_id,))
    db.execute("UPDATE rows SET url=NULL,display=NULL WHERE batch_id=?", (batch_id,))
    db.execute("UPDATE batches SET state=?,reason=NULL WHERE batch_id=?", (state, batch_id))


def _reconcile_pending(
    db: sqlite3.Connection,
    batch_id: str,
    reconcile_row: Callable[[str], str | None],
) -> None:
    # Only the bounded attempted slice can have unrecorded canonical outcomes.
    for row in db.execute(
        "SELECT idx,operation_id FROM rows WHERE batch_id=? AND state='pending' "
        "ORDER BY idx LIMIT ?",
        (batch_id, _STEP_ROWS),
    ).fetchall():
        outcome = reconcile_row(row["operation_id"])
        db.execute(
            "UPDATE rows SET state=? WHERE batch_id=? AND idx=?",
            (outcome or "canceled", batch_id, row["idx"]),
        )


def execute(
    operation: str,
    args: dict[str, Any],
    *,
    binding_id: str,
    principal: str,
    authorize: Callable[[], None],
    save_row: Callable[[str, str, str], str],
    reconcile_row: Callable[[str], str | None],
) -> dict[str, object]:
    authorize()
    now = int(time.time())
    with _staging() as db, db:
        db.execute("BEGIN IMMEDIATE")
        authorize()
        db.execute(
            "DELETE FROM batches WHERE state IN ('upload','preview') "
            "AND (touched<=? OR created<=?)",
            (now - _IDLE, now - _LIFETIME),
        )
        db.commit()
        db.execute("BEGIN IMMEDIATE")
        authorize()
        if operation == "import.begin":
            count = db.execute(
                "SELECT count(*) FROM batches WHERE principal=? AND state IN "
                "('upload','preview','active')",
                (principal,),
            ).fetchone()[0]
            if count >= _MAX_BATCHES:
                raise Refused("import_preview_limit")
            batch_id = str(uuid4())
            db.execute(
                "INSERT INTO batches VALUES (?,?,?,'upload',?,?,?,?,?,NULL,?,?)",
                (
                    batch_id,
                    binding_id,
                    principal,
                    args["format"],
                    args["reason"],
                    args["bytes"],
                    args["digest"],
                    args["chunks"],
                    now,
                    now,
                ),
            )
            return {"batch_id": batch_id}

        batch_id = args["batch_id"]
        batch = db.execute(
            "SELECT * FROM batches WHERE batch_id=? AND binding_id=? AND principal=?",
            (batch_id, binding_id, principal),
        ).fetchone()
        if batch is None:
            raise Refused("import_not_found")
        state = batch["state"]
        if state in {"upload", "preview"}:
            db.execute("UPDATE batches SET touched=? WHERE batch_id=?", (now, batch_id))
        if operation == "import.chunk":
            _put_chunk(db, batch, args["index"], args["data"])
            return {"state": _progress(db, batch_id)["state"]}
        if operation == "import.preview":
            if state != "preview":
                raise Refused("import_incomplete")
            return {
                "digest": batch["preview_digest"],
                "total": _progress(db, batch_id)["total"],
                **_page(db, batch_id, args["offset"], args["limit"], preview=True),
            }
        if operation == "import.status":
            if state in {"complete", "canceled"}:
                _reconcile_pending(db, batch_id, reconcile_row)
            return {
                **_progress(db, batch_id),
                **_page(db, batch_id, args["offset"], args["limit"], preview=False),
            }
        if operation == "import.cancel":
            if state not in {"complete", "canceled"}:
                _reconcile_pending(db, batch_id, reconcile_row)
                db.execute(
                    "UPDATE rows SET state='canceled' WHERE batch_id=? AND state='ready'",
                    (batch_id,),
                )
                _clear_content(db, batch_id, "canceled")
                state = "canceled"
            return {"state": state}
        if operation == "import.confirm":
            if batch["preview_digest"] is None or batch["preview_digest"] != args["digest"]:
                raise Refused("import_confirmation_mismatch")
            if state == "preview":
                state = "active"
                db.execute("UPDATE batches SET state='active' WHERE batch_id=?", (batch_id,))
            return {"state": state}
        if state not in {"active", "complete", "canceled"}:
            raise Refused("import_not_confirmed")
        if state == "active":
            rows = db.execute(
                "SELECT * FROM rows WHERE batch_id=? AND state IN ('ready','pending') "
                "ORDER BY idx LIMIT ?",
                (batch_id, _STEP_ROWS),
            ).fetchall()
            # Keep the small attempted slice before any cross-store mutation.
            # A cancel after a lost reply can reconcile only attempted rows.
            db.executemany(
                "UPDATE rows SET state='pending' WHERE batch_id=? AND idx=?",
                ((batch_id, row["idx"]) for row in rows),
            )
            db.commit()
            db.execute("BEGIN IMMEDIATE")
            authorize()
            if _progress(db, batch_id)["state"] != "active":
                return _progress(db, batch_id)
            for row in rows:
                current = db.execute(
                    "SELECT state FROM rows WHERE batch_id=? AND idx=?",
                    (batch_id, row["idx"]),
                ).fetchone()
                if current["state"] != "pending":
                    continue
                authorize()
                result = save_row(row["url"], batch["reason"], row["operation_id"])
                if result not in {"pending", "accepted", "refused"}:
                    raise Refused("import_staging_unavailable")
                db.execute(
                    "UPDATE rows SET state=? WHERE batch_id=? AND idx=?",
                    (result, batch_id, row["idx"]),
                )
            progress = _progress(db, batch_id)
            if progress["processed"] == progress["total"]:
                _clear_content(db, batch_id, "complete")
        return _progress(db, batch_id)


def validate_import_result(operation: str, args: dict[str, Any], result: object) -> bool:
    """Validate the canonical handler's bounded result before forwarding it."""
    if not isinstance(result, dict) or operation not in OPERATIONS:
        return False
    if operation == "import.begin":
        value = result.get("batch_id")
        try:
            return (
                set(result) == {"batch_id"}
                and isinstance(value, str)
                and str(UUID(value)) == value
                and UUID(value).version == 4
            )
        except ValueError:
            return False
    state = result.get("state")
    if operation == "import.chunk":
        return set(result) == {"state"} and state in ("upload", "preview", "active")
    if operation == "import.confirm":
        return set(result) == {"state"} and state in ("active", "complete", "canceled")
    if operation == "import.cancel":
        return set(result) == {"state"} and state in ("complete", "canceled")

    total = result.get("total")
    if type(total) is not int or not 0 <= total <= MAX_ROWS:
        return False
    fields = {"total"}
    if operation == "import.preview":
        fields.add("digest")
        if not _digest(result.get("digest")):
            return False
    else:
        fields.update(("state", "processed"))
        batch_states = ("active", "complete", "canceled")
        allowed_states = (
            (*batch_states, "upload", "preview") if operation == "import.status" else batch_states
        )
        if state not in allowed_states or not _number(result.get("processed"), 0, total):
            return False
    if operation == "import.step":
        return set(result) == fields

    fields.update(("rows", "next_offset"))
    rows = result.get("rows")
    if set(result) != fields or not isinstance(rows, list):
        return False
    limit, offset = args["limit"], args["offset"]
    if len(rows) != min(limit, max(0, total - offset)):
        return False
    count = offset + len(rows)
    next_offset = result["next_offset"]
    if (count < total and (type(next_offset) is not int or next_offset != count)) or (
        count >= total and next_offset is not None
    ):
        return False
    for index, row in enumerate(rows, start=offset):
        if not isinstance(row, dict) or type(row.get("index")) is not int or row["index"] != index:
            return False
        row_state = row.get("state")
        states: tuple[str, ...] = ("ready", "duplicate", "skipped")
        keys = {"index", "state"}
        if operation == "import.preview":
            keys.add("url")
            url = row.get("url")
            if not isinstance(url, str) or len(url) > 512:
                return False
            if row_state == "ready":
                try:
                    parts = urlsplit(url)
                    if (
                        parts.scheme != "https"
                        or not parts.hostname
                        or parts.username is not None
                        or "?" in url
                        or "#" in url
                    ):
                        return False
                except ValueError:
                    return False
            elif url:
                return False
        else:
            states += ("pending", "accepted", "refused", "canceled")
        if set(row) != keys or row_state not in states:
            return False
    return True

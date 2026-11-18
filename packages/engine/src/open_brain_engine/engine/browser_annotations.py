"""Grant-scoped highlights on accepted saved-item captures."""

from __future__ import annotations

import json
import sqlite3
import unicodedata
from contextlib import closing
from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import cast
from uuid import UUID

from open_brain_engine.capture.redaction import has_redaction_finding

from .browser_pages import BrowserPages, BrowserResult, _operation, _operation_key
from .contracts import project_public_result_text
from .local_schema import open_local_database_read_only
from .local_store import _LocalStore
from .saved_item_identity import SavedItemSource


def uuid4_id(value: str) -> str:
    parsed = UUID(value)
    if str(parsed) != value or parsed.version != 4:
        raise ValueError("invalid browser identifier")
    return value


class MaskedAnnotation(ValueError):
    """The saved-item projection would mask some part of this anchor or note."""


@dataclass(frozen=True, slots=True)
class Annotation:
    annotation_id: str
    quote: str
    prefix: str
    suffix: str
    note: str | None = None

    def canonical(self) -> Annotation:
        return Annotation(
            self.annotation_id,
            unicodedata.normalize("NFC", self.quote),
            unicodedata.normalize("NFC", self.prefix),
            unicodedata.normalize("NFC", self.suffix),
            unicodedata.normalize("NFC", self.note) if self.note is not None else None,
        )

    def validate(self, source_reference: str) -> None:
        uuid4_id(self.annotation_id)
        fields = [(self.quote, 4096), (self.prefix, 128), (self.suffix, 128)]
        if self.note is not None:
            fields.append((self.note, 1024))
        for value, limit in fields:
            if not isinstance(value, str) or len(value) > limit or "\x00" in value:
                raise ValueError("invalid browser annotation")
            value.encode("utf-8")
        for value in [*(value for value, _ in fields), self.prefix + self.quote + self.suffix]:
            value = unicodedata.normalize("NFC", value)
            if (
                has_redaction_finding(value)
                or project_public_result_text(value, protected_literals=(source_reference,))
                != value
            ):
                raise MaskedAnnotation("browser_annotation_masked")
        if not self.quote.strip() or self.note is not None and not self.note.strip():
            raise ValueError("invalid browser annotation")


def annotation_text(db: sqlite3.Connection, capture_id: str, body: str) -> str:
    if db.execute("PRAGMA user_version").fetchone()[0] < 26:
        return body
    rows = db.execute(
        "SELECT quote,note FROM browser_annotations WHERE capture_id=? AND state='accepted' "
        "ORDER BY annotation_id",
        (capture_id,),
    )
    parts = (unicodedata.normalize("NFC", part) for row in rows for part in row if part)
    return "\n".join([body, *parts])


def operation_row(
    db: sqlite3.Connection, grant: sqlite3.Row, operation_id: str
) -> sqlite3.Row | None:
    row = db.execute(
        "SELECT * FROM browser_annotations WHERE brain_id=? AND workflow_principal=? AND "
        "connector_name=? AND connection_id=? AND resource_id=? AND operation_id=?",
        _operation_key(grant, operation_id),
    ).fetchone()
    return cast(sqlite3.Row | None, row)


class BrowserAnnotations(BrowserPages):
    def _parent(
        self,
        db: sqlite3.Connection,
        grant: sqlite3.Row,
        item_key: str,
        capture_id: str,
    ) -> str:
        source = SavedItemSource(
            grant["connector_name"], grant["connection_id"], grant["resource_id"]
        )
        delivery = source.delivery_id(item_key)
        page = self._visible(db, grant, delivery)
        if page is None or page.capture_id != capture_id:
            raise PermissionError("browser_operation_denied")
        return delivery

    def add(
        self, token: str, operation_id: str, item_key: str, capture_id: str, annotation: Annotation
    ) -> BrowserResult:
        uuid4_id(operation_id)
        if operation_id == annotation.annotation_id:
            raise ValueError("invalid browser identifier")
        with _LocalStore(self.profile, initialize=False).transaction() as db:
            grant = self._grant(db, token, "save")
            delivery = self._parent(db, grant, item_key, capture_id)
            reference = db.execute(
                "SELECT source_reference FROM captures WHERE capture_id=?", (capture_id,)
            ).fetchone()[0]
            annotation.validate(reference)
            annotation = annotation.canonical()
            payload = {**asdict(annotation), "capture_id": capture_id, "item_key": item_key}
            digest = sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            key = _operation_key(grant, operation_id)
            prior = operation_row(db, grant, operation_id)
            if prior:
                if prior["request_sha256"] != digest:
                    # Early schema26 rows retained the caller's Unicode form. Accept
                    # their original valid bytes after canonical-equivalence checking.
                    legacy = Annotation(*(prior[field] for field in asdict(annotation)))
                    original = {**asdict(legacy), "capture_id": capture_id, "item_key": item_key}
                    original_sha = sha256(
                        json.dumps(original, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest()
                    if (
                        prior["state"] != "accepted" or original_sha != prior["request_sha256"]
                        or legacy.canonical() != annotation or legacy == annotation
                    ):
                        raise ValueError("browser_operation_conflict")
                if prior["state"] != "accepted":
                    return BrowserResult("refused")
            else:
                from .browser_filing import filing_row

                if (
                    filing_row(db, grant, operation_id)
                    or _operation(db, grant, operation_id)
                    or db.execute(
                        "SELECT 1 FROM browser_annotations WHERE annotation_id=?",
                        (annotation.annotation_id,),
                    ).fetchone()
                ):
                    raise ValueError("browser_operation_conflict")
                db.execute(
                    "INSERT INTO browser_annotations VALUES"
                    "(?,?,?,?,?,?,?,?,?,?,?,'accepted',?,?,?,?)",
                    (
                        *key,
                        digest,
                        delivery,
                        item_key,
                        capture_id,
                        annotation.annotation_id,
                        annotation.quote,
                        annotation.prefix,
                        annotation.suffix,
                        annotation.note,
                    ),
                )
                from .search_projection import upsert_search_document

                parent = db.execute(
                    "SELECT c.search_text,c.accepted_at,c.payload_family,d.title,d.space_id "
                    "FROM captures c JOIN search_documents d ON c.capture_id=d.result_id "
                    "WHERE c.capture_id=?",
                    (capture_id,),
                ).fetchone()
                if parent is None:
                    raise ValueError("browser_parent_index_unavailable")
                upsert_search_document(
                    db,
                    result_id=capture_id,
                    capture_id=capture_id,
                    record_type="source",
                    payload_family=parent["payload_family"],
                    space_id=parent["space_id"],
                    title=parent["title"],
                    body=parent["search_text"],
                    canonical_path=None,
                    updated_at=parent["accepted_at"],
                )
                db.execute(
                    "UPDATE engine_generations SET retrieval_generation=retrieval_generation+1,"
                    "vector_generation=vector_generation+1"
                )
            return BrowserResult("accepted", capture_id, annotation.annotation_id)

    def list(
        self, token: str, item_key: str, capture_id: str, *, limit: int, offset: int
    ) -> tuple[list[dict[str, str | None]], int | None]:
        if (
            type(limit) is not int
            or not 1 <= limit <= 10
            or type(offset) is not int
            or not 0 <= offset < 2**31
        ):
            raise ValueError("invalid browser page")
        with closing(open_local_database_read_only(self.profile)) as db:
            if not db.in_transaction:
                db.execute("BEGIN")
            self._parent(db, self._grant(db, token, "lookup"), item_key, capture_id)
            rows = db.execute(
                "SELECT annotation_id,quote,prefix,suffix,note FROM browser_annotations "
                "WHERE capture_id=? AND state='accepted' ORDER BY annotation_id LIMIT ? OFFSET ?",
                (capture_id, limit + 1, offset),
            ).fetchall()
            page = []
            size = 0
            for row in rows[:limit]:
                item = dict(row)
                size += len(json.dumps(item))
                if size > 220 * 1024:
                    break
                page.append(item)
            return page, offset + len(page) if len(rows) > len(page) else None

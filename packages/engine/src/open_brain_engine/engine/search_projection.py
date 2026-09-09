"""Public-safe live search projection writes."""

from __future__ import annotations

import json
import sqlite3
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.core.models import ContentOrigin, Provenance

from .contracts import project_public_result_text


def public_search_text(value: str, *, protected_source_reference: str) -> str:
    """Normalize and remove protected values before text reaches the live index."""
    normalized = unicodedata.normalize("NFC", value)
    return project_public_result_text(
        normalized,
        protected_literals=(protected_source_reference,),
    )


def source_search_title(
    *,
    payload_family: str,
    body: str,
) -> str:
    first_line = next((line.strip() for line in body.splitlines() if line.strip()), "")
    return first_line[:200] or f"{payload_family} source"


def public_source_origin(capture: Mapping[str, object] | sqlite3.Row) -> str:
    """Return the bounded origin retained in public retrieval results."""
    try:
        return _durable_source_origin(capture)
    except ValueError:
        return ContentOrigin.UNKNOWN.value


def _durable_source_origin(capture: Mapping[str, object] | sqlite3.Row) -> str:
    """Validate the durable provenance edge used to derive projection trust."""
    try:
        raw_provenance = capture["provenance_json"]
        if isinstance(raw_provenance, bytes):
            raw_provenance = raw_provenance.decode("utf-8")
        if not isinstance(raw_provenance, str):
            raise ValueError
        value = json.loads(raw_provenance)
        if not isinstance(value, dict):
            raise ValueError
        keys = frozenset(value)
        direct_keys = {"content_origin", "owner_context", "source_ref"}
        portable_keys = direct_keys | {"transformation_receipts"}
        if keys not in {frozenset(direct_keys), frozenset(portable_keys)}:
            raise ValueError
        if "transformation_receipts" in value and not isinstance(
            value["transformation_receipts"], list
        ):
            raise ValueError
        provenance = Provenance.from_dict({key: value[key] for key in direct_keys})
        source_reference = capture["source_reference"]
        durable_origin = capture["source_origin"]
        if (
            not isinstance(source_reference, str)
            or not source_reference
            or provenance.source_ref != source_reference
            or durable_origin not in {"owner", "third_party"}
            or (
                durable_origin == "owner"
                and provenance.content_origin is not ContentOrigin.OWNER_AUTHORED
            )
            or (
                durable_origin == "third_party"
                and provenance.content_origin is ContentOrigin.OWNER_AUTHORED
            )
        ):
            raise ValueError
    except (KeyError, TypeError, UnicodeDecodeError, ValueError) as error:
        raise ValueError("search projection provenance is invalid") from error
    return provenance.content_origin.value


def source_trust(origin: str) -> str:
    """Map source provenance to the bounded retrieval trust vocabulary."""
    if origin == ContentOrigin.OWNER_AUTHORED.value:
        return "owner"
    if origin == ContentOrigin.THIRD_PARTY.value:
        return "third_party"
    return "unverified"


@dataclass(frozen=True, slots=True)
class SearchDocumentProjection:
    title: str
    body: str
    trust: str
    provenance_json: str


def project_search_document(
    connection: sqlite3.Connection,
    *,
    result_id: str,
    capture_id: str,
    record_type: str,
    title: str,
    body: str,
    canonical_path: str | None,
) -> SearchDocumentProjection:
    """Derive public text, trust, and provenance from the durable capture graph."""
    capture = connection.execute(
        """
        SELECT source_origin, source_reference, provenance_json, action, canonical_path
        FROM captures
        WHERE capture_id = ?
        """,
        (capture_id,),
    ).fetchone()
    if capture is None:
        raise ValueError("search projection capture is unavailable")
    source_reference = capture["source_reference"]
    if not isinstance(source_reference, str) or not source_reference:
        raise ValueError("search projection capture is unavailable")
    origin = _durable_source_origin(capture)
    if record_type == "source":
        trust = source_trust(origin)
    elif record_type == "canonical":
        trust = _canonical_trust(
            connection,
            capture=capture,
            origin=origin,
            result_id=result_id,
            canonical_path=canonical_path,
        )
    else:
        raise ValueError("invalid search record type")
    return SearchDocumentProjection(
        title=public_search_text(title, protected_source_reference=source_reference),
        body=public_search_text(body, protected_source_reference=source_reference),
        trust=trust,
        provenance_json=portable_canonical_json_bytes({"capture_id": capture_id}).decode("utf-8"),
    )


def _canonical_trust(
    connection: sqlite3.Connection,
    *,
    capture: sqlite3.Row,
    origin: str,
    result_id: str,
    canonical_path: str | None,
) -> str:
    if origin in {ContentOrigin.UNKNOWN.value, ContentOrigin.MIXED.value}:
        return "unverified"
    if (
        origin == ContentOrigin.OWNER_AUTHORED.value
        and capture["action"] == "canonical_note"
        and capture["canonical_path"] == canonical_path
    ):
        return "owner"
    reviewed = connection.execute(
        """
        SELECT 1
        FROM decisions
        WHERE page_id = ?
          AND outcome IN ('approved', 'edited')
          AND publication_id IS NOT NULL
        LIMIT 1
        """,
        (result_id,),
    ).fetchone()
    if reviewed is None:
        raise ValueError("canonical search trust is unavailable")
    return "reviewed"


def upsert_search_document(
    connection: sqlite3.Connection,
    *,
    result_id: str,
    capture_id: str,
    record_type: str,
    payload_family: str,
    space_id: str | None,
    title: str,
    body: str,
    canonical_path: str | None,
    updated_at: str,
) -> None:
    """Write the one public-safe projection used by matching and retrieval."""
    existing = connection.execute(
        """
        SELECT capture_id, record_type, payload_family
        FROM search_documents
        WHERE result_id = ?
        """,
        (result_id,),
    ).fetchone()
    identity = (capture_id, record_type, payload_family)
    if existing is not None and (
        str(existing["capture_id"]),
        str(existing["record_type"]),
        str(existing["payload_family"]),
    ) != identity:
        raise ValueError("search result identity conflict")
    projection = project_search_document(
        connection,
        result_id=result_id,
        capture_id=capture_id,
        record_type=record_type,
        title=title,
        body=body,
        canonical_path=canonical_path,
    )
    connection.execute(
        """
        INSERT INTO search_documents (
            result_id, capture_id, record_type, payload_family, space_id,
            title, body, trust, provenance_json, canonical_path, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(result_id) DO UPDATE SET
            space_id = excluded.space_id,
            title = excluded.title,
            body = excluded.body,
            trust = excluded.trust,
            provenance_json = excluded.provenance_json,
            canonical_path = excluded.canonical_path,
            updated_at = excluded.updated_at
        """,
        (
            result_id,
            capture_id,
            record_type,
            payload_family,
            space_id,
            projection.title,
            projection.body,
            projection.trust,
            projection.provenance_json,
            canonical_path,
            updated_at,
        ),
    )


__all__ = [
    "public_search_text",
    "public_source_origin",
    "project_search_document",
    "SearchDocumentProjection",
    "source_trust",
    "source_search_title",
    "upsert_search_document",
]

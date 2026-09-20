"""Public-safe live search projection writes."""

from __future__ import annotations

import json
import sqlite3
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.core.models import ContentOrigin, Provenance

from .contracts import project_public_result_text
from .privacy_projection import (
    RepairedPrivacyEvidence,
    RetainedPrivacyEvidence,
    project_retained_privacy_evidence,
)
from .privacy_store import (
    effective_privacy_enabled,
    owner_repair_projection_enabled,
    record_invalid_evidence,
)


def public_search_text(
    value: str,
    *,
    protected_source_reference: str,
    additional_source_references: tuple[str, ...] = (),
) -> str:
    """Normalize and remove protected values before text reaches the live index."""
    normalized = unicodedata.normalize("NFC", value)
    return project_public_result_text(
        normalized,
        protected_literals=(protected_source_reference, *additional_source_references),
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
    canonical_frontmatter_trust: str | None
    provenance_json: str


def canonical_source_rows(
    connection: sqlite3.Connection, *, result_id: str, capture_id: str
) -> tuple[sqlite3.Row, ...]:
    """Resolve ordered source membership from the current canonical publication."""
    head = connection.execute(
        "SELECT proposal_id, capture_id FROM review_page_heads WHERE page_id = ?", (result_id,)
    ).fetchone()
    if head is None:
        rows = tuple(
            connection.execute("SELECT * FROM captures WHERE capture_id = ?", (capture_id,))
        )
    else:
        if head["capture_id"] != capture_id:
            raise ValueError("canonical source identity conflict")
        rows = tuple(
            connection.execute(
                "SELECT c.* FROM review_sources s JOIN captures c USING (capture_id) "
                "WHERE s.proposal_id = ? ORDER BY s.ordinal",
                (head["proposal_id"],),
            )
        )
    if not 1 <= len(rows) <= 32 or rows[0]["capture_id"] != capture_id:
        raise ValueError("canonical source provenance is unavailable")
    return rows


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
    captures: tuple[sqlite3.Row, ...] = (capture,)
    capture_ids: tuple[str, ...] = (capture_id,)
    if record_type == "source":
        trust = source_trust(origin)
        canonical_frontmatter_trust = None
    elif record_type == "canonical":
        captures = canonical_source_rows(connection, result_id=result_id, capture_id=capture_id)
        capture_ids = tuple(str(source["capture_id"]) for source in captures)
        origins = {_durable_source_origin(source) for source in captures}
        origin = next(iter(origins)) if len(origins) == 1 else ContentOrigin.MIXED.value
        trust, canonical_frontmatter_trust = _canonical_trust(
            connection,
            capture=capture,
            origin=origin,
            result_id=result_id,
            canonical_path=canonical_path,
        )
    else:
        raise ValueError("invalid search record type")
    additional_references = tuple(str(source["source_reference"]) for source in captures[1:])
    provenance: dict[str, object] = {"capture_id": capture_id}
    if len(capture_ids) > 1:
        provenance["capture_ids"] = list(capture_ids)
    return SearchDocumentProjection(
        title=public_search_text(
            title,
            protected_source_reference=source_reference,
            additional_source_references=additional_references,
        ),
        body=public_search_text(
            body,
            protected_source_reference=source_reference,
            additional_source_references=additional_references,
        ),
        trust=trust,
        canonical_frontmatter_trust=canonical_frontmatter_trust,
        provenance_json=portable_canonical_json_bytes(provenance).decode("utf-8"),
    )


def _canonical_trust(
    connection: sqlite3.Connection,
    *,
    capture: sqlite3.Row,
    origin: str,
    result_id: str,
    canonical_path: str | None,
) -> tuple[str, str]:
    if (
        origin == ContentOrigin.OWNER_AUTHORED.value
        and capture["action"] == "canonical_note"
        and capture["canonical_path"] == canonical_path
        and connection.execute(
            "SELECT 1 FROM review_page_heads WHERE page_id = ?", (result_id,)
        ).fetchone()
        is None
    ):
        return ("owner", "owner")
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
    search_trust = (
        "unverified"
        if origin in {ContentOrigin.UNKNOWN.value, ContentOrigin.MIXED.value}
        else "reviewed"
    )
    return (search_trust, "reviewed")


def _retained_capture_privacy(
    connection: sqlite3.Connection, capture_id: str
) -> tuple[str | int | float | bytes | None]:
    row = connection.execute(
        "SELECT privacy_json FROM captures WHERE capture_id = ?", (capture_id,)
    ).fetchone()
    if row is None:
        # A missing capture row is missing evidence and fails closed to the typed
        # invalid path; it never surfaces as an untyped error.
        return (None,)
    return (cast("str | int | float | bytes | None", row[0]),)


def _canonical_member_privacy_values(
    connection: sqlite3.Connection, *, result_id: str, capture_id: str
) -> tuple[str | int | float | bytes | None, ...]:
    """Resolve retained canonical member values without the current-row graph checks.

    Used only after ``canonical_source_rows`` proved the graph mismatched, so the
    invalid-evidence digest still binds the same deterministic retained values the
    current head proposal resolves.
    """
    head = connection.execute(
        "SELECT proposal_id FROM review_page_heads WHERE page_id = ?", (result_id,)
    ).fetchone()
    if head is None:
        return _retained_capture_privacy(connection, capture_id)
    return tuple(
        cast("str | int | float | bytes | None", row[0])
        for row in connection.execute(
            "SELECT c.privacy_json FROM review_sources s JOIN captures c USING (capture_id) "
            "WHERE s.proposal_id = ? ORDER BY s.ordinal",
            (head["proposal_id"],),
        )
    )


def project_search_privacy(
    connection: sqlite3.Connection, *, result_id: str, capture_id: str, record_type: str
) -> RetainedPrivacyEvidence:
    """Project one search row's effective privacy from its exact retained source set.

    Source rows read their own capture's immutable privacy evidence. Canonical rows
    resolve their current retained member set; a member-graph mismatch fails closed to
    ``inconsistent`` without blocking any unrelated row. The projection never infers
    privacy from title, body, space, or path.
    """
    if record_type == "source":
        return project_retained_privacy_evidence(
            _retained_capture_privacy(connection, capture_id)
        )
    if record_type != "canonical":
        return project_retained_privacy_evidence((), caller_declared_inconsistent=True)
    try:
        rows = canonical_source_rows(connection, result_id=result_id, capture_id=capture_id)
    except ValueError:
        return project_retained_privacy_evidence(
            _canonical_member_privacy_values(
                connection, result_id=result_id, capture_id=capture_id
            ),
            caller_declared_inconsistent=True,
        )
    return project_retained_privacy_evidence(tuple(row["privacy_json"] for row in rows))


def write_search_privacy(
    connection: sqlite3.Connection,
    *,
    result_id: str,
    evidence: RetainedPrivacyEvidence | RepairedPrivacyEvidence,
    applied_repair: tuple[str, int] | None = None,
) -> None:
    """Persist one search row's complete effective-privacy projection."""
    privacy_values = (
        evidence.tier.value,
        int(evidence.authority.cloud),
        int(evidence.authority.external_egress),
        None if evidence.invalid_reason is None else evidence.invalid_reason.value,
        evidence.invalid_evidence_sha256,
    )
    if owner_repair_projection_enabled(connection):
        connection.execute(
            """
            UPDATE search_documents
            SET effective_tier = ?, effective_cloud = ?, effective_external_egress = ?,
                invalid_evidence_reason = ?, invalid_evidence_sha256 = ?,
                applied_repair_id = ?, applied_repair_sequence = ?
            WHERE result_id = ?
            """,
            (
                *privacy_values,
                None if applied_repair is None else applied_repair[0],
                None if applied_repair is None else applied_repair[1],
                result_id,
            ),
        )
        return
    connection.execute(
        """
        UPDATE search_documents
        SET effective_tier = ?, effective_cloud = ?, effective_external_egress = ?,
            invalid_evidence_reason = ?, invalid_evidence_sha256 = ?
        WHERE result_id = ?
        """,
        (*privacy_values, result_id),
    )


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
    if (
        existing is not None
        and (
            str(existing["capture_id"]),
            str(existing["record_type"]),
            str(existing["payload_family"]),
        )
        != identity
    ):
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
    base_values = (
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
    )
    # Effective privacy exists only from schema eight; historical schema-seven
    # runtimes keep writing their exact historical row shape.
    if effective_privacy_enabled(connection):
        if owner_repair_projection_enabled(connection):
            from .privacy_repairs import resolve_search_privacy

            privacy, applied_repair = resolve_search_privacy(
                connection,
                result_id=result_id,
                capture_id=capture_id,
                record_type=record_type,
            )
            connection.execute(
                """
                INSERT INTO search_documents (
                    result_id, capture_id, record_type, payload_family, space_id,
                    title, body, trust, provenance_json, canonical_path, updated_at,
                    effective_tier, effective_cloud, effective_external_egress,
                    invalid_evidence_reason, invalid_evidence_sha256,
                    applied_repair_id, applied_repair_sequence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(result_id) DO UPDATE SET
                    space_id = excluded.space_id,
                    title = excluded.title,
                    body = excluded.body,
                    trust = excluded.trust,
                    provenance_json = excluded.provenance_json,
                    canonical_path = excluded.canonical_path,
                    updated_at = excluded.updated_at,
                    effective_tier = excluded.effective_tier,
                    effective_cloud = excluded.effective_cloud,
                    effective_external_egress = excluded.effective_external_egress,
                    invalid_evidence_reason = excluded.invalid_evidence_reason,
                    invalid_evidence_sha256 = excluded.invalid_evidence_sha256,
                    applied_repair_id = excluded.applied_repair_id,
                    applied_repair_sequence = excluded.applied_repair_sequence
                """,
                (
                    *base_values,
                    privacy.tier.value,
                    int(privacy.authority.cloud),
                    int(privacy.authority.external_egress),
                    None if privacy.invalid_reason is None else privacy.invalid_reason.value,
                    privacy.invalid_evidence_sha256,
                    None if applied_repair is None else applied_repair[0],
                    None if applied_repair is None else applied_repair[1],
                ),
            )
            record_invalid_evidence(
                connection,
                target_kind="search_document",
                target_id=result_id,
                evidence=privacy,
            )
            return
        privacy = project_search_privacy(
            connection, result_id=result_id, capture_id=capture_id, record_type=record_type
        )
        connection.execute(
            """
            INSERT INTO search_documents (
                result_id, capture_id, record_type, payload_family, space_id,
                title, body, trust, provenance_json, canonical_path, updated_at,
                effective_tier, effective_cloud, effective_external_egress,
                invalid_evidence_reason, invalid_evidence_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(result_id) DO UPDATE SET
                space_id = excluded.space_id,
                title = excluded.title,
                body = excluded.body,
                trust = excluded.trust,
                provenance_json = excluded.provenance_json,
                canonical_path = excluded.canonical_path,
                updated_at = excluded.updated_at,
                effective_tier = excluded.effective_tier,
                effective_cloud = excluded.effective_cloud,
                effective_external_egress = excluded.effective_external_egress,
                invalid_evidence_reason = excluded.invalid_evidence_reason,
                invalid_evidence_sha256 = excluded.invalid_evidence_sha256
            """,
            (
                *base_values,
                privacy.tier.value,
                int(privacy.authority.cloud),
                int(privacy.authority.external_egress),
                None if privacy.invalid_reason is None else privacy.invalid_reason.value,
                privacy.invalid_evidence_sha256,
            ),
        )
        record_invalid_evidence(
            connection,
            target_kind="search_document",
            target_id=result_id,
            evidence=privacy,
        )
        return
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
        base_values,
    )


__all__ = [
    "public_search_text",
    "public_source_origin",
    "project_search_document",
    "project_search_privacy",
    "SearchDocumentProjection",
    "source_trust",
    "source_search_title",
    "upsert_search_document",
    "write_search_privacy",
]

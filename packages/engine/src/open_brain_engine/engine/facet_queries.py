"""Authority-filtered exact facet selection and immutable content projection seams."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .contracts import LocalEngineContext
from .facet_contracts import FacetError, validate_wire
from .facet_store import get_row, semantic_row
from .records import RecordProjector
from .t03_contracts import EffectiveAuthority, T03Error


@dataclass(frozen=True, slots=True)
class AuthorizedSubject:
    kind: str
    id: str
    record_id: str
    revision_id: str
    head_version: int | None
    summary: dict[str, Any]

    def to_wire(self) -> dict[str, str]:
        return {"kind": self.kind, "id": self.id}


@dataclass(frozen=True, slots=True)
class FacetSelection:
    subject: AuthorizedSubject
    classification_version: int
    memberships: tuple[dict[str, Any], ...]
    accepted_at: str

    def to_wire(self) -> dict[str, Any]:
        return {
            "subject": self.subject.to_wire(),
            "record": self.subject.summary,
            "classification_version": self.classification_version,
        }


def resolve_subject(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
    record_id: str,
    authority: EffectiveAuthority,
) -> AuthorizedSubject:
    """Anchor resolution always applies current eligibility before returning identity."""
    projector = RecordProjector(profile, connection, authority)
    try:
        if record_id.startswith("capture_"):
            projected = projector.source(record_id)
            summary = projected.summary
            source = connection.execute(
                "SELECT * FROM logical_sources WHERE source_id=?", (summary["source_id"],)
            ).fetchone()
            assert source is not None
            return AuthorizedSubject(
                "source",
                source["source_id"],
                summary["record_id"],
                summary["revision_id"],
                source["head_version"],
                summary,
            )
        if record_id.startswith("page_"):
            projected = projector.canonical(record_id)
            # A destination route cannot grant access to any retained provenance member.
            for capture_id in projected.summary["provenance"]["capture_ids"]:
                projector.source(capture_id, expected=capture_id, history=True)
            return AuthorizedSubject(
                "canonical",
                record_id,
                record_id,
                projected.summary["revision_id"],
                None,
                projected.summary,
            )
    except T03Error as error:
        code = (
            error.code if error.code in {"not_found", "revision_changed"} else "operation_pending"
        )
        raise FacetError(code) from None
    raise FacetError("not_found")


def authorize_subject(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
    subject: dict[str, str],
    authority: EffectiveAuthority,
) -> AuthorizedSubject:
    if subject["kind"] == "canonical":
        return resolve_subject(connection, profile, subject["id"], authority)
    source = connection.execute(
        "SELECT head_capture_id FROM logical_sources WHERE source_id=?", (subject["id"],)
    ).fetchone()
    if source is None:
        raise FacetError("not_found")
    return resolve_subject(connection, profile, source[0], authority)


def require_query_scope(selector: dict[str, Any], authority: EffectiveAuthority) -> None:
    if any(not authority.permits_space(space_id) for space_id in selector["space_ids"]):
        raise FacetError("not_found")


def resolve_query(
    connection: sqlite3.Connection,
    query_id: str,
    authority: EffectiveAuthority,
    expected_version: int | None = None,
) -> dict[str, Any]:
    row = get_row(connection, "query", query_id)
    if row is None:
        raise FacetError("not_found")
    require_query_scope(row["selector"], authority)
    if expected_version is not None and row["version"] != expected_version:
        raise FacetError("version_conflict")
    return row


def require_values(connection: sqlite3.Connection, value_ids: list[str]) -> None:
    for value_id in value_ids:
        if get_row(connection, "value", value_id) is None:
            raise FacetError("not_found")


def subject_version(connection: sqlite3.Connection, subject: AuthorizedSubject) -> int:
    row = get_row(connection, "subject", subject.to_wire())
    return 0 if row is None else int(row["version"])


def membership_rows(
    connection: sqlite3.Connection,
    subject_kind: str,
    subject_id: str,
    *,
    present_only: bool = True,
) -> list[dict[str, Any]]:
    return [
        semantic_row(row)
        for row in connection.execute(
            "SELECT * FROM facet_assignments WHERE subject_kind=? AND subject_id=? "
            + ("AND present=1 " if present_only else "")
            + "ORDER BY value_id",
            (subject_kind, subject_id),
        )
    ]


def selected_source_state(selection: FacetSelection) -> dict[str, Any]:
    if selection.subject.kind != "source":
        raise FacetError("invalid_arguments")
    return {
        "source_id": selection.subject.id,
        "capture_id": selection.subject.record_id,
        "head_version": selection.subject.head_version,
        "memberships": [dict(item) for item in selection.memberships],
    }


def _acceptance_key(value: str | None) -> str:
    if value is None:
        return ""
    return (
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        .astimezone(UTC)
        .strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    )


def select_records(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
    selector: dict[str, Any],
    authority: EffectiveAuthority,
    *,
    source_only: bool = False,
    limit: int | None = None,
    after: tuple[str, str, str] | None = None,
) -> list[FacetSelection]:
    """Resolve current eligible heads in the caller's consistent snapshot.

    Optional limit is applied after authorization and deterministic logical-record
    sorting; publication requests 33 to detect a complete-set overflow.
    """
    selector = validate_wire("selector", selector)
    values = selector["all_value_ids"]
    require_values(connection, values)
    connection.create_function("facet_acceptance_key", 1, _acceptance_key, deterministic=True)
    candidates = connection.execute(
        "WITH matching AS (SELECT subject_kind,subject_id FROM facet_assignments "
        "WHERE present=1 AND value_id IN (" + ",".join("?" for _ in values) + ") "
        "GROUP BY subject_kind,subject_id HAVING COUNT(*)=?), "
        "source_times AS (SELECT source_id,facet_acceptance_key(recorded_at) AS accepted_at "
        "FROM source_revisions WHERE sequence=1), "
        "page_times AS (SELECT page_id,MIN(facet_acceptance_key(recorded_at)) AS accepted_at FROM ("
        "SELECT m.page_id,c.accepted_at AS recorded_at FROM canonical_revision_members m "
        "JOIN captures c USING(publication_id) UNION ALL "
        "SELECT m.page_id,d.recorded_at FROM canonical_revision_members m "
        "JOIN decisions d USING(publication_id)) GROUP BY page_id) "
        "SELECT m.*,CASE m.subject_kind WHEN 'source' THEN s.accepted_at "
        "ELSE p.accepted_at END AS accepted_at "
        "FROM matching m LEFT JOIN source_times s ON m.subject_kind='source' "
        "AND m.subject_id=s.source_id "
        "LEFT JOIN page_times p ON m.subject_kind='canonical' AND m.subject_id=p.page_id "
        "ORDER BY accepted_at,m.subject_id,m.subject_kind",
        (*values, len(values)),
    )
    selections = []
    for row in candidates:
        kind = row["subject_kind"]
        accepted = row["accepted_at"]
        if not accepted or after is not None and (accepted, row["subject_id"], kind) <= after:
            continue
        if (
            source_only
            and kind != "source"
            or selector["record_types"]
            and kind not in selector["record_types"]
        ):
            continue
        if kind == "canonical" and selector["payload_families"]:
            continue
        try:
            subject = authorize_subject(
                connection, profile, {"kind": kind, "id": row["subject_id"]}, authority
            )
        except FacetError as error:
            if error.code == "not_found":
                continue
            raise
        if selector["space_ids"] and subject.summary["space_id"] not in selector["space_ids"]:
            continue
        if (
            selector["payload_families"]
            and subject.summary["payload_family"] not in selector["payload_families"]
        ):
            continue
        selected = tuple(
            {"value_id": item["value_id"], "membership_version": item["membership_version"]}
            for item in membership_rows(connection, kind, subject.id)
            if item["value_id"] in values
        )
        selections.append(
            FacetSelection(subject, subject_version(connection, subject), selected, accepted)
        )
        if limit is not None and len(selections) >= limit:
            break
    return selections

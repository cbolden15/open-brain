"""Deterministic current-state evidence for unchanged Portable Brain v5.

The caller owns both the connection and its read transaction.  This module
never opens a second database view and never treats mutable search rows as the
source of Portable state; it derives the expected result identities from the
durable source and canonical heads, then uses the engine repair resolver to
produce and audit their privacy assertions.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from types import MappingProxyType
from typing import Any, Never, cast

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.core.models import ValidationError
from open_brain_engine.portable.v5 import (
    EFFECTIVE_PRIVACY_PATH,
    ISSUER_MIGRATION_PATH,
    LEGACY_BINDINGS_PATH,
    encode_retained_privacy_value,
)

from .issuer_state import validate_issuer_digest, verify_issuer_evidence
from .privacy_projection import (
    RepairedPrivacyEvidence,
    RetainedPrivacyEvidence,
    apply_privacy_repair,
    effective_privacy_json,
    project_retained_privacy_evidence,
)
from .privacy_repairs import (
    PrivacyRepairError,
    PrivacyRepairReceipt,
    audit_privacy_repair_ledger,
    resolve_canonical_revision_privacy,
    resolve_search_privacy,
)
from .relationship_store import relationship_metadata
from .source_store import source_metadata
from .t03_contracts import T03Error

_SUPPORTED_STATE_SCHEMA_VERSIONS = frozenset({9, 10})
_EVIDENCE_SCHEMA_VERSION = 1
_VALID_INVALID_REASONS = frozenset({"missing", "malformed", "inconsistent"})


@dataclass(frozen=True, slots=True)
class PortableV5StateEvidence:
    """Canonical v5 sidecars plus the stable local semantic commitment."""

    sidecars: Mapping[str, bytes]
    semantic_state: Mapping[str, object]
    semantic_state_sha256: str


def _fail(message: str) -> Never:
    raise ValidationError(message)


def _canonical_object(payload: object, *, label: str) -> dict[str, Any]:
    if not isinstance(payload, str):
        _fail(f"{label} is not canonical JSON text")
    try:
        value = json.loads(payload)
    except TypeError, ValueError:
        _fail(f"{label} is not canonical JSON")
    if type(value) is not dict or portable_canonical_json_bytes(value) != payload.encode("utf-8"):
        _fail(f"{label} is not canonical JSON")
    return cast(dict[str, Any], value)


def _canonical_json_value(
    payload: object,
    *,
    label: str,
    storage_class: type[str] | type[bytes],
    nullable: bool = False,
    require_canonical: bool = True,
) -> object:
    if payload is None and nullable:
        return None
    if type(payload) is not storage_class:
        _fail(f"{label} has an invalid SQLite storage class")
    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    try:
        value: object = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError, ValueError:
        _fail(f"{label} is not canonical JSON")
    if require_canonical and portable_canonical_json_bytes(value) != raw:
        _fail(f"{label} is not canonical JSON")
    return value


def _required_text(value: object, *, label: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        _fail(f"{label} is invalid")
    return value


def _optional_text(value: object, *, label: str) -> str | None:
    if value is not None and not isinstance(value, str):
        _fail(f"{label} is invalid")
    return value


def _effective_object(
    evidence: RetainedPrivacyEvidence | RepairedPrivacyEvidence,
) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads(effective_privacy_json(cast(RetainedPrivacyEvidence, evidence))),
    )


def _validate_snapshot_boundary(connection: sqlite3.Connection) -> None:
    if not connection.in_transaction:
        _fail("Portable v5 evidence requires an active transaction")
    version = connection.execute("PRAGMA user_version").fetchone()
    if (
        version is None
        or type(version[0]) is not int
        or version[0] not in _SUPPORTED_STATE_SCHEMA_VERSIONS
    ):
        _fail("Portable v5 evidence requires a supported current state schema")


def normalized_capture_rows(connection: sqlite3.Connection) -> list[dict[str, object]]:
    """Return the complete capture semantic projection in binary identity order.

    Stored JSON is decoded only after requiring its exact canonical representation.
    This makes the result JSON-safe without losing a meaningful SQLite value type.
    """
    normalized: list[dict[str, object]] = []
    for row in connection.execute(
        "SELECT capture_id, payload_family, payload_json, search_text, title, "
        "source_origin, source_reference, provenance_json, actor_id, role_claim_json, "
        "space_id, accepted_at, action, canonical_path, page_id, publication_id, "
        "publication_path FROM captures ORDER BY capture_id COLLATE BINARY"
    ):
        normalized.append(
            {
                "capture_id": _required_text(row["capture_id"], label="capture identity"),
                "payload_family": _required_text(
                    row["payload_family"], label="capture payload family"
                ),
                "payload_json": _canonical_json_value(
                    row["payload_json"],
                    label="capture payload",
                    storage_class=bytes,
                ),
                "search_text": _required_text(
                    row["search_text"], label="capture search text", allow_empty=True
                ),
                "title": _optional_text(row["title"], label="capture title"),
                "source_origin": _required_text(
                    row["source_origin"], label="capture source origin"
                ),
                "source_reference": _required_text(
                    row["source_reference"], label="capture source reference"
                ),
                "provenance_json": _canonical_json_value(
                    row["provenance_json"],
                    label="capture provenance",
                    storage_class=str,
                    nullable=True,
                    require_canonical=False,
                ),
                "actor_id": _optional_text(row["actor_id"], label="capture actor"),
                "role_claim_json": _canonical_json_value(
                    row["role_claim_json"],
                    label="capture role claim",
                    storage_class=str,
                    nullable=True,
                    require_canonical=False,
                ),
                "space_id": _optional_text(row["space_id"], label="capture space"),
                "accepted_at": _required_text(row["accepted_at"], label="capture acceptance time"),
                "action": _required_text(row["action"], label="capture action"),
                "canonical_path": _optional_text(
                    row["canonical_path"], label="capture canonical path"
                ),
                "page_id": _optional_text(row["page_id"], label="capture page"),
                "publication_id": _optional_text(
                    row["publication_id"], label="capture publication"
                ),
                "publication_path": _optional_text(
                    row["publication_path"], label="capture publication path"
                ),
            }
        )
    return normalized


def _retained_and_bases(
    connection: sqlite3.Connection,
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    dict[str, str | bytes | None],
    dict[tuple[str, str], RetainedPrivacyEvidence],
]:
    revisions = connection.execute(
        "SELECT r.capture_id, r.source_sha256, r.source_bytes, c.privacy_json, "
        "typeof(c.privacy_json) AS privacy_storage_class "
        "FROM source_revisions AS r JOIN captures AS c USING (capture_id) "
        "ORDER BY r.capture_id COLLATE BINARY"
    ).fetchall()
    if len(revisions) != connection.execute("SELECT count(*) FROM source_revisions").fetchone()[0]:
        _fail("retained privacy source coverage mismatch")

    retained_rows: list[dict[str, object]] = []
    retained: dict[str, str | bytes | None] = {}
    bases: dict[tuple[str, str], RetainedPrivacyEvidence] = {}
    for row in revisions:
        capture_id = row["capture_id"]
        source_digest = row["source_sha256"]
        source_bytes = row["source_bytes"]
        value = row["privacy_json"]
        storage_class = row["privacy_storage_class"]
        if not isinstance(capture_id, str) or not capture_id:
            _fail("retained privacy capture identity invalid")
        if not isinstance(source_bytes, bytes) or sha256(source_bytes).hexdigest() != (
            validate_issuer_digest(source_digest)
        ):
            _fail("retained privacy source digest mismatch")
        # captures.privacy_json has TEXT affinity in schema 9.  INTEGER and REAL
        # DML are coerced to text; observing either class means the schema was
        # corrupted outside the supported DDL and must fail closed.
        expected_storage = {None: "null", str: "text", bytes: "blob"}.get(
            None if value is None else type(value)
        )
        if expected_storage != storage_class:
            _fail("unsupported schema-9 retained privacy storage class")
        encoded = encode_retained_privacy_value(value)
        retained_rows.append(
            {
                "capture_id": capture_id,
                "source_sha256": source_digest,
                "privacy_json": encoded,
            }
        )
        retained[capture_id] = value
        bases[("source_revision", capture_id)] = project_retained_privacy_evidence((value,))

    member_rows = connection.execute(
        "SELECT revision_id, ordinal, capture_id FROM canonical_revision_members "
        "ORDER BY revision_id COLLATE BINARY, ordinal"
    ).fetchall()
    members: dict[str, list[str]] = {}
    expected_ordinal: dict[str, int] = {}
    for row in member_rows:
        revision_id = row["revision_id"]
        capture_id = row["capture_id"]
        ordinal = row["ordinal"]
        if (
            not isinstance(revision_id, str)
            or not revision_id
            or capture_id not in retained
            or type(ordinal) is not int
            or ordinal != expected_ordinal.get(revision_id, 0)
        ):
            _fail("canonical privacy membership invalid")
        members.setdefault(revision_id, []).append(capture_id)
        expected_ordinal[revision_id] = ordinal + 1
    for revision_id, capture_ids in members.items():
        bases[("canonical_revision", revision_id)] = project_retained_privacy_evidence(
            tuple(retained[capture_id] for capture_id in capture_ids)
        )

    stored_rows: dict[tuple[str, str], str] = {}
    for kind, table, column in (
        ("source_revision", "source_revision_privacy", "capture_id"),
        ("canonical_revision", "canonical_revision_privacy", "revision_id"),
    ):
        for row in connection.execute(
            f"SELECT {column} AS target_id, effective_privacy_json FROM {table} "  # noqa: S608
            f"ORDER BY {column} COLLATE BINARY"  # noqa: S608
        ):
            key = (kind, row["target_id"])
            if key in stored_rows or not isinstance(key[1], str):
                _fail("base privacy projection identity invalid")
            stored_rows[key] = row["effective_privacy_json"]
    if set(stored_rows) != set(bases):
        _fail("base privacy projection coverage mismatch")

    base_rows: list[dict[str, object]] = []
    for key, evidence in sorted(bases.items()):
        effective = _canonical_object(stored_rows[key], label="base privacy projection")
        if effective != _effective_object(evidence):
            _fail("base privacy projection mismatch")
        base_rows.append(
            {"target_kind": key[0], "target_id": key[1], "effective_privacy": effective}
        )
    return retained_rows, base_rows, retained, bases


def _invalid_evidence(
    connection: sqlite3.Connection,
    bases: Mapping[tuple[str, str], RetainedPrivacyEvidence],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    revision_markers: dict[tuple[str, str], set[tuple[str, str]]] = {}
    seen: set[tuple[str, str, str]] = set()
    for row in connection.execute(
        "SELECT target_kind, target_id, invalid_reason, invalid_evidence_sha256 "
        "FROM privacy_invalid_evidence ORDER BY target_kind COLLATE BINARY, "
        "target_id COLLATE BINARY, invalid_evidence_sha256 COLLATE BINARY"
    ):
        kind, target_id, reason, digest = tuple(row)
        if (
            kind not in {"source_revision", "canonical_revision", "search_document"}
            or not isinstance(target_id, str)
            or not target_id
            or reason not in _VALID_INVALID_REASONS
        ):
            _fail("invalid privacy evidence marker")
        validate_issuer_digest(digest)
        marker_key = (kind, target_id, digest)
        if marker_key in seen:
            _fail("duplicate privacy evidence marker")
        seen.add(marker_key)
        if kind != "search_document":
            if (kind, target_id) not in bases:
                _fail("privacy evidence marker target missing")
            revision_markers.setdefault((kind, target_id), set()).add((reason, digest))
        rows.append(
            {
                "target_kind": kind,
                "target_id": target_id,
                "invalid_reason": reason,
                "invalid_evidence_sha256": digest,
            }
        )
    for base_key, base in bases.items():
        expected = (
            set()
            if base.valid
            else {(cast(Any, base.invalid_reason).value, base.invalid_evidence_sha256)}
        )
        if revision_markers.get(base_key, set()) != expected:
            _fail("privacy evidence marker coverage mismatch")
    return rows


def _repairs(connection: sqlite3.Connection) -> list[dict[str, object]]:
    try:
        audit_privacy_repair_ledger(connection)
    except (PrivacyRepairError, T03Error) as error:
        raise ValidationError("privacy repair ledger audit failed") from error
    rows: list[dict[str, object]] = []
    repair_ids: set[str] = set()
    operation_ids: set[str] = set()
    previous_sequence = 0
    for row in connection.execute(
        "SELECT repair_id, repair_sequence, operation_id, receipt_json, "
        "replacement_privacy_json FROM privacy_repair_ledger ORDER BY repair_sequence"
    ):
        sequence = row["repair_sequence"]
        if (
            type(sequence) is not int
            or sequence <= previous_sequence
            or row["repair_id"] in repair_ids
            or row["operation_id"] in operation_ids
        ):
            _fail("privacy repair order or identity invalid")
        receipt_payload = _canonical_object(row["receipt_json"], label="privacy repair receipt")
        receipt = PrivacyRepairReceipt.decode(receipt_payload)
        replacement = _canonical_object(
            row["replacement_privacy_json"], label="privacy repair replacement"
        )
        if (
            receipt.repair_id != row["repair_id"]
            or receipt.repair_sequence != sequence
            or receipt.operation_id != row["operation_id"]
            or receipt.replacement.to_dict() != replacement
        ):
            _fail("privacy repair receipt mismatch")
        rows.append(receipt_payload)
        repair_ids.add(receipt.repair_id)
        operation_ids.add(receipt.operation_id)
        previous_sequence = sequence
    return rows


def _resolved_revisions(
    connection: sqlite3.Connection,
    bases: Mapping[tuple[str, str], RetainedPrivacyEvidence],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for (kind, target_id), _base in sorted(bases.items()):
        if kind == "source_revision":
            evidence, applied = resolve_search_privacy(
                connection,
                result_id=target_id,
                capture_id=target_id,
                record_type="source",
            )
        else:
            evidence, active = resolve_canonical_revision_privacy(connection, revision_id=target_id)
            applied = None
            if active is not None:
                evidence = apply_privacy_repair(
                    evidence,
                    active.replacement,
                    applied_repair_id=active.repair_id,
                    applied_repair_sequence=active.repair_sequence,
                )
                applied = (active.repair_id, active.repair_sequence)
        rows.append(
            {
                "target_kind": kind,
                "target_id": target_id,
                "effective_privacy": _effective_object(evidence),
                "applied_repair_id": None if applied is None else applied[0],
                "applied_repair_sequence": None if applied is None else applied[1],
            }
        )
    return rows


def _expected_search_identities(
    connection: sqlite3.Connection,
) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for row in connection.execute(
        "SELECT head_capture_id FROM logical_sources "
        "WHERE historical_only=0 AND lifecycle='active' AND availability='available' "
        "ORDER BY head_capture_id COLLATE BINARY"
    ):
        capture_id = row["head_capture_id"]
        if not isinstance(capture_id, str) or not capture_id:
            _fail("source search identity invalid")
        rows.append((capture_id, capture_id, "source"))
    for row in connection.execute(
        "SELECT c.page_id, c.capture_id FROM captures AS c "
        "WHERE c.canonical_path IS NOT NULL AND c.page_id IS NOT NULL "
        "AND c.publication_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM review_page_heads h WHERE h.page_id=c.page_id) "
        "UNION ALL "
        "SELECT d.page_id, p.capture_id FROM decisions AS d "
        "JOIN proposals AS p USING (proposal_id) "
        "WHERE d.canonical_path IS NOT NULL AND d.page_id IS NOT NULL "
        "AND d.publication_id IS NOT NULL AND d.publication_path IS NOT NULL "
        "AND d.outcome IN ('approved','edited') "
        "AND NOT EXISTS (SELECT 1 FROM review_page_heads h WHERE h.page_id=d.page_id) "
        "UNION ALL "
        "SELECT h.page_id, h.capture_id FROM review_page_heads AS h "
        "JOIN decisions AS d ON d.publication_id=h.publication_id "
        "AND d.page_id=h.page_id AND d.proposal_id=h.proposal_id "
        "AND d.canonical_path=h.canonical_path "
        "WHERE d.stage=3 AND d.outcome IN ('approved','edited')"
    ):
        page_id, capture_id = tuple(row)
        if not isinstance(page_id, str) or not page_id or not isinstance(capture_id, str):
            _fail("canonical search identity invalid")
        rows.append((page_id, capture_id, "canonical"))
    rows.sort(key=lambda item: item[0])
    if len({row[0] for row in rows}) != len(rows):
        _fail("search result identity is not unique")
    return rows


def normalized_authoritative_search_rows(
    connection: sqlite3.Connection,
) -> list[dict[str, object]]:
    """Return complete current search semantics ordered by result identity.

    Mutable rows supply committed public content, while identity coverage and
    privacy are independently derived from durable source/page heads and the
    repair resolver. Source space uses the current logical-source route rather
    than the capture's historical intake space.
    """
    expected = _expected_search_identities(connection)
    stored = connection.execute(
        "SELECT result_id, capture_id, record_type, payload_family, space_id, title, body, "
        "trust, provenance_json, canonical_path, updated_at, effective_tier, "
        "effective_cloud, effective_external_egress, invalid_evidence_reason, "
        "invalid_evidence_sha256, applied_repair_id, applied_repair_sequence "
        "FROM search_documents ORDER BY result_id COLLATE BINARY"
    ).fetchall()
    actual = [(row["result_id"], row["capture_id"], row["record_type"]) for row in stored]
    if actual != expected:
        _fail("repair-aware search coverage mismatch")
    source_spaces: dict[str, str | None] = {}
    for row in connection.execute(
        "SELECT head_capture_id, space_id FROM logical_sources "
        "WHERE historical_only=0 AND lifecycle='active' AND availability='available' "
        "ORDER BY head_capture_id COLLATE BINARY"
    ):
        capture_id = _required_text(row["head_capture_id"], label="source search identity")
        if capture_id in source_spaces:
            _fail("source search identity is not unique")
        source_spaces[capture_id] = _optional_text(
            row["space_id"], label="current logical-source space"
        )
    rows: list[dict[str, object]] = []
    for row in stored:
        result_id = _required_text(row["result_id"], label="search result identity")
        capture_id = _required_text(row["capture_id"], label="search capture identity")
        record_type = _required_text(row["record_type"], label="search record type")
        space_id = _optional_text(row["space_id"], label="search space")
        if record_type == "source" and space_id != source_spaces.get(capture_id):
            _fail("source search current route mismatch")
        evidence, applied = resolve_search_privacy(
            connection,
            result_id=result_id,
            capture_id=capture_id,
            record_type=record_type,
        )
        if evidence.invalid_reason is not None:
            marker = connection.execute(
                "SELECT invalid_reason FROM privacy_invalid_evidence "
                "WHERE target_kind='search_document' AND target_id=? "
                "AND invalid_evidence_sha256=?",
                (result_id, evidence.invalid_evidence_sha256),
            ).fetchone()
            if marker is None or marker[0] != evidence.invalid_reason.value:
                _fail("current search invalid evidence marker mismatch")
        stored_privacy = (
            row["effective_tier"],
            row["effective_cloud"],
            row["effective_external_egress"],
            row["invalid_evidence_reason"],
            row["invalid_evidence_sha256"],
            row["applied_repair_id"],
            row["applied_repair_sequence"],
        )
        expected_privacy = (
            evidence.tier.value,
            int(evidence.authority.cloud),
            int(evidence.authority.external_egress),
            None if evidence.invalid_reason is None else evidence.invalid_reason.value,
            evidence.invalid_evidence_sha256,
            None if applied is None else applied[0],
            None if applied is None else applied[1],
        )
        if stored_privacy != expected_privacy:
            _fail("repair-aware search privacy mismatch")
        rows.append(
            {
                "result_id": result_id,
                "capture_id": capture_id,
                "record_type": record_type,
                "payload_family": _required_text(
                    row["payload_family"], label="search payload family"
                ),
                "space_id": space_id,
                "title": _required_text(row["title"], label="search title", allow_empty=True),
                "body": _required_text(row["body"], label="search body", allow_empty=True),
                "trust": _required_text(row["trust"], label="search trust"),
                "provenance_json": _canonical_json_value(
                    row["provenance_json"],
                    label="search provenance",
                    storage_class=str,
                    require_canonical=False,
                ),
                "canonical_path": _optional_text(
                    row["canonical_path"], label="search canonical path"
                ),
                "updated_at": _required_text(row["updated_at"], label="search update time"),
                "effective_privacy": _effective_object(evidence),
                "applied_repair_id": None if applied is None else applied[0],
                "applied_repair_sequence": None if applied is None else applied[1],
            }
        )
    return rows


def _resolved_search(
    rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    return [
        {
            "result_id": row["result_id"],
            "capture_id": row["capture_id"],
            "record_type": row["record_type"],
            "effective_privacy": row["effective_privacy"],
            "applied_repair_id": row["applied_repair_id"],
            "applied_repair_sequence": row["applied_repair_sequence"],
        }
        for row in rows
    ]


def _issuer_sidecars(
    connection: sqlite3.Connection, *, tenant_id: str
) -> tuple[dict[str, object], dict[str, object]]:
    verify_issuer_evidence(connection, tenant_id=tenant_id)
    identities = connection.execute(
        "SELECT tenant_id, brain_id, issuer_epoch, legacy_issuer_epoch, recorded_at "
        "FROM brain_identity"
    ).fetchall()
    if len(identities) != 1:
        _fail("issuer identity missing")
    identity = identities[0]
    bindings = [
        {
            "artifact_path": row["artifact_path"],
            "jsonl_ordinal": row["jsonl_ordinal"],
            "payload_sha256": row["payload_sha256"],
            "issuer_epoch": row["issuer_epoch"],
        }
        for row in connection.execute(
            "SELECT artifact_path, jsonl_ordinal, payload_sha256, issuer_epoch "
            "FROM legacy_issuer_bindings ORDER BY artifact_path COLLATE BINARY, "
            "coalesce(jsonl_ordinal, -1)"
        )
    ]
    marker_rows = connection.execute(
        "SELECT source_manifest_bytes, source_manifest_sha256, brain_id, "
        "legacy_issuer_epoch, current_issuer_epoch, legacy_binding_manifest_sha256, "
        "recorded_at FROM issuer_migration_marker"
    ).fetchall()
    marker: dict[str, object] | None = None
    if marker_rows:
        if len(marker_rows) != 1:
            _fail("issuer migration marker is not singular")
        row = marker_rows[0]
        marker = {
            "source_portable_manifest_bytes_base64": base64.b64encode(
                row["source_manifest_bytes"]
            ).decode("ascii"),
            "source_portable_manifest_sha256": row["source_manifest_sha256"],
            "brain_id": row["brain_id"],
            "designated_legacy_issuer_epoch": row["legacy_issuer_epoch"],
            "current_issuer_epoch": row["current_issuer_epoch"],
            "legacy_binding_manifest_sha256": row["legacy_binding_manifest_sha256"],
            "recorded_at": row["recorded_at"],
        }
    return (
        {"schema_version": _EVIDENCE_SCHEMA_VERSION, "bindings": bindings},
        {
            "schema_version": _EVIDENCE_SCHEMA_VERSION,
            "tenant_id": identity["tenant_id"],
            "brain_id": identity["brain_id"],
            "current_issuer_epoch": identity["issuer_epoch"],
            "legacy_issuer_epoch": identity["legacy_issuer_epoch"],
            "identity_recorded_at": identity["recorded_at"],
            "migration_marker": marker,
        },
    )


def serialize_portable_v5_state(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    relationship_sidecar_present: bool,
) -> PortableV5StateEvidence:
    """Serialize all v5 state from one active caller-owned schema-nine snapshot."""
    if type(relationship_sidecar_present) is not bool:
        _fail("relationship sidecar presence must be a bool")
    _validate_snapshot_boundary(connection)
    bindings, issuer = _issuer_sidecars(connection, tenant_id=tenant_id)
    retained_rows, base_rows, _retained, bases = _retained_and_bases(connection)
    captures = normalized_capture_rows(connection)
    search = normalized_authoritative_search_rows(connection)
    privacy = {
        "schema_version": _EVIDENCE_SCHEMA_VERSION,
        "retained_privacy": retained_rows,
        "base_projections": base_rows,
        "invalid_evidence": _invalid_evidence(connection, bases),
        "repairs": _repairs(connection),
        "resolved_revisions": _resolved_revisions(connection, bases),
        "resolved_search": _resolved_search(search),
    }
    relation = relationship_metadata(connection)
    if relation is not None and not relationship_sidecar_present:
        _fail("relationship sidecar presence mismatch")
    if relationship_sidecar_present and relation is None:
        relation = {"schema_version": 1, "relationships": [], "decisions": []}
    sources = source_metadata(connection)
    sidecars = {
        EFFECTIVE_PRIVACY_PATH: portable_canonical_json_bytes(privacy),
        LEGACY_BINDINGS_PATH: portable_canonical_json_bytes(bindings),
        ISSUER_MIGRATION_PATH: portable_canonical_json_bytes(issuer),
    }
    semantic_state: dict[str, object] = {
        "schema_version": _EVIDENCE_SCHEMA_VERSION,
        "captures": captures,
        "authoritative_search": search,
        "source_metadata": sources,
        "relationship_evidence_present": relationship_sidecar_present,
        "relationship_evidence": relation,
        "legacy_bindings": bindings,
        "issuer_migration": issuer,
        "effective_privacy": privacy,
    }
    digest = sha256(portable_canonical_json_bytes(semantic_state)).hexdigest()
    return PortableV5StateEvidence(
        sidecars=MappingProxyType(sidecars),
        semantic_state=MappingProxyType(semantic_state),
        semantic_state_sha256=digest,
    )


def verify_portable_v5_semantic_state(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    relationship_sidecar_present: bool,
    expected_sha256: str,
) -> PortableV5StateEvidence:
    """Recompute and compare the semantic commitment after restore or reopen."""
    validate_issuer_digest(expected_sha256)
    evidence = serialize_portable_v5_state(
        connection,
        tenant_id=tenant_id,
        relationship_sidecar_present=relationship_sidecar_present,
    )
    if evidence.semantic_state_sha256 != expected_sha256:
        _fail("Portable v5 semantic state mismatch")
    return evidence


__all__ = [
    "PortableV5StateEvidence",
    "normalized_authoritative_search_rows",
    "normalized_capture_rows",
    "serialize_portable_v5_state",
    "verify_portable_v5_semantic_state",
]

"""Slack variant bindings and engine-owned selection resolution."""

from __future__ import annotations

import json
import sqlite3
from hashlib import sha256
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.core.models import Authority, PrivacyDecision, PrivacyReason, PrivacyTier

from .sharing_contracts import SharingError, SlackSelectionCreateRequest, SlackSharingPreviewRequest
from .slack_sharing_projection import PROJECTION_VERSION, project_copy_text, projection_sha256
from .t03_contracts import EffectiveAuthority

if TYPE_CHECKING:
    from .contracts import LocalEngineContext
    from .local import BrainEngine


def copy_privacy() -> PrivacyDecision:
    return PrivacyDecision.create(
        tier=PrivacyTier.WORK,
        reason=PrivacyReason.POLICY_WORK,
        policy_version="sharing-slack-work-copy.v1",
        authority=Authority(cloud=False, external_egress=True),
    )


def selection_value(
    connection: sqlite3.Connection, selection_id: str
) -> tuple[dict[str, Any], str]:
    from .sharing import _json

    row = connection.execute(
        "SELECT * FROM slack_recall_selections WHERE selection_id=?", (selection_id,)
    ).fetchone()
    if row is None:
        raise SharingError("not_found")
    value = _json(bytes(row["selection_bytes"]))
    request = SlackSelectionCreateRequest(**_json(bytes(row["request_bytes"])))
    digest = sha256(
        b"open-brain-slack-recall-selection.v1\0" + portable_canonical_json_bytes(value)
    ).hexdigest()
    expected = {
        "selection_version": 1,
        "selection_id": selection_id,
        "brain_id": request.brain_id,
        "issuer_epoch": request.issuer_epoch,
        "destination_space_id": request.destination_space_id,
        "member_source_ids": list(request.member_source_ids),
    }
    if (
        value != expected
        or digest != row["selection_sha256"]
        or request.request_sha256 != row["request_sha256"]
        or portable_canonical_json_bytes(request.value()) != bytes(row["request_bytes"])
        or request.operation_id != row["operation_id"]
    ):
        raise SharingError("binding_mismatch")
    return value, digest


def create_selection(engine: BrainEngine, request: SlackSelectionCreateRequest) -> dict[str, Any]:
    from .normalization import _timestamp
    from .sharing import _identity, _original_evidence

    if not isinstance(request, SlackSelectionCreateRequest):
        raise SharingError("invalid_arguments")
    with engine._writer_lease.acquire_shared_writer(), engine._store.transaction() as connection:
        _identity(connection, request.brain_id, request.issuer_epoch)
        previous = connection.execute(
            "SELECT * FROM slack_recall_selections WHERE operation_id=?", (request.operation_id,)
        ).fetchone()
        if previous is not None:
            if bytes(previous["request_bytes"]) != portable_canonical_json_bytes(request.value()):
                raise SharingError("invalid_arguments")
            value, _ = selection_value(connection, previous["selection_id"])
            return value
        for source_id in request.member_source_ids:
            row = connection.execute(
                "SELECT s.*,l.lifecycle_version FROM logical_sources s "
                "JOIN source_lifecycle_state l USING(source_id) WHERE source_id=?",
                (source_id,),
            ).fetchone()
            if row is None:
                raise SharingError("not_found")
            _original_evidence(
                connection,
                engine,
                SlackSharingPreviewRequest(
                    operation_id=request.operation_id,
                    source_id=source_id,
                    expected_head=row["head_capture_id"],
                    expected_head_version=row["head_version"],
                    expected_lifecycle_version=row["lifecycle_version"],
                    expected_route_version=row["route_version"],
                    brain_id=request.brain_id,
                    issuer_epoch=request.issuer_epoch,
                    destination_space_id=request.destination_space_id,
                    provider_ids=("selection-validation",),
                ),
            )
        selection_id = "selection_" + str(uuid4())
        value = {
            "selection_version": 1,
            "selection_id": selection_id,
            "brain_id": request.brain_id,
            "issuer_epoch": request.issuer_epoch,
            "destination_space_id": request.destination_space_id,
            "member_source_ids": list(request.member_source_ids),
        }
        raw = portable_canonical_json_bytes(value)
        digest = sha256(b"open-brain-slack-recall-selection.v1\0" + raw).hexdigest()
        connection.execute(
            "INSERT INTO slack_recall_selections VALUES(?,?,?,?,?,?,?)",
            (
                selection_id,
                request.operation_id,
                portable_canonical_json_bytes(request.value()),
                request.request_sha256,
                raw,
                digest,
                _timestamp(engine._clock()),
            ),
        )
        return value


def slack_preview_fields(
    connection: sqlite3.Connection, request: SlackSharingPreviewRequest, evidence: dict[str, Any]
) -> dict[str, Any]:
    selection_sha = None
    if request.selection_id is not None:
        value, selection_sha = selection_value(connection, request.selection_id)
        if (value["brain_id"], value["issuer_epoch"], value["destination_space_id"]) != (
            request.brain_id,
            request.issuer_epoch,
            request.destination_space_id,
        ) or request.source_id not in value["member_source_ids"]:
            raise SharingError("binding_mismatch")
    return {
        "dto_version": 2,
        "sharing_kind": "slack_work_evidence",
        "destination_space_id": request.destination_space_id,
        "selection_id": request.selection_id,
        "selection_sha256": selection_sha,
        "namespace": evidence["namespace"],
        "revision_key": evidence["revision_key"],
        "intake_binding_sha256": evidence["intake_binding_sha256"],
        "projection_version": PROJECTION_VERSION,
        "projection_sha256": projection_sha256(evidence["text"]),
        "message_text_sha256": sha256(
            json.loads(evidence["text"])["message_text"].encode()
        ).hexdigest(),
        "copy_privacy": copy_privacy().to_dict(),
    }


def slack_copy_eligible(
    connection: sqlite3.Connection,
    profile: LocalEngineContext | None,
    capture_id: str,
    frozen: dict[str, Any],
    *,
    allow_projection_drift: bool = False,
) -> bool:
    from .sharing import _json, _original_evidence

    if profile is None:
        return False
    row = connection.execute(
        "SELECT request_bytes,request_sha256 FROM sharing_previews WHERE preview_id=?",
        (frozen["preview_id"],),
    ).fetchone()
    if row is None:
        return False
    request = SlackSharingPreviewRequest(**_json(bytes(row["request_bytes"])))
    if (
        portable_canonical_json_bytes(request.value()) != bytes(row["request_bytes"])
        or request.request_sha256 != row["request_sha256"]
        or list(request.provider_ids) != frozen["provider_ids"]
        or request.source_id != frozen["source_id"]
        or request.expected_head != frozen["original_capture_id"]
        or request.expected_head_version != frozen["head_version"]
        or request.expected_lifecycle_version != frozen["lifecycle_version"]
        or request.expected_route_version != frozen["route_version"]
        or request.brain_id != frozen["brain_id"]
        or request.issuer_epoch != frozen["issuer_epoch"]
    ):
        return False
    evidence = _original_evidence(
        connection, cast("BrainEngine", SimpleNamespace(profile=profile)), request
    )
    expected = slack_preview_fields(connection, request, evidence)
    projection_fields = {"projection_version", "projection_sha256", "message_text_sha256"}
    if any(
        frozen.get(key) != value
        for key, value in expected.items()
        if not allow_projection_drift or key not in projection_fields
    ):
        return False
    copy = connection.execute(
        "SELECT s.*,l.lifecycle_version FROM logical_sources s "
        "JOIN source_revisions r USING(source_id) JOIN source_lifecycle_state l USING(source_id) "
        "WHERE r.capture_id=?",
        (capture_id,),
    ).fetchone()
    return bool(
        copy is not None
        and copy["head_capture_id"] == capture_id
        and copy["space_id"] == request.destination_space_id
        and not copy["historical_only"]
        and copy["lifecycle"] == "active"
        and copy["availability"] == "available"
        and frozen["copy_privacy"] == copy_privacy().to_dict()
        and (
            allow_projection_drift
            or (
                frozen["projection_version"] == PROJECTION_VERSION
                and evidence["text"] == frozen["text"]
                and project_copy_text(frozen["text"], source_reference=frozen["marker"])
                == frozen["text"]
                and projection_sha256(frozen["text"]) == frozen["projection_sha256"]
            )
        )
    )


def read_selection(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
    capture_id: str,
    authority: EffectiveAuthority,
) -> dict[str, Any] | None:
    from .mcp_eligibility import capture_visible
    from .records import RecordProjector
    from .sharing import EligibilityMode, _json, sharing_eligible
    from .t03_contracts import T03Error

    if not capture_visible(connection, capture_id, authority):
        raise T03Error("not_found")
    row = connection.execute(
        "SELECT p.preview_bytes FROM sharing_links l JOIN sharing_decisions d USING(approval_id) "
        "JOIN sharing_previews p USING(preview_id) WHERE l.copy_capture_id=?",
        (capture_id,),
    ).fetchone()
    if row is None:
        return None
    frozen = _json(bytes(row["preview_bytes"]))
    if frozen.get("dto_version") != 2 or frozen.get("selection_id") is None:
        return None
    value, digest = selection_value(connection, frozen["selection_id"])
    if digest != frozen["selection_sha256"]:
        raise T03Error("not_found")
    members: list[dict[str, Any]] = []
    for ordinal, source_id in enumerate(value["member_source_ids"]):
        member: dict[str, Any] = {"ordinal": ordinal, "status": "unavailable"}
        candidates = connection.execute(
            "SELECT l.copy_capture_id,p.preview_bytes FROM sharing_links l "
            "JOIN sharing_decisions d USING(approval_id) JOIN sharing_previews p USING(preview_id) "
            "WHERE p.source_id=? ORDER BY l.copy_capture_id",
            (source_id,),
        ).fetchall()
        for candidate in candidates:
            try:
                other = _json(bytes(candidate["preview_bytes"]))
            except KeyError, TypeError, ValueError, UnicodeError:
                continue
            if (
                other.get("selection_id") != value["selection_id"]
                or other.get("selection_sha256") != digest
                or other.get("destination_space_id") != value["destination_space_id"]
            ):
                continue
            candidate_id = candidate["copy_capture_id"]
            try:
                # Apply direct-read gates without recursively expanding the selection.
                RecordProjector(profile, connection, authority).source(
                    candidate_id, expected=candidate_id
                )
            except T03Error:
                continue
            if sharing_eligible(
                connection,
                candidate_id,
                mode=EligibilityMode.EXTERNAL_READ
                if authority.provider_id
                else EligibilityMode.LOCAL_CURRENT,
                provider_id=authority.provider_id,
                brain_id=authority.brain_id,
                issuer_epoch=authority.issuer_epoch,
                profile=profile,
            ):
                member.update(status="visible", record_id=candidate_id, revision_id=candidate_id)
                break
        members.append(member)
    return {
        "selection_id": value["selection_id"],
        "selection_version": 1,
        "member_count": len(members),
        "member_ordinal": value["member_source_ids"].index(frozen["source_id"]),
        "members": members,
    }

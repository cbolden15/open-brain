"""Portable12-only Slack sharing and immutable selection evidence validation."""
# ruff: noqa: E501

from __future__ import annotations

import json
from collections.abc import Mapping
from hashlib import sha256
from typing import Any

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.engine.sharing_contracts import (
    SlackSelectionCreateRequest,
    SlackSharingPreviewRequest,
)
from open_brain_engine.engine.slack_sharing_projection import (
    PROJECTION_VERSION,
    build_envelope,
    project_copy_text,
    projection_sha256,
)

from .v1 import PortableValidationError, _timestamp
from .v5 import ISSUER_MIGRATION_PATH
from .v7 import _b64, _decoded, _rows, _uuid4

SELECTIONS_PATH = "history/sharing/slack-recall-selections-v1.json"
SELECTION_COLUMNS = (
    "selection_id:s",
    "operation_id:s",
    "request_bytes:b",
    "request_sha256:s",
    "selection_bytes:b",
    "selection_sha256:s",
    "created_at:s",
)
SLACK_PREVIEW_FIELDS = frozenset(
    {
        "sharing_kind",
        "destination_space_id",
        "selection_id",
        "selection_sha256",
        "namespace",
        "revision_key",
        "intake_binding_sha256",
        "projection_version",
        "projection_sha256",
        "message_text_sha256",
    }
)


def validate_selections(files: Mapping[str, bytes]) -> dict[str, Any]:
    try:
        from .facets_v10 import references_from_portable_files

        value = _decoded(files[SELECTIONS_PATH])
        if (
            set(value) != {"schema_version", "selections"}
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 1
        ):
            raise ValueError("selection sidecar version invalid")
        rows = _rows(value["selections"], SELECTION_COLUMNS)
        issuer = _decoded(files[ISSUER_MIGRATION_PATH])
        references = references_from_portable_files(files)
        operations: set[str] = set()
        for row in rows:
            _uuid4(row["selection_id"], "selection_")
            request_raw = _b64(row["request_bytes"])
            request = SlackSelectionCreateRequest(**_decoded(request_raw))
            selection = _decoded(_b64(row["selection_bytes"]))
            expected = {
                "selection_version": 1,
                "selection_id": row["selection_id"],
                "brain_id": request.brain_id,
                "issuer_epoch": request.issuer_epoch,
                "destination_space_id": request.destination_space_id,
                "member_source_ids": list(request.member_source_ids),
            }
            if (
                selection != expected
                or canonical(request.value()) != request_raw
                or request.request_sha256 != row["request_sha256"]
                or request.operation_id != row["operation_id"]
                or row["operation_id"] in operations
                or (request.brain_id, request.issuer_epoch)
                != (issuer["brain_id"], issuer["current_issuer_epoch"])
                or request.destination_space_id not in references.space_ids
                or not set(request.member_source_ids) <= references.source_ids
                or sha256(
                    b"open-brain-slack-recall-selection.v1\0" + canonical(selection)
                ).hexdigest()
                != row["selection_sha256"]
            ):
                raise ValueError("selection operation binding invalid")
            _timestamp(row["created_at"], "selection creation")
            operations.add(row["operation_id"])
        return value
    except PortableValidationError:
        raise
    except KeyError, TypeError, ValueError, UnicodeError:
        raise PortableValidationError("Portable12 selection evidence invalid") from None


def validate_slack_preview(
    files: Mapping[str, bytes],
    preview: dict[str, Any],
    request: Any,
    original: dict[str, Any],
    envelope: dict[str, Any],
    admission: dict[str, Any],
) -> None:
    if not isinstance(request, SlackSharingPreviewRequest):
        raise ValueError("Slack preview request invalid")
    namespace = envelope["submission"]["namespace"]
    namespace_row = next(
        row for row in admission["source_namespaces"] if row["source_id"] == request.source_id
    )
    claimed = envelope["submission"]
    intake = next(
        row
        for row in admission["source_intakes"]
        if row["source_id"] == request.source_id and row["revision_key"] == claimed["revision_key"]
    )
    commitment = sha256(
        canonical(
            {
                "submission": _decoded(_b64(intake["submission_json"])),
                "receipt": json.loads(intake["receipt_json"]),
                "plan": json.loads(intake["plan_json"]),
                "namespace": json.loads(namespace_row["namespace_json"]),
            }
        )
    ).hexdigest()
    expected_text = build_envelope(
        {
            "namespace": namespace,
            "revision": {"source_reference": original["source"]["reference"]},
            "record": original,
        },
        capture_id=request.expected_head,
    )
    if (
        preview["sharing_kind"] != "slack_work_evidence"
        or preview["destination_space_id"] != request.destination_space_id
        or preview["space_id"] != request.destination_space_id
        or preview["selection_id"] != request.selection_id
        or preview["namespace"] != namespace
        or preview["revision_key"] != claimed["revision_key"]
        or namespace["connector_name"] != "slack"
        or not namespace["connection_id"].startswith("account:")
        or not namespace["resource_id"].startswith("channel:")
        or not namespace["external_id"].startswith(("message:", "reply:"))
        or claimed != _decoded(_b64(intake["submission_json"]))
        or preview["intake_binding_sha256"] != commitment
        or preview["projection_version"] != PROJECTION_VERSION
        or preview["text"] != expected_text
        or project_copy_text(preview["text"], source_reference=preview["marker"]) != preview["text"]
        or preview["projection_sha256"] != projection_sha256(expected_text)
        or preview["message_text_sha256"]
        != sha256(json.loads(expected_text)["message_text"].encode()).hexdigest()
    ):
        raise ValueError("Slack source/citation proof mismatch")
    selections = validate_selections(files)
    if request.selection_id is None:
        if preview["selection_sha256"] is not None:
            raise ValueError("unselected preview carries selection digest")
    else:
        row = next(
            row for row in selections["selections"] if row["selection_id"] == request.selection_id
        )
        selected = _decoded(_b64(row["selection_bytes"]))
        if (
            request.source_id not in selected["member_source_ids"]
            or preview["selection_sha256"] != row["selection_sha256"]
            or selected["destination_space_id"] != request.destination_space_id
            or row["created_at"] > preview["created_at"]
        ):
            raise ValueError("Slack selection membership mismatch")


def validate_sharing_authority_v12(files: Mapping[str, bytes]) -> dict[str, Any]:
    from .v7 import _validate_sharing_authority

    validate_selections(files)
    value = _validate_sharing_authority(files, allow_slack=True)
    from .v8_capture_metadata import CAPTURE_METADATA_PATH

    linked = {row["copy_capture_id"] for row in value["sharing_links"]}
    for row in _decoded(files[CAPTURE_METADATA_PATH])["captures"]:
        if (
            row["delivery_id"].startswith(("sharing-copy.", "sharing.copy."))
            and row["capture_id"] not in linked
        ):
            raise PortableValidationError("Portable12 unlinked copy delivery")
    return value

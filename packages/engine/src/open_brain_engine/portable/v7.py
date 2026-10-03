"""Portable Brain 7: required immutable sharing authority and semantic links."""
# ruff: noqa: E501

from __future__ import annotations

import base64
import json
import os
import tomllib
from collections.abc import Mapping
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast
from uuid import UUID

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.storage.filesystem import (
    RootConfinementError,
    RootIdentity,
    assert_root_identity,
    capture_root_identity,
    open_root_descriptor,
)

from .v1 import (
    PortableSnapshot,
    PortableValidationError,
    _portable_record,
    _snapshot_directory,
    _timestamp,
)
from .v4 import SOURCE_METADATA_PATH
from .v5 import ISSUER_MIGRATION_PATH, _manifest, manifest_v5
from .v6 import SOURCE_ADMISSION_PATH, SOURCE_LIFECYCLE_PATH, validate_portable_file_set_v6

SHARING_APPROVALS_PATH = "history/sharing/approvals-v1.json"
V7_SIDECAR_PATHS = frozenset({SHARING_APPROVALS_PATH})
PORTABLE_V7_SCHEMA_CATALOG_DIGEST = sha256(
    canonical(
        {"base": "portable-brain-v6-source-authority", "sharing_authority": 1, "schema_version": 7}
    )
).hexdigest()

# Suffixes specify exact JSON/SQLite type; ? means nullable.
SHARING_COLUMNS: dict[str, tuple[str, ...]] = {
    "sharing_job_identity": (
        "singleton:i",
        "tenant_id:s",
        "actor_id:s",
        "role_id:s",
        "role_claim_id:s",
        "brain_id:s",
        "issuer_epoch:i",
    ),
    "sharing_previews": (
        "preview_id:s",
        "operation_id:s",
        "request_bytes:b",
        "request_sha256:s",
        "preview_bytes:b",
        "preview_sha256:s",
        "source_id:s",
        "original_capture_id:s",
        "expires_at:s",
    ),
    "sharing_decisions": (
        "approval_id:s",
        "operation_id:s",
        "preview_id:s",
        "request_bytes:b",
        "request_sha256:s",
        "decision:s",
        "approval_version:i",
        "destination_brain_id:s",
        "copy_delivery_id:s?",
        "copy_submission_bytes:b?",
        "copy_submission_sha256:s?",
        "copy_capture_id:s?",
        "receipt_bytes:b",
    ),
    "sharing_links": (
        "approval_id:s",
        "original_capture_id:s",
        "copy_capture_id:s",
        "marker:s",
        "submission_sha256:s",
        "linked_at:s",
    ),
    "sharing_revocations": (
        "operation_id:s",
        "approval_id:s",
        "request_bytes:b",
        "request_sha256:s",
        "resulting_version:i",
        "reason:s",
        "recorded_at:s",
        "receipt_bytes:b",
    ),
}


def manifest_v7(
    files: Mapping[str, bytes], *, tenant_id: str, export_id: str, created_at: str
) -> dict[str, object]:
    value = manifest_v5(files, tenant_id=tenant_id, export_id=export_id, created_at=created_at)
    value.update(
        schema_version=7,
        layout_version=7,
        contract_version="7",
        compatibility={"maximum_contract_version": "7", "minimum_contract_version": "1"},
        schema_catalog_digest=PORTABLE_V7_SCHEMA_CATALOG_DIGEST,
    )
    return value


def _b64(value: str) -> bytes:
    raw = base64.b64decode(value, validate=True)
    if base64.b64encode(raw).decode("ascii") != value:
        raise ValueError("noncanonical sharing base64")
    return raw


def _decoded(raw: bytes) -> dict[str, Any]:
    result = json.loads(raw)
    if type(result) is not dict or canonical(result) != raw:
        raise ValueError("sharing JSON is not canonical")
    return result


def _rows(value: Any, columns: tuple[str, ...]) -> list[dict[str, Any]]:
    if type(value) is not list:
        raise ValueError("sharing rows invalid")
    names = {item.split(":")[0] for item in columns}
    result: list[dict[str, Any]] = []
    previous: str | int | None = None
    for row in value:
        if type(row) is not dict or set(row) != names:
            raise ValueError("sharing row fields invalid")
        for column in columns:
            name, kind = column.split(":")
            item = row[name]
            if item is None and kind.endswith("?"):
                continue
            if kind.startswith("i"):
                if type(item) is not int or item < 0:
                    raise ValueError("sharing integer invalid")
            elif type(item) is not str or not item or "\x00" in item:
                raise ValueError("sharing string invalid")
            if kind.startswith("b"):
                _b64(item)
            elif name.endswith("sha256") and (
                len(item) != 64 or any(char not in "0123456789abcdef" for char in item)
            ):
                raise ValueError("sharing digest invalid")
        identity = row[columns[0].split(":")[0]]
        if previous is not None and identity <= previous:
            raise ValueError("sharing row identities not sorted and unique")
        previous = identity
        result.append(row)
    return result


_PREVIEW_FIELDS = frozenset(
    {
        "dto_version",
        "preview_id",
        "source_id",
        "original_capture_id",
        "source_sha256",
        "payload_sha256",
        "privacy_sha256",
        "provenance_sha256",
        "managed_delivery_id",
        "managed_envelope_sha256",
        "observation",
        "head_version",
        "lifecycle_version",
        "route_version",
        "space_id",
        "text",
        "text_sha256",
        "marker",
        "copy_privacy",
        "provider_ids",
        "brain_id",
        "issuer_epoch",
        "job_identity",
        "created_at",
        "expires_at",
    }
)
_DECISION_RECEIPT_FIELDS = frozenset(
    {
        "dto_version",
        "brain_id",
        "issuer_epoch",
        "operation_id",
        "request_sha256",
        "preview_id",
        "approval_id",
        "decision",
        "state",
        "approval_version",
        "destination_brain_id",
        "copy_delivery_id",
        "copy_capture_id",
        "receipt_sha256",
    }
)
_REVOKE_RECEIPT_FIELDS = frozenset(
    {
        "dto_version",
        "brain_id",
        "issuer_epoch",
        "operation_id",
        "request_sha256",
        "approval_id",
        "approval_version",
        "destination_brain_id",
        "state",
        "receipt_sha256",
    }
)


def _uuid4(value: str, prefix: str = "") -> None:
    suffix = value.removeprefix(prefix)
    parsed = UUID(suffix)
    if not value.startswith(prefix) or str(parsed) != suffix or parsed.version != 4:
        raise ValueError("sharing UUID invalid")


def _preview(value: dict[str, Any]) -> None:
    from open_brain_engine.engine.contracts import TextPayload
    from open_brain_engine.engine.sharing import _copy_privacy
    from open_brain_engine.engine.source_observation import SourceRevisionObservation

    if set(value) != _PREVIEW_FIELDS:
        raise ValueError("sharing preview shape invalid")
    for name in (
        "dto_version",
        "head_version",
        "lifecycle_version",
        "route_version",
        "issuer_epoch",
    ):
        if type(value[name]) is not int or not 0 <= value[name] <= 9007199254740991:
            raise ValueError("sharing preview integer invalid")
    if value["dto_version"] != 1 or value["issuer_epoch"] < 1:
        raise ValueError("sharing preview version invalid")
    for name in _PREVIEW_FIELDS - {
        "dto_version",
        "head_version",
        "lifecycle_version",
        "route_version",
        "issuer_epoch",
        "space_id",
        "observation",
        "copy_privacy",
        "provider_ids",
        "job_identity",
    }:
        if type(value[name]) is not str or not value[name] or "\x00" in value[name]:
            raise ValueError("sharing preview string invalid")
        if name.endswith("sha256") and (
            len(value[name]) != 64 or any(char not in "0123456789abcdef" for char in value[name])
        ):
            raise ValueError("sharing preview digest invalid")
    if value["space_id"] is not None and (
        type(value["space_id"]) is not str or not value["space_id"].startswith("space_")
    ):
        raise ValueError("sharing preview space invalid")
    _uuid4(value["preview_id"])
    job = value["job_identity"]
    if type(job) is not dict or set(job) != {
        "tenant_id",
        "actor_id",
        "role_id",
        "role_claim_id",
        "capabilities",
        "brain_id",
        "issuer_epoch",
    }:
        raise ValueError("sharing preview job shape invalid")
    if (
        type(job["issuer_epoch"]) is not int
        or not 1 <= job["issuer_epoch"] <= 9007199254740991
        or type(job["capabilities"]) is not list
        or job["capabilities"] != ["capture.accept"]
    ):
        raise ValueError("sharing preview job authority invalid")
    for name in ("tenant_id", "actor_id", "role_id", "role_claim_id", "brain_id"):
        if type(job[name]) is not str or not job[name] or "\x00" in job[name]:
            raise ValueError("sharing preview job identity invalid")
    for name, prefix in (
        ("actor_id", "actor_"),
        ("role_id", "role_"),
        ("role_claim_id", "role_claim_"),
    ):
        _uuid4(job[name], prefix)
    if (
        TextPayload(value["text"]).text != value["text"]
        or len(value["text"].encode("utf-8")) > 65_536
    ):
        raise ValueError("sharing preview text invalid")
    SourceRevisionObservation.from_value(value["observation"])
    if canonical(value["copy_privacy"]) != canonical(_copy_privacy().to_dict()):
        raise ValueError("sharing copy privacy invalid")
    created = datetime.fromisoformat(_timestamp(value["created_at"], "sharing preview"))
    expiry = datetime.fromisoformat(_timestamp(value["expires_at"], "sharing preview"))
    if expiry - created != timedelta(minutes=15):
        raise ValueError("sharing preview expiry invalid")


def _receipt(raw: bytes, fields: frozenset[str]) -> dict[str, Any]:
    value = _decoded(raw)
    if (
        set(value) != fields
        or type(value["approval_version"]) is not int
        or type(value["dto_version"]) is not int
        or value["dto_version"] != 1
        or type(value["issuer_epoch"]) is not int
    ):
        raise ValueError("sharing receipt shape invalid")
    digest = value.pop("receipt_sha256", None)
    if digest != sha256(b"open-brain-sharing-receipt.v1\0" + canonical(value)).hexdigest():
        raise ValueError("sharing receipt digest mismatch")
    value["receipt_sha256"] = digest
    return value


def validate_sharing_authority(files: Mapping[str, bytes]) -> dict[str, Any]:
    """Cross-check separately retained source/copy bytes after v6 validation."""
    try:
        value = _portable_record(files.get(SHARING_APPROVALS_PATH, b""), "sharing approvals")
        if (
            set(value) != {"schema_version", *SHARING_COLUMNS}
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 1
        ):
            raise ValueError("sharing sidecar fields invalid")
        rows = {name: _rows(value[name], columns) for name, columns in SHARING_COLUMNS.items()}
        source_metadata = _decoded(files[SOURCE_METADATA_PATH])
        source_admission = _decoded(files[SOURCE_ADMISSION_PATH])
        lifecycle = _decoded(files[SOURCE_LIFECYCLE_PATH])
        lifecycle_versions = {
            row["source_id"]: row["lifecycle_version"]
            for row in lifecycle["source_lifecycle_state"]
        }
        issuer = _decoded(files[ISSUER_MIGRATION_PATH])
        source_by_id = {source["source_id"]: source for source in source_metadata["sources"]}
        revision_by_id = {
            revision["capture_id"]: revision for revision in source_metadata["revisions"]
        }

        def record(capture_id: str) -> dict[str, Any]:
            revision = revision_by_id[capture_id]
            raw = files[revision["source_path"]]
            if sha256(raw).hexdigest() != revision["source_sha256"]:
                raise ValueError("sharing source digest mismatch")
            result = _decoded(raw)
            if result["capture_id"] != capture_id:
                raise ValueError("sharing source identity mismatch")
            return result

        jobs = rows["sharing_job_identity"]
        if len(jobs) > 1 or (rows["sharing_previews"] and len(jobs) != 1):
            raise ValueError("sharing job identity invalid")
        if jobs and (jobs[0]["singleton"] != 1 or jobs[0]["issuer_epoch"] < 1):
            raise ValueError("sharing job binding invalid")
        if jobs:
            job = jobs[0]
            if (job["tenant_id"], job["brain_id"], job["issuer_epoch"]) != (
                issuer["tenant_id"],
                issuer["brain_id"],
                issuer["current_issuer_epoch"],
            ):
                raise ValueError("sharing job issuer mismatch")
            for name, prefix in (
                ("actor_id", "actor_"),
                ("role_id", "role_"),
                ("role_claim_id", "role_claim_"),
            ):
                _uuid4(job[name], prefix)
            owner = tomllib.loads(files["brain.toml"].decode("utf-8"))
            if any(
                job[name] == owner[owner_name]
                for name, owner_name in (
                    ("actor_id", "owner_actor_id"),
                    ("role_id", "owner_role_id"),
                    ("role_claim_id", "owner_role_claim_id"),
                )
            ):
                raise ValueError("sharing job cannot use an owner identity")
        previews: dict[str, dict[str, Any]] = {}
        preview_operations: set[str] = set()
        for row in rows["sharing_previews"]:
            raw = _b64(row["preview_bytes"])
            preview = _decoded(raw)
            _preview(preview)
            request_raw = _b64(row["request_bytes"])
            from open_brain_engine.engine.sharing_contracts import (
                SharingPreviewRequest,
                parse_sharing_request,
            )

            preview_request = parse_sharing_request(request_raw, SharingPreviewRequest)
            if (
                request_raw != canonical(preview_request.value())
                or preview_request.request_sha256 != row["request_sha256"]
                or preview_request.operation_id != row["operation_id"]
                or preview_request.source_id != row["source_id"]
                or preview_request.expected_head != row["original_capture_id"]
                or row["operation_id"] in preview_operations
                or sha256(b"open-brain-sharing-preview.v1\0" + raw).hexdigest()
                != row["preview_sha256"]
                or preview.get("preview_id") != row["preview_id"]
                or preview.get("source_id") != row["source_id"]
                or preview.get("original_capture_id") != row["original_capture_id"]
                or preview.get("expires_at") != row["expires_at"]
                or preview.get("brain_id") != preview_request.brain_id
                or preview.get("issuer_epoch") != preview_request.issuer_epoch
                or preview.get("provider_ids") != list(preview_request.provider_ids)
                or preview["job_identity"]
                != {
                    **{key: item for key, item in jobs[0].items() if key != "singleton"},
                    "capabilities": ["capture.accept"],
                }
                or preview.get("head_version") != preview_request.expected_head_version
                or preview.get("route_version") != preview_request.expected_route_version
                or preview.get("lifecycle_version") != preview_request.expected_lifecycle_version
                or (preview["brain_id"], preview["issuer_epoch"])
                != (jobs[0]["brain_id"], jobs[0]["issuer_epoch"])
                or preview.get("text_sha256") != sha256(preview["text"].encode("utf-8")).hexdigest()
            ):
                raise ValueError("sharing preview binding invalid")
            source = source_by_id[row["source_id"]]
            original = record(row["original_capture_id"])
            if (
                revision_by_id[row["original_capture_id"]]["source_id"] != row["source_id"]
                or preview["source_sha256"]
                != revision_by_id[row["original_capture_id"]]["source_sha256"]
                or original["payload"].get("supplied_text") != preview["text"]
                or original["privacy"].get("tier") != "public"
                or original["privacy"].get("authority")
                != {"cloud": False, "external_egress": False}
                or original["source"].get("origin") != "third_party"
                or source["head_version"] < preview["head_version"]
                or preview["head_version"] < 1
                or source["head_capture_id"] == row["original_capture_id"]
                and source["head_version"] != preview["head_version"]
                or source["head_capture_id"] != row["original_capture_id"]
                and source["head_version"] <= preview["head_version"]
                or preview["lifecycle_version"] != 0
                or datetime.fromisoformat(preview["created_at"])
                < datetime.fromisoformat(original["accepted_at"])
                or source["route_version"] < preview["route_version"]
                or source["route_version"] == preview["route_version"]
                and source["space_id"] != preview["space_id"]
                or preview["route_version"] == 0
                and preview["space_id"] != original["space_id"]
                or lifecycle_versions[row["source_id"]] < preview["lifecycle_version"]
                or preview["provenance_sha256"]
                != sha256(
                    canonical(
                        {
                            key: original["provenance"][key]
                            for key in ("source_ref", "content_origin", "owner_context")
                        }
                    )
                ).hexdigest()
            ):
                raise ValueError("sharing original mismatch")
            managed = next(
                (
                    item
                    for item in source_admission["managed_source_deliveries"]
                    if item["delivery_id"] == preview["managed_delivery_id"]
                ),
                None,
            )
            if (
                managed is None
                or managed["source_id"] != row["source_id"]
                or managed["envelope_sha256"] != preview["managed_envelope_sha256"]
                or (managed["destination_brain_id"], managed["issuer_epoch"])
                != (preview["brain_id"], preview["issuer_epoch"])
            ):
                raise ValueError("sharing managed admission mismatch")
            source_receipt = json.loads(managed["receipt_json"])["source_receipt"]
            envelope = _decoded(_b64(managed["envelope_bytes"]))
            if (
                source_receipt["capture_id"] != row["original_capture_id"]
                or source_receipt["outcome"] != "captured"
                or envelope.get("dto_version") != 2
                or envelope.get("observation") != preview["observation"]
                or preview["observation"].get("normalization_version")
                not in ("saved-markdown-continuous.v1", "saved-markdown-continuous.v2")
                or sha256(canonical(original["privacy"])).hexdigest() != preview["privacy_sha256"]
                or sha256(canonical(original["payload"])).hexdigest() != preview["payload_sha256"]
                or preview["marker"]
                != f"urn:open-brain:sharing-copy:v1:{row['preview_id']}:{row['original_capture_id'].removeprefix('capture_')}"
            ):
                raise ValueError("sharing observation mismatch")
            preview_operations.add(row["operation_id"])
            previews[row["preview_id"]] = preview
        decisions: dict[str, dict[str, Any]] = {}
        decided_previews: set[str] = set()
        decision_operations: set[str] = set()
        for row in rows["sharing_decisions"]:
            from open_brain_engine.engine.contracts import JournalEnvelope
            from open_brain_engine.engine.sharing_contracts import (
                SharingDecisionRequest,
                parse_sharing_request,
            )

            request_raw = _b64(row["request_bytes"])
            decision_request = parse_sharing_request(request_raw, SharingDecisionRequest)
            receipt = _receipt(_b64(row["receipt_bytes"]), _DECISION_RECEIPT_FIELDS)
            _uuid4(row["approval_id"])
            preview = previews[row["preview_id"]]
            if (
                request_raw != canonical(decision_request.value())
                or decision_request.request_sha256 != row["request_sha256"]
                or decision_request.operation_id != row["operation_id"]
                or decision_request.preview_id != row["preview_id"]
                or decision_request.preview_sha256
                != next(
                    item["preview_sha256"]
                    for item in rows["sharing_previews"]
                    if item["preview_id"] == row["preview_id"]
                )
                or decision_request.decision != row["decision"]
                or decision_request.expected_decision_version != 0
                or decision_request.destination_brain_id != row["destination_brain_id"]
                or decision_request.destination_brain_id != preview["brain_id"]
                or decision_request.brain_id != preview["brain_id"]
                or decision_request.issuer_epoch != preview["issuer_epoch"]
                or row["approval_version"] not in {1, 2}
                or row["preview_id"] in decided_previews
                or row["operation_id"] in decision_operations
                or receipt["approval_id"] != row["approval_id"]
                or receipt["request_sha256"] != row["request_sha256"]
                or receipt["preview_id"] != row["preview_id"]
                or receipt["decision"] != row["decision"]
                or receipt["copy_capture_id"] != row["copy_capture_id"]
                or receipt["operation_id"] != row["operation_id"]
                or receipt["destination_brain_id"] != row["destination_brain_id"]
                or receipt["copy_delivery_id"] != row["copy_delivery_id"]
                or receipt["brain_id"] != decision_request.brain_id
                or receipt["issuer_epoch"] != decision_request.issuer_epoch
                # The immutable decision receipt remains v1 after revocation.
                or receipt["approval_version"] != 1
            ):
                raise ValueError("sharing decision binding invalid")
            if row["decision"] == "reject":
                if (
                    any(
                        row[name] is not None
                        for name in (
                            "copy_delivery_id",
                            "copy_submission_bytes",
                            "copy_submission_sha256",
                            "copy_capture_id",
                        )
                    )
                    or receipt["state"] != "rejected"
                ):
                    raise ValueError("sharing rejection admits a copy")
            else:
                if row["copy_capture_id"] is None or receipt["state"] not in {
                    "captured",
                    "history_only",
                }:
                    raise ValueError("sharing pending copy cannot be exported")
                copy_envelope = JournalEnvelope.from_bytes(_b64(row["copy_submission_bytes"]))
                submission = copy_envelope.submission
                copied = record(row["copy_capture_id"])
                expected_role = {
                    key: jobs[0][key]
                    for key in ("tenant_id", "actor_id", "role_id", "role_claim_id")
                }
                expected_role["capabilities"] = ["capture.accept"]
                expected_provenance = {
                    "source_ref": preview["marker"],
                    "content_origin": "third_party",
                    "owner_context": "automation_absent",
                }
                if (
                    submission.delivery_id != row["copy_delivery_id"]
                    or submission.delivery_id
                    != f"sharing-copy.v1:{row['approval_id']}:{row['copy_submission_sha256']}"
                    or submission.request_sha256() != row["copy_submission_sha256"]
                    or submission.source_reference != preview["marker"]
                    or submission.payload.to_dict() != {"family": "text", "text": preview["text"]}
                    or submission.privacy.to_dict() != preview["copy_privacy"]
                    or copied["source"]["reference"] != preview["marker"]
                    or copied["payload"] != submission.payload.to_dict()
                    or copied["privacy"] != copy_envelope.admitted_privacy.to_dict()
                    or copy_envelope.admitted_privacy.to_dict() != preview["copy_privacy"]
                    or submission.submission_path.value != "public_job"
                    or submission.action.value != "quick"
                    or submission.source_origin.value != "third_party"
                    or submission.tenant_id != jobs[0]["tenant_id"]
                    or canonical(dict(submission.role_claim)) != canonical(expected_role)
                    or submission.provenance.to_dict() != expected_provenance
                    or copied["provenance"]
                    != {**expected_provenance, "transformation_receipts": []}
                    or copied["source"]["origin"] != "third_party"
                    or copied["tenant_id"] != submission.tenant_id
                    or copied["actor_id"] != submission.actor_id
                    or canonical(copied["role_claim"]) != canonical(dict(submission.role_claim))
                    or copied["intent"] is not None
                    or copied["capture_why"] is not None
                    or submission.intent is not None
                    or submission.space_id is not None
                    or submission.title is not None
                    or submission.occurrence_at is not None
                    or submission.capture_why is not None
                    or submission.capture_why_origin.value != "automation_absent"
                    or jobs[0]["actor_id"] != submission.actor_id
                    or jobs[0]["role_id"] != submission.role_claim["role_id"]
                    or jobs[0]["role_claim_id"] != submission.role_claim["role_claim_id"]
                ):
                    raise ValueError("sharing copy envelope mismatch")
            decisions[row["approval_id"]] = row
            decided_previews.add(row["preview_id"])
            decision_operations.add(row["operation_id"])
        links = {row["approval_id"]: row for row in rows["sharing_links"]}
        if set(links) != {
            approval_id for approval_id, row in decisions.items() if row["decision"] == "approve"
        }:
            raise ValueError("sharing linked copy set mismatch")
        copy_ids: set[str] = set()
        for approval_id, link in links.items():
            decision = decisions[approval_id]
            preview = previews[decision["preview_id"]]
            if (
                link["copy_capture_id"] != decision["copy_capture_id"]
                or link["original_capture_id"] != preview["original_capture_id"]
                or link["marker"] != preview["marker"]
                or link["submission_sha256"] != decision["copy_submission_sha256"]
                or link["copy_capture_id"] in copy_ids
            ):
                raise ValueError("sharing link mismatch")
            _timestamp(link["linked_at"], "sharing link")
            if datetime.fromisoformat(link["linked_at"]) < datetime.fromisoformat(
                preview["created_at"]
            ):
                raise ValueError("sharing link precedes preview")
            copy_ids.add(link["copy_capture_id"])
        for capture_id in revision_by_id:
            if (
                record(capture_id)["source"]["reference"].startswith(
                    "urn:open-brain:sharing-copy:v1:"
                )
                and capture_id not in copy_ids
            ):
                raise ValueError("unlinked sharing marker")
        revoked_approvals: set[str] = set()
        for row in rows["sharing_revocations"]:
            from open_brain_engine.engine.sharing_contracts import (
                SharingRevokeRequest,
                parse_sharing_request,
            )

            request_raw = _b64(row["request_bytes"])
            revoke_request = parse_sharing_request(request_raw, SharingRevokeRequest)
            receipt = _receipt(_b64(row["receipt_bytes"]), _REVOKE_RECEIPT_FIELDS)
            decision = decisions[row["approval_id"]]
            preview = previews[decision["preview_id"]]
            if (
                request_raw != canonical(revoke_request.value())
                or revoke_request.request_sha256 != row["request_sha256"]
                or revoke_request.operation_id != row["operation_id"]
                or revoke_request.approval_id != row["approval_id"]
                or revoke_request.destination_brain_id != decision["destination_brain_id"]
                or revoke_request.expected_approval_version != 1
                or revoke_request.brain_id != preview["brain_id"]
                or revoke_request.issuer_epoch != preview["issuer_epoch"]
                or revoke_request.reason != row["reason"]
                or row["resulting_version"] != 2
                or decision["approval_version"] != 2
                or decision["decision"] != "approve"
                or row["approval_id"] in revoked_approvals
                or receipt["approval_id"] != row["approval_id"]
                or receipt["approval_version"] != 2
                or receipt["state"] != "revoked"
                or receipt["operation_id"] != row["operation_id"]
                or receipt["request_sha256"] != row["request_sha256"]
                or receipt["destination_brain_id"] != decision["destination_brain_id"]
                or receipt["brain_id"] != revoke_request.brain_id
                or receipt["issuer_epoch"] != revoke_request.issuer_epoch
            ):
                raise ValueError("sharing revocation mismatch")
            _timestamp(row["recorded_at"], "sharing revocation")
            revoked_approvals.add(row["approval_id"])
        if any(
            row["approval_version"] == 2 and row["approval_id"] not in revoked_approvals
            for row in decisions.values()
        ):
            raise ValueError("sharing projection version mismatch")
        return value
    except PortableValidationError:
        raise
    except KeyError, TypeError, ValueError, UnicodeError, OverflowError, IndexError, StopIteration:
        raise PortableValidationError("Portable v7 sharing authority invalid") from None


def validate_portable_file_set_v7(files: Mapping[str, bytes], *, tenant_id: str) -> None:
    validate_portable_file_set_v6(
        {path: payload for path, payload in files.items() if path not in V7_SIDECAR_PATHS},
        tenant_id=tenant_id,
    )
    validate_sharing_authority(files)


def validated_portable_snapshot_v7(
    root: Path, *, expected_root_identity: RootIdentity | None = None
) -> PortableSnapshot:
    try:
        identity = (
            capture_root_identity(root)
            if expected_root_identity is None
            else expected_root_identity
        )
        descriptor = open_root_descriptor(root, identity)
        files: dict[str, bytes] = {}
        try:
            _snapshot_directory(descriptor, (), files)
        finally:
            os.close(descriptor)
        manifest = _portable_record(files.get("portable-manifest.json", b""), "manifest")
        inventory = _manifest(manifest, version=7, catalog=PORTABLE_V7_SCHEMA_CATALOG_DIGEST)
        payloads = {path: data for path, data in files.items() if path != "portable-manifest.json"}
        if inventory != {path: sha256(data).hexdigest() for path, data in payloads.items()}:
            raise PortableValidationError("manifest checksum or inventory mismatch")
        validate_portable_file_set_v7(payloads, tenant_id=cast(str, manifest["tenant_id"]))
        assert_root_identity(root, identity)
        return PortableSnapshot(
            root_identity=identity, manifest=manifest, files=MappingProxyType(files)
        )
    except PortableValidationError:
        raise
    except OSError, RootConfinementError, KeyError, TypeError, ValueError:
        raise PortableValidationError("Portable v7 root or manifest invalid") from None

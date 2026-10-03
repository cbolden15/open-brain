"""Sharing authority validation joins independent evidence, not self hashes alone."""

from __future__ import annotations

import base64
import json
from hashlib import sha256
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.engine.sharing_contracts import (
    SharingDecisionRequest,
    SharingRevokeRequest,
)
from open_brain_engine.portable.v1 import PortableSnapshot, PortableValidationError
from open_brain_engine.portable.v7 import (
    SHARING_APPROVALS_PATH,
    manifest_v7,
    validate_portable_file_set_v7,
)
from open_brain_engine.portable.v8 import (
    HISTORICAL_AUTHORITY_PATH,
    manifest_v8,
    validate_historical_authority,
    validate_portable_file_set_v8,
)
from open_brain_engine.portable.versioned import validated_portable_snapshot

from packages.app.tests.unit.engine.test_sharing_tasks import _managed_source


@pytest.fixture(params=[7, 8])
def portable_version(request: pytest.FixtureRequest) -> int:
    return cast(int, request.param)


def _files_for_version(snapshot: PortableSnapshot, version: int) -> dict[str, bytes]:
    files = {
        path: data for path, data in snapshot.files.items() if path != "portable-manifest.json"
    }
    if version == 7:
        # Make a valid synthetic v7 file set only when no historical authority
        # exists. Never label actual v8 history as an old-format archive.
        historical = validate_historical_authority(files)
        assert historical.records == ()
        assert historical.registry.generation == 0
        assert historical.registry.memberships == ()
        del files[HISTORICAL_AUTHORITY_PATH]
    validator = {7: validate_portable_file_set_v7, 8: validate_portable_file_set_v8}[version]
    validator(files, tenant_id=str(snapshot.manifest["tenant_id"]))
    return files


def _pack(value: object) -> str:
    return base64.b64encode(_forged_json(value)).decode("ascii")


def _forged_json(value: object) -> bytes:
    # Keep malicious numeric types intact rather than normalizing them in fixtures.
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _unpack(value: str) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(base64.b64decode(value, validate=True)))


def _rehash_receipt(value: dict[str, Any]) -> str:
    value.pop("receipt_sha256")
    value["receipt_sha256"] = sha256(
        b"open-brain-sharing-receipt.v1\0" + _forged_json(value)
    ).hexdigest()
    return _pack(value)


def _approved_export(tmp_path: Path):  # type: ignore[no-untyped-def]
    tasks, request, owner, _ = _managed_source(tmp_path)
    assert tasks.sharing is not None
    preview = tasks.sharing.preview(request, authority=owner)
    approval = tasks.sharing.decide(
        SharingDecisionRequest(
            operation_id="sharing.forgery.approve",
            preview_id=preview.preview_id,
            preview_sha256=preview.preview_sha256,
            brain_id=request.brain_id,
            issuer_epoch=request.issuer_epoch,
            destination_brain_id=request.brain_id,
            expected_decision_version=0,
            decision="approve",
        ),
        authority=owner,
    )
    tasks.sharing.revoke(
        SharingRevokeRequest(
            operation_id="sharing.forgery.revoke",
            approval_id=approval.approval_id,
            expected_approval_version=1,
            brain_id=request.brain_id,
            issuer_epoch=request.issuer_epoch,
            destination_brain_id=request.brain_id,
            reason="owner_choice",
        ),
        authority=owner,
    )
    export = tmp_path / "export"
    tasks.portability.export(export, export_id="export_" + str(uuid4()))
    return tasks, validated_portable_snapshot(export)


@pytest.mark.parametrize(
    "subject,field,replacement",
    [
        ("decision", "dto_version", True),
        ("decision", "dto_version", 1.0),
        ("decision", "dto_version", 2),
        ("decision", "brain_id", "brn_forged"),
        ("decision", "issuer_epoch", True),
        ("decision", "issuer_epoch", 1.0),
        ("decision", "issuer_epoch", 2),
        ("revocation", "dto_version", True),
        ("revocation", "dto_version", 1.0),
        ("revocation", "dto_version", 2),
        ("revocation", "brain_id", "brn_forged"),
        ("revocation", "issuer_epoch", True),
        ("revocation", "issuer_epoch", 1.0),
        ("revocation", "issuer_epoch", 2),
        ("decision", "operation_id", "sharing.forged"),
        ("decision", "destination_brain_id", "brn_forged"),
        ("decision", "copy_delivery_id", "sharing-copy.v1:forged"),
        ("decision", "approval_version", 2),
        ("decision", "approval_version", True),
        ("decision", "approval_version", 1.0),
        ("decision", "unexpected", True),
        ("decision", "request_sha256", "f" * 64),
        ("decision", "preview_id", "00000000-0000-4000-8000-000000000001"),
        ("decision", "approval_id", "00000000-0000-4000-8000-000000000001"),
        ("decision", "decision", "reject"),
        ("decision", "state", "pending"),
        ("decision", "copy_capture_id", "capture_00000000-0000-4000-8000-000000000001"),
        ("revocation", "operation_id", "sharing.forged"),
        ("revocation", "request_sha256", "f" * 64),
        ("revocation", "destination_brain_id", "brn_forged"),
        ("revocation", "approval_version", 2.0),
        ("revocation", "unexpected", True),
        ("revocation", "approval_id", "00000000-0000-4000-8000-000000000001"),
        ("revocation", "state", "captured"),
        ("preview", "unexpected", True),
        ("preview", "dto_version", True),
        ("preview", "dto_version", 1.0),
        ("preview", "created_at", "2000-01-01T00:00:00Z"),
        ("preview", "provenance_sha256", "f" * 64),
        ("preview", "head_version", True),
        ("preview", "lifecycle_version", False),
        ("preview", "route_version", 0.0),
        ("preview", "issuer_epoch", True),
        ("preview", "space_id", "space_00000000-0000-4000-8000-000000000001"),
        ("preview", "copy_privacy", {"tier": "public"}),
        ("preview", "text_sha256", "f" * 64),
        ("preview", "managed_delivery_id", "forged.delivery"),
        ("preview", "managed_envelope_sha256", "f" * 64),
        ("preview_job", "tenant_id", "tenant_forged"),
        ("preview_job", "actor_id", "actor_00000000-0000-4000-8000-000000000001"),
        ("preview_job", "role_id", "role_00000000-0000-4000-8000-000000000001"),
        ("preview_job", "role_claim_id", "role_claim_00000000-0000-4000-8000-000000000001"),
        ("preview_job", "brain_id", "brn_forged"),
        ("preview_job", "issuer_epoch", 2),
        ("preview_job", "issuer_epoch", True),
        ("preview_job", "issuer_epoch", 1.0),
        ("preview_job", "capabilities", ["capture.accept", "review.decide"]),
        ("preview_job", "capabilities", "capture.accept"),
        ("preview_job", "unexpected", True),
        ("preview_job", "missing", "tenant_id"),
        ("preview", "job_identity", None),
        ("preview_bound", "head_version", 0),
        ("preview_bound", "window", "2000-01-01T00:00:00Z"),
        ("job", "tenant_id", "tenant_00000000-0000-4000-8000-000000000001"),
        ("job", "brain_id", "brn_forged"),
        ("job", "issuer_epoch", 2),
        ("job", "actor_id", "actor_not-a-uuid"),
        ("job", "role_id", "role_not-a-uuid"),
        ("job", "role_claim_id", "role_claim_not-a-uuid"),
        ("revoke_request", "brain_id", "brn_forged"),
        ("revoke_request", "issuer_epoch", 2),
        ("revocation_row", "reason", "forged_reason"),
        ("envelope", "owner_context", "owner_authored"),
        ("envelope", "source_origin", "unknown"),
        ("envelope", "actor_id", "actor_00000000-0000-4000-8000-000000000001"),
        ("envelope", "role_id", "role_00000000-0000-4000-8000-000000000001"),
        ("envelope", "role_claim_id", "role_claim_00000000-0000-4000-8000-000000000001"),
        ("envelope", "capabilities", ["capture.accept", "review.decide"]),
        ("envelope", "intent", "reference"),
        ("envelope", "title", "Forged title"),
        ("envelope", "source_ref", "urn:open-brain:sharing-copy:v1:forged"),
        ("delivery", "domain", "forged-copy.v1"),
    ],
)
def test_rehashed_sharing_semantic_forgery_refuses_before_promotion(
    tmp_path: Path, subject: str, field: str, replacement: object, portable_version: int,
) -> None:
    tasks, snapshot = _approved_export(tmp_path)
    files = _files_for_version(snapshot, portable_version)
    value = json.loads(files[SHARING_APPROVALS_PATH])
    preview_row, decision, revoke = (
        value[name][0] for name in ("sharing_previews", "sharing_decisions", "sharing_revocations")
    )
    if subject in {"decision", "revocation"}:
        row = decision if subject == "decision" else revoke
        receipt = _unpack(row["receipt_bytes"])
        receipt[field] = replacement
        row["receipt_bytes"] = _rehash_receipt(receipt)
    elif subject in {"preview", "preview_bound", "preview_job"}:
        frozen = _unpack(preview_row["preview_bytes"])
        if subject == "preview_job":
            if field == "missing":
                frozen["job_identity"].pop(str(replacement))
            else:
                frozen["job_identity"][field] = replacement
        elif field == "window":
            frozen["created_at"] = replacement
            frozen["expires_at"] = "2000-01-01T00:15:00Z"
            preview_row["expires_at"] = frozen["expires_at"]
        else:
            frozen[field] = replacement
        if subject == "preview_bound" and field == "head_version":
            original_request = _unpack(preview_row["request_bytes"])
            original_request["expected_head_version"] = replacement
            preview_row["request_bytes"] = _pack(original_request)
            preview_row["request_sha256"] = sha256(
                b"open-brain-sharing-request.v1\0" + canonical(original_request)
            ).hexdigest()
        preview_row["preview_bytes"] = _pack(frozen)
        preview_row["preview_sha256"] = sha256(
            b"open-brain-sharing-preview.v1\0" + _forged_json(frozen)
        ).hexdigest()
        request = _unpack(decision["request_bytes"])
        request["preview_sha256"] = preview_row["preview_sha256"]
        decision["request_bytes"] = _pack(request)
        decision["request_sha256"] = sha256(
            b"open-brain-sharing-request.v1\0" + canonical(request)
        ).hexdigest()
        receipt = _unpack(decision["receipt_bytes"])
        receipt["request_sha256"] = decision["request_sha256"]
        decision["receipt_bytes"] = _rehash_receipt(receipt)
    elif subject == "job":
        value["sharing_job_identity"][0][field] = replacement
    elif subject == "revoke_request":
        request = _unpack(revoke["request_bytes"])
        request[field] = replacement
        revoke["request_bytes"] = _pack(request)
        revoke["request_sha256"] = sha256(
            b"open-brain-sharing-request.v1\0" + canonical(request)
        ).hexdigest()
        receipt = _unpack(revoke["receipt_bytes"])
        receipt["request_sha256"] = revoke["request_sha256"]
        revoke["receipt_bytes"] = _rehash_receipt(receipt)
    elif subject == "revocation_row":
        revoke[field] = replacement
    else:
        envelope = _unpack(decision["copy_submission_bytes"])
        submission = envelope["submission"]
        if field in {"owner_context", "source_ref"}:
            submission["provenance"][field] = replacement
            if field == "source_ref":
                submission["source_reference"] = replacement
        elif field in {"role_id", "role_claim_id", "capabilities"}:
            submission["role_claim"][field] = replacement
            if field != "capabilities":
                value["sharing_job_identity"][0][field] = replacement
        elif field == "actor_id":
            submission[field] = replacement
            submission["role_claim"][field] = replacement
            value["sharing_job_identity"][0][field] = replacement
        elif field != "domain":
            submission[field] = replacement
        if field == "source_origin":
            submission["provenance"]["content_origin"] = replacement
        request_value = {
            key: submission[key]
            for key in (
                "actor_id",
                "capture_why",
                "capture_why_origin",
                "schema_version",
                "intent",
                "payload",
                "privacy",
                "provenance",
                "role_claim",
                "source_origin",
                "source_reference",
                "space_id",
                "tenant_id",
                "title",
            )
        }
        decision["copy_submission_sha256"] = sha256(canonical(request_value)).hexdigest()
        domain = replacement if field == "domain" else "sharing-copy.v1"
        delivery = f"{domain}:{decision['approval_id']}:{decision['copy_submission_sha256']}"
        submission["delivery_id"] = delivery
        decision["copy_delivery_id"] = delivery
        decision["copy_submission_bytes"] = _pack(envelope)
        value["sharing_links"][0]["submission_sha256"] = decision["copy_submission_sha256"]
        receipt = _unpack(decision["receipt_bytes"])
        receipt["copy_delivery_id"] = delivery
        decision["receipt_bytes"] = _rehash_receipt(receipt)
    files[SHARING_APPROVALS_PATH] = canonical(value)
    forged = tmp_path / "forged"
    for path, data in files.items():
        destination = forged / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    manifest = {7: manifest_v7, 8: manifest_v8}[portable_version](
        files,
        tenant_id=tasks.profile.tenant_id,
        export_id=str(snapshot.manifest["export_id"]),
        created_at=str(snapshot.manifest["created_at"]),
    )
    (forged / "portable-manifest.json").write_bytes(canonical(manifest))
    # Retained source/capture and intake files are exact, independent witnesses.
    assert all(
        data == snapshot.files[path]
        for path, data in files.items()
        if path != SHARING_APPROVALS_PATH
    )
    with pytest.raises(PortableValidationError, match="sharing authority"):
        validated_portable_snapshot(forged)
    target = tmp_path / "refused-import"
    with pytest.raises(PortableValidationError, match="sharing authority"):
        tasks.portability.import_clean(forged, target, import_id="import_" + str(uuid4()))
    assert not target.exists()


@pytest.mark.parametrize(
    "field,owner_field",
    [
        ("actor_id", "owner_actor_id"),
        ("role_id", "owner_role_id"),
        ("role_claim_id", "owner_role_claim_id"),
    ],
)
def test_undecided_preview_job_cannot_use_retained_owner_identity(
    tmp_path: Path, field: str, owner_field: str, portable_version: int,
) -> None:
    import tomllib

    tasks, request, owner, _ = _managed_source(tmp_path)
    assert tasks.sharing is not None
    tasks.sharing.preview(request, authority=owner)
    export = tmp_path / "export"
    tasks.portability.export(export, export_id="export_" + str(uuid4()))
    snapshot = validated_portable_snapshot(export)
    files = _files_for_version(snapshot, portable_version)
    sidecar = json.loads(files[SHARING_APPROVALS_PATH])
    sidecar["sharing_job_identity"][0][field] = tomllib.loads(files["brain.toml"].decode("utf-8"))[
        owner_field
    ]
    files[SHARING_APPROVALS_PATH] = canonical(sidecar)
    with pytest.raises(PortableValidationError, match="sharing authority"):
        {7: validate_portable_file_set_v7, 8: validate_portable_file_set_v8}[portable_version](
            files, tenant_id=tasks.profile.tenant_id,
        )


def test_sharing_provider_forgery_fails_with_recomputed_manifest(
    tmp_path: Path, portable_version: int,
) -> None:
    tasks, request, owner, _ = _managed_source(tmp_path)
    assert tasks.sharing is not None
    preview = tasks.sharing.preview(request, authority=owner)
    tasks.sharing.decide(
        SharingDecisionRequest(
            operation_id="sharing.v7.approve",
            preview_id=preview.preview_id,
            preview_sha256=preview.preview_sha256,
            brain_id=request.brain_id,
            issuer_epoch=request.issuer_epoch,
            destination_brain_id=request.brain_id,
            expected_decision_version=0,
            decision="approve",
        ),
        authority=owner,
    )
    export = tmp_path / "export"
    tasks.portability.export(export, export_id="export_" + str(uuid4()))
    snapshot = validated_portable_snapshot(export)
    files = _files_for_version(snapshot, portable_version)
    sidecar = json.loads(files[SHARING_APPROVALS_PATH])
    row = sidecar["sharing_previews"][0]
    import base64

    frozen = json.loads(base64.b64decode(row["preview_bytes"], validate=True))
    frozen["provider_ids"] = ["gemini"]
    raw = canonical(frozen)
    row["preview_bytes"] = base64.b64encode(raw).decode("ascii")
    row["preview_sha256"] = sha256(b"open-brain-sharing-preview.v1\0" + raw).hexdigest()
    files[SHARING_APPROVALS_PATH] = canonical(sidecar)
    forged = tmp_path / "forged"
    for path, data in files.items():
        destination = forged / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    manifest = {7: manifest_v7, 8: manifest_v8}[portable_version](
        files,
        tenant_id=tasks.profile.tenant_id,
        export_id=str(snapshot.manifest["export_id"]),
        created_at=str(snapshot.manifest["created_at"]),
    )
    (forged / "portable-manifest.json").write_bytes(canonical(manifest))
    with pytest.raises(PortableValidationError, match="sharing authority"):
        validated_portable_snapshot(forged)

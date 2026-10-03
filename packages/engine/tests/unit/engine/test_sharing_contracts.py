"""Synthetic hostile-input matrix for the closed owner sharing requests."""

from __future__ import annotations

import json
from dataclasses import fields, replace
from hashlib import sha256
from typing import Any, cast

import pytest
from open_brain_engine.engine.sharing_contracts import (
    SharingDecisionReceipt,
    SharingDecisionRequest,
    SharingError,
    SharingInspection,
    SharingInspectRequest,
    SharingPreview,
    SharingPreviewRequest,
    SharingRevokeReceipt,
    SharingRevokeRequest,
    parse_sharing_request,
)

_BRAIN = "brn_aaaaaaaaaaaaaaaaaaaaaaaaaa"
_UUID = "00000000-0000-4000-8000-000000000001"
_REQUESTS = (
    SharingPreviewRequest(
        operation_id="sharing.contract.preview",
        source_id="source_synthetic",
        expected_head="capture_synthetic",
        expected_head_version=1,
        expected_lifecycle_version=0,
        expected_route_version=0,
        brain_id=_BRAIN,
        issuer_epoch=1,
        provider_ids=("anthropic", "openai"),
    ),
    SharingInspectRequest(subject_id=_UUID),
    SharingDecisionRequest(
        operation_id="sharing.contract.decision",
        preview_id=_UUID,
        preview_sha256="a" * 64,
        brain_id=_BRAIN,
        issuer_epoch=1,
        destination_brain_id=_BRAIN,
        expected_decision_version=0,
        decision="approve",
    ),
    SharingRevokeRequest(
        operation_id="sharing.contract.revoke",
        approval_id=_UUID,
        expected_approval_version=1,
        brain_id=_BRAIN,
        issuer_epoch=1,
        destination_brain_id=_BRAIN,
        reason="owner_choice",
    ),
)
_FIELDS = tuple((dto, field.name) for dto in _REQUESTS for field in fields(dto))


@pytest.mark.parametrize("dto", _REQUESTS)
def test_all_sharing_request_types_roundtrip_exactly(dto: object) -> None:
    value = dto.value()  # type: ignore[attr-defined]
    parsed = parse_sharing_request(json.dumps(value).encode(), type(dto))  # type: ignore[type-var]
    assert parsed == dto
    assert parsed.request_sha256 == dto.request_sha256  # type: ignore[attr-defined]


@pytest.mark.parametrize(("dto", "field"), _FIELDS)
@pytest.mark.parametrize("invalid", [None, {}, [], True, 1.5])
def test_every_sharing_field_refuses_invalid_types(
    dto: object, field: str, invalid: object
) -> None:
    with pytest.raises(SharingError, match="invalid_arguments"):
        replace(dto, **{field: invalid})  # type: ignore[type-var]


@pytest.mark.parametrize(("dto", "field"), _FIELDS)
def test_every_sharing_field_is_required_and_unknown_keys_are_refused(
    dto: object, field: str
) -> None:
    value = dto.value()  # type: ignore[attr-defined]
    del value[field]
    with pytest.raises(SharingError, match="invalid_arguments"):
        parse_sharing_request(json.dumps(value).encode(), type(dto))  # type: ignore[type-var]
    value[field] = dto.value()[field]  # type: ignore[attr-defined]
    value["unexpected"] = "synthetic"
    with pytest.raises(SharingError, match="invalid_arguments"):
        parse_sharing_request(json.dumps(value).encode(), type(dto))  # type: ignore[type-var]


@pytest.mark.parametrize("dto", _REQUESTS)
@pytest.mark.parametrize(
    "raw",
    [
        b"\xff",
        b"{}" + b" " * 65_535,
        b"[" * 20_000 + b"0" + b"]" * 20_000,
        b'{"dto_version":Infinity}',
        b'{"dto_version":-Infinity}',
        b'{"dto_version":1,"dto_version":1}',
        b"null",
        b"[]",
    ],
)
def test_hostile_json_returns_only_bounded_sharing_error(dto: object, raw: bytes) -> None:
    with pytest.raises(SharingError, match="invalid_arguments"):
        parse_sharing_request(raw, type(dto))  # type: ignore[type-var]


@pytest.mark.parametrize(("dto", "field"), _FIELDS)
@pytest.mark.parametrize("invalid", ["", "\ud800", "synthetic\nvalue", "x" * 257])
def test_every_sharing_field_refuses_hostile_or_oversized_text(
    dto: object, field: str, invalid: str
) -> None:
    with pytest.raises(SharingError, match="invalid_arguments"):
        replace(dto, **{field: invalid})  # type: ignore[type-var]


@pytest.mark.parametrize(
    "providers",
    [(), ("openai", "openai"), ("openai", "anthropic"), ("OPENAI",), ("x" * 65,)],
)
def test_preview_provider_set_is_nonempty_unique_sorted_and_bounded(
    providers: tuple[str, ...],
) -> None:
    with pytest.raises(SharingError, match="invalid_arguments"):
        replace(_REQUESTS[0], provider_ids=providers)


@pytest.mark.parametrize("invalid", [-1, 9007199254740992])
@pytest.mark.parametrize(
    ("dto", "field"),
    [
        (dto, field.name)
        for dto in _REQUESTS
        for field in fields(dto)
        if type(getattr(dto, field.name)) is int
    ],
)
def test_all_integer_fields_refuse_outside_portable_integer_domain(
    dto: object, field: str, invalid: int
) -> None:
    with pytest.raises(SharingError, match="invalid_arguments"):
        replace(dto, **{field: invalid})  # type: ignore[type-var]


def _signed(body: dict[str, object]) -> dict[str, object]:
    # Preserve hostile numeric types; the trusted serializer correctly refuses them.
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return {
        **body,
        "receipt_sha256": sha256(b"open-brain-sharing-receipt.v1\0" + raw).hexdigest(),
    }


_RECEIPT_BODIES = (
    (
        SharingDecisionReceipt,
        {
            "dto_version": 1,
            "brain_id": _BRAIN,
            "issuer_epoch": 1,
            "operation_id": "sharing.contract.decision",
            "request_sha256": "a" * 64,
            "preview_id": _UUID,
            "approval_id": _UUID,
            "decision": "approve",
            "state": "captured",
            "approval_version": 1,
            "destination_brain_id": _BRAIN,
            "copy_delivery_id": f"sharing-copy.v1:{_UUID}:{'b' * 64}",
            "copy_capture_id": "capture_synthetic",
        },
    ),
    (
        SharingRevokeReceipt,
        {
            "dto_version": 1,
            "brain_id": _BRAIN,
            "issuer_epoch": 1,
            "operation_id": "sharing.contract.revoke",
            "request_sha256": "a" * 64,
            "approval_id": _UUID,
            "approval_version": 2,
            "destination_brain_id": _BRAIN,
            "state": "revoked",
        },
    ),
)
_RECEIPT_FIELDS = tuple((kind, body, field) for kind, body in _RECEIPT_BODIES for field in body)


@pytest.mark.parametrize(("kind", "body"), _RECEIPT_BODIES)
def test_versioned_mutation_receipt_validates_exact_signed_body(
    kind: type[SharingDecisionReceipt] | type[SharingRevokeReceipt], body: dict[str, object]
) -> None:
    receipt = kind(**cast(dict[str, Any], _signed(body)))
    assert receipt.dto_version == 1
    assert receipt.brain_id == _BRAIN
    assert receipt.issuer_epoch == 1
    with pytest.raises(SharingError, match="invalid_arguments"):
        replace(receipt, receipt_sha256="0" * 64)


@pytest.mark.parametrize(("kind", "body", "field"), _RECEIPT_FIELDS)
@pytest.mark.parametrize("invalid", [None, {}, [], True, 1.5])
def test_every_receipt_field_refuses_bad_type_even_with_recomputed_digest(
    kind: type[SharingDecisionReceipt] | type[SharingRevokeReceipt],
    body: dict[str, object],
    field: str,
    invalid: object,
) -> None:
    with pytest.raises(SharingError, match="invalid_arguments"):
        kind(**cast(dict[str, Any], _signed({**body, field: invalid})))


_PREVIEW = SharingPreview(
    preview_id=_UUID,
    preview_sha256="a" * 64,
    source_id="source_synthetic",
    original_capture_id="capture_synthetic",
    text="Synthetic exact preview 漢字\n",
    text_sha256=sha256("Synthetic exact preview 漢字\n".encode()).hexdigest(),
    expires_at="2026-10-03T01:00:00Z",
    provider_ids=("anthropic", "openai"),
    brain_id=_BRAIN,
    issuer_epoch=1,
)


@pytest.mark.parametrize("field", [field.name for field in fields(_PREVIEW)])
@pytest.mark.parametrize("invalid", [None, {}, [], True, 1.5])
def test_every_preview_output_field_refuses_invalid_types(field: str, invalid: object) -> None:
    with pytest.raises(SharingError, match="invalid_arguments"):
        replace(_PREVIEW, **cast(dict[str, Any], {field: invalid}))


@pytest.mark.parametrize("invalid", ["\ud800", "x" * 65_537, "", "mismatched digest"])
def test_preview_output_refuses_unsafe_or_digest_mismatched_text(invalid: str) -> None:
    with pytest.raises(SharingError, match="invalid_arguments"):
        replace(_PREVIEW, text=invalid)


@pytest.mark.parametrize(
    "field",
    [
        "preview",
        "decision",
        "approval_id",
        "copy_capture_id",
        "revoked",
        "decision_receipt",
        "dto_version",
    ],
)
@pytest.mark.parametrize("invalid", [None, {}, [], True, 1.5])
def test_inspection_output_refuses_inconsistent_fields(field: str, invalid: object) -> None:
    decision = _signed(_RECEIPT_BODIES[0][1])
    inspection = SharingInspection(
        preview=_PREVIEW,
        decision="approve",
        approval_id=_UUID,
        copy_capture_id="capture_synthetic",
        revoked=False,
        decision_receipt=decision,
        revoke_receipt=None,
    )
    with pytest.raises(SharingError, match="invalid_arguments"):
        replace(inspection, **cast(dict[str, Any], {field: invalid}))

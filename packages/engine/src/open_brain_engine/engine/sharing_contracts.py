"""Closed owner-only contracts for one approved, separately captured copy."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any, cast
from uuid import UUID

from open_brain_engine.core.ids import portable_canonical_json_bytes

_CODES = frozenset(
    {
        "invalid_arguments",
        "unsupported_capability",
        "not_found",
        "revision_changed",
        "operation_pending",
        "preview_expired",
        "binding_mismatch",
        "response_too_large",
    }
)
_PROVIDER = re.compile(r"[a-z][a-z0-9._-]{0,63}\Z")
_OPERATION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MAX_REQUEST_BYTES = 65_536


class SharingError(ValueError):
    def __init__(self, code: str) -> None:
        if code not in _CODES:
            raise ValueError("unknown sharing error")
        self.code = code
        super().__init__(code)


def _text(value: object, *, pattern: re.Pattern[str] | None = None, limit: int = 256) -> str:
    if type(value) is not str or not value or len(value) > limit:
        raise SharingError("invalid_arguments")
    try:
        value.encode("utf-8", "strict")
    except UnicodeError:
        raise SharingError("invalid_arguments") from None
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise SharingError("invalid_arguments")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise SharingError("invalid_arguments")
    return value


def _uuid(value: object) -> str:
    text = _text(value, limit=36)
    try:
        parsed = UUID(text)
    except TypeError, ValueError:
        raise SharingError("invalid_arguments") from None
    if parsed.version != 4 or str(parsed) != text:
        raise SharingError("invalid_arguments")
    return text


def _version(value: object, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= 9007199254740991:
        raise SharingError("invalid_arguments")
    return value


def _digest(value: object) -> str:
    return _text(value, pattern=_DIGEST, limit=64)


def _identity(value: object, prefix: str) -> str:
    result = _text(value)
    if not result.startswith(prefix):
        raise SharingError("invalid_arguments")
    return result


def _request_sha256(value: Mapping[str, object]) -> str:
    return sha256(
        b"open-brain-sharing-request.v1\0" + portable_canonical_json_bytes(value)
    ).hexdigest()


class _SharingRequest:
    def value(self) -> dict[str, object]:
        return {field.name: getattr(self, field.name) for field in fields(cast(Any, self))}

    @property
    def request_sha256(self) -> str:
        return _request_sha256(self.value())


@dataclass(frozen=True, slots=True, kw_only=True)
class SharingPreviewRequest(_SharingRequest):
    operation_id: str
    source_id: str
    expected_head: str
    expected_head_version: int
    expected_lifecycle_version: int
    expected_route_version: int
    brain_id: str
    issuer_epoch: int
    provider_ids: tuple[str, ...]
    dto_version: int = 1

    def __post_init__(self) -> None:
        if _version(self.dto_version, minimum=1) != 1:
            raise SharingError("invalid_arguments")
        _text(self.operation_id, pattern=_OPERATION)
        _identity(self.source_id, "source_")
        _identity(self.expected_head, "capture_")
        _identity(self.brain_id, "brn_")
        _version(self.expected_head_version)
        _version(self.expected_lifecycle_version)
        _version(self.expected_route_version)
        _version(self.issuer_epoch, minimum=1)
        if type(self.provider_ids) not in (tuple, list) or not 1 <= len(self.provider_ids) <= 16:
            raise SharingError("invalid_arguments")
        values = tuple(_text(item, pattern=_PROVIDER, limit=64) for item in self.provider_ids)
        if values != tuple(sorted(set(values))):
            raise SharingError("invalid_arguments")
        object.__setattr__(self, "provider_ids", values)


@dataclass(frozen=True, slots=True, kw_only=True)
class SharingInspectRequest(_SharingRequest):
    subject_id: str
    dto_version: int = 1

    def __post_init__(self) -> None:
        if _version(self.dto_version, minimum=1) != 1:
            raise SharingError("invalid_arguments")
        _uuid(self.subject_id)


@dataclass(frozen=True, slots=True, kw_only=True)
class SharingDecisionRequest(_SharingRequest):
    operation_id: str
    preview_id: str
    preview_sha256: str
    brain_id: str
    issuer_epoch: int
    destination_brain_id: str
    expected_decision_version: int
    decision: str
    dto_version: int = 1

    def __post_init__(self) -> None:
        if _version(self.dto_version, minimum=1) != 1:
            raise SharingError("invalid_arguments")
        _text(self.operation_id, pattern=_OPERATION)
        _uuid(self.preview_id)
        _digest(self.preview_sha256)
        _identity(self.brain_id, "brn_")
        _identity(self.destination_brain_id, "brn_")
        _version(self.issuer_epoch, minimum=1)
        _text(self.decision)
        if _version(self.expected_decision_version) != 0 or self.decision not in {
            "approve",
            "reject",
        }:
            raise SharingError("invalid_arguments")


@dataclass(frozen=True, slots=True, kw_only=True)
class SharingRevokeRequest(_SharingRequest):
    operation_id: str
    approval_id: str
    expected_approval_version: int
    brain_id: str
    issuer_epoch: int
    destination_brain_id: str
    reason: str
    dto_version: int = 1

    def __post_init__(self) -> None:
        if _version(self.dto_version, minimum=1) != 1:
            raise SharingError("invalid_arguments")
        _text(self.operation_id, pattern=_OPERATION)
        _uuid(self.approval_id)
        _version(self.expected_approval_version, minimum=1)
        _identity(self.brain_id, "brn_")
        _identity(self.destination_brain_id, "brn_")
        _version(self.issuer_epoch, minimum=1)
        _text(self.reason, limit=96)


@dataclass(frozen=True, slots=True)
class SharingPreview:
    preview_id: str
    preview_sha256: str
    source_id: str
    original_capture_id: str
    text: str
    text_sha256: str
    expires_at: str
    provider_ids: tuple[str, ...]
    brain_id: str
    issuer_epoch: int
    dto_version: int = 1

    def __post_init__(self) -> None:
        if _version(self.dto_version, minimum=1) != 1:
            raise SharingError("invalid_arguments")
        _uuid(self.preview_id)
        _digest(self.preview_sha256)
        _identity(self.source_id, "source_")
        _identity(self.original_capture_id, "capture_")
        _identity(self.brain_id, "brn_")
        _version(self.issuer_epoch, minimum=1)
        _digest(self.text_sha256)
        if type(self.text) is not str or not self.text or len(self.text) > 65_536:
            raise SharingError("invalid_arguments")
        try:
            encoded = self.text.encode("utf-8", "strict")
        except UnicodeError:
            raise SharingError("invalid_arguments") from None
        if len(encoded) > 65_536 or sha256(encoded).hexdigest() != self.text_sha256:
            raise SharingError("invalid_arguments")
        _text(self.expires_at, limit=64)
        try:
            expiry = datetime.fromisoformat(self.expires_at)
        except ValueError:
            raise SharingError("invalid_arguments") from None
        if not self.expires_at.endswith("Z") or expiry.utcoffset() != timedelta(0):
            raise SharingError("invalid_arguments")
        if type(self.provider_ids) is not tuple or not 1 <= len(self.provider_ids) <= 16:
            raise SharingError("invalid_arguments")
        providers = tuple(_text(item, pattern=_PROVIDER, limit=64) for item in self.provider_ids)
        if providers != tuple(sorted(set(providers))):
            raise SharingError("invalid_arguments")


@dataclass(frozen=True, slots=True)
class SharingInspection:
    preview: SharingPreview
    decision: str | None
    approval_id: str | None
    copy_capture_id: str | None
    revoked: bool
    decision_receipt: Mapping[str, object] | None
    revoke_receipt: Mapping[str, object] | None
    dto_version: int = 1

    def __post_init__(self) -> None:
        if _version(self.dto_version, minimum=1) != 1 or not isinstance(
            self.preview, SharingPreview
        ):
            raise SharingError("invalid_arguments")
        if type(self.revoked) is not bool:
            raise SharingError("invalid_arguments")
        if self.decision is None:
            if (
                any(
                    value is not None
                    for value in (
                        self.approval_id,
                        self.copy_capture_id,
                        self.decision_receipt,
                        self.revoke_receipt,
                    )
                )
                or self.revoked
            ):
                raise SharingError("invalid_arguments")
            return
        try:
            if not isinstance(self.decision_receipt, Mapping):
                raise SharingError("invalid_arguments")
            decision = SharingDecisionReceipt(**cast(dict[str, Any], dict(self.decision_receipt)))
            revoke = (
                None
                if self.revoke_receipt is None
                else SharingRevokeReceipt(**cast(dict[str, Any], dict(self.revoke_receipt)))
            )
        except TypeError, ValueError:
            raise SharingError("invalid_arguments") from None
        if (
            decision.preview_id != self.preview.preview_id
            or decision.brain_id != self.preview.brain_id
            or decision.issuer_epoch != self.preview.issuer_epoch
            or decision.approval_id != self.approval_id
            or decision.decision != self.decision
            or decision.copy_capture_id != self.copy_capture_id
            or self.revoked != (revoke is not None)
            or revoke is not None
            and (
                revoke.approval_id != decision.approval_id
                or revoke.brain_id != decision.brain_id
                or revoke.issuer_epoch != decision.issuer_epoch
                or decision.decision != "approve"
            )
        ):
            raise SharingError("invalid_arguments")


@dataclass(frozen=True, slots=True)
class SharingDecisionReceipt:
    dto_version: int
    brain_id: str
    issuer_epoch: int
    operation_id: str
    request_sha256: str
    preview_id: str
    approval_id: str
    decision: str
    state: str
    approval_version: int
    destination_brain_id: str
    copy_delivery_id: str | None
    copy_capture_id: str | None
    receipt_sha256: str

    def __post_init__(self) -> None:
        _validate_receipt(self)
        _uuid(self.preview_id)
        _text(self.decision)
        _text(self.state)
        if _version(self.approval_version, minimum=1) != 1:
            raise SharingError("invalid_arguments")
        if self.decision == "reject":
            if (
                self.state != "rejected"
                or self.copy_delivery_id is not None
                or self.copy_capture_id is not None
            ):
                raise SharingError("invalid_arguments")
        elif self.decision == "approve":
            if self.state not in {"pending", "captured", "history_only"}:
                raise SharingError("invalid_arguments")
            delivery = _text(self.copy_delivery_id, limit=256).split(":")
            if len(delivery) != 3 or delivery[0] != "sharing-copy.v1":
                raise SharingError("invalid_arguments")
            if _uuid(delivery[1]) != self.approval_id:
                raise SharingError("invalid_arguments")
            _digest(delivery[2])
            if self.state == "pending":
                if self.copy_capture_id is not None:
                    raise SharingError("invalid_arguments")
            else:
                _identity(self.copy_capture_id, "capture_")
        else:
            raise SharingError("invalid_arguments")


@dataclass(frozen=True, slots=True)
class SharingRevokeReceipt:
    dto_version: int
    brain_id: str
    issuer_epoch: int
    operation_id: str
    request_sha256: str
    approval_id: str
    approval_version: int
    destination_brain_id: str
    state: str
    receipt_sha256: str

    def __post_init__(self) -> None:
        _validate_receipt(self)
        if _version(self.approval_version, minimum=1) != 2 or self.state != "revoked":
            raise SharingError("invalid_arguments")


def _validate_receipt(value: SharingDecisionReceipt | SharingRevokeReceipt) -> None:
    if _version(value.dto_version, minimum=1) != 1:
        raise SharingError("invalid_arguments")
    _identity(value.brain_id, "brn_")
    _version(value.issuer_epoch, minimum=1)
    _text(value.operation_id, pattern=_OPERATION)
    _digest(value.request_sha256)
    _uuid(value.approval_id)
    if _identity(value.destination_brain_id, "brn_") != value.brain_id:
        raise SharingError("invalid_arguments")
    _digest(value.receipt_sha256)
    body = {
        field.name: getattr(value, field.name)
        for field in fields(value)
        if field.name != "receipt_sha256"
    }
    try:
        digest = sha256(
            b"open-brain-sharing-receipt.v1\0" + portable_canonical_json_bytes(body)
        ).hexdigest()
    except TypeError, ValueError, UnicodeError:
        raise SharingError("invalid_arguments") from None
    if digest != value.receipt_sha256:
        raise SharingError("invalid_arguments")


SharingRequest = (
    SharingPreviewRequest | SharingInspectRequest | SharingDecisionRequest | SharingRevokeRequest
)


def parse_sharing_request[T: SharingRequest](raw: bytes, kind: type[T]) -> T:
    if type(raw) is not bytes or len(raw) > _MAX_REQUEST_BYTES:
        raise SharingError("invalid_arguments")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise SharingError("invalid_arguments")
            result[key] = value
        return result

    def constant(_value: str) -> None:
        raise SharingError("invalid_arguments")

    try:
        value = json.loads(
            raw.decode("utf-8", "strict"), object_pairs_hook=pairs, parse_constant=constant
        )
        if type(value) is not dict or set(value) != {field.name for field in fields(kind)}:
            raise SharingError("invalid_arguments")
        return kind(**cast(dict[str, Any], value))
    except UnicodeError, ValueError, TypeError, OverflowError:
        raise SharingError("invalid_arguments") from None


__all__ = [
    "SharingError",
    "SharingPreviewRequest",
    "SharingInspectRequest",
    "SharingDecisionRequest",
    "SharingRevokeRequest",
    "SharingPreview",
    "SharingInspection",
    "SharingDecisionReceipt",
    "SharingRevokeReceipt",
    "parse_sharing_request",
]

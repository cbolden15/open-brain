"""Closed, dependency-light values for the ``outbox.v1`` delivery contract."""

from __future__ import annotations

import base64
import binascii
import json
import re
import unicodedata
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import cast

from open_brain_engine.core.access_contracts import derive_brain_id, validate_issuer_epoch
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.core.models import PrivacyTier, ValidationError
from open_brain_engine.engine.contracts import (
    Payload,
    TextPayload,
    destination_bound_request_sha256,
)

OUTBOX_CONTRACT_VERSION = "outbox.v1"

_BRAIN_ID = re.compile(r"brn_[a-z2-7]{26}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_MAX_REFERENCE_LENGTH = 256
_MAX_INT_FIELD = 2**31 - 1
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z")
_TIER_PRECEDENCE = {
    PrivacyTier.PUBLIC: 0,
    PrivacyTier.WORK: 1,
    PrivacyTier.PERSONAL: 2,
    PrivacyTier.UNKNOWN: 3,
    PrivacyTier.SECRET: 4,
}

__all__ = [
    "OUTBOX_CONTRACT_VERSION",
    "DeliveryEnvelope",
    "OutboxContractError",
    "TerminalReceipt",
    "TerminalReceiptStatus",
    "outbox_request_digest",
    "verify_terminal_receipt",
]


class OutboxContractError(ValueError):
    """An outbox envelope or terminal receipt violates ``outbox.v1``."""


class TerminalReceiptStatus(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class DeliveryEnvelope:
    """Immutable destination-bound capture request carrying the engine digest."""

    contract_version: str
    destination_brain_id: str
    expected_issuer_epoch: int
    tenant_id: str
    principal_id: str
    delivery_id: str
    request_digest: str
    requested_tier: PrivacyTier
    policy_ref: str
    payload: object
    enqueued_at: str
    retry_age_limit_seconds: int
    retry_attempt_limit: int
    attempts: int = 0
    last_attempt_at: str | None = None
    last_attempt_result: str | None = None
    quarantine_reason: str | None = None
    terminal_receipt: object = None
    lineage_delivery_id: str | None = None

    def __post_init__(self) -> None:
        _validate_contract_version(self.contract_version)
        _validate_brain_id(self.destination_brain_id)
        _validate_epoch(self.expected_issuer_epoch)
        _validate_reference(self.tenant_id, "tenant ID")
        _validate_reference(self.principal_id, "principal ID")
        _validate_reference(self.delivery_id, "delivery ID")
        _validate_reference(self.policy_ref, "policy reference")
        requested_tier = _privacy_tier(self.requested_tier, "requested tier")
        payload = _canonical_payload(self.payload)
        # Self-verifying: the stored digest must equal the engine's
        # destination-bound digest recomputed from these envelope fields, so
        # a tampered body or digest never parses. The title is implicitly
        # None; no destination-bound surface carries one today.
        expected_digest = _expected_request_digest(
            destination_brain_id=self.destination_brain_id,
            issuer_epoch=self.expected_issuer_epoch,
            tenant_id=self.tenant_id,
            principal_id=self.principal_id,
            payload=payload,
            requested_tier=requested_tier,
        )
        if not isinstance(self.request_digest, str) or (
            _SHA256.fullmatch(self.request_digest) is None or self.request_digest != expected_digest
        ):
            raise OutboxContractError("invalid request digest")
        _validate_timestamp(self.enqueued_at, "enqueue time")
        _validate_window(self.retry_age_limit_seconds, "retry age limit")
        _validate_window(self.retry_attempt_limit, "retry attempt limit")
        if type(self.attempts) is not int or not 0 <= self.attempts <= _MAX_INT_FIELD:
            raise OutboxContractError("invalid attempt count")
        if self.attempts == 0 and (
            self.last_attempt_at is not None or self.last_attempt_result is not None
        ):
            raise OutboxContractError("invalid attempt metadata")
        if self.last_attempt_at is not None:
            _validate_timestamp(self.last_attempt_at, "last attempt time")
        if self.last_attempt_result is not None:
            _validate_reference(self.last_attempt_result, "last attempt result")
        if self.quarantine_reason is not None:
            _validate_reference(self.quarantine_reason, "quarantine reason")
        terminal_receipt = (
            None
            if self.terminal_receipt is None
            else _validated_terminal_receipt(self.terminal_receipt)
        )
        if self.lineage_delivery_id is not None:
            _validate_reference(self.lineage_delivery_id, "lineage delivery ID")
        object.__setattr__(self, "requested_tier", requested_tier)
        object.__setattr__(self, "payload", _freeze_json(_canonical_payload(self.payload)))
        object.__setattr__(self, "terminal_receipt", terminal_receipt)

    @classmethod
    def create(
        cls,
        *,
        destination_brain_id: str,
        expected_issuer_epoch: int,
        tenant_id: str,
        principal_id: str,
        delivery_id: str,
        requested_tier: PrivacyTier | str,
        policy_ref: str,
        payload: object,
        enqueued_at: str,
        retry_age_limit_seconds: int,
        retry_attempt_limit: int,
        lineage_delivery_id: str | None = None,
    ) -> DeliveryEnvelope:
        tier = _privacy_tier(requested_tier, "requested tier")
        canonical_payload = _canonical_payload(payload)
        request_digest = _expected_request_digest(
            destination_brain_id=destination_brain_id,
            issuer_epoch=expected_issuer_epoch,
            tenant_id=tenant_id,
            principal_id=principal_id,
            payload=canonical_payload,
            requested_tier=tier,
        )
        return cls(
            contract_version=OUTBOX_CONTRACT_VERSION,
            destination_brain_id=destination_brain_id,
            expected_issuer_epoch=expected_issuer_epoch,
            tenant_id=tenant_id,
            principal_id=principal_id,
            delivery_id=delivery_id,
            request_digest=request_digest,
            requested_tier=tier,
            policy_ref=policy_ref,
            payload=canonical_payload,
            enqueued_at=enqueued_at,
            retry_age_limit_seconds=retry_age_limit_seconds,
            retry_attempt_limit=retry_attempt_limit,
            lineage_delivery_id=lineage_delivery_id,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "attempts": self.attempts,
            "contract_version": self.contract_version,
            "delivery_id": self.delivery_id,
            "destination_brain_id": self.destination_brain_id,
            "enqueued_at": self.enqueued_at,
            "expected_issuer_epoch": self.expected_issuer_epoch,
            "last_attempt_at": self.last_attempt_at,
            "last_attempt_result": self.last_attempt_result,
            "lineage_delivery_id": self.lineage_delivery_id,
            "payload": _thaw_json(self.payload),
            "policy_ref": self.policy_ref,
            "principal_id": self.principal_id,
            "quarantine_reason": self.quarantine_reason,
            "request_digest": self.request_digest,
            "requested_tier": self.requested_tier.value,
            "retry_age_limit_seconds": self.retry_age_limit_seconds,
            "retry_attempt_limit": self.retry_attempt_limit,
            "tenant_id": self.tenant_id,
            "terminal_receipt": _thaw_json(self.terminal_receipt),
        }

    @classmethod
    def from_dict(cls, value: object) -> DeliveryEnvelope:
        expected = {
            "attempts",
            "contract_version",
            "delivery_id",
            "destination_brain_id",
            "enqueued_at",
            "expected_issuer_epoch",
            "last_attempt_at",
            "last_attempt_result",
            "lineage_delivery_id",
            "payload",
            "policy_ref",
            "principal_id",
            "quarantine_reason",
            "request_digest",
            "requested_tier",
            "retry_age_limit_seconds",
            "retry_attempt_limit",
            "tenant_id",
            "terminal_receipt",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise OutboxContractError("invalid delivery envelope fields")
        try:
            return cls(
                contract_version=cast(str, value["contract_version"]),
                destination_brain_id=cast(str, value["destination_brain_id"]),
                expected_issuer_epoch=cast(int, value["expected_issuer_epoch"]),
                tenant_id=cast(str, value["tenant_id"]),
                principal_id=cast(str, value["principal_id"]),
                delivery_id=cast(str, value["delivery_id"]),
                request_digest=cast(str, value["request_digest"]),
                requested_tier=cast(PrivacyTier, value["requested_tier"]),
                policy_ref=cast(str, value["policy_ref"]),
                payload=value["payload"],
                enqueued_at=cast(str, value["enqueued_at"]),
                retry_age_limit_seconds=cast(int, value["retry_age_limit_seconds"]),
                retry_attempt_limit=cast(int, value["retry_attempt_limit"]),
                attempts=cast(int, value["attempts"]),
                last_attempt_at=cast("str | None", value["last_attempt_at"]),
                last_attempt_result=cast("str | None", value["last_attempt_result"]),
                quarantine_reason=cast("str | None", value["quarantine_reason"]),
                terminal_receipt=value["terminal_receipt"],
                lineage_delivery_id=cast("str | None", value["lineage_delivery_id"]),
            )
        except OutboxContractError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise OutboxContractError("invalid delivery envelope") from error


@dataclass(frozen=True, slots=True)
class TerminalReceipt:
    """Terminal acceptance evidence returned by the destination Brain."""

    status: TerminalReceiptStatus
    brain_id: str
    issuer_epoch: int
    delivery_id: str
    request_digest: str
    final_admitted_tier: PrivacyTier
    protection_acknowledgement: object = None

    def __post_init__(self) -> None:
        try:
            status = TerminalReceiptStatus(self.status)
        except (TypeError, ValueError) as error:
            raise OutboxContractError("invalid terminal receipt status") from error
        _validate_brain_id(self.brain_id)
        _validate_epoch(self.issuer_epoch)
        _validate_reference(self.delivery_id, "delivery ID")
        _validate_request_digest(self.request_digest)
        final_tier = _privacy_tier(self.final_admitted_tier, "final admitted tier")
        # Null only while the deferred receipt-protection finalizer is
        # undefined (owner decision S11); verification ignores the field.
        if self.protection_acknowledgement is not None:
            raise OutboxContractError("invalid protection acknowledgement")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "final_admitted_tier", final_tier)

    def to_dict(self) -> dict[str, object]:
        return {
            "brain_id": self.brain_id,
            "delivery_id": self.delivery_id,
            "final_admitted_tier": self.final_admitted_tier.value,
            "issuer_epoch": self.issuer_epoch,
            "protection_acknowledgement": self.protection_acknowledgement,
            "request_digest": self.request_digest,
            "status": self.status.value,
        }

    @classmethod
    def from_dict(cls, value: object) -> TerminalReceipt:
        expected = {
            "brain_id",
            "delivery_id",
            "final_admitted_tier",
            "issuer_epoch",
            "protection_acknowledgement",
            "request_digest",
            "status",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise OutboxContractError("invalid terminal receipt fields")
        try:
            return cls(
                status=cast(TerminalReceiptStatus, value["status"]),
                brain_id=cast(str, value["brain_id"]),
                issuer_epoch=cast(int, value["issuer_epoch"]),
                delivery_id=cast(str, value["delivery_id"]),
                request_digest=cast(str, value["request_digest"]),
                final_admitted_tier=cast(PrivacyTier, value["final_admitted_tier"]),
                protection_acknowledgement=value["protection_acknowledgement"],
            )
        except OutboxContractError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise OutboxContractError("invalid terminal receipt") from error


def outbox_request_digest(
    *,
    destination_brain_id: str,
    issuer_epoch: int,
    tenant_id: str,
    principal_id: str,
    payload: Payload,
    requested_tier: PrivacyTier | str | None = None,
    title: str | None = None,
) -> str:
    """The engine's destination-bound request digest under its outbox name.

    The single digest definition is the engine helper; this alias exists so
    outbox callers keep one import point. The preimage excludes the delivery
    ID and the policy reference: the destination dedupes on the delivery ID,
    and the policy only decides whether a request may be attempted at all.
    """
    try:
        return destination_bound_request_sha256(
            destination_brain_id=destination_brain_id,
            issuer_epoch=issuer_epoch,
            tenant_id=tenant_id,
            principal_id=principal_id,
            payload=payload,
            requested_tier=requested_tier,
            title=title,
        )
    except UnicodeError as error:
        raise OutboxContractError("invalid canonical request encoding") from error
    except (AttributeError, TypeError, ValueError) as error:
        raise OutboxContractError("invalid destination-bound request") from error


def verify_terminal_receipt(
    envelope: DeliveryEnvelope, receipt: TerminalReceipt
) -> TerminalReceipt:
    """Verify a terminal receipt against every delivery binding and privacy narrowing."""
    if not isinstance(envelope, DeliveryEnvelope) or not isinstance(receipt, TerminalReceipt):
        raise OutboxContractError("invalid terminal receipt binding")
    try:
        checked_envelope = DeliveryEnvelope.from_dict(envelope.to_dict())
        checked_receipt = TerminalReceipt.from_dict(receipt.to_dict())
    except (TypeError, ValueError) as error:
        raise OutboxContractError("invalid terminal receipt binding") from error
    if (
        checked_receipt.brain_id != checked_envelope.destination_brain_id
        or checked_receipt.issuer_epoch != checked_envelope.expected_issuer_epoch
        or checked_receipt.delivery_id != checked_envelope.delivery_id
        or checked_receipt.request_digest != checked_envelope.request_digest
        or _TIER_PRECEDENCE[checked_receipt.final_admitted_tier]
        < _TIER_PRECEDENCE[checked_envelope.requested_tier]
    ):
        raise OutboxContractError("terminal receipt binding mismatch")
    return checked_receipt


def _validate_contract_version(value: object) -> None:
    if value != OUTBOX_CONTRACT_VERSION or type(value) is not str:
        raise OutboxContractError("invalid outbox contract version")


def _validate_request_digest(value: object) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise OutboxContractError("invalid request digest")
    return value


def _validate_timestamp(value: object, label: str) -> str:
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        raise OutboxContractError(f"invalid {label}")
    return value


def _validate_window(value: object, label: str) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_INT_FIELD:
        raise OutboxContractError(f"invalid {label}")
    return value


def _validated_terminal_receipt(value: object) -> object:
    if not isinstance(value, dict):
        raise OutboxContractError("invalid terminal receipt metadata")
    checked = TerminalReceipt.from_dict(value)
    return _freeze_json(checked.to_dict())


def _envelope_payload(payload: object) -> TextPayload:
    """Rebuild the one payload family the destination-bound surfaces carry."""
    if (
        not isinstance(payload, dict)
        or set(payload) != {"family", "text"}
        or payload["family"] != "text"
        or not isinstance(payload["text"], str)
    ):
        raise OutboxContractError("invalid envelope payload family")
    try:
        return TextPayload(payload["text"])
    except (TypeError, ValueError) as error:
        raise OutboxContractError("invalid envelope payload") from error


def _expected_request_digest(
    *,
    destination_brain_id: str,
    issuer_epoch: int,
    tenant_id: str,
    principal_id: str,
    payload: object,
    requested_tier: PrivacyTier,
) -> str:
    return outbox_request_digest(
        destination_brain_id=destination_brain_id,
        issuer_epoch=issuer_epoch,
        tenant_id=tenant_id,
        principal_id=principal_id,
        payload=_envelope_payload(payload),
        requested_tier=requested_tier,
    )


def _validate_brain_id(value: object) -> str:
    if not isinstance(value, str) or _BRAIN_ID.fullmatch(value) is None:
        raise OutboxContractError("invalid Brain ID")
    try:
        raw = base64.b32decode(value.removeprefix("brn_").upper() + "======")
        parsed = uuid.UUID(bytes=raw)
        canonical = derive_brain_id(f"tenant_{parsed}")
    except (binascii.Error, ValueError, ValidationError) as error:
        raise OutboxContractError("invalid Brain ID") from error
    if canonical != value:
        raise OutboxContractError("invalid Brain ID")
    return value


def _validate_epoch(value: object) -> int:
    try:
        return validate_issuer_epoch(cast(int, value))
    except (TypeError, ValueError) as error:
        raise OutboxContractError("invalid issuer epoch") from error


def _validate_reference(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_REFERENCE_LENGTH
        or unicodedata.normalize("NFC", value) != value
        or any(
            ord(character) < 33 or ord(character) == 127 or 0xD800 <= ord(character) <= 0xDFFF
            for character in value
        )
    ):
        raise OutboxContractError(f"invalid {label}")
    return value


def _privacy_tier(value: object, label: str) -> PrivacyTier:
    if not isinstance(value, str):
        raise OutboxContractError(f"invalid {label}")
    try:
        return PrivacyTier(value)
    except (TypeError, ValueError) as error:
        raise OutboxContractError(f"invalid {label}") from error


def _canonical_payload(value: object) -> object:
    try:
        return json.loads(portable_canonical_json_bytes(value))
    except (TypeError, ValueError, OverflowError, json.JSONDecodeError) as error:
        raise OutboxContractError("invalid JSON payload") from error


def _freeze_json(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value

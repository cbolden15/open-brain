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
from hashlib import sha256
from types import MappingProxyType
from typing import cast

from open_brain_engine.core.access_contracts import derive_brain_id, validate_issuer_epoch
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.core.models import PrivacyTier, ValidationError

OUTBOX_CONTRACT_VERSION = "outbox.v1"

_BRAIN_ID = re.compile(r"brn_[a-z2-7]{26}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_MAX_REFERENCE_LENGTH = 256
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
    """Immutable destination-bound capture request with a canonical digest."""

    contract_version: str
    destination_brain_id: str
    expected_issuer_epoch: int
    delivery_id: str
    request_digest: str
    requested_tier: PrivacyTier
    policy_ref: str
    payload: object

    def __post_init__(self) -> None:
        _validate_contract_version(self.contract_version)
        _validate_brain_id(self.destination_brain_id)
        _validate_epoch(self.expected_issuer_epoch)
        _validate_reference(self.delivery_id, "delivery ID")
        _validate_reference(self.policy_ref, "policy reference")
        requested_tier = _privacy_tier(self.requested_tier, "requested tier")
        payload = _canonical_payload(self.payload)
        expected_digest = outbox_request_digest(
            destination_brain_id=self.destination_brain_id,
            expected_issuer_epoch=self.expected_issuer_epoch,
            delivery_id=self.delivery_id,
            requested_tier=requested_tier,
            policy_ref=self.policy_ref,
            payload=payload,
        )
        if not isinstance(self.request_digest, str) or (
            _SHA256.fullmatch(self.request_digest) is None
            or self.request_digest != expected_digest
        ):
            raise OutboxContractError("invalid request digest")
        object.__setattr__(self, "requested_tier", requested_tier)
        object.__setattr__(self, "payload", _freeze_json(payload))

    @classmethod
    def create(
        cls,
        *,
        destination_brain_id: str,
        expected_issuer_epoch: int,
        delivery_id: str,
        requested_tier: PrivacyTier | str,
        policy_ref: str,
        payload: object,
    ) -> DeliveryEnvelope:
        digest = outbox_request_digest(
            destination_brain_id=destination_brain_id,
            expected_issuer_epoch=expected_issuer_epoch,
            delivery_id=delivery_id,
            requested_tier=requested_tier,
            policy_ref=policy_ref,
            payload=payload,
        )
        return cls(
            contract_version=OUTBOX_CONTRACT_VERSION,
            destination_brain_id=destination_brain_id,
            expected_issuer_epoch=expected_issuer_epoch,
            delivery_id=delivery_id,
            request_digest=digest,
            requested_tier=_privacy_tier(requested_tier, "requested tier"),
            policy_ref=policy_ref,
            payload=payload,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "delivery_id": self.delivery_id,
            "destination_brain_id": self.destination_brain_id,
            "expected_issuer_epoch": self.expected_issuer_epoch,
            "payload": _thaw_json(self.payload),
            "policy_ref": self.policy_ref,
            "request_digest": self.request_digest,
            "requested_tier": self.requested_tier.value,
        }

    @classmethod
    def from_dict(cls, value: object) -> DeliveryEnvelope:
        expected = {
            "contract_version",
            "delivery_id",
            "destination_brain_id",
            "expected_issuer_epoch",
            "payload",
            "policy_ref",
            "request_digest",
            "requested_tier",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise OutboxContractError("invalid delivery envelope fields")
        try:
            return cls(
                contract_version=cast(str, value["contract_version"]),
                destination_brain_id=cast(str, value["destination_brain_id"]),
                expected_issuer_epoch=cast(int, value["expected_issuer_epoch"]),
                delivery_id=cast(str, value["delivery_id"]),
                request_digest=cast(str, value["request_digest"]),
                requested_tier=cast(PrivacyTier, value["requested_tier"]),
                policy_ref=cast(str, value["policy_ref"]),
                payload=value["payload"],
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

    def __post_init__(self) -> None:
        try:
            status = TerminalReceiptStatus(self.status)
        except (TypeError, ValueError) as error:
            raise OutboxContractError("invalid terminal receipt status") from error
        _validate_brain_id(self.brain_id)
        _validate_epoch(self.issuer_epoch)
        _validate_reference(self.delivery_id, "delivery ID")
        if not isinstance(self.request_digest, str) or _SHA256.fullmatch(
            self.request_digest
        ) is None:
            raise OutboxContractError("invalid request digest")
        final_tier = _privacy_tier(self.final_admitted_tier, "final admitted tier")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "final_admitted_tier", final_tier)

    def to_dict(self) -> dict[str, object]:
        return {
            "brain_id": self.brain_id,
            "delivery_id": self.delivery_id,
            "final_admitted_tier": self.final_admitted_tier.value,
            "issuer_epoch": self.issuer_epoch,
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
            )
        except OutboxContractError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise OutboxContractError("invalid terminal receipt") from error


def outbox_request_digest(
    *,
    destination_brain_id: str,
    expected_issuer_epoch: int,
    delivery_id: str,
    requested_tier: PrivacyTier | str,
    policy_ref: str,
    payload: object,
) -> str:
    """Digest every non-digest ``outbox.v1`` request field canonically."""
    _validate_brain_id(destination_brain_id)
    _validate_epoch(expected_issuer_epoch)
    _validate_reference(delivery_id, "delivery ID")
    _validate_reference(policy_ref, "policy reference")
    tier = _privacy_tier(requested_tier, "requested tier")
    canonical_payload = _canonical_payload(payload)
    preimage = {
        "contract_version": OUTBOX_CONTRACT_VERSION,
        "delivery_id": delivery_id,
        "destination_brain_id": destination_brain_id,
        "expected_issuer_epoch": expected_issuer_epoch,
        "payload": canonical_payload,
        "policy_ref": policy_ref,
        "requested_tier": tier.value,
    }
    try:
        return sha256(portable_canonical_json_bytes(preimage)).hexdigest()
    except UnicodeError as error:
        raise OutboxContractError("invalid canonical request encoding") from error


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
            ord(character) < 33
            or ord(character) == 127
            or 0xD800 <= ord(character) <= 0xDFFF
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

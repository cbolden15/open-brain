"""Immutable identity, authorization, privacy, and legacy-epoch contracts."""

from __future__ import annotations

import base64
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import PurePosixPath

from .ids import portable_canonical_json_bytes
from .models import Authority, PrivacyDecision, PrivacyTier, ValidationError

__all__ = [
    "AggregatedPrivacyDecision",
    "BrainIdentity",
    "LegacyEpochBindingEvidence",
    "aggregate_privacy_decisions",
    "derive_brain_id",
    "privacy_decision_sha256",
    "validate_authorization_generation",
    "validate_issuer_epoch",
    "validate_stored_privacy_decision",
]

_TENANT_ID = re.compile(
    r"tenant_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
_SHA256 = re.compile(r"[0-9a-f]{64}")
_TIER_PRECEDENCE = {
    PrivacyTier.PUBLIC: 0,
    PrivacyTier.WORK: 1,
    PrivacyTier.PERSONAL: 2,
    PrivacyTier.UNKNOWN: 3,
    PrivacyTier.SECRET: 4,
}

type PrivacyDecisionInput = PrivacyDecision | Mapping[str, object]


def derive_brain_id(tenant_id: str) -> str:
    """Derive the durable Secure Node Brain ID from a canonical Portable tenant ID."""
    parsed = _validate_tenant_id(tenant_id)
    encoded = base64.b32encode(parsed.bytes).decode("ascii").rstrip("=").lower()
    return f"brn_{encoded}"


def validate_issuer_epoch(value: object) -> int:
    """Return a positive issuer epoch or fail closed."""
    if type(value) is not int or value <= 0:
        raise ValidationError("invalid issuer epoch")
    return value


def validate_authorization_generation(value: int) -> int:
    """Return a nonnegative authorization generation or fail closed."""
    if type(value) is not int or value < 0:
        raise ValidationError("invalid authorization generation")
    return value


@dataclass(frozen=True, slots=True)
class BrainIdentity:
    """Placement-independent durable identity and its stationary issuer epoch."""

    tenant_id: str
    brain_id: str
    issuer_epoch: int

    def __post_init__(self) -> None:
        if not isinstance(self.brain_id, str) or self.brain_id != derive_brain_id(self.tenant_id):
            raise ValidationError("invalid Brain identity")
        validate_issuer_epoch(self.issuer_epoch)

    @classmethod
    def create(cls, *, tenant_id: str, issuer_epoch: int) -> BrainIdentity:
        return cls(
            tenant_id=tenant_id,
            brain_id=derive_brain_id(tenant_id),
            issuer_epoch=issuer_epoch,
        )


def validate_stored_privacy_decision(value: object) -> PrivacyDecision:
    """Validate every stored PrivacyDecision field and return the canonical value.

    Accepts untrusted decoded payloads: anything but a decision instance or a
    mapping fails closed before any field is read.
    """
    if isinstance(value, PrivacyDecision):
        stored: Mapping[str, object] = value.to_dict()
    elif isinstance(value, Mapping):
        stored = value
    else:
        raise ValidationError("invalid stored privacy decision")
    return PrivacyDecision.from_dict(stored)


def privacy_decision_sha256(value: PrivacyDecisionInput) -> str:
    """Digest the complete validated decision using Portable canonical JSON."""
    decision = validate_stored_privacy_decision(value)
    return sha256(portable_canonical_json_bytes(decision.to_dict())).hexdigest()


@dataclass(frozen=True, slots=True)
class AggregatedPrivacyDecision:
    """Complete effective privacy and immutable lineage evidence for derived data."""

    tier: PrivacyTier
    authority: Authority
    source_decision_sha256s: tuple[str, ...]
    confirmation_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.tier, PrivacyTier):
            raise ValidationError("invalid aggregated privacy decision")
        if not isinstance(self.authority, Authority) or (
            type(self.authority.cloud) is not bool
            or type(self.authority.external_egress) is not bool
        ):
            raise ValidationError("invalid aggregated privacy decision")
        if self.tier in {PrivacyTier.SECRET, PrivacyTier.UNKNOWN} and (
            self.authority.cloud or self.authority.external_egress
        ):
            raise ValidationError("invalid aggregated privacy decision")
        if (
            not isinstance(self.source_decision_sha256s, tuple)
            or not self.source_decision_sha256s
            or tuple(sorted(set(self.source_decision_sha256s)))
            != self.source_decision_sha256s
            or any(
                not isinstance(digest, str) or _SHA256.fullmatch(digest) is None
                for digest in self.source_decision_sha256s
            )
        ):
            raise ValidationError("invalid aggregated privacy decision")
        if (
            not isinstance(self.confirmation_refs, tuple)
            or tuple(sorted(set(self.confirmation_refs))) != self.confirmation_refs
            or any(
                not isinstance(reference, str) or not reference
                for reference in self.confirmation_refs
            )
        ):
            raise ValidationError("invalid aggregated privacy decision")


def aggregate_privacy_decisions(
    values: Iterable[PrivacyDecisionInput],
) -> AggregatedPrivacyDecision:
    """Combine a nonempty source set without broadening tier or egress authority."""
    decisions = tuple(validate_stored_privacy_decision(value) for value in values)
    if not decisions:
        raise ValidationError("privacy aggregation requires at least one source decision")
    tier = max((decision.tier for decision in decisions), key=_TIER_PRECEDENCE.__getitem__)
    return AggregatedPrivacyDecision(
        tier=tier,
        authority=Authority(
            cloud=all(decision.authority.cloud for decision in decisions),
            external_egress=all(decision.authority.external_egress for decision in decisions),
        ),
        source_decision_sha256s=tuple(
            sorted({privacy_decision_sha256(decision) for decision in decisions})
        ),
        confirmation_refs=tuple(
            sorted(
                {
                    decision.confirmation_ref
                    for decision in decisions
                    if decision.confirmation_ref is not None
                }
            )
        ),
    )


@dataclass(frozen=True, slots=True)
class LegacyEpochBindingEvidence:
    """Exact portable evidence binding a pre-cutover payload to an issuer epoch."""

    artifact_path: str
    jsonl_ordinal: int | None
    payload_sha256: str
    issuer_epoch: int

    def __post_init__(self) -> None:
        _validate_portable_artifact_path(self.artifact_path)
        is_jsonl = PurePosixPath(self.artifact_path).suffix == ".jsonl"
        if is_jsonl != (self.jsonl_ordinal is not None) or (
            self.jsonl_ordinal is not None
            and (type(self.jsonl_ordinal) is not int or self.jsonl_ordinal < 0)
        ):
            raise ValidationError("invalid JSONL ordinal")
        if not isinstance(self.payload_sha256, str) or _SHA256.fullmatch(
            self.payload_sha256
        ) is None:
            raise ValidationError("invalid payload SHA-256")
        validate_issuer_epoch(self.issuer_epoch)


def _validate_tenant_id(value: str) -> uuid.UUID:
    if not isinstance(value, str) or _TENANT_ID.fullmatch(value) is None:
        raise ValidationError("invalid tenant ID")
    try:
        parsed = uuid.UUID(value.removeprefix("tenant_"))
    except ValueError as error:  # pragma: no cover - the regex already narrows the value.
        raise ValidationError("invalid tenant ID") from error
    if parsed.version != 4 or value != f"tenant_{parsed}":
        raise ValidationError("invalid tenant ID")
    return parsed


def _validate_portable_artifact_path(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError("invalid portable artifact path")
    path = PurePosixPath(value)
    if (
        value == "."
        or path.is_absolute()
        or "\\" in value
        or "\x00" in value
        or any(part in {"", ".", "..", ".open-brain"} for part in path.parts)
        or path.as_posix() != value
    ):
        raise ValidationError("invalid portable artifact path")
    return value

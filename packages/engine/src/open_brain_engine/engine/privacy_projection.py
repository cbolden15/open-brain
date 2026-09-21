"""Engine-owned projection of retained privacy evidence onto effective privacy."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256

from ..core.access_contracts import (
    AggregatedPrivacyDecision,
    aggregate_privacy_decisions,
    privacy_decision_sha256,
    validate_stored_privacy_decision,
)
from ..core.ids import portable_canonical_json_bytes
from ..core.models import (
    Authority,
    PrivacyDecision,
    PrivacyReason,
    PrivacyTier,
    ValidationError,
    narrowest_tier,
)

__all__ = [
    "InvalidPrivacyEvidenceReason",
    "RepairedPrivacyEvidence",
    "RetainedPrivacyEvidence",
    "apply_privacy_repair",
    "effective_privacy_json",
    "narrow_retained_privacy_decision",
    "project_retained_privacy_evidence",
]


def effective_privacy_json(evidence: RetainedPrivacyEvidence) -> str:
    """Encode one complete effective-privacy projection as canonical Portable JSON.

    Migration backfill and new search-document writes must share this one encoding so
    their durable projection rows stay byte-identical.
    """
    return portable_canonical_json_bytes(
        {
            "tier": evidence.tier.value,
            "authority": {
                "cloud": evidence.authority.cloud,
                "external_egress": evidence.authority.external_egress,
            },
            "source_decision_sha256s": list(evidence.source_decision_sha256s),
            "confirmation_refs": list(evidence.confirmation_refs),
            "invalid_reason": (
                None if evidence.invalid_reason is None else evidence.invalid_reason.value
            ),
            "invalid_evidence_sha256": evidence.invalid_evidence_sha256,
        }
    ).decode("utf-8")


_INVALID_EVIDENCE_DOMAIN = "open-brain.privacy.invalid-evidence.v1"
_SHA256 = re.compile(r"[0-9a-f]{64}")

type RetainedPrivacyValue = str | int | float | bytes | None


class InvalidPrivacyEvidenceReason(StrEnum):
    """Exact reason retained privacy evidence failed closed."""

    MISSING = "missing"
    MALFORMED = "malformed"
    INCONSISTENT = "inconsistent"


@dataclass(frozen=True, slots=True)
class RetainedPrivacyEvidence:
    """Complete effective privacy projected from retained evidence for one record."""

    tier: PrivacyTier
    authority: Authority
    source_decision_sha256s: tuple[str, ...]
    confirmation_refs: tuple[str, ...]
    invalid_reason: InvalidPrivacyEvidenceReason | None
    invalid_evidence_sha256: str | None

    @property
    def valid(self) -> bool:
        """Whether the projection was fully supported by valid retained evidence."""
        return self.invalid_reason is None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.tier, PrivacyTier)
            or not isinstance(self.authority, Authority)
            or type(self.authority.cloud) is not bool
            or type(self.authority.external_egress) is not bool
        ):
            raise ValidationError("invalid retained privacy evidence")
        if not _are_sorted_digests(self.source_decision_sha256s):
            raise ValidationError("invalid retained privacy evidence")
        if not _are_sorted_references(self.confirmation_refs):
            raise ValidationError("invalid retained privacy evidence")
        if self.tier in {PrivacyTier.SECRET, PrivacyTier.UNKNOWN} and (
            self.authority.cloud or self.authority.external_egress
        ):
            raise ValidationError("invalid retained privacy evidence")
        if self.invalid_reason is None:
            if self.invalid_evidence_sha256 is not None or not self.source_decision_sha256s:
                raise ValidationError("invalid retained privacy evidence")
            return
        if (
            not isinstance(self.invalid_reason, InvalidPrivacyEvidenceReason)
            or self.tier is not PrivacyTier.UNKNOWN
            or self.authority.cloud
            or self.authority.external_egress
            or self.source_decision_sha256s
            or self.confirmation_refs
            or not isinstance(self.invalid_evidence_sha256, str)
            or _SHA256.fullmatch(self.invalid_evidence_sha256) is None
        ):
            raise ValidationError("invalid retained privacy evidence")


@dataclass(frozen=True, slots=True)
class RepairedPrivacyEvidence:
    """Effective privacy after one accepted owner repair replaces invalid evidence.

    The replacement decision supplies the effective tier, authority, decision digest,
    and confirmation reference; the original invalid reason and digest survive as
    retained lineage so the repair never rewrites what the evidence failed on.
    """

    tier: PrivacyTier
    authority: Authority
    source_decision_sha256s: tuple[str, ...]
    confirmation_refs: tuple[str, ...]
    invalid_reason: InvalidPrivacyEvidenceReason
    invalid_evidence_sha256: str
    applied_repair_id: str
    applied_repair_sequence: int

    @property
    def valid(self) -> bool:
        """A repaired projection resolves invalid evidence but keeps its lineage."""
        return False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.tier, PrivacyTier)
            or not isinstance(self.authority, Authority)
            or type(self.authority.cloud) is not bool
            or type(self.authority.external_egress) is not bool
        ):
            raise ValidationError("invalid repaired privacy evidence")
        if not _are_sorted_digests(self.source_decision_sha256s):
            raise ValidationError("invalid repaired privacy evidence")
        if not _are_sorted_references(self.confirmation_refs):
            raise ValidationError("invalid repaired privacy evidence")
        if self.tier in {PrivacyTier.SECRET, PrivacyTier.UNKNOWN} and (
            self.authority.cloud or self.authority.external_egress
        ):
            raise ValidationError("invalid repaired privacy evidence")
        if (
            not isinstance(self.invalid_reason, InvalidPrivacyEvidenceReason)
            or not isinstance(self.invalid_evidence_sha256, str)
            or _SHA256.fullmatch(self.invalid_evidence_sha256) is None
        ):
            raise ValidationError("invalid repaired privacy evidence")
        if (
            not isinstance(self.applied_repair_id, str)
            or not self.applied_repair_id
            or type(self.applied_repair_sequence) is not int
            or self.applied_repair_sequence < 1
        ):
            raise ValidationError("invalid repaired privacy evidence")


def apply_privacy_repair(
    base: RetainedPrivacyEvidence,
    replacement: PrivacyDecision,
    *,
    applied_repair_id: str,
    applied_repair_sequence: int,
) -> RepairedPrivacyEvidence:
    """Overlay one validated replacement decision onto invalid retained evidence.

    Only invalid evidence may be repaired: a valid base ignores repairs entirely.
    The replacement must itself satisfy the closed authority contract, so a
    repaired secret or unknown tier can never authorize egress.
    """
    if base.valid:
        raise ValidationError("privacy repair requires invalid retained evidence")
    assert base.invalid_reason is not None
    assert base.invalid_evidence_sha256 is not None
    decision = validate_stored_privacy_decision(replacement)
    if decision.tier in {PrivacyTier.SECRET, PrivacyTier.UNKNOWN} and (
        decision.authority.cloud or decision.authority.external_egress
    ):
        raise ValidationError("repaired tier cannot authorize egress")
    return RepairedPrivacyEvidence(
        tier=decision.tier,
        authority=decision.authority,
        source_decision_sha256s=(privacy_decision_sha256(decision),),
        confirmation_refs=(
            () if decision.confirmation_ref is None else (decision.confirmation_ref,)
        ),
        invalid_reason=base.invalid_reason,
        invalid_evidence_sha256=base.invalid_evidence_sha256,
        applied_repair_id=applied_repair_id,
        applied_repair_sequence=applied_repair_sequence,
    )


# PUBLIC never appears here: it can never be the narrowest tier of a wider
# submitted decision, so it cannot name a narrowing reason.
_NARROWED_REASON: dict[PrivacyTier, PrivacyReason] = {
    PrivacyTier.WORK: PrivacyReason.POLICY_WORK,
    PrivacyTier.PERSONAL: PrivacyReason.PERSONAL_LOCAL_ONLY,
    PrivacyTier.SECRET: PrivacyReason.SECRET_DETECTED,
    PrivacyTier.UNKNOWN: PrivacyReason.CLASSIFICATION_MISSING,
}


def narrow_retained_privacy_decision(
    decision: PrivacyDecision, final_tier: PrivacyTier
) -> PrivacyDecision:
    """Bind one admission-narrowed final tier onto the retained privacy decision.

    The retained decision is returned unchanged unless ``final_tier`` is
    narrower under :func:`narrowest_tier`, so a wider signal can never broaden
    tier or egress authority. A narrowed decision keeps the submitted policy
    version, names the canonical boundary reason for its tier, keeps egress
    authority only where that closed reason permits it, and drops the
    confirmation reference, so what is retained stays one valid decision the
    effective-privacy projection derives unchanged.
    """
    if not isinstance(decision, PrivacyDecision) or not isinstance(final_tier, PrivacyTier):
        raise ValidationError("invalid narrowed privacy decision")
    if narrowest_tier(decision.tier, final_tier) is decision.tier:
        return decision
    reason = _NARROWED_REASON[final_tier]
    authority = (
        decision.authority
        if reason is PrivacyReason.POLICY_WORK
        else Authority(cloud=False, external_egress=False)
    )
    return PrivacyDecision.create(
        tier=final_tier,
        reason=reason,
        policy_version=decision.policy_version,
        authority=authority,
    )


def project_retained_privacy_evidence(
    retained: Iterable[RetainedPrivacyValue] | RetainedPrivacyValue,
    *,
    caller_declared_inconsistent: bool = False,
) -> RetainedPrivacyEvidence:
    """Project one or more stored PrivacyDecision JSON values onto effective privacy.

    ``retained`` is one bare SQLite storage value or an iterable of them, so the
    projection is total over the SQLite storage classes: bare ``None`` (or any
    ``None`` element) is missing evidence and every other non-text value is
    malformed evidence. ``caller_declared_inconsistent`` must be a strict ``bool``.

    Valid evidence aggregates by engine tier precedence and egress-authority
    intersection. Missing, malformed, or caller-declared inconsistent evidence fails
    closed to an ``unknown`` local-only projection carrying the exact invalid reason
    and a durable domain-separated digest over the exact retained values, in that
    precedence order. Non-text retained values reach that digest only through
    deterministic type-tagged preimage encodings, never as raw floats or blobs. The
    digest binds the retained values as an ordered tuple even though valid
    aggregation is order-independent, so migration callers must pass each record's
    retained values in a deterministic order (for example a stable ``ORDER BY``) to
    keep invalid-evidence digests reproducible. The projection never infers privacy
    from content, title, space, or path.
    """
    if type(caller_declared_inconsistent) is not bool:
        raise ValidationError("caller_declared_inconsistent must be a bool")
    values: tuple[RetainedPrivacyValue, ...] = (
        tuple(retained)
        if isinstance(retained, Iterable) and not isinstance(retained, (str, bytes))
        else (retained,)
    )
    if not values or any(value is None for value in values):
        return _invalid_projection(InvalidPrivacyEvidenceReason.MISSING, values)
    decisions: list[PrivacyDecision] = []
    for value in values:
        if not isinstance(value, str):
            return _invalid_projection(InvalidPrivacyEvidenceReason.MALFORMED, values)
        try:
            decision = validate_stored_privacy_decision(json.loads(value))
        except ValueError:
            return _invalid_projection(InvalidPrivacyEvidenceReason.MALFORMED, values)
        decisions.append(decision)
    if caller_declared_inconsistent:
        return _invalid_projection(InvalidPrivacyEvidenceReason.INCONSISTENT, values)
    return _valid_projection(aggregate_privacy_decisions(decisions))


def _invalid_projection(
    reason: InvalidPrivacyEvidenceReason, values: tuple[RetainedPrivacyValue, ...]
) -> RetainedPrivacyEvidence:
    digest = sha256(
        portable_canonical_json_bytes(
            {
                "domain": _INVALID_EVIDENCE_DOMAIN,
                "reason": reason.value,
                "retained": [_invalid_evidence_preimage_value(value) for value in values],
            }
        )
    ).hexdigest()
    return RetainedPrivacyEvidence(
        tier=PrivacyTier.UNKNOWN,
        authority=Authority(cloud=False, external_egress=False),
        source_decision_sha256s=(),
        confirmation_refs=(),
        invalid_reason=reason,
        invalid_evidence_sha256=digest,
    )


def _invalid_evidence_preimage_value(value: object) -> object:
    """Encode one retained value as a deterministic, type-tagged digest input.

    ``None`` and text keep their direct Portable JSON encodings; every other value
    is tagged so unsupported floats and blobs never reach canonical JSON and
    distinct storage classes can never collide in the digest preimage.
    """
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return {"type": "boolean", "value": value}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "real", "value": value.hex()}
    if isinstance(value, bytes):
        return {"type": "blob", "value": value.hex()}
    return {"type": "unsupported", "python_type": type(value).__name__}


def _valid_projection(
    aggregated: AggregatedPrivacyDecision,
) -> RetainedPrivacyEvidence:
    return RetainedPrivacyEvidence(
        tier=aggregated.tier,
        authority=aggregated.authority,
        source_decision_sha256s=aggregated.source_decision_sha256s,
        confirmation_refs=aggregated.confirmation_refs,
        invalid_reason=None,
        invalid_evidence_sha256=None,
    )


def _are_sorted_digests(values: tuple[str, ...]) -> bool:
    return tuple(sorted(set(values))) == values and all(
        isinstance(value, str) and _SHA256.fullmatch(value) is not None for value in values
    )


def _are_sorted_references(values: tuple[str, ...]) -> bool:
    return tuple(sorted(set(values))) == values and all(
        isinstance(value, str) and value for value in values
    )

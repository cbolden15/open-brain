"""Closed startup policy mapping for confidentiality-scoped foreground sessions."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from open_brain_engine.core.access_contracts import (
    validate_authorization_generation,
    validate_issuer_epoch,
)
from open_brain_engine.core.models import PrivacyTier, ValidationError
from open_brain_engine.engine.consent_contracts import (
    ConsentContractError,
    EgressMode,
    ProviderConsentState,
)
from open_brain_engine.engine.t03_contracts import EffectiveAuthority

__all__ = [
    "LauncherPolicy",
    "LauncherPolicyError",
    "parse_launcher_policy",
    "require_capture_tier",
    "validate_startup_policy",
]

_POLICY_VERSION = "launcher-policy.v1"
_POLICY_FIELDS = frozenset(
    {
        "policy_version",
        "principal_id",
        "session_id",
        "capabilities",
        "space_ids",
        "allowed_read_tiers",
        "allowed_capture_tiers",
        "egress_mode",
        "provider_id",
        "consent_id",
        "authorization_generation",
        "brain_id",
        "issuer_epoch",
    }
)
_CLIENT_FORBIDDEN_FIELDS = _POLICY_FIELDS | {"owner"}
_SPACE_ID = re.compile(
    r"space_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


class LauncherPolicyError(ValueError):
    """The launcher policy failed before a scoped operation could start."""

    def __init__(self, code: str) -> None:
        if code not in {
            "capture_tier_denied",
            "client_policy_override",
            "consent_unavailable",
            "destination_mismatch",
            "invalid_policy",
            "issuer_mismatch",
            "stale_policy",
        }:
            raise ValueError("invalid launcher policy error code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class LauncherPolicy:
    principal_id: str
    session_id: str
    capabilities: frozenset[str]
    space_ids: frozenset[str] | None
    allowed_read_tiers: frozenset[PrivacyTier]
    allowed_capture_tiers: frozenset[PrivacyTier]
    egress_mode: EgressMode
    provider_id: str | None
    consent_id: str | None
    authorization_generation: int
    brain_id: str
    issuer_epoch: int

    def to_authority(self) -> EffectiveAuthority:
        return EffectiveAuthority(
            principal_id=self.principal_id,
            session_id=self.session_id,
            capabilities=self.capabilities,
            space_ids=self.space_ids,
            authorization_generation=self.authorization_generation,
            owner=False,
            allowed_read_tiers=self.allowed_read_tiers,
            allowed_capture_tiers=self.allowed_capture_tiers,
            egress_mode=self.egress_mode,
            provider_id=self.provider_id,
            consent_id=self.consent_id,
            brain_id=self.brain_id,
            issuer_epoch=self.issuer_epoch,
        )

    def permits_capture_tier(self, tier: PrivacyTier) -> bool:
        return tier in self.allowed_capture_tiers


def parse_launcher_policy(raw: str | bytes | Mapping[str, object]) -> LauncherPolicy:
    """Parse the exact launcher-policy.v1 object without accepting extensions."""
    try:
        value = _load_object(raw)
        if set(value) != _POLICY_FIELDS or value["policy_version"] != _POLICY_VERSION:
            raise ValueError
        principal_id = _bounded_text(value["principal_id"])
        session_id = _bounded_text(value["session_id"])
        capabilities = _string_set(value["capabilities"], pattern=r"[a-z][a-z-]{0,63}")
        space_ids = _optional_string_set(value["space_ids"], pattern=_SPACE_ID.pattern, limit=256)
        read_tiers = _tier_set(value["allowed_read_tiers"])
        capture_tiers = _tier_set(value["allowed_capture_tiers"])
        if read_tiers & {PrivacyTier.SECRET, PrivacyTier.UNKNOWN}:
            raise ValueError
        raw_egress_mode = value["egress_mode"]
        if not isinstance(raw_egress_mode, str):
            raise ValueError
        egress_mode = EgressMode(raw_egress_mode)
        provider_id = _optional_text(value["provider_id"])
        consent_id = _optional_text(value["consent_id"])
        authorization_generation = cast(int, value["authorization_generation"])
        issuer_epoch = cast(int, value["issuer_epoch"])
        validate_authorization_generation(authorization_generation)
        validate_issuer_epoch(issuer_epoch)
        brain_id = cast(str, value["brain_id"])
        if not isinstance(brain_id, str) or re.fullmatch(r"brn_[a-z2-7]{26}", brain_id) is None:
            raise ValueError
        if egress_mode is EgressMode.OWNER_LOCAL:
            if provider_id is not None or consent_id is not None:
                raise ValueError
        elif (
            provider_id is None
            or re.fullmatch(r"[a-z][a-z0-9._-]{0,63}", provider_id) is None
            or consent_id is None
            or re.fullmatch(r"consent_[0-9a-f]{32}", consent_id) is None
        ):
            raise ValueError
        policy = LauncherPolicy(
            principal_id=principal_id,
            session_id=session_id,
            capabilities=capabilities,
            space_ids=space_ids,
            allowed_read_tiers=read_tiers,
            allowed_capture_tiers=capture_tiers,
            egress_mode=egress_mode,
            provider_id=provider_id,
            consent_id=consent_id,
            authorization_generation=authorization_generation,
            brain_id=brain_id,
            issuer_epoch=issuer_epoch,
        )
        policy.to_authority()
        return policy
    except (KeyError, TypeError, ValueError, ValidationError):
        raise LauncherPolicyError("invalid_policy") from None


def validate_startup_policy(
    raw: str | bytes | Mapping[str, object],
    *,
    current_brain_id: str,
    current_issuer_epoch: int,
    current_authorization_generation: int,
    consent_state: ProviderConsentState | None,
    client_arguments: Mapping[str, object] | None = None,
) -> EffectiveAuthority:
    """Validate current destination state and return immutable effective authority."""
    if client_arguments is not None and set(client_arguments) & _CLIENT_FORBIDDEN_FIELDS:
        raise LauncherPolicyError("client_policy_override")
    policy = parse_launcher_policy(raw)
    if (
        not isinstance(current_brain_id, str)
        or re.fullmatch(r"brn_[a-z2-7]{26}", current_brain_id) is None
    ):
        raise LauncherPolicyError("destination_mismatch")
    try:
        validate_issuer_epoch(current_issuer_epoch)
    except ValidationError:
        raise LauncherPolicyError("issuer_mismatch") from None
    try:
        validate_authorization_generation(current_authorization_generation)
    except ValidationError:
        raise LauncherPolicyError("stale_policy") from None
    if policy.brain_id != current_brain_id:
        raise LauncherPolicyError("destination_mismatch")
    if policy.issuer_epoch != current_issuer_epoch:
        raise LauncherPolicyError("issuer_mismatch")
    if policy.authorization_generation != current_authorization_generation:
        raise LauncherPolicyError("stale_policy")
    if policy.egress_mode is EgressMode.EXTERNAL_PROVIDER:
        if (
            not isinstance(consent_state, ProviderConsentState)
            or policy.provider_id is None
            or policy.consent_id is None
        ):
            raise LauncherPolicyError("consent_unavailable")
        try:
            consent = consent_state.active_consent(
                consent_id=policy.consent_id,
                provider_id=policy.provider_id,
                authorization_generation=current_authorization_generation,
            )
        except ConsentContractError:
            raise LauncherPolicyError("consent_unavailable") from None
        if not policy.allowed_read_tiers <= consent.allowed_tiers:
            raise LauncherPolicyError("consent_unavailable")
    return policy.to_authority()


def require_capture_tier(authority: EffectiveAuthority, tier: PrivacyTier) -> None:
    """Reject a capture tier not present in the immutable startup authority."""
    if not isinstance(tier, PrivacyTier) or not authority.permits_capture_tier(tier):
        raise LauncherPolicyError("capture_tier_denied")


def _load_object(raw: str | bytes | Mapping[str, object]) -> dict[str, object]:
    if isinstance(raw, Mapping):
        if any(not isinstance(key, str) for key in raw):
            raise ValueError
        return dict(raw)
    size = len(raw.encode("utf-8") if isinstance(raw, str) else raw)
    if size > 65536:
        raise ValueError

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    value = json.loads(raw, object_pairs_hook=reject_duplicates)
    if not isinstance(value, dict):
        raise ValueError
    return cast(dict[str, object], value)


def _bounded_text(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 256
        or any(ord(character) < 32 or 0xD800 <= ord(character) <= 0xDFFF for character in value)
    ):
        raise ValueError
    return value


def _optional_text(value: object) -> str | None:
    return None if value is None else _bounded_text(value)


def _string_set(value: object, *, pattern: str, limit: int | None = None) -> frozenset[str]:
    if not isinstance(value, list) or limit is not None and len(value) > limit:
        raise ValueError
    if (
        len(set(map(str, value))) != len(value)
        or any(not isinstance(item, str) or re.fullmatch(pattern, item) is None for item in value)
    ):
        raise ValueError
    return frozenset(cast(list[str], value))


def _optional_string_set(
    value: object, *, pattern: str, limit: int | None = None
) -> frozenset[str] | None:
    return None if value is None else _string_set(value, pattern=pattern, limit=limit)


def _tier_set(value: object) -> frozenset[PrivacyTier]:
    if not isinstance(value, list) or len(set(map(str, value))) != len(value):
        raise ValueError
    return frozenset(PrivacyTier(item) for item in value)

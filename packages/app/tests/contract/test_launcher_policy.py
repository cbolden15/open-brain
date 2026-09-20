from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import FrozenInstanceError

import pytest
from open_brain_engine.core.access_contracts import derive_brain_id
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine.consent_contracts import ProviderConsentState
from open_brain_engine.engine.t03_contracts import EffectiveAuthority

from open_brain.services.launcher_policy import (
    LauncherPolicyError,
    parse_launcher_policy,
    require_capture_tier,
    validate_startup_policy,
)

BRAIN_ID = derive_brain_id("tenant_00000000-0000-4000-8000-000000000001")
OTHER_BRAIN_ID = derive_brain_id("tenant_00000000-0000-4000-8000-000000000002")
CONSENT_ID = "consent_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def external_consent() -> ProviderConsentState:
    return ProviderConsentState().grant(
        owner=True,
        provider_id="synthetic-provider",
        allowed_tiers=frozenset({PrivacyTier.PUBLIC, PrivacyTier.WORK}),
        operation_id="grant-1",
        decided_at="2026-09-19T12:00:00Z",
        consent_id_factory=lambda: CONSENT_ID,
    ).state


def policy(*, external: bool = True) -> dict[str, object]:
    return {
        "policy_version": "launcher-policy.v1",
        "principal_id": "synthetic-principal",
        "session_id": "synthetic-session",
        "capabilities": ["content-read", "search"],
        "space_ids": None,
        "allowed_read_tiers": ["public", "work"],
        "allowed_capture_tiers": ["personal", "work"],
        "egress_mode": "external_provider" if external else "owner_local",
        "provider_id": "synthetic-provider" if external else None,
        "consent_id": CONSENT_ID if external else None,
        "authorization_generation": 1 if external else 0,
        "brain_id": BRAIN_ID,
        "issuer_epoch": 7,
    }


def validate_external(value: dict[str, object]) -> EffectiveAuthority:
    return validate_startup_policy(
        value,
        current_brain_id=BRAIN_ID,
        current_issuer_epoch=7,
        current_authorization_generation=1,
        consent_state=external_consent(),
    )


def test_closed_policy_maps_every_field_to_immutable_authority() -> None:
    source = policy()
    parsed = parse_launcher_policy(source)
    cast_capabilities = source["capabilities"]
    assert isinstance(cast_capabilities, list)
    cast_capabilities.append("history-read")
    assert "history-read" not in parsed.capabilities

    authority = validate_external(policy())
    assert authority.principal_id == "synthetic-principal"
    assert authority.session_id == "synthetic-session"
    assert authority.capabilities == frozenset({"content-read", "search"})
    assert authority.space_ids is None
    assert authority.allowed_read_tiers == frozenset({PrivacyTier.PUBLIC, PrivacyTier.WORK})
    assert authority.allowed_capture_tiers == frozenset(
        {PrivacyTier.PERSONAL, PrivacyTier.WORK}
    )
    assert authority.egress_mode.value == "external_provider"
    assert authority.provider_id == "synthetic-provider"
    assert authority.consent_id == CONSENT_ID
    assert authority.authorization_generation == 1
    assert authority.brain_id == BRAIN_ID
    assert authority.issuer_epoch == 7
    assert not authority.owner
    with pytest.raises(FrozenInstanceError):
        authority.session_id = "replacement"  # type: ignore[misc]


@pytest.mark.parametrize("field", sorted(set(policy()) | {"owner"}))
def test_client_cannot_supply_policy_fields(field: str) -> None:
    with pytest.raises(LauncherPolicyError, match="client_policy_override"):
        validate_startup_policy(
            policy(),
            current_brain_id=BRAIN_ID,
            current_issuer_epoch=7,
            current_authorization_generation=1,
            consent_state=external_consent(),
            client_arguments={field: "override"},
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.pop("brain_id"),
        lambda value: value.update({"unexpected": True}),
        lambda value: value.update({"owner": True}),
        lambda value: value.update({"policy_version": "launcher-policy.v2"}),
        lambda value: value.update({"allowed_read_tiers": ["unknown"]}),
        lambda value: value.update({"allowed_read_tiers": ["secret"]}),
        lambda value: value.update({"provider_id": None}),
        lambda value: value.update({"issuer_epoch": 0}),
    ],
)
def test_missing_malformed_or_extended_policy_fails_closed(
    mutation: Callable[[dict[str, object]], object],
) -> None:
    value = policy()
    mutation(value)
    with pytest.raises(LauncherPolicyError, match="invalid_policy"):
        parse_launcher_policy(value)

    duplicate = json.dumps(policy()).replace(
        '"policy_version": "launcher-policy.v1",',
        '"policy_version": "launcher-policy.v1", "policy_version": "launcher-policy.v1",',
    )
    with pytest.raises(LauncherPolicyError, match="invalid_policy"):
        parse_launcher_policy(duplicate)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("brain_id", OTHER_BRAIN_ID, "destination_mismatch"),
        ("issuer_epoch", 8, "issuer_mismatch"),
        ("authorization_generation", 0, "stale_policy"),
    ],
)
def test_destination_issuer_and_generation_must_be_current(
    field: str, value: object, error: str
) -> None:
    arguments = {
        "current_brain_id": BRAIN_ID,
        "current_issuer_epoch": 7,
        "current_authorization_generation": 1,
        "consent_state": external_consent(),
    }
    if field == "brain_id":
        arguments["current_brain_id"] = value
    elif field == "issuer_epoch":
        arguments["current_issuer_epoch"] = value
    else:
        arguments["current_authorization_generation"] = value
    with pytest.raises(LauncherPolicyError, match=error):
        validate_startup_policy(policy(), **arguments)  # type: ignore[arg-type]


def test_external_policy_requires_matching_active_consent_and_allowed_tiers() -> None:
    mismatched = policy()
    mismatched["consent_id"] = "consent_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    with pytest.raises(LauncherPolicyError, match="consent_unavailable"):
        validate_external(mismatched)

    too_broad = policy()
    too_broad["allowed_read_tiers"] = ["public", "personal"]
    with pytest.raises(LauncherPolicyError, match="consent_unavailable"):
        validate_external(too_broad)

    revoked = external_consent().revoke(
        owner=True,
        consent_id=CONSENT_ID,
        operation_id="revoke-1",
        decided_at="2026-09-19T12:01:00Z",
    ).state
    revoked_policy = policy()
    revoked_policy["authorization_generation"] = 2
    with pytest.raises(LauncherPolicyError, match="consent_unavailable"):
        validate_startup_policy(
            revoked_policy,
            current_brain_id=BRAIN_ID,
            current_issuer_epoch=7,
            current_authorization_generation=2,
            consent_state=revoked,
        )


def test_capture_tier_must_be_inside_immutable_policy_set() -> None:
    authority = validate_external(policy())
    require_capture_tier(authority, PrivacyTier.PERSONAL)
    with pytest.raises(LauncherPolicyError, match="capture_tier_denied"):
        require_capture_tier(authority, PrivacyTier.PUBLIC)


def test_owner_local_policy_needs_no_provider_consent() -> None:
    authority = validate_startup_policy(
        policy(external=False),
        current_brain_id=BRAIN_ID,
        current_issuer_epoch=7,
        current_authorization_generation=0,
        consent_state=None,
    )
    assert authority.egress_mode.value == "owner_local"
    assert authority.provider_id is None and authority.consent_id is None


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"current_brain_id": "malformed"}, "destination_mismatch"),
        ({"current_issuer_epoch": True}, "issuer_mismatch"),
        ({"current_authorization_generation": True}, "stale_policy"),
        ({"consent_state": object()}, "consent_unavailable"),
    ],
)
def test_malformed_runtime_state_fails_closed(
    changes: dict[str, object], error: str
) -> None:
    arguments: dict[str, object] = {
        "current_brain_id": BRAIN_ID,
        "current_issuer_epoch": 7,
        "current_authorization_generation": 1,
        "consent_state": external_consent(),
    }
    arguments.update(changes)
    with pytest.raises(LauncherPolicyError, match=error):
        validate_startup_policy(policy(), **arguments)  # type: ignore[arg-type]

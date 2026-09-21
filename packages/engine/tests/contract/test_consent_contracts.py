from __future__ import annotations

from dataclasses import replace

import pytest
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine.consent_contracts import (
    ConsentContractError,
    ConsentTransition,
    EgressMode,
    ProviderConsentRecord,
    ProviderConsentState,
)

CONSENT_A = "consent_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
CONSENT_B = "consent_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
CONSENT_C = "consent_cccccccccccccccccccccccccccccccc"
NOW = "2026-09-19T12:00:00Z"
LATER = "2026-09-19T12:01:00Z"


def grant(state: ProviderConsentState | None = None) -> ConsentTransition:
    if state is None:
        state = ProviderConsentState()
    return state.grant(
        owner=True,
        provider_id="synthetic-provider",
        allowed_tiers=frozenset({PrivacyTier.PUBLIC, PrivacyTier.WORK}),
        operation_id="grant-1",
        decided_at=NOW if state.authorization_generation == 0 else LATER,
        consent_id_factory=lambda: CONSENT_A,
    )


def test_owner_grant_inspect_and_operation_id_idempotency() -> None:
    transition = grant()
    assert transition.state.authorization_generation == 1
    assert transition.receipt.consent_id == CONSENT_A
    assert transition.state.inspect(owner=True).records == transition.state.records
    assert transition.state.inspect(owner=True).authorization_generation == 1

    replay = grant(transition.state)
    assert replay.state is transition.state
    assert replay.receipt == transition.receipt
    with pytest.raises(ConsentContractError, match="operation_conflict"):
        transition.state.grant(
            owner=True,
            provider_id="synthetic-provider",
            allowed_tiers=frozenset({PrivacyTier.PUBLIC}),
            operation_id="grant-1",
            decided_at=NOW,
            consent_id_factory=lambda: CONSENT_B,
        )

    duplicate = transition.state.grant(
        owner=True,
        provider_id="synthetic-provider",
        allowed_tiers=frozenset({PrivacyTier.PUBLIC, PrivacyTier.WORK}),
        operation_id="grant-duplicate",
        decided_at=NOW,
        consent_id_factory=lambda: CONSENT_B,
    )
    assert duplicate.receipt.duplicate
    assert duplicate.receipt.consent_id == CONSENT_A
    assert duplicate.state.authorization_generation == 1


def test_replace_and_revoke_each_advance_once_and_never_reactivate_ids() -> None:
    granted = grant().state
    replaced = granted.replace(
        owner=True,
        consent_id=CONSENT_A,
        provider_id="replacement-provider",
        allowed_tiers=frozenset({PrivacyTier.PERSONAL}),
        operation_id="replace-1",
        decided_at=LATER,
        consent_id_factory=lambda: CONSENT_B,
    )
    assert replaced.state.authorization_generation == 2
    assert replaced.receipt.previous_consent_id == CONSENT_A
    assert not replaced.state.records[0].active
    assert replaced.state.records[0].revoked_generation == 2
    assert replaced.state.records[1].consent_id == CONSENT_B
    assert replaced.state.active_consent(
        consent_id=CONSENT_B,
        provider_id="replacement-provider",
        authorization_generation=2,
    ).allowed_tiers == frozenset({PrivacyTier.PERSONAL})
    with pytest.raises(ConsentContractError, match="consent_unavailable"):
        replaced.state.active_consent(
            consent_id=CONSENT_A,
            provider_id="synthetic-provider",
            authorization_generation=2,
        )

    revoked = replaced.state.revoke(
        owner=True,
        consent_id=CONSENT_B,
        operation_id="revoke-1",
        decided_at="2026-09-19T12:02:00Z",
    )
    assert revoked.state.authorization_generation == 3
    duplicate = revoked.state.revoke(
        owner=True,
        consent_id=CONSENT_B,
        operation_id="revoke-duplicate",
        decided_at="2026-09-19T12:03:00Z",
    )
    assert duplicate.receipt.duplicate
    assert duplicate.state.authorization_generation == 3
    with pytest.raises(ConsentContractError, match="consent_unavailable"):
        duplicate.state.active_consent(
            consent_id=CONSENT_B,
            provider_id="replacement-provider",
            authorization_generation=3,
        )


def test_consent_administration_is_owner_only() -> None:
    state = ProviderConsentState()
    with pytest.raises(ConsentContractError, match="owner_required"):
        state.inspect(owner=False)
    with pytest.raises(ConsentContractError, match="owner_required"):
        state.grant(
            owner=False,
            provider_id="synthetic-provider",
            allowed_tiers=frozenset({PrivacyTier.PUBLIC}),
            operation_id="grant-1",
            decided_at=NOW,
        )


def test_active_lookup_denies_missing_stale_multiple_active_and_corrupt_state() -> None:
    state = grant().state
    for arguments in (
        {
            "consent_id": CONSENT_B,
            "provider_id": "synthetic-provider",
            "authorization_generation": 1,
        },
        {
            "consent_id": CONSENT_A,
            "provider_id": "synthetic-provider",
            "authorization_generation": 0,
        },
        {
            "consent_id": "malformed",
            "provider_id": "synthetic-provider",
            "authorization_generation": 1,
        },
    ):
        with pytest.raises(ConsentContractError, match="consent_unavailable"):
            state.active_consent(**arguments)  # type: ignore[arg-type]

    second = ProviderConsentRecord(
        consent_id=CONSENT_B,
        provider_id="synthetic-provider",
        egress_mode=EgressMode.EXTERNAL_PROVIDER,
        allowed_tiers=frozenset({PrivacyTier.PUBLIC}),
        active=True,
        granted_generation=1,
        granted_at=NOW,
    )
    multiply_active = replace(state, records=(*state.records, second))
    with pytest.raises(ConsentContractError, match="consent_unavailable"):
        multiply_active.active_consent(
            consent_id=CONSENT_A,
            provider_id="synthetic-provider",
            authorization_generation=1,
        )

    future_record = replace(state.records[0], consent_id=CONSENT_C, granted_generation=2)
    corrupt = replace(state, records=(future_record,))
    with pytest.raises(ConsentContractError, match="consent_unavailable"):
        corrupt.active_consent(
            consent_id=CONSENT_C,
            provider_id="synthetic-provider",
            authorization_generation=1,
        )


def test_consent_rejects_local_mode_and_nonexternal_tiers() -> None:
    for tiers in (
        frozenset({PrivacyTier.SECRET}),
        frozenset({PrivacyTier.UNKNOWN}),
        frozenset(),
    ):
        with pytest.raises(ConsentContractError, match="invalid_consent"):
            ProviderConsentState().grant(
                owner=True,
                provider_id="synthetic-provider",
                allowed_tiers=tiers,
                operation_id="grant-invalid",
                decided_at=NOW,
                consent_id_factory=lambda: CONSENT_A,
            )
    with pytest.raises(ConsentContractError, match="invalid_consent"):
        ProviderConsentState().grant(
            owner=True,
            provider_id="synthetic-provider",
            allowed_tiers=frozenset({PrivacyTier.PUBLIC}),
            operation_id="grant-local",
            decided_at=NOW,
            egress_mode=EgressMode.OWNER_LOCAL,
            consent_id_factory=lambda: CONSENT_A,
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"provider_id": "bad\ud800"},
        {"allowed_tiers": frozenset({"public"})},
        {"egress_mode": "external_provider"},
        {"decided_at": "bad\ud800"},
    ],
)
def test_malformed_grant_inputs_stay_inside_contract_error_boundary(
    changes: dict[str, object],
) -> None:
    arguments: dict[str, object] = {
        "owner": True,
        "provider_id": "synthetic-provider",
        "allowed_tiers": frozenset({PrivacyTier.PUBLIC}),
        "operation_id": "grant-invalid-boundary",
        "decided_at": NOW,
        "consent_id_factory": lambda: CONSENT_A,
    }
    arguments.update(changes)
    with pytest.raises(ConsentContractError, match="invalid_consent"):
        ProviderConsentState().grant(**arguments)  # type: ignore[arg-type]

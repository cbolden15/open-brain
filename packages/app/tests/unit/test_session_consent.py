from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from open_brain_engine.core.access_contracts import derive_brain_id
from open_brain_engine.core.models import PrivacyTier

from open_brain.services.session_consent import (
    DurableProviderConsentStore,
    SessionConsentError,
)

BRAIN_ID = derive_brain_id("tenant_00000000-0000-4000-8000-000000000001")
OTHER_BRAIN_ID = derive_brain_id("tenant_00000000-0000-4000-8000-000000000002")
CONSENT_A = "consent_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
CONSENT_B = "consent_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
NOW = "2026-09-23T12:00:00Z"


def store(
    tmp_path: Path, *, brain_id: str = BRAIN_ID, issuer_epoch: int = 7
) -> DurableProviderConsentStore:
    root = tmp_path / "authority"
    root.mkdir(mode=0o700)
    return DurableProviderConsentStore(
        root / "provider-consent.json",
        brain_id=brain_id,
        issuer_epoch=issuer_epoch,
    )


def test_owner_lifecycle_is_durable_atomic_and_idempotent(tmp_path: Path) -> None:
    consent = store(tmp_path)
    granted = consent.grant(
        provider_id="synthetic-provider",
        allowed_tiers=frozenset({PrivacyTier.PUBLIC, PrivacyTier.WORK}),
        operation_id="grant-1",
        decided_at=NOW,
        consent_id_factory=lambda: CONSENT_A,
    )
    assert granted.receipt.authorization_generation == 1
    assert stat.S_IMODE(consent.path.stat().st_mode) == 0o600

    reopened = DurableProviderConsentStore(
        consent.path,
        brain_id=BRAIN_ID,
        issuer_epoch=7,
    )
    assert reopened.inspect().authorization_generation == 1
    replay = reopened.grant(
        provider_id="synthetic-provider",
        allowed_tiers=frozenset({PrivacyTier.PUBLIC, PrivacyTier.WORK}),
        operation_id="grant-1",
        decided_at="2026-09-23T12:01:00Z",
        consent_id_factory=lambda: CONSENT_B,
    )
    assert replay.receipt == granted.receipt

    replaced = reopened.replace(
        consent_id=CONSENT_A,
        provider_id="synthetic-provider",
        allowed_tiers=frozenset({PrivacyTier.PUBLIC}),
        operation_id="replace-1",
        decided_at="2026-09-23T12:02:00Z",
        consent_id_factory=lambda: CONSENT_B,
    )
    assert replaced.receipt.authorization_generation == 2
    revoked = reopened.revoke(
        consent_id=CONSENT_B,
        operation_id="revoke-1",
        decided_at="2026-09-23T12:03:00Z",
    )
    assert revoked.receipt.authorization_generation == 3
    assert reopened.load() == revoked.state


def test_absent_malformed_foreign_and_stale_state_fail_closed(tmp_path: Path) -> None:
    consent = store(tmp_path)
    with pytest.raises(SessionConsentError, match="consent_unavailable"):
        consent.load()

    consent.path.write_text("{}", encoding="utf-8")
    consent.path.chmod(0o600)
    with pytest.raises(SessionConsentError, match="invalid_consent_state"):
        consent.load()

    consent.path.unlink()
    consent.grant(
        provider_id="synthetic-provider",
        allowed_tiers=frozenset({PrivacyTier.PUBLIC}),
        operation_id="grant-1",
        decided_at=NOW,
        consent_id_factory=lambda: CONSENT_A,
    )
    for changed in (
        DurableProviderConsentStore(
            consent.path,
            brain_id=OTHER_BRAIN_ID,
            issuer_epoch=7,
        ),
        DurableProviderConsentStore(
            consent.path,
            brain_id=BRAIN_ID,
            issuer_epoch=8,
        ),
    ):
        with pytest.raises(SessionConsentError, match="consent_state_mismatch"):
            changed.load()

    value = json.loads(consent.path.read_bytes())
    value["unexpected"] = True
    consent.path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(SessionConsentError, match="invalid_consent_state"):
        consent.load()


def test_state_requires_an_absolute_file_inside_owner_only_directory(tmp_path: Path) -> None:
    open_parent = tmp_path / "open"
    open_parent.mkdir(mode=0o755)
    open_parent.chmod(0o755)
    unsafe = DurableProviderConsentStore(
        open_parent / "provider-consent.json",
        brain_id=BRAIN_ID,
        issuer_epoch=7,
    )
    with pytest.raises(SessionConsentError, match="unsafe_consent_state"):
        unsafe.grant(
            provider_id="synthetic-provider",
            allowed_tiers=frozenset({PrivacyTier.PUBLIC}),
            operation_id="grant-1",
            decided_at=NOW,
            consent_id_factory=lambda: CONSENT_A,
        )

    with pytest.raises(SessionConsentError, match="unsafe_consent_state"):
        DurableProviderConsentStore(
            Path("relative.json"),
            brain_id=BRAIN_ID,
            issuer_epoch=7,
        )

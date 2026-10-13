from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import open_local_engine

from open_brain.profile import compile_single_user_local
from open_brain.services.launcher_policy import LauncherPolicyError
from open_brain.services.local_operations import current_brain_identity
from open_brain.services.session_authority import TrustedSessionAuthority
from open_brain.services.session_consent import DurableProviderConsentStore

CONSENT_ID = "consent_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


@pytest.fixture
def tasks(tmp_path: Path) -> Any:
    return open_local_engine(compile_single_user_local(tmp_path / "brain"))


def write_policy(
    path: Path,
    tasks: Any,
    *,
    read_tiers: tuple[str, ...],
    external: bool = False,
    generation: int = 0,
) -> None:
    brain_id, issuer_epoch = current_brain_identity(tasks)
    path.write_text(
        json.dumps(
            {
                "policy_version": "launcher-policy.v1",
                "principal_id": "synthetic-principal",
                "session_id": "synthetic-session",
                "capabilities": ["content-read", "search"],
                "space_ids": None,
                "allowed_read_tiers": list(read_tiers),
                "allowed_capture_tiers": [],
                "egress_mode": "external_provider" if external else "owner_local",
                "provider_id": "synthetic-provider" if external else None,
                "consent_id": CONSENT_ID if external else None,
                "authorization_generation": generation,
                "brain_id": brain_id,
                "issuer_epoch": issuer_epoch,
            }
        ),
        encoding="utf-8",
    )


def test_owner_local_policy_is_loaded_and_static_mapping_is_revalidated(
    tasks: Any, tmp_path: Path
) -> None:
    authority_root = tmp_path / "authority"
    authority_root.mkdir(mode=0o700)
    policy_path = authority_root / "general.json"
    write_policy(policy_path, tasks, read_tiers=("public", "work"))
    source = TrustedSessionAuthority(tasks, policy_path=policy_path)

    authority = source.load()
    assert authority.allowed_read_tiers == frozenset(
        {PrivacyTier.PUBLIC, PrivacyTier.WORK}
    )
    assert source.revalidate(authority) is authority

    write_policy(policy_path, tasks, read_tiers=("public", "work", "personal"))
    with pytest.raises(LauncherPolicyError, match="stale_policy"):
        source.revalidate(authority)


def test_external_policy_requires_current_durable_consent(
    tasks: Any, tmp_path: Path
) -> None:
    authority_root = tmp_path / "authority"
    authority_root.mkdir(mode=0o700)
    policy_path = authority_root / "external.json"
    consent_path = authority_root / "provider-consent.json"
    write_policy(
        policy_path,
        tasks,
        read_tiers=("public", "work"),
        external=True,
        generation=1,
    )
    source = TrustedSessionAuthority(
        tasks,
        policy_path=policy_path,
        consent_state_path=consent_path,
    )
    with pytest.raises(LauncherPolicyError, match="consent_unavailable"):
        source.load()

    brain_id, issuer_epoch = current_brain_identity(tasks)
    consent = DurableProviderConsentStore(
        consent_path,
        brain_id=brain_id,
        issuer_epoch=issuer_epoch,
    )
    consent.grant(
        provider_id="synthetic-provider",
        allowed_tiers=frozenset({PrivacyTier.PUBLIC, PrivacyTier.WORK}),
        operation_id="grant-1",
        decided_at="2026-09-23T12:00:00Z",
        consent_id_factory=lambda: CONSENT_ID,
    )
    authority = source.load()
    assert authority.egress_mode.value == "external_provider"

    consent.revoke(
        consent_id=CONSENT_ID,
        operation_id="revoke-1",
        decided_at="2026-09-23T12:01:00Z",
    )
    with pytest.raises(LauncherPolicyError, match="stale_policy"):
        source.revalidate(authority)


def test_external_policy_rejects_missing_consent_path(tasks: Any, tmp_path: Path) -> None:
    authority_root = tmp_path / "authority"
    authority_root.mkdir(mode=0o700)
    policy_path = authority_root / "external.json"
    write_policy(
        policy_path,
        tasks,
        read_tiers=("public",),
        external=True,
        generation=1,
    )
    with pytest.raises(LauncherPolicyError, match="consent_unavailable"):
        TrustedSessionAuthority(tasks, policy_path=policy_path).load()

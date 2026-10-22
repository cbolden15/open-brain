"""Create-only guide policy helper against the real launcher parser and consent store."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from open_brain_engine.core.models import PrivacyTier

from open_brain.services.launcher_policy import parse_launcher_policy
from open_brain.services.scoped_policy_authoring import (
    PolicyAuthoringError,
    PolicyInputs,
    apply_scoped_policy,
    current_work_consent,
    guide_principal,
    preview_scoped_policy,
)
from open_brain.services.session_consent import DurableProviderConsentStore

BRAIN = "brn_" + "a" * 26
SPACE = "space_00000000-0000-4000-8000-000000000001"
CONSENT = "consent_" + "b" * 32


@pytest.fixture
def state(tmp_path: Path) -> dict[str, Any]:
    (tmp_path / "consent").mkdir(mode=0o700)
    consent = tmp_path / "consent" / "state.json"
    store = DurableProviderConsentStore(consent, brain_id=BRAIN, issuer_epoch=1)
    store.grant(
        provider_id="openai", allowed_tiers=frozenset({PrivacyTier.WORK}),
        operation_id="grant.synthetic", decided_at="2026-10-22T12:00:00Z",
        consent_id_factory=lambda: CONSENT,
    )
    consent_id, generation = current_work_consent(
        consent, provider_id="openai", brain_id=BRAIN, issuer_epoch=1
    )
    inputs = PolicyInputs(
        workflow_id="guide:synthetic-1", destination_space_id=SPACE, provider_id="openai",
        consent_id=consent_id, authorization_generation=generation, brain_id=BRAIN,
        issuer_epoch=1,
    )
    return {"consent": consent, "store": store, "inputs": inputs,
            "policy": tmp_path / "policy" / "policy.json"}


def test_preview_is_the_unchanged_v1_format_with_generated_ids_and_writes_nothing(
    state: dict[str, Any],
) -> None:
    preview = preview_scoped_policy(state["policy"], state["inputs"], consent_path=state["consent"])
    assert preview.expected_absent is True
    assert preview.policy_sha256 == hashlib.sha256(preview.payload).hexdigest()
    document = json.loads(preview.payload)
    digest = hashlib.sha256(b"guide:synthetic-1").hexdigest()
    assert guide_principal("guide:synthetic-1") == (
        f"guide:{digest}:client", f"guide:{digest}:session"
    )
    assert document == {
        "policy_version": "launcher-policy.v1",
        "principal_id": f"guide:{digest}:client",
        "session_id": f"guide:{digest}:session",
        "capabilities": ["content-read", "search"],
        "space_ids": [SPACE],
        "allowed_read_tiers": ["work"],
        "allowed_capture_tiers": [],
        "egress_mode": "external_provider",
        "provider_id": "openai",
        "consent_id": CONSENT,
        "authorization_generation": 1,
        "brain_id": BRAIN,
        "issuer_epoch": 1,
    }
    parse_launcher_policy(preview.payload)
    assert not state["policy"].parent.exists()


def test_apply_is_create_only_private_and_never_overwrites(state: dict[str, Any]) -> None:
    preview = preview_scoped_policy(state["policy"], state["inputs"], consent_path=state["consent"])
    with pytest.raises(PolicyAuthoringError, match="revision_conflict"):
        apply_scoped_policy(state["policy"], state["inputs"], policy_sha256="0" * 64,
                            expected_absent=True, consent_path=state["consent"])
    assert not state["policy"].exists()
    assert apply_scoped_policy(
        state["policy"], state["inputs"], policy_sha256=preview.policy_sha256,
        expected_absent=True, consent_path=state["consent"],
    ) == preview.policy_sha256
    assert state["policy"].read_bytes() == preview.payload
    assert stat.S_IMODE(os.lstat(state["policy"]).st_mode) == 0o600
    assert stat.S_IMODE(os.lstat(state["policy"].parent).st_mode) == 0o700
    assert [item.name for item in state["policy"].parent.iterdir()] == ["policy.json"]
    with pytest.raises(PolicyAuthoringError, match="revision_conflict"):
        apply_scoped_policy(state["policy"], state["inputs"], policy_sha256=preview.policy_sha256,
                            expected_absent=True, consent_path=state["consent"])
    assert state["policy"].read_bytes() == preview.payload


def test_revoked_consent_refuses_before_any_policy_write(state: dict[str, Any]) -> None:
    preview = preview_scoped_policy(state["policy"], state["inputs"], consent_path=state["consent"])
    state["store"].revoke(consent_id=CONSENT, operation_id="revoke.synthetic",
                          decided_at="2026-10-22T13:00:00Z")
    with pytest.raises(PolicyAuthoringError, match="authority_refused"):
        apply_scoped_policy(state["policy"], state["inputs"], policy_sha256=preview.policy_sha256,
                            expected_absent=True, consent_path=state["consent"])
    assert not state["policy"].parent.exists()
    with pytest.raises(PolicyAuthoringError, match="authority_refused"):
        current_work_consent(state["consent"], provider_id="openai", brain_id=BRAIN,
                             issuer_epoch=1)


def test_existing_exact_scope_policy_is_reused_and_widened_policy_refuses(
    state: dict[str, Any],
) -> None:
    state["policy"].parent.mkdir(mode=0o700)
    existing = json.loads(
        preview_scoped_policy(
            state["policy"], state["inputs"], consent_path=state["consent"]
        ).payload
    )
    # Reuse requires the generated guide IDs, a private file and a private parent.
    for change, file_mode, parent_mode in (
        ({"principal_id": "owner-chosen-client"}, 0o600, 0o700),
        ({"session_id": "owner-chosen-session"}, 0o600, 0o700),
        ({}, 0o644, 0o700),
        ({}, 0o600, 0o750),
    ):
        state["policy"].write_text(json.dumps({**existing, **change}))
        state["policy"].chmod(file_mode)
        state["policy"].parent.chmod(parent_mode)
        with pytest.raises(PolicyAuthoringError):
            preview_scoped_policy(state["policy"], state["inputs"], consent_path=state["consent"])
    state["policy"].parent.chmod(0o700)
    state["policy"].write_text(json.dumps(existing))
    state["policy"].chmod(0o600)
    preview = preview_scoped_policy(state["policy"], state["inputs"], consent_path=state["consent"])
    assert preview.expected_absent is False
    assert preview.payload == state["policy"].read_bytes()
    before = os.lstat(state["policy"])
    assert apply_scoped_policy(
        state["policy"], state["inputs"], policy_sha256=preview.policy_sha256,
        expected_absent=False, consent_path=state["consent"],
    ) == preview.policy_sha256
    assert os.lstat(state["policy"]).st_ino == before.st_ino
    state["policy"].write_text(json.dumps({**existing, "allowed_capture_tiers": ["work"]}))
    with pytest.raises(PolicyAuthoringError, match="authority_refused"):
        preview_scoped_policy(state["policy"], state["inputs"], consent_path=state["consent"])
    with pytest.raises(PolicyAuthoringError, match="revision_conflict"):
        apply_scoped_policy(state["policy"], state["inputs"], policy_sha256=preview.policy_sha256,
                            expected_absent=False, consent_path=state["consent"])


def test_wider_or_missing_consent_is_never_used(tmp_path: Path) -> None:
    (tmp_path / "consent").mkdir(mode=0o700)
    consent = tmp_path / "consent" / "state.json"
    with pytest.raises(PolicyAuthoringError, match="authority_refused"):
        current_work_consent(consent, provider_id="openai", brain_id=BRAIN, issuer_epoch=1)
    DurableProviderConsentStore(consent, brain_id=BRAIN, issuer_epoch=1).grant(
        provider_id="openai", allowed_tiers=frozenset({PrivacyTier.WORK, PrivacyTier.PERSONAL}),
        operation_id="grant.wide", decided_at="2026-10-22T12:00:00Z",
    )
    with pytest.raises(PolicyAuthoringError, match="authority_refused"):
        current_work_consent(consent, provider_id="openai", brain_id=BRAIN, issuer_epoch=1)


def test_short_writes_still_create_the_complete_policy(
    state: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    preview = preview_scoped_policy(state["policy"], state["inputs"], consent_path=state["consent"])
    original = os.write
    monkeypatch.setattr("open_brain.services.scoped_policy_authoring.os.write",
                        lambda fd, data: original(fd, bytes(data)[:7]))
    apply_scoped_policy(state["policy"], state["inputs"], policy_sha256=preview.policy_sha256,
                        expected_absent=True, consent_path=state["consent"])
    monkeypatch.undo()
    assert state["policy"].read_bytes() == preview.payload


def test_grant_time_consent_store_failure_is_an_unknown_outcome() -> None:
    from open_brain.services.session_consent import SessionConsentError
    from open_brain.services.source_guide_release import _classify

    for code in ("unsafe_consent_state", "invalid_consent_state", "consent_unavailable"):
        failure = _classify(SessionConsentError(code), action="consent")
        assert failure.code == "operation_unknown"

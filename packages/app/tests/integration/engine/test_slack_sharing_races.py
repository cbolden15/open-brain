"""Synchronized source-state snapshots and final consent boundary."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from typing import Any

import pytest
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import paging

from open_brain.services.local_mcp import LocalMcpAdapter
from open_brain.services.mcp_protocol import McpCallError
from open_brain.services.session_authority import TrustedSessionAuthority
from open_brain.services.session_consent import DurableProviderConsentStore
from open_brain.services.t03_adapters import T03AppAdapter
from packages.app.tests.integration.engine.test_slack_sharing import _revoke, _single
from packages.app.tests.integration.services.test_slack_decision_recall import _message


@pytest.mark.parametrize("transition", ["revoke", "edit"])
@pytest.mark.parametrize("boundary", ["before", "after"])
def test_source_mutation_respects_returning_read_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transition: str, boundary: str
) -> None:
    journey = _single(tmp_path)
    approved = journey.approve(journey.sources()[0])[3]
    copy = approved.copy_capture_id
    assert copy is not None
    entered, committed = Event(), Event()
    original = paging.read_snapshot

    @contextmanager
    def barrier(engine: Any):  # type: ignore[no-untyped-def]
        if boundary == "before":
            entered.set()
            assert committed.wait(20), "mutation did not commit"
        with original(engine) as connection:
            yield connection
        if boundary == "after":
            entered.set()
            assert committed.wait(20), "mutation did not commit"

    with monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=1) as executor:
        patch.setattr(paging, "read_snapshot", barrier)
        future = executor.submit(journey.read, copy)
        assert entered.wait(20), "reader did not reach barrier"
        try:
            if transition == "revoke":
                _revoke(journey, approved)
            else:
                journey.scan(
                    [
                        _message(
                            "1760000001.000001",
                            "Changed onboarding rationale",
                            edited="1760000301.000001",
                        )
                    ]
                )
        finally:
            committed.set()
        if boundary == "before":
            with pytest.raises(McpCallError, match="not_found"):
                future.result(timeout=20)
        else:
            result = future.result(timeout=20)
            assert result["record"]["record_id"] == copy
            assert result["complete"] is True
    with pytest.raises(McpCallError, match="not_found"):
        journey.read(copy)


def test_consent_revocation_after_snapshot_prevents_mcp_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journey = _single(tmp_path)
    copy = journey.approve(journey.sources()[0])[3].copy_capture_id
    assert copy is not None
    authority_root = tmp_path / "authority"
    authority_root.mkdir(mode=0o700)
    policy, consent_path = authority_root / "policy.json", authority_root / "consent.json"
    consent_id = "consent_" + "a" * 32
    consent = DurableProviderConsentStore(
        consent_path, brain_id=journey.brain_id, issuer_epoch=journey.epoch
    )
    consent.grant(
        provider_id="synthetic-provider",
        allowed_tiers=frozenset({PrivacyTier.WORK}),
        operation_id="grant.race",
        decided_at="2026-10-14T12:00:00Z",
        consent_id_factory=lambda: consent_id,
    )
    policy.write_text(
        json.dumps(
            {
                "policy_version": "launcher-policy.v1",
                "principal_id": "synthetic-client",
                "session_id": "w1a-consent",
                "capabilities": ["search", "content-read"],
                "space_ids": [journey.space],
                "allowed_read_tiers": ["work"],
                "allowed_capture_tiers": [],
                "egress_mode": "external_provider",
                "provider_id": "synthetic-provider",
                "consent_id": consent_id,
                "authorization_generation": 1,
                "brain_id": journey.brain_id,
                "issuer_epoch": journey.epoch,
            }
        )
    )
    source = TrustedSessionAuthority(
        journey.tasks, policy_path=policy, consent_state_path=consent_path
    )
    authority = source.load()
    mcp = LocalMcpAdapter(
        authority=authority,
        negotiated=T03AppAdapter(journey.tasks, authority),
        revalidate_authority=lambda: source.revalidate(authority),
    )
    complete, revoked = Event(), Event()
    original = paging.read_snapshot

    @contextmanager
    def barrier(engine: Any):  # type: ignore[no-untyped-def]
        with original(engine) as connection:
            yield connection
        complete.set()
        assert revoked.wait(20), "consent did not revoke"

    with monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=1) as executor:
        patch.setattr(paging, "read_snapshot", barrier)
        future = executor.submit(
            mcp.call_tool,
            "brain_read",
            {"dto_version": 1, "record_id": copy, "expected_revision_id": copy},
        )
        assert complete.wait(20), "reader did not complete snapshot"
        try:
            consent.revoke(
                consent_id=consent_id,
                operation_id="revoke.race",
                decided_at="2026-10-14T12:01:00Z",
            )
        finally:
            revoked.set()
        from open_brain.services.launcher_policy import LauncherPolicyError

        with pytest.raises(LauncherPolicyError, match="stale_policy"):
            future.result(timeout=20)


def test_real_reader_lease_serializes_revocation(tmp_path: Path) -> None:
    from open_brain_engine.storage.locks import LockBusyError

    journey = _single(tmp_path)
    approved = journey.approve(journey.sources()[0])[3]
    assert approved.copy_capture_id is not None
    entered, attempted = Event(), Event()

    def reader() -> None:
        with paging.read_snapshot(journey.tasks.sharing._engine):
            entered.set()
            assert attempted.wait(20), "writer did not attempt revocation"

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(reader)
        assert entered.wait(20), "reader did not acquire lease"
        try:
            with pytest.raises(LockBusyError):
                _revoke(journey, approved)
        finally:
            attempted.set()
        future.result(timeout=20)
    assert journey.read(approved.copy_capture_id)["complete"]
    _revoke(journey, approved)
    with pytest.raises(McpCallError, match="not_found"):
        journey.read(approved.copy_capture_id)

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import (
    AdmissionLimits,
    BrainEngine,
    EngineTaskSet,
    ProtectionAcknowledgement,
    ReceiptProtectionPort,
    ReceiptProtectionRequest,
)

from open_brain.profile import compile_single_user_local
from open_brain_connectors.outbox.contracts import (
    DeliveryEnvelope,
    TerminalReceipt,
    TerminalReceiptStatus,
)
from open_brain_connectors.outbox.drain import DeliveryFailure, DrainResult, run_drain_cycle
from open_brain_connectors.outbox.store import EnqueueResult, OutboxItemState, OutboxStore
from open_brain_connectors.outbox.transport import (
    OutboxTransportError,
    SyntheticStartupPolicy,
    SyntheticTransport,
)

PRINCIPAL_ID = "synthetic-transport-principal"
ALLOWED_TIERS = frozenset({PrivacyTier.PUBLIC, PrivacyTier.WORK, PrivacyTier.PERSONAL})


def _brain(
    tmp_path: Path,
    *,
    limits: AdmissionLimits | None = None,
    protector: ReceiptProtectionPort | None = None,
) -> tuple[EngineTaskSet, str, int, str]:
    """One disposable real Brain; returns its tasks, identity, epoch, and tenant."""
    profile = compile_single_user_local(tmp_path / "brain")
    tasks = BrainEngine.open(
        profile,
        admission_limits=limits,
        receipt_protection_port=protector,
    ).tasks
    connection = sqlite3.connect(profile.root / ".open-brain/state/phase1.sqlite3")
    try:
        row = connection.execute("SELECT brain_id, issuer_epoch FROM brain_identity").fetchone()
    finally:
        connection.close()
    assert row is not None
    return tasks, cast(str, row[0]), cast(int, row[1]), profile.tenant_id


def _policy(
    brain_id: str,
    issuer_epoch: int,
    tenant_id: str,
    *,
    tiers: frozenset[PrivacyTier] = ALLOWED_TIERS,
) -> SyntheticStartupPolicy:
    return SyntheticStartupPolicy(
        destination_brain_id=brain_id,
        issuer_epoch=issuer_epoch,
        tenant_id=tenant_id,
        principal_id=PRINCIPAL_ID,
        allowed_capture_tiers=tiers,
    )


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Protector:
    def protect(
        self,
        request: ReceiptProtectionRequest,
        *,
        timeout_seconds: float,
    ) -> ProtectionAcknowledgement:
        assert timeout_seconds == 5.0
        return ProtectionAcknowledgement.for_request(
            request,
            protection_reference=f"protected:{request.commitment_sha256}",
            protected_at="2026-09-24T12:00:00Z",
        )


def _envelope(
    brain_id: str,
    issuer_epoch: int,
    tenant_id: str,
    *,
    delivery_id: str = "delivery.transport-001",
    payload_text: str = "Synthetic transport body",
    requested_tier: PrivacyTier = PrivacyTier.WORK,
) -> DeliveryEnvelope:
    return DeliveryEnvelope.create(
        destination_brain_id=brain_id,
        expected_issuer_epoch=issuer_epoch,
        tenant_id=tenant_id,
        principal_id=PRINCIPAL_ID,
        delivery_id=delivery_id,
        requested_tier=requested_tier,
        policy_ref="policy.synthetic-v1",
        payload={"family": "text", "text": payload_text},
        enqueued_at=_stamp(),
        retry_age_limit_seconds=86400,
        retry_attempt_limit=8,
    )


def test_accepted_engine_receipt_maps_to_a_bound_terminal_receipt(tmp_path: Path) -> None:
    tasks, brain_id, epoch, tenant_id = _brain(tmp_path)
    transport = SyntheticTransport(tasks, _policy(brain_id, epoch, tenant_id))
    envelope = _envelope(brain_id, epoch, tenant_id)

    outcome = transport(envelope)

    assert isinstance(outcome, TerminalReceipt)
    assert outcome.status is TerminalReceiptStatus.ACCEPTED
    assert outcome.brain_id == brain_id
    assert outcome.issuer_epoch == epoch
    assert outcome.delivery_id == envelope.delivery_id
    assert outcome.request_digest == envelope.request_digest
    assert outcome.final_admitted_tier is PrivacyTier.WORK
    assert outcome.protection_acknowledgement is None


def test_replayed_identical_delivery_maps_to_a_duplicate_receipt(tmp_path: Path) -> None:
    tasks, brain_id, epoch, tenant_id = _brain(tmp_path)
    transport = SyntheticTransport(tasks, _policy(brain_id, epoch, tenant_id))
    envelope = _envelope(brain_id, epoch, tenant_id)
    assert isinstance(transport(envelope), TerminalReceipt)

    outcome = transport(envelope)

    assert isinstance(outcome, TerminalReceipt)
    assert outcome.status is TerminalReceiptStatus.DUPLICATE
    assert outcome.request_digest == envelope.request_digest
    assert outcome.brain_id == brain_id


def test_tier_outside_the_policy_is_a_terminal_tier_not_permitted_failure(
    tmp_path: Path,
) -> None:
    tasks, brain_id, epoch, tenant_id = _brain(tmp_path)
    narrow = frozenset({PrivacyTier.PUBLIC, PrivacyTier.WORK})
    transport = SyntheticTransport(tasks, _policy(brain_id, epoch, tenant_id, tiers=narrow))
    envelope = _envelope(brain_id, epoch, tenant_id, requested_tier=PrivacyTier.PERSONAL)

    outcome = transport(envelope)

    assert isinstance(outcome, DeliveryFailure)
    assert outcome.code == "tier_not_permitted"
    assert outcome.retryable is False


def test_rate_limited_under_small_limits_is_a_retryable_failure(tmp_path: Path) -> None:
    limits = AdmissionLimits(requests_per_minute_per_principal=1)
    tasks, brain_id, epoch, tenant_id = _brain(tmp_path, limits=limits)
    transport = SyntheticTransport(tasks, _policy(brain_id, epoch, tenant_id))
    first = _envelope(brain_id, epoch, tenant_id, delivery_id="delivery.transport-first")
    second = _envelope(brain_id, epoch, tenant_id, delivery_id="delivery.transport-second")
    assert isinstance(transport(first), TerminalReceipt)

    outcome = transport(second)

    assert isinstance(outcome, DeliveryFailure)
    assert outcome.code == "rate_limited"
    assert outcome.retryable is True


def test_same_delivery_id_with_different_bytes_is_a_terminal_delivery_conflict(
    tmp_path: Path,
) -> None:
    tasks, brain_id, epoch, tenant_id = _brain(tmp_path)
    transport = SyntheticTransport(tasks, _policy(brain_id, epoch, tenant_id))
    first = _envelope(
        brain_id, epoch, tenant_id, delivery_id="delivery.transport-clash", payload_text="First"
    )
    second = _envelope(
        brain_id, epoch, tenant_id, delivery_id="delivery.transport-clash", payload_text="Second"
    )
    assert isinstance(transport(first), TerminalReceipt)

    outcome = transport(second)

    assert isinstance(outcome, DeliveryFailure)
    assert outcome.code == "delivery_conflict"
    assert outcome.retryable is False


def test_policy_tenant_mismatch_is_refused_at_construction(tmp_path: Path) -> None:
    tasks, brain_id, epoch, tenant_id = _brain(tmp_path)
    policy = SyntheticStartupPolicy(
        destination_brain_id=brain_id,
        issuer_epoch=epoch,
        tenant_id="tenant_00000000-0000-4000-8000-000000000000",
        principal_id=PRINCIPAL_ID,
        allowed_capture_tiers=ALLOWED_TIERS,
    )

    with pytest.raises(OutboxTransportError):
        SyntheticTransport(tasks, policy)


def test_one_drain_cycle_end_to_end_with_the_synthetic_transport(tmp_path: Path) -> None:
    limits = AdmissionLimits(requests_per_minute_per_principal=3)
    tasks, brain_id, epoch, tenant_id = _brain(tmp_path, limits=limits)
    transport = SyntheticTransport(tasks, _policy(brain_id, epoch, tenant_id))
    duplicate_source = _envelope(
        brain_id,
        epoch,
        tenant_id,
        delivery_id="delivery.transport-dup",
        payload_text="Duplicate destination body",
    )
    assert isinstance(transport(duplicate_source), TerminalReceipt)
    store = OutboxStore(tmp_path / "outbox", max_items=64, max_bytes=1024 * 1024)
    accepted = _envelope(
        brain_id,
        epoch,
        tenant_id,
        delivery_id="delivery.transport-ok",
        payload_text="Accepted destination body",
    )
    accepted_after_replay = _envelope(
        brain_id,
        epoch,
        tenant_id,
        delivery_id="delivery.transport-rate",
        payload_text="Rate limited destination body",
    )
    for envelope in (accepted, duplicate_source, accepted_after_replay):
        assert store.enqueue(envelope) is EnqueueResult.QUEUED

    summary = run_drain_cycle(store, transport, max_batch_items=16, max_batch_bytes=1024 * 1024)

    assert summary.result is DrainResult.COMPLETED
    assert summary.accepted == 2
    assert summary.duplicate == 1
    assert summary.retried == 0
    assert summary.quarantined_refused == 0
    accepted_record = json.loads(
        (store.directory / "delivery.transport-ok.json").read_bytes().decode("utf-8")
    )
    assert accepted_record["record_version"] == "outbox.terminal.v1"
    assert accepted_record["terminal_kind"] == "receipt"
    assert accepted_record["receipt"]["status"] == "accepted"
    assert (
        b"Accepted destination body"
        not in (store.directory / "delivery.transport-ok.json").read_bytes()
    )
    duplicate_record = json.loads(
        (store.directory / "delivery.transport-dup.json").read_bytes().decode("utf-8")
    )
    assert duplicate_record["receipt"]["status"] == "duplicate"
    assert (
        b"Duplicate destination body"
        not in (store.directory / "delivery.transport-dup.json").read_bytes()
    )
    accepted_after_replay_raw = (
        store.directory / "delivery.transport-rate.json"
    ).read_bytes()
    assert b"Rate limited destination body" not in accepted_after_replay_raw
    accepted_after_replay_value = json.loads(accepted_after_replay_raw.decode("utf-8"))
    assert accepted_after_replay_value["receipt"]["status"] == "accepted"
    assert accepted_after_replay_value["attempts"] == 0
    assert [item.state for item in store.scan()] == [
        OutboxItemState.TERMINAL,
        OutboxItemState.TERMINAL,
        OutboxItemState.TERMINAL,
    ]


def test_required_protection_retries_without_removing_an_unprotected_body(
    tmp_path: Path,
) -> None:
    tasks, brain_id, epoch, tenant_id = _brain(tmp_path)
    transport = SyntheticTransport(tasks, _policy(brain_id, epoch, tenant_id))
    store = OutboxStore(tmp_path / "outbox", max_items=8, max_bytes=1024 * 1024)
    envelope = _envelope(brain_id, epoch, tenant_id)
    assert store.enqueue(envelope) is EnqueueResult.QUEUED

    summary = run_drain_cycle(
        store,
        transport,
        max_batch_items=4,
        max_batch_bytes=1024 * 1024,
        require_independent_protection=True,
    )

    assert summary.retried == 1
    assert store.scan()[0].state is OutboxItemState.QUEUED
    assert b"Synthetic transport body" in (
        store.directory / f"{envelope.delivery_id}.json"
    ).read_bytes()


def test_required_protection_still_quarantines_a_wrong_brain_null_receipt(
    tmp_path: Path,
) -> None:
    tasks, brain_id, epoch, tenant_id = _brain(tmp_path)
    transport = SyntheticTransport(tasks, _policy(brain_id, epoch, tenant_id))
    store = OutboxStore(tmp_path / "outbox", max_items=8, max_bytes=1024 * 1024)
    envelope = _envelope(brain_id, epoch, tenant_id)
    assert store.enqueue(envelope) is EnqueueResult.QUEUED
    receipt = transport(envelope)
    assert isinstance(receipt, TerminalReceipt)
    object.__setattr__(receipt, "brain_id", "brn_" + "q" * 26)

    summary = run_drain_cycle(
        store,
        lambda _envelope: receipt,
        max_batch_items=4,
        max_batch_bytes=1024 * 1024,
        require_independent_protection=True,
    )

    assert summary.quarantined_receipt_mismatch == 1
    assert store.scan()[0].state is OutboxItemState.QUARANTINED
    assert b"Synthetic transport body" in (
        store.directory / f"{envelope.delivery_id}.json"
    ).read_bytes()


def test_required_protection_releases_only_a_verified_protected_body(tmp_path: Path) -> None:
    tasks, brain_id, epoch, tenant_id = _brain(tmp_path, protector=_Protector())
    transport = SyntheticTransport(tasks, _policy(brain_id, epoch, tenant_id))
    store = OutboxStore(tmp_path / "outbox", max_items=8, max_bytes=1024 * 1024)
    envelope = _envelope(brain_id, epoch, tenant_id)
    assert store.enqueue(envelope) is EnqueueResult.QUEUED

    summary = run_drain_cycle(
        store,
        transport,
        max_batch_items=4,
        max_batch_bytes=1024 * 1024,
        require_independent_protection=True,
    )

    assert summary.accepted == 1
    assert store.scan()[0].state is OutboxItemState.TERMINAL
    raw = (store.directory / f"{envelope.delivery_id}.json").read_bytes()
    assert b"Synthetic transport body" not in raw
    assert json.loads(raw)["receipt"]["protection_acknowledgement"]["status"] == "protected"

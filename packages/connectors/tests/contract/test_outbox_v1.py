from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from open_brain_engine.core.access_contracts import derive_brain_id
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import BrainEngine, CaptureSubmission, TextPayload
from open_brain_engine.engine.contracts import (
    LocalEngineContext,
    destination_bound_request_sha256,
)
from open_brain_engine.engine.t03_contracts import EffectiveAuthority
from open_brain_engine.providers.base import ProviderMode

import open_brain_connectors.outbox as outbox
from open_brain_connectors.outbox import (
    DeliveryEnvelope,
    OutboxContractError,
    TerminalReceipt,
    TerminalReceiptStatus,
    outbox_request_digest,
    verify_terminal_receipt,
)

TENANT_ID = "tenant_123e4567-e89b-42d3-a456-426614174000"
OTHER_TENANT_ID = "tenant_00000000-0000-4000-8000-000000000000"
BRAIN_ID = derive_brain_id(TENANT_ID)
OTHER_BRAIN_ID = derive_brain_id(OTHER_TENANT_ID)
PRINCIPAL_ID = "synthetic-destination-principal"
PAYLOAD_TEXT = "Synthetic destination-bound outbox capture"
ENQUEUED_AT = "2026-09-21T12:00:00Z"


def _payload() -> TextPayload:
    return TextPayload(PAYLOAD_TEXT)


def _digest(
    *,
    requested_tier: PrivacyTier | str | None = PrivacyTier.WORK,
    payload: TextPayload | None = None,
    title: str | None = None,
    tenant_id: str = TENANT_ID,
    principal_id: str = PRINCIPAL_ID,
    destination_brain_id: str = BRAIN_ID,
    issuer_epoch: int = 7,
) -> str:
    return outbox_request_digest(
        destination_brain_id=destination_brain_id,
        issuer_epoch=issuer_epoch,
        tenant_id=tenant_id,
        principal_id=principal_id,
        payload=_payload() if payload is None else payload,
        requested_tier=requested_tier,
        title=title,
    )


def _envelope(
    *,
    requested_tier: PrivacyTier = PrivacyTier.WORK,
    payload: object | None = None,
    lineage_delivery_id: str | None = None,
) -> DeliveryEnvelope:
    return DeliveryEnvelope.create(
        destination_brain_id=BRAIN_ID,
        expected_issuer_epoch=7,
        tenant_id=TENANT_ID,
        principal_id=PRINCIPAL_ID,
        delivery_id="delivery.synthetic-001",
        requested_tier=requested_tier,
        policy_ref="policy.synthetic-v1",
        payload=_payload().to_dict() if payload is None else payload,
        enqueued_at=ENQUEUED_AT,
        retry_age_limit_seconds=86400,
        retry_attempt_limit=8,
        lineage_delivery_id=lineage_delivery_id,
    )


def _receipt(
    envelope: DeliveryEnvelope,
    *,
    status: TerminalReceiptStatus = TerminalReceiptStatus.ACCEPTED,
    brain_id: str | None = None,
    issuer_epoch: int | None = None,
    delivery_id: str | None = None,
    request_digest: str | None = None,
    final_admitted_tier: PrivacyTier = PrivacyTier.WORK,
) -> TerminalReceipt:
    return TerminalReceipt(
        status=status,
        brain_id=envelope.destination_brain_id if brain_id is None else brain_id,
        issuer_epoch=envelope.expected_issuer_epoch if issuer_epoch is None else issuer_epoch,
        delivery_id=envelope.delivery_id if delivery_id is None else delivery_id,
        request_digest=envelope.request_digest if request_digest is None else request_digest,
        final_admitted_tier=final_admitted_tier,
    )


def test_outbox_request_digest_delegates_to_the_engine_helper() -> None:
    shared = {
        "destination_brain_id": BRAIN_ID,
        "issuer_epoch": 7,
        "tenant_id": TENANT_ID,
        "principal_id": PRINCIPAL_ID,
        "payload": _payload(),
    }
    assert outbox_request_digest(
        destination_brain_id=BRAIN_ID,
        issuer_epoch=7,
        tenant_id=TENANT_ID,
        principal_id=PRINCIPAL_ID,
        payload=_payload(),
        requested_tier=PrivacyTier.WORK,
    ) == destination_bound_request_sha256(
        destination_brain_id=BRAIN_ID,
        issuer_epoch=7,
        tenant_id=TENANT_ID,
        principal_id=PRINCIPAL_ID,
        payload=_payload(),
        requested_tier=PrivacyTier.WORK,
    )
    assert shared["destination_brain_id"] == BRAIN_ID


def test_envelope_digest_is_the_engine_destination_bound_digest() -> None:
    envelope = _envelope()
    assert envelope.request_digest == destination_bound_request_sha256(
        destination_brain_id=BRAIN_ID,
        issuer_epoch=7,
        tenant_id=TENANT_ID,
        principal_id=PRINCIPAL_ID,
        payload=_payload(),
        requested_tier=PrivacyTier.WORK,
    )
    assert envelope.request_digest != _digest(requested_tier=PrivacyTier.PERSONAL)
    assert envelope.request_digest != _digest(principal_id="synthetic-other-principal")
    assert envelope.request_digest != _digest(tenant_id=OTHER_TENANT_ID)
    assert envelope.request_digest != _digest(payload=TextPayload("Different synthetic text"))


def test_envelope_rejects_a_tampered_payload_or_digest() -> None:
    value = _envelope().to_dict()
    cast(dict[str, object], value["payload"])["text"] = "tampered synthetic text"
    with pytest.raises(OutboxContractError, match="invalid request digest"):
        DeliveryEnvelope.from_dict(value)

    value = _envelope().to_dict()
    value["request_digest"] = "b" * 64
    with pytest.raises(OutboxContractError, match="invalid request digest"):
        DeliveryEnvelope.from_dict(value)

    value = _envelope().to_dict()
    value["tenant_id"] = OTHER_TENANT_ID
    with pytest.raises(OutboxContractError, match="invalid request digest"):
        DeliveryEnvelope.from_dict(value)


def test_envelope_detaches_and_freezes_input_payload() -> None:
    payload = {"family": "text", "text": "original"}
    envelope = _envelope(payload=payload)
    payload["text"] = "changed"

    serialized = envelope.to_dict()
    assert serialized["payload"] == {"family": "text", "text": "original"}
    frozen = cast(Mapping[str, object], envelope.payload)
    with pytest.raises(TypeError):
        frozen["text"] = "changed"  # type: ignore[index]
    with pytest.raises((FrozenInstanceError, AttributeError)):
        envelope.delivery_id = "changed"  # type: ignore[misc]


def test_serialization_returns_detached_payload_and_round_trips() -> None:
    envelope = _envelope()
    serialized = envelope.to_dict()
    assert DeliveryEnvelope.from_dict(serialized) == envelope

    payload = cast(dict[str, object], serialized["payload"])
    payload["present"] = False

    assert envelope.to_dict()["payload"] != serialized["payload"]


def test_delivery_envelope_parsing_requires_exact_keys() -> None:
    value = _envelope().to_dict()
    for key in tuple(value):
        missing = dict(value)
        del missing[key]
        with pytest.raises(OutboxContractError, match="fields"):
            DeliveryEnvelope.from_dict(missing)
    with pytest.raises(OutboxContractError, match="fields"):
        DeliveryEnvelope.from_dict({**value, "queue_position": 0})


@pytest.mark.parametrize(
    "change",
    [
        {"contract_version": "outbox.v2"},
        {"destination_brain_id": "brn_invalid"},
        {"expected_issuer_epoch": 0},
        {"delivery_id": ""},
        {"requested_tier": "private"},
        {"policy_ref": ""},
        {"tenant_id": ""},
        {"principal_id": ""},
        {"request_digest": "A" * 64},
        {"enqueued_at": "2026-09-21 12:00:00"},
        {"enqueued_at": "synthetic-time"},
        {"retry_age_limit_seconds": 0},
        {"retry_attempt_limit": 0},
        {"retry_age_limit_seconds": True},
        {"attempts": -1},
        {"attempts": "2"},
        {"last_attempt_at": "not-a-timestamp"},
        {"last_attempt_result": ""},
        {"quarantine_reason": ""},
        {"lineage_delivery_id": ""},
        {"terminal_receipt": "accepted"},
        {"terminal_receipt": {"status": "accepted"}},
    ],
)
def test_delivery_envelope_rejects_malformed_fields(change: dict[str, object]) -> None:
    value = _envelope().to_dict()
    value.update(change)
    with pytest.raises(OutboxContractError):
        DeliveryEnvelope.from_dict(value)


def test_envelope_rejects_attempt_metadata_without_attempts() -> None:
    value = _envelope().to_dict()
    value["last_attempt_at"] = "2026-09-21T12:00:01Z"
    with pytest.raises(OutboxContractError, match="attempt metadata"):
        DeliveryEnvelope.from_dict(value)
    value = _envelope().to_dict()
    value["last_attempt_result"] = "rate_limited"
    with pytest.raises(OutboxContractError, match="attempt metadata"):
        DeliveryEnvelope.from_dict(value)


def test_envelope_carries_and_round_trips_lifecycle_metadata() -> None:
    terminal = _receipt(_envelope()).to_dict()
    envelope = DeliveryEnvelope(
        contract_version=outbox.OUTBOX_CONTRACT_VERSION,
        destination_brain_id=BRAIN_ID,
        expected_issuer_epoch=7,
        tenant_id=TENANT_ID,
        principal_id=PRINCIPAL_ID,
        delivery_id="delivery.synthetic-001",
        request_digest=_digest(),
        requested_tier=PrivacyTier.WORK,
        policy_ref="policy.synthetic-v1",
        payload=_payload().to_dict(),
        enqueued_at=ENQUEUED_AT,
        retry_age_limit_seconds=3600,
        retry_attempt_limit=5,
        attempts=2,
        last_attempt_at="2026-09-21T12:01:02.250000Z",
        last_attempt_result="rate_limited",
        quarantine_reason="attempt_exhausted",
        terminal_receipt=terminal,
        lineage_delivery_id="delivery.synthetic-000",
    )
    terminal["status"] = "duplicate"

    stored = cast(Mapping[str, object], envelope.terminal_receipt)
    assert stored["status"] == "accepted"
    serialized = envelope.to_dict()
    assert (
        cast(Mapping[str, object], serialized["terminal_receipt"])["protection_acknowledgement"]
        is None
    )
    assert DeliveryEnvelope.from_dict(serialized) == envelope


@pytest.mark.parametrize("surrogate", ["\ud800", "\udfff"])
@pytest.mark.parametrize("reference_field", ["delivery_id", "policy_ref"])
def test_outbox_request_rejects_surrogate_references_at_the_envelope_boundary(
    surrogate: str, reference_field: str
) -> None:
    values: dict[str, object] = {
        "destination_brain_id": BRAIN_ID,
        "expected_issuer_epoch": 7,
        "tenant_id": TENANT_ID,
        "principal_id": PRINCIPAL_ID,
        "delivery_id": "delivery.synthetic-001",
        "requested_tier": PrivacyTier.WORK,
        "policy_ref": "policy.synthetic-v1",
        "payload": _payload().to_dict(),
        "enqueued_at": ENQUEUED_AT,
        "retry_age_limit_seconds": 86400,
        "retry_attempt_limit": 8,
    }
    values[reference_field] = f"synthetic{surrogate}reference"

    with pytest.raises(OutboxContractError):
        DeliveryEnvelope.create(**values)  # type: ignore[arg-type]


def test_outbox_request_digest_wraps_engine_value_errors() -> None:
    with pytest.raises(OutboxContractError):
        _digest(payload=cast(TextPayload, object()))
    with pytest.raises(OutboxContractError):
        _digest(tenant_id="tenant_synthetic\ud800invalid")


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({1: "not-json"}, "JSON payload"),
        ({"bytes": b"no"}, "JSON payload"),
        ({"float": 1.5}, "JSON payload"),
        ({"family": "event"}, "envelope payload family"),
    ],
)
def test_delivery_envelope_rejects_noncanonical_or_non_text_payloads(
    payload: object, message: str
) -> None:
    with pytest.raises(OutboxContractError, match=message):
        _envelope(payload=payload)


@pytest.mark.parametrize(
    "status", [TerminalReceiptStatus.ACCEPTED, TerminalReceiptStatus.DUPLICATE]
)
def test_accepted_and_duplicate_receipts_verify(status: TerminalReceiptStatus) -> None:
    envelope = _envelope()
    receipt = _receipt(envelope, status=status, final_admitted_tier=PrivacyTier.PERSONAL)

    assert TerminalReceipt.from_dict(receipt.to_dict()) == receipt
    assert verify_terminal_receipt(envelope, receipt) == receipt


def test_terminal_receipt_parsing_requires_exact_keys() -> None:
    value = _receipt(_envelope()).to_dict()
    for key in tuple(value):
        missing = dict(value)
        del missing[key]
        with pytest.raises(OutboxContractError, match="fields"):
            TerminalReceipt.from_dict(missing)
    with pytest.raises(OutboxContractError, match="fields"):
        TerminalReceipt.from_dict({**value, "retry_after": 1})


def test_terminal_receipt_protection_acknowledgement_is_null_only() -> None:
    envelope = _envelope()
    receipt = _receipt(envelope)
    assert receipt.protection_acknowledgement is None
    assert receipt.to_dict()["protection_acknowledgement"] is None

    value = receipt.to_dict()
    value["protection_acknowledgement"] = {"finalizer": "synthetic"}
    with pytest.raises(OutboxContractError, match="protection acknowledgement"):
        TerminalReceipt.from_dict(value)
    with pytest.raises(OutboxContractError, match="protection acknowledgement"):
        TerminalReceipt(
            status=TerminalReceiptStatus.ACCEPTED,
            brain_id=envelope.destination_brain_id,
            issuer_epoch=envelope.expected_issuer_epoch,
            delivery_id=envelope.delivery_id,
            request_digest=envelope.request_digest,
            final_admitted_tier=PrivacyTier.WORK,
            protection_acknowledgement="synthetic",
        )
    assert verify_terminal_receipt(envelope, receipt) == receipt


@pytest.mark.parametrize("status", ["pending", "failed", "conflicted", "quarantined"])
def test_terminal_receipt_rejects_nonterminal_statuses(status: str) -> None:
    value = _receipt(_envelope()).to_dict()
    value["status"] = status
    with pytest.raises(OutboxContractError, match="status"):
        TerminalReceipt.from_dict(value)


@pytest.mark.parametrize("surrogate", ["\ud800", "\udfff"])
def test_terminal_receipt_rejects_surrogate_delivery_ids_on_construction_and_parse(
    surrogate: str,
) -> None:
    envelope = _envelope()
    delivery_id = f"synthetic{surrogate}delivery"
    with pytest.raises(OutboxContractError):
        TerminalReceipt(
            status=TerminalReceiptStatus.ACCEPTED,
            brain_id=envelope.destination_brain_id,
            issuer_epoch=envelope.expected_issuer_epoch,
            delivery_id=delivery_id,
            request_digest=envelope.request_digest,
            final_admitted_tier=PrivacyTier.WORK,
        )

    value = _receipt(envelope).to_dict()
    value["delivery_id"] = delivery_id
    with pytest.raises(OutboxContractError):
        TerminalReceipt.from_dict(value)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"brain_id": OTHER_BRAIN_ID}, "binding mismatch"),
        ({"issuer_epoch": 8}, "binding mismatch"),
        ({"delivery_id": "delivery.synthetic-002"}, "binding mismatch"),
        ({"request_digest": "b" * 64}, "binding mismatch"),
        ({"final_admitted_tier": PrivacyTier.PUBLIC}, "binding mismatch"),
    ],
)
def test_terminal_receipt_rejects_every_binding_mismatch(
    change: dict[str, object], message: str
) -> None:
    envelope = _envelope()
    values: dict[str, object] = {
        "status": TerminalReceiptStatus.ACCEPTED,
        "brain_id": envelope.destination_brain_id,
        "issuer_epoch": envelope.expected_issuer_epoch,
        "delivery_id": envelope.delivery_id,
        "request_digest": envelope.request_digest,
        "final_admitted_tier": PrivacyTier.WORK,
    }
    values.update(change)
    receipt = TerminalReceipt(**values)  # type: ignore[arg-type]

    with pytest.raises(OutboxContractError, match=message):
        verify_terminal_receipt(envelope, receipt)


@pytest.mark.parametrize("tier", ["private", "", 1, None])
def test_terminal_receipt_rejects_invalid_final_tiers(tier: object) -> None:
    value = _receipt(_envelope()).to_dict()
    value["final_admitted_tier"] = tier
    with pytest.raises(OutboxContractError, match="final admitted tier"):
        TerminalReceipt.from_dict(value)


def _engine_profile(root: Path) -> LocalEngineContext:
    tenant_id = f"tenant_{uuid4()}"
    owner_actor_id = f"actor_{uuid4()}"
    return LocalEngineContext(
        root=root,
        root_identity=(os.stat(root).st_dev, os.stat(root).st_ino),
        tenant_id=tenant_id,
        owner_actor_id=owner_actor_id,
        owner_role_claim={
            "actor_id": owner_actor_id,
            "capabilities": ["owner.capture"],
            "role_claim_id": f"role_claim_{uuid4()}",
            "role_id": f"role_{uuid4()}",
            "tenant_id": tenant_id,
        },
        provider_mode=ProviderMode.NONE,
        starter_spaces=(),
    )


def test_real_engine_receipt_verifies_against_the_envelope_that_produced_it(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    root.mkdir(mode=0o700)
    # The writer lease opens the engine state root without creating it; a
    # deployment profile compiler normally provides this layout.
    (root / ".open-brain").mkdir(mode=0o700)
    (root / ".open-brain" / "state").mkdir(mode=0o700)
    profile = _engine_profile(root)
    engine = BrainEngine.open(profile)
    brain_id = derive_brain_id(profile.tenant_id)
    authority = EffectiveAuthority(
        principal_id=PRINCIPAL_ID,
        session_id="synthetic-destination-session",
        capabilities=frozenset(),
        space_ids=None,
        allowed_read_tiers=frozenset({PrivacyTier.PUBLIC, PrivacyTier.WORK, PrivacyTier.PERSONAL}),
        allowed_capture_tiers=frozenset(set(PrivacyTier)),
        brain_id=brain_id,
        issuer_epoch=1,
    )
    payload = TextPayload("Synthetic round-trip destination-bound capture")
    submission = CaptureSubmission.for_destination_bound(
        profile=engine.profile,
        authority=authority,
        payload=payload,
        delivery_id="delivery.outbox.roundtrip-1",
        requested_tier=PrivacyTier.WORK,
    )
    envelope = DeliveryEnvelope.create(
        destination_brain_id=brain_id,
        expected_issuer_epoch=1,
        tenant_id=profile.tenant_id,
        principal_id=PRINCIPAL_ID,
        delivery_id="delivery.outbox.roundtrip-1",
        requested_tier=PrivacyTier.WORK,
        policy_ref="policy.synthetic-v1",
        payload=payload.to_dict(),
        enqueued_at=ENQUEUED_AT,
        retry_age_limit_seconds=86400,
        retry_attempt_limit=8,
    )

    receipt = engine.capture.submit(submission)
    assert receipt.duplicate is False
    assert envelope.request_digest == receipt.request_sha256
    accepted = TerminalReceipt(
        status=TerminalReceiptStatus.ACCEPTED,
        brain_id=cast(str, receipt.destination_brain_id),
        issuer_epoch=cast(int, receipt.issuer_epoch),
        delivery_id=cast(str, receipt.delivery_id),
        request_digest=receipt.request_sha256,
        final_admitted_tier=receipt.final_admitted_tier,
    )
    assert verify_terminal_receipt(envelope, accepted) == accepted

    replay = engine.capture.submit(submission)
    assert replay.duplicate is True
    assert envelope.request_digest == replay.request_sha256
    duplicate = TerminalReceipt(
        status=TerminalReceiptStatus.DUPLICATE,
        brain_id=cast(str, replay.destination_brain_id),
        issuer_epoch=cast(int, replay.issuer_epoch),
        delivery_id=cast(str, replay.delivery_id),
        request_digest=replay.request_sha256,
        final_admitted_tier=replay.final_admitted_tier,
    )
    assert verify_terminal_receipt(envelope, duplicate) == duplicate


def test_contract_slice_exposes_no_queue_retry_quarantine_or_storage_api() -> None:
    assert set(outbox.__all__) == {
        "OUTBOX_CONTRACT_VERSION",
        "DeliveryEnvelope",
        "OutboxContractError",
        "TerminalReceipt",
        "TerminalReceiptStatus",
        "outbox_request_digest",
        "verify_terminal_receipt",
    }
    assert not any(
        hasattr(outbox, name)
        for name in ("OutboxQueue", "RetryPolicy", "Quarantine", "OutboxStorage", "Transport")
    )

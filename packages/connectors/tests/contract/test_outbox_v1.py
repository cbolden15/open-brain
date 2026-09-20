from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from hashlib import sha256
from typing import cast

import pytest
from open_brain_engine.core.access_contracts import derive_brain_id
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.core.models import PrivacyTier

import open_brain_connectors.outbox as outbox
import open_brain_connectors.outbox.contracts as outbox_contracts
from open_brain_connectors.outbox import (
    OUTBOX_CONTRACT_VERSION,
    DeliveryEnvelope,
    OutboxContractError,
    TerminalReceipt,
    TerminalReceiptStatus,
    outbox_request_digest,
    verify_terminal_receipt,
)

TENANT_ID = "tenant_123e4567-e89b-42d3-a456-426614174000"
BRAIN_ID = derive_brain_id(TENANT_ID)
OTHER_BRAIN_ID = derive_brain_id("tenant_00000000-0000-4000-8000-000000000000")


def _envelope(
    *, requested_tier: PrivacyTier = PrivacyTier.WORK, payload: object | None = None
) -> DeliveryEnvelope:
    return DeliveryEnvelope.create(
        destination_brain_id=BRAIN_ID,
        expected_issuer_epoch=7,
        delivery_id="delivery.synthetic-001",
        requested_tier=requested_tier,
        policy_ref="policy.synthetic-v1",
        payload={"items": [1, {"kind": "synthetic"}], "present": True}
        if payload is None
        else payload,
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


def test_request_digest_is_deterministic_and_covers_the_exact_preimage() -> None:
    payload = {"z": [2, 1], "a": {"value": "synthetic"}}
    envelope = _envelope(payload=payload)
    preimage = {
        "contract_version": OUTBOX_CONTRACT_VERSION,
        "delivery_id": envelope.delivery_id,
        "destination_brain_id": envelope.destination_brain_id,
        "expected_issuer_epoch": envelope.expected_issuer_epoch,
        "payload": payload,
        "policy_ref": envelope.policy_ref,
        "requested_tier": envelope.requested_tier.value,
    }

    assert envelope.request_digest == sha256(
        portable_canonical_json_bytes(preimage)
    ).hexdigest()
    assert envelope.request_digest == outbox_request_digest(
        destination_brain_id=BRAIN_ID,
        expected_issuer_epoch=7,
        delivery_id="delivery.synthetic-001",
        requested_tier=PrivacyTier.WORK,
        policy_ref="policy.synthetic-v1",
        payload={"a": {"value": "synthetic"}, "z": [2, 1]},
    )


def test_envelope_detaches_and_deeply_freezes_input_payload() -> None:
    payload = {"items": [{"value": "original"}]}
    envelope = _envelope(payload=payload)
    payload["items"][0]["value"] = "changed"

    serialized = envelope.to_dict()
    assert serialized["payload"] == {"items": [{"value": "original"}]}
    frozen = cast(Mapping[str, object], envelope.payload)
    with pytest.raises(TypeError):
        frozen["new"] = True  # type: ignore[index]
    nested = cast(tuple[object, ...], frozen["items"])
    with pytest.raises(TypeError):
        cast(Mapping[str, object], nested[0])["value"] = "changed"  # type: ignore[index]
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
        DeliveryEnvelope.from_dict({**value, "attempts": 0})


@pytest.mark.parametrize(
    "change",
    [
        {"contract_version": "outbox.v2"},
        {"destination_brain_id": "brn_invalid"},
        {"expected_issuer_epoch": 0},
        {"delivery_id": ""},
        {"requested_tier": "private"},
        {"policy_ref": ""},
        {"request_digest": "A" * 64},
    ],
)
def test_delivery_envelope_rejects_malformed_fields(change: dict[str, object]) -> None:
    value = _envelope().to_dict()
    value.update(change)
    with pytest.raises(OutboxContractError):
        DeliveryEnvelope.from_dict(value)


@pytest.mark.parametrize("surrogate", ["\ud800", "\udfff"])
@pytest.mark.parametrize("reference_field", ["delivery_id", "policy_ref"])
@pytest.mark.parametrize("entry_point", ["create", "digest"])
def test_outbox_request_rejects_surrogate_references_at_the_contract_boundary(
    surrogate: str, reference_field: str, entry_point: str
) -> None:
    values: dict[str, object] = {
        "destination_brain_id": BRAIN_ID,
        "expected_issuer_epoch": 7,
        "delivery_id": "delivery.synthetic-001",
        "requested_tier": PrivacyTier.WORK,
        "policy_ref": "policy.synthetic-v1",
        "payload": {"kind": "synthetic"},
    }
    values[reference_field] = f"synthetic{surrogate}reference"

    with pytest.raises(OutboxContractError):
        if entry_point == "create":
            DeliveryEnvelope.create(**values)  # type: ignore[arg-type]
        else:
            outbox_request_digest(**values)  # type: ignore[arg-type]


def test_canonical_request_encoding_unicode_failure_is_wrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical_json = portable_canonical_json_bytes
    call_count = 0

    def fail_request_encoding(value: object) -> bytes:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogate")
        return canonical_json(value)

    monkeypatch.setattr(outbox_contracts, "portable_canonical_json_bytes", fail_request_encoding)

    with pytest.raises(OutboxContractError):
        outbox_request_digest(
            destination_brain_id=BRAIN_ID,
            expected_issuer_epoch=7,
            delivery_id="delivery.synthetic-001",
            requested_tier=PrivacyTier.WORK,
            policy_ref="policy.synthetic-v1",
            payload={"kind": "synthetic"},
        )


@pytest.mark.parametrize("payload", [{1: "not-json"}, {"float": 1.5}, {"bytes": b"no"}])
def test_delivery_envelope_rejects_noncanonical_json_payloads(payload: object) -> None:
    with pytest.raises(OutboxContractError, match="JSON payload"):
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

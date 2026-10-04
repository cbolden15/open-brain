"""Receipt integrity is distinct from present publication eligibility."""

from hashlib import sha256

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.engine.historical_contracts import HistoricalReceipt
from open_brain_engine.engine.sharing_contracts import SharingError


def _value(**changes: object) -> dict[str, object]:
    body: dict[str, object] = {
        "dto_version": 1,
        "operation_id": "baseline.synthetic",
        "request_sha256": "a" * 64,
        "destination": {"brain_id": "brn_synthetic", "issuer_epoch": 1},
        "source_id": "source_synthetic",
        "original_capture_id": "capture_original",
        "copy_capture_id": None,
        "outcome": "baseline_adopted",
        "claim_generation": 1,
        "relation_version": None,
        "recorded_at": "2026-10-03T00:00:00Z",
    }
    body.update(changes)
    body["receipt_sha256"] = sha256(
        b"open-brain-historical-receipt.v1\0" + portable_canonical_json_bytes(body)
    ).hexdigest()
    return body


def test_receipt_round_trip_preserves_original_bytes() -> None:
    value = _value()
    receipt = HistoricalReceipt.from_value(value)
    assert receipt.value() == value
    raw = portable_canonical_json_bytes(value)
    assert HistoricalReceipt.from_json(raw).canonical_bytes() == raw


@pytest.mark.parametrize(
    "changes",
    [
        {"dto_version": True},
        {"claim_generation": 0},
        {"outcome": "pending"},
        {"copy_capture_id": "capture_copy"},
        {"relation_version": 0},
        {"recorded_at": "not-a-timestamp"},
        {"recorded_at": "2026-10-03T00:00:00"},
        {"recorded_at": "2026-13-03T00:00:00Z"},
    ],
)
def test_receipt_refuses_invalid_state_even_with_recomputed_digest(
    changes: dict[str, object],
) -> None:
    with pytest.raises(SharingError, match="invalid_arguments"):
        HistoricalReceipt.from_value(_value(**changes))


def test_receipt_refuses_tampering_and_serialized_authority() -> None:
    value = _value()
    for change in ({"request_sha256": "b" * 64}, {"owner": True}, {"outcome": "pending"}):
        with pytest.raises(SharingError, match="invalid_arguments"):
            HistoricalReceipt.from_value(dict(value, **change))


@pytest.mark.parametrize("outcome", ["historical_copy_linked", "historical_copy_revoked"])
def test_relation_receipt_requires_distinct_copy_and_positive_version(outcome: str) -> None:
    value = _value(outcome=outcome, copy_capture_id="capture_copy", relation_version=1)
    assert HistoricalReceipt.from_value(value).value() == value
    for change in (
        {"copy_capture_id": None},
        {"copy_capture_id": "capture_original"},
        {"relation_version": None},
        {"relation_version": True},
    ):
        with pytest.raises(SharingError, match="invalid_arguments"):
            HistoricalReceipt.from_value(_value(**dict(value_without_digest(value), **change)))


def value_without_digest(value: dict[str, object]) -> dict[str, object]:
    return {key: item for key, item in value.items() if key != "receipt_sha256"}


@pytest.mark.parametrize("copy", [None, "capture_copy"])
def test_denial_receipt_has_no_relation_authority(copy: str | None) -> None:
    value = _value(outcome="denied_claim_recorded", copy_capture_id=copy)
    assert HistoricalReceipt.from_value(value).relation_version is None

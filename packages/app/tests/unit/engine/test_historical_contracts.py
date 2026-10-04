"""Closed historical authority components distinguish CAS from eligibility."""

from dataclasses import replace

import pytest
from open_brain_engine.engine.historical_contracts import (
    HistoricalDestination,
    HistoricalSourceCAS,
    RetainedCaptureEvidence,
)
from open_brain_engine.engine.sharing_contracts import SharingError


def _source(**changes: object) -> HistoricalSourceCAS:
    value = {
        "source_id": "source_synthetic",
        "expected_head": "capture_synthetic",
        "expected_head_version": 1,
        "expected_route_version": 0,
        "expected_lifecycle_version": 0,
        "expected_control_epoch": 0,
        "expected_lifecycle": "active",
        "expected_availability": "available",
        "expected_historical_only": False,
    }
    value.update(changes)
    return HistoricalSourceCAS.from_value(value)


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_lifecycle": "retired"},
        {"expected_availability": "missing"},
        {"expected_availability": "inaccessible"},
        {"expected_availability": "unknown"},
        {"expected_historical_only": True},
    ],
)
def test_exact_inactive_witness_is_valid_for_denial_but_not_activation(
    changes: dict[str, object],
) -> None:
    witness = _source(**changes)
    assert HistoricalSourceCAS.from_value(witness.value()) == witness
    with pytest.raises(SharingError, match="revision_changed"):
        witness.require_active()


def test_active_witness_can_be_used_for_positive_admission() -> None:
    _source().require_active()


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_lifecycle": "reactivated"},
        {"expected_availability": "deleted"},
        {"expected_historical_only": 0},
        {"expected_control_epoch": True},
        {"expected_route_version": -1},
        {"expected_head_version": 9007199254740992},
        {"source_id": "source_bad\nidentity"},
        {"expected_lifecycle": {"kind": "active"}},
        {"owner": True},
    ],
)
def test_source_witness_rejects_invalid_or_authority_fields(changes: dict[str, object]) -> None:
    with pytest.raises(SharingError, match="invalid_arguments"):
        _source(**changes)


def test_destination_is_closed_and_epoch_is_not_a_boolean() -> None:
    value = {"brain_id": "brn_synthetic", "issuer_epoch": 1}
    destination = HistoricalDestination.from_value(value)
    assert destination.value() == value
    for invalid in (dict(value, issuer_epoch=True), dict(value, authority="owner")):
        with pytest.raises(SharingError, match="invalid_arguments"):
            HistoricalDestination.from_value(invalid)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"brain_id":"brn_synthetic","issuer_epoch":1,"issuer_epoch":2}',
        b'{"brain_id":"brn_synthetic","issuer_epoch":true}',
        b'{"brain_id":"brn_synthetic","issuer_epoch":NaN}',
        b'{"brain_id":"brn_synthetic","issuer_epoch":1,"owner":true}',
        b'{"brain_id":"brn_synthetic"}',
        b"\xff",
        b" " * 65_537,
    ],
)
def test_json_boundary_refuses_ambiguous_or_invalid_identity(raw: bytes) -> None:
    with pytest.raises(SharingError, match="invalid_arguments"):
        HistoricalDestination.from_json(raw)


def test_json_boundary_reads_exact_destination() -> None:
    assert HistoricalDestination.from_json(
        b'{"brain_id":"brn_synthetic","issuer_epoch":1}'
    ) == HistoricalDestination(brain_id="brn_synthetic", issuer_epoch=1)


def test_retained_capture_binds_privacy_separately_from_legacy_request_digest() -> None:
    evidence = RetainedCaptureEvidence(
        capture_id="capture_synthetic",
        source_sha256="a" * 64,
        retained_delivery_id="synthetic.original",
        retained_request_sha256="b" * 64,
        privacy_sha256="c" * 64,
    )
    assert RetainedCaptureEvidence.from_value(evidence.value()) == evidence
    assert replace(evidence, privacy_sha256="d" * 64) != evidence
    with pytest.raises(SharingError, match="invalid_arguments"):
        RetainedCaptureEvidence.from_value(dict(evidence.value(), privacy_sha256="unknown"))
    with pytest.raises(SharingError, match="invalid_arguments"):
        RetainedCaptureEvidence.from_value(dict(evidence.value(), role="owner"))

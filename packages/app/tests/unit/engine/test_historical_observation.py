"""Historical reconciliation decodes existing custody without changing its meaning."""

import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.engine.contracts import (
    EventPayload,
    FilePayload,
    MeasurementPayload,
    Payload,
    ReferencePayload,
    TextPayload,
)
from open_brain_engine.engine.historical_contracts import (
    HistoricalBaselineRequest,
    HistoricalDestination,
    HistoricalSourceCAS,
    RetainedCaptureEvidence,
)
from open_brain_engine.engine.historical_observation import decode_historical_observation
from open_brain_engine.engine.sharing_contracts import SharingError
from open_brain_engine.engine.source_intake import SourceRevisionObservedDelivery

from packages.app.tests.unit.engine.test_source_observation import _delivery, _observation


def _observed(tmp_path: Path) -> SourceRevisionObservedDelivery:
    delivery = _delivery(tmp_path)
    return SourceRevisionObservedDelivery(
        binding=delivery.binding,
        submission=delivery.submission,
        expected_lifecycle_version=0,
        delivery_id=delivery.delivery_id,
        observation=_observation(delivery),
    )


def test_existing_observed_bytes_and_hashes_are_preserved(tmp_path: Path) -> None:
    original = _observed(tmp_path)
    decoded = decode_historical_observation(json.loads(original.custody_bytes()))
    assert decoded == original
    assert decoded.custody_bytes() == original.custody_bytes()
    assert decoded.envelope_sha256 == original.envelope_sha256


@pytest.mark.parametrize(
    "payload",
    [
        TextPayload("synthetic text"),
        ReferencePayload("https://example.test/synthetic", "synthetic supplied text"),
        FilePayload("synthetic.txt", "text/plain", b"synthetic bytes"),
        EventPayload("synthetic.event", "2026-10-03T00:00:00Z", {"synthetic": "value"}),
        MeasurementPayload("12.5", "count", "2026-10-03T00:00:00Z", {"synthetic": "value"}),
    ],
)
def test_all_existing_payload_families_preserve_exact_observed_bytes(
    tmp_path: Path,
    payload: Payload,
) -> None:
    delivery = _delivery(tmp_path)
    occurrence = (
        payload.occurrence_at if isinstance(payload, EventPayload | MeasurementPayload) else None
    )
    capture = replace(delivery.submission.capture, payload=payload, occurrence_at=occurrence)
    delivery = replace(
        delivery,
        submission=replace(
            delivery.submission,
            capture=capture,
            canonical_sha256=capture.request_sha256(),
        ),
    )
    observed = SourceRevisionObservedDelivery(
        binding=delivery.binding,
        submission=delivery.submission,
        expected_lifecycle_version=0,
        delivery_id=delivery.delivery_id,
        observation=_observation(delivery),
    )
    decoded = decode_historical_observation(json.loads(observed.custody_bytes()))
    assert decoded.custody_bytes() == observed.custody_bytes()
    assert decoded.submission.capture.payload == payload


@pytest.mark.parametrize(
    "location",
    ["root", "binding", "submission", "capture", "payload", "privacy", "provenance", "observation"],
)
def test_unknown_fields_never_disappear_during_reconstruction(
    tmp_path: Path,
    location: str,
) -> None:
    value = json.loads(_observed(tmp_path).custody_bytes())
    target = value
    if location in ("binding", "submission", "observation"):
        target = value[location]
    elif location in ("capture", "payload", "privacy", "provenance"):
        target = value["submission"]["capture"]
        if location != "capture":
            target = target[location]
    target["owner_authority"] = True
    with pytest.raises(SharingError, match="invalid_arguments"):
        decode_historical_observation(value)


@pytest.mark.parametrize(
    "change",
    ["v1", "bool_version", "missing_capture", "file_bytes", "wrong_digest", "owner_capture"],
)
def test_decoder_refuses_non_observed_or_reinterpreted_envelopes(
    tmp_path: Path, change: str
) -> None:
    value = json.loads(_observed(tmp_path).custody_bytes())
    if change == "v1":
        value["dto_version"] = 1
    elif change == "bool_version":
        value["submission"]["dto_version"] = True
    elif change == "missing_capture":
        value["submission"].pop("capture")
    elif change == "file_bytes":
        value["submission"]["file_bytes_base64"] = "YWJj"
    elif change == "wrong_digest":
        value["submission"]["canonical_sha256"] = "0" * 64
    else:
        value["submission"]["capture"] = {
            "action": "quick",
            "payload": {"family": "text", "text": "owner"},
        }
    with pytest.raises(SharingError, match="invalid_arguments"):
        decode_historical_observation(value)


def test_observed_decoder_is_bounded(tmp_path: Path) -> None:
    value = json.loads(_observed(tmp_path).custody_bytes())
    value["submission"]["capture"]["payload"]["text"] = "x" * 65_536
    assert len(portable_canonical_json_bytes(value)) > 65_536
    with pytest.raises(SharingError, match="invalid_arguments"):
        decode_historical_observation(value)


@pytest.mark.parametrize("location", ["binding", "submission", "ordering"])
def test_malformed_nested_containers_have_stable_refusal(tmp_path: Path, location: str) -> None:
    value = json.loads(_observed(tmp_path).custody_bytes())
    if location == "ordering":
        value["submission"]["ordering"] = None
    else:
        value[location]["namespace"] = list(value[location]["namespace"])
    with pytest.raises(SharingError, match="invalid_arguments"):
        decode_historical_observation(value)


def _baseline(tmp_path: Path) -> HistoricalBaselineRequest:
    observed = _observed(tmp_path)
    capture_id = "capture_" + str(uuid4())
    observed = replace(observed, submission=replace(observed.submission, expected_head=capture_id))
    return HistoricalBaselineRequest(
        operation_id="baseline.synthetic",
        destination=HistoricalDestination(
            brain_id=observed.binding.destination_brain_id,
            issuer_epoch=observed.binding.issuer_epoch,
        ),
        source_cas=HistoricalSourceCAS(
            source_id="source_synthetic",
            expected_head=capture_id,
            expected_head_version=1,
            expected_route_version=0,
            expected_lifecycle_version=0,
            expected_control_epoch=0,
            expected_lifecycle="active",
            expected_availability="available",
            expected_historical_only=False,
        ),
        retained_original=RetainedCaptureEvidence(
            capture_id=capture_id,
            source_sha256="a" * 64,
            retained_delivery_id="retained.owner",
            retained_request_sha256="b" * 64,
            privacy_sha256="c" * 64,
        ),
        observed_delivery=observed,
        expected_claim_generation=0,
    )


def test_baseline_keeps_old_owner_and_current_observed_evidence_distinct(tmp_path: Path) -> None:
    request = _baseline(tmp_path)
    raw = request.canonical_bytes()
    assert HistoricalBaselineRequest.from_json(raw) == request
    assert (
        request.retained_original.retained_request_sha256
        != request.observed_delivery.submission.canonical_sha256
    )
    value = request.value()
    value["owner_authority"] = True
    with pytest.raises(SharingError, match="invalid_arguments"):
        HistoricalBaselineRequest.from_value(value)


@pytest.mark.parametrize("change", ["destination", "head", "lifecycle", "control", "original"])
def test_baseline_refuses_conflicting_enclosing_evidence(tmp_path: Path, change: str) -> None:
    request = _baseline(tmp_path)
    with pytest.raises(SharingError, match="invalid_arguments"):
        if change == "destination":
            replace(request, destination=replace(request.destination, issuer_epoch=2))
        elif change == "head":
            replace(request, source_cas=replace(request.source_cas, expected_head="capture_other"))
        elif change == "lifecycle":
            replace(request, source_cas=replace(request.source_cas, expected_lifecycle_version=1))
        elif change == "control":
            replace(request, source_cas=replace(request.source_cas, expected_control_epoch=1))
        else:
            replace(
                request,
                retained_original=replace(request.retained_original, capture_id="capture_other"),
            )


@pytest.mark.parametrize("change", ["retired", "missing", "historical"])
def test_baseline_positive_admission_refuses_inactive_witness(tmp_path: Path, change: str) -> None:
    request = _baseline(tmp_path)
    changes: dict[str, dict[str, object]] = {
        "retired": {"expected_lifecycle": "retired"},
        "missing": {"expected_availability": "missing"},
        "historical": {"expected_historical_only": True},
    }
    source = HistoricalSourceCAS.from_value(dict(request.source_cas.value(), **changes[change]))
    with pytest.raises(SharingError, match="revision_changed"):
        replace(request, source_cas=source)

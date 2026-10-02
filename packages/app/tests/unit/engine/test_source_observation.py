from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.engine import TextPayload, open_local_engine
from open_brain_engine.engine.local import BrainEngine
from open_brain_engine.engine.local_schema import open_local_database_read_only
from open_brain_engine.engine.source_intake import (
    SourceRevisionBinding,
    SourceRevisionDelivery,
    SourceRevisionObservedDelivery,
    SourceRevisionSubmission,
)
from open_brain_engine.engine.source_observation import SourceRevisionObservation
from open_brain_engine.engine.t03_contracts import T03Error

from open_brain.profile import compile_single_user_local
from packages.app.tests.unit.engine.test_foundation_contracts import _public_submission


def _delivery(tmp_path: Path) -> SourceRevisionDelivery:
    profile = compile_single_user_local(tmp_path / "brain")
    tasks = open_local_engine(profile)
    capture = _public_submission(tasks)
    namespace = dict(
        connector_name="synthetic", connection_id="one", resource_id="one", external_id="one"
    )
    with open_local_database_read_only(profile) as connection:
        brain_id, epoch = connection.execute(
            "SELECT brain_id,issuer_epoch FROM brain_identity"
        ).fetchone()
    binding = SourceRevisionBinding(
        destination_brain_id=brain_id,
        issuer_epoch=epoch,
        root_fingerprint="synthetic-root",
        accepted_source_id="synthetic-selection",
        namespace=namespace,
    )
    return SourceRevisionDelivery(
        binding=binding,
        submission=SourceRevisionSubmission(
            capture=capture,
            namespace=namespace,
            revision_key="one",
            canonical_sha256=capture.request_sha256(),
            expected_head=None,
            ordering={"kind": "unordered"},
            expected_control_epoch=0,
        ),
        expected_lifecycle_version=0,
        delivery_id="synthetic.observed",
    )


def _observation(delivery: SourceRevisionDelivery) -> SourceRevisionObservation:
    capture = delivery.submission.capture
    return SourceRevisionObservation(
        original_sha256=sha256(b"synthetic raw").hexdigest(),
        transformed_sha256=sha256(b"synthetic transformed").hexdigest(),
        normalization_version="synthetic.normalization.v1",
        privacy_policy_version=capture.privacy.policy_version,
        privacy_policy_sha256=sha256(
            portable_canonical_json_bytes(capture.privacy.to_dict())
        ).hexdigest(),
        admitted_payload_sha256=sha256(
            portable_canonical_json_bytes(capture.payload.to_dict())
        ).hexdigest(),
    )


def test_observed_delivery_has_distinct_closed_v2_custody(tmp_path: Path) -> None:
    delivery = _delivery(tmp_path)
    observation = _observation(delivery)
    observed = SourceRevisionObservedDelivery(
        binding=delivery.binding,
        submission=delivery.submission,
        expected_lifecycle_version=0,
        delivery_id=delivery.delivery_id,
        observation=observation,
    )
    legacy = json.loads(delivery.custody_bytes())
    value = json.loads(observed.custody_bytes())
    assert legacy["dto_version"] == 1 and "observation" not in legacy
    assert value == {**legacy, "dto_version": 2, "observation": observation.value()}
    assert observed.envelope_sha256 != delivery.envelope_sha256
    assert SourceRevisionObservation.from_value(value["observation"]) == observation


@pytest.mark.parametrize(
    "field",
    [
        "privacy_policy_sha256",
        "privacy_policy_version",
        "admitted_payload_sha256",
    ],
)
def test_observed_delivery_refuses_inconsistent_admission_evidence(
    tmp_path: Path, field: str
) -> None:
    delivery = _delivery(tmp_path)
    value = _observation(delivery).value()
    value[field] = "different-version" if field.endswith("version") else "0" * 64
    observation = SourceRevisionObservation.from_value(value)
    with pytest.raises(T03Error, match="invalid_arguments"):
        SourceRevisionObservedDelivery(
            binding=delivery.binding,
            submission=delivery.submission,
            expected_lifecycle_version=0,
            delivery_id=delivery.delivery_id,
            observation=observation,
        )


@pytest.mark.parametrize("version", [True, 1.0, 2.0, False])
def test_revision_delivery_versions_are_exact_integers(tmp_path: Path, version: Any) -> None:
    delivery = _delivery(tmp_path)
    with pytest.raises(T03Error, match="invalid_arguments"):
        replace(delivery, dto_version=version)
    observation = _observation(delivery)
    with pytest.raises(T03Error, match="invalid_arguments"):
        replace(observation, dto_version=version)
    with pytest.raises(T03Error, match="invalid_arguments"):
        SourceRevisionObservedDelivery(
            binding=delivery.binding,
            submission=delivery.submission,
            expected_lifecycle_version=0,
            delivery_id=delivery.delivery_id,
            observation=observation,
            dto_version=version,
        )


@pytest.mark.parametrize(
    "field",
    [
        "original_sha256",
        "transformed_sha256",
        "normalization_version",
        "privacy_policy_version",
        "privacy_policy_sha256",
        "admitted_payload_sha256",
    ],
)
def test_observation_refuses_missing_or_unknown_fields(tmp_path: Path, field: str) -> None:
    value = _observation(_delivery(tmp_path)).value()
    value.pop(field)
    with pytest.raises(T03Error, match="invalid_arguments"):
        SourceRevisionObservation.from_value(value)
    value = _observation(_delivery(tmp_path)).value()
    value["unexpected"] = "synthetic"
    with pytest.raises(T03Error, match="invalid_arguments"):
        SourceRevisionObservation.from_value(value)


@pytest.mark.parametrize("alteration", ["source", "capture", "epoch", "outcome", "custody"])
def test_managed_receipt_verification_rejects_forged_nested_evidence(
    tmp_path: Path, alteration: str
) -> None:
    delivery = _delivery(tmp_path)
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    sink = engine.sources.public_revision_sink(delivery.binding)
    accepted = sink.submit(delivery)
    sink.verify_receipt(delivery, accepted)
    assert accepted.source_receipt is not None
    changes = cast(
        dict[str, Any],
        {
            "source": {"source_id": "source_" + "0" * 36},
            "capture": {"capture_id": "capture_" + "0" * 36},
            "epoch": {"control_epoch": accepted.source_receipt.control_epoch + 1},
            "outcome": {"outcome": "history_only"},
            "custody": {"custody_id": "custody_synthetic"},
        }[alteration],
    )
    nested = replace(accepted.source_receipt, **changes)
    forged = replace(accepted, source_receipt=nested, outcome=nested.outcome)
    with pytest.raises(T03Error, match="invalid_arguments"):
        sink.verify_receipt(delivery, forged)
    assert sink.submit(delivery) == accepted


def test_managed_pending_namespace_cannot_reserve_a_second_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    delivery = _delivery(tmp_path)
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    sink = engine.sources.public_revision_sink(delivery.binding)
    changed = replace(delivery.submission.capture, payload=TextPayload("synthetic second"))
    second = replace(
        delivery,
        delivery_id="synthetic.second",
        submission=replace(
            delivery.submission,
            revision_key="two",
            capture=changed,
            canonical_sha256=changed.request_sha256(),
        ),
    )
    original = engine.sources.submit_revision

    def interleave(submission: SourceRevisionSubmission) -> Any:
        with pytest.raises(T03Error, match="operation_pending"):
            sink.submit(second)
        return original(submission)

    with monkeypatch.context() as fault:
        fault.setattr(engine.sources, "submit_revision", interleave)
        accepted = sink.submit(delivery)
    sink.verify_receipt(delivery, accepted)
    with open_local_database_read_only(engine.profile) as connection:
        assert (
            connection.execute("SELECT count(*) FROM managed_source_deliveries").fetchone()[0] == 1
        )
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 1


def test_managed_new_delivery_cannot_reinterpret_an_existing_intake(tmp_path: Path) -> None:
    delivery = _delivery(tmp_path)
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    sink = engine.sources.public_revision_sink(delivery.binding)
    accepted = sink.submit(delivery)
    assert accepted.source_receipt is not None
    replay = replace(
        delivery,
        delivery_id="synthetic.reinterpreted",
        submission=replace(
            delivery.submission,
            expected_head=accepted.source_receipt.capture_id,
            ordering={"kind": "predecessor", "revision_key": "one"},
        ),
    )
    with pytest.raises(T03Error, match="invalid_arguments"):
        sink.submit(replay)
    sink.verify_receipt(delivery, accepted)
    with open_local_database_read_only(engine.profile) as connection:
        assert (
            connection.execute("SELECT count(*) FROM managed_source_deliveries").fetchone()[0] == 1
        )

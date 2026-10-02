from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

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


@pytest.mark.parametrize("path", ["managed", "direct"])
def test_managed_pending_namespace_cannot_reserve_a_second_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    delivery = _delivery(tmp_path)
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    sink = engine.sources.public_revision_sink(delivery.binding)
    changed = replace(
        delivery.submission.capture,
        payload=TextPayload("synthetic second"),
        delivery_id="synthetic.second.capture",
    )
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
            if path == "managed":
                sink.submit(second)
            else:
                original(second.submission)
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


@pytest.mark.parametrize(
    "invalid",
    [
        "foreign_head",
        "first_predecessor",
        "wrong_predecessor",
        "unordered_update",
        "incomparable_monotonic",
    ],
)
def test_managed_fresh_invalid_order_or_head_leaves_no_reservation(
    tmp_path: Path, invalid: str
) -> None:
    delivery = _delivery(tmp_path)
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    sink = engine.sources.public_revision_sink(delivery.binding)
    expected_count = 0
    if invalid in {"foreign_head", "first_predecessor"}:
        submission = replace(
            delivery.submission,
            expected_head="capture_" + str(uuid4()) if invalid == "foreign_head" else None,
            ordering={"kind": "predecessor", "revision_key": "missing"}
            if invalid == "first_predecessor"
            else {"kind": "unordered"},
        )
    else:
        accepted = sink.submit(delivery)
        assert accepted.source_receipt is not None
        expected_count = 1
        capture = replace(
            delivery.submission.capture,
            payload=TextPayload("synthetic invalid order"),
            delivery_id="synthetic.invalid.two",
        )
        orders: dict[str, Any] = {
            "wrong_predecessor": {"kind": "predecessor", "revision_key": "missing"},
            "unordered_update": {"kind": "unordered"},
            "incomparable_monotonic": {
                "kind": "monotonic",
                "provider_namespace": "synthetic",
                "epoch": "one",
                "sequence": 2,
            },
        }
        submission = replace(
            delivery.submission,
            capture=capture,
            revision_key="two",
            canonical_sha256=capture.request_sha256(),
            expected_head=accepted.source_receipt.capture_id,
            ordering=orders[invalid],
        )
    invalid_delivery = replace(delivery, submission=submission, delivery_id="synthetic.invalid")
    with pytest.raises(T03Error, match="revision_changed|source_revision_conflict"):
        sink.submit(invalid_delivery)
    with open_local_database_read_only(engine.profile) as connection:
        for table in ("captures", "source_intakes", "managed_source_deliveries"):
            assert (
                connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == expected_count
            )
        assert connection.execute("SELECT count(*) FROM source_quarantine").fetchone()[0] == 0
    BrainEngine.open(engine.profile)


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
            capture=replace(
                delivery.submission.capture, delivery_id="synthetic.reinterpreted.capture"
            ),
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


def test_managed_admission_does_not_reserve_over_an_existing_pending_direct_intake(
    tmp_path: Path,
) -> None:
    from open_brain_engine.engine import CaptureFault, InjectedFault

    delivery = _delivery(tmp_path)
    profile = compile_single_user_local(tmp_path / "brain")
    engine = BrainEngine.open(profile, faults={CaptureFault.AFTER_CAPTURE_RESERVATION})
    changed = replace(
        delivery.submission.capture,
        payload=TextPayload("synthetic direct pending"),
        delivery_id="synthetic.direct.pending.capture",
    )
    direct = replace(
        delivery.submission,
        capture=changed,
        revision_key="direct",
        canonical_sha256=changed.request_sha256(),
    )
    with pytest.raises(InjectedFault):
        engine.sources.submit_revision(direct)
    with pytest.raises(T03Error, match="operation_pending"):
        engine.sources.public_revision_sink(delivery.binding).submit(delivery)
    with open_local_database_read_only(profile) as connection:
        assert (
            connection.execute("SELECT count(*) FROM managed_source_deliveries").fetchone()[0] == 0
        )
        assert connection.execute("SELECT count(*) FROM source_intakes").fetchone()[0] == 1
    reopened = BrainEngine.open(profile)
    with pytest.raises(T03Error, match="revision_changed"):
        reopened.sources.public_revision_sink(delivery.binding).submit(delivery)
    with open_local_database_read_only(profile) as connection:
        assert (
            connection.execute("SELECT count(*) FROM managed_source_deliveries").fetchone()[0] == 0
        )
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM source_revisions").fetchone()[0] == 1
    reopened.tasks.portability.export(tmp_path / "export", export_id="export_" + str(uuid4()))


def test_managed_admission_refuses_reused_source_delivery_id_before_sql_insert(
    tmp_path: Path,
) -> None:
    delivery = _delivery(tmp_path)
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    sink = engine.sources.public_revision_sink(delivery.binding)
    accepted = sink.submit(delivery)
    assert accepted.source_receipt is not None
    changed = replace(delivery.submission.capture, payload=TextPayload("synthetic reused delivery"))
    second = replace(
        delivery,
        delivery_id="synthetic.reused",
        submission=replace(
            delivery.submission,
            capture=changed,
            revision_key="two",
            canonical_sha256=changed.request_sha256(),
            expected_head=accepted.source_receipt.capture_id,
            ordering={"kind": "predecessor", "revision_key": "one"},
        ),
    )
    with pytest.raises(T03Error, match="invalid_arguments"):
        sink.submit(second)
    sink.verify_receipt(delivery, accepted)
    with open_local_database_read_only(engine.profile) as connection:
        assert (
            connection.execute("SELECT count(*) FROM managed_source_deliveries").fetchone()[0] == 1
        )
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 1

"""Immutable managed receipts are not current page eligibility witnesses."""

from dataclasses import replace
from pathlib import Path

import pytest
from open_brain_engine.engine import BrainEngine
from open_brain_engine.engine.historical_recovery import _historical_transaction
from open_brain_engine.engine.historical_tasks import adopt_historical_baseline
from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission
from open_brain_engine.engine.t03_contracts import EffectiveAuthority, T03Error

from open_brain.profile import compile_single_user_local
from packages.app.tests.unit.engine.test_historical_baseline import _baseline
from packages.app.tests.unit.engine.test_historical_continuity import _adopt, _successor


@pytest.mark.parametrize(
    "change", [None, "head", "route", "availability", "historical", "control", "lifecycle"]
)
def test_normal_receipt_current_checkpoint_revalidation(tmp_path: Path, change: str | None) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    baseline = _adopt(engine)
    delivery = _successor(baseline)
    sink = engine.sources.public_revision_sink(delivery.binding)
    receipt = sink.submit(delivery)
    result = sink.lookup_checkpoint(delivery, receipt, selection_generation="selected.v1")
    assert result.receipt == receipt
    assert receipt.source_receipt is not None
    assert result.source_cas.expected_head == receipt.source_receipt.capture_id
    if change is None:
        with sink.revision_page_checkpoint(
            ((delivery, result),), selection_generation="selected.v1"
        ):
            pass
        return
    if change == "head":
        following = replace(
            delivery,
            delivery_id="synthetic.following.delivery",
            submission=replace(
                delivery.submission,
                capture=replace(
                    delivery.submission.capture, delivery_id="synthetic.following.capture"
                ),
                revision_key="synthetic.following.key",
                expected_head=result.source_cas.expected_head,
                ordering={"kind": "predecessor", "revision_key": delivery.submission.revision_key},
            ),
        )
        sink.submit(following)
    else:
        with _historical_transaction(engine.profile, lambda: None) as connection:
            statement = {
                "route": "UPDATE logical_sources SET route_version=route_version+1",
                "availability": "UPDATE logical_sources SET availability='missing'",
                "historical": "UPDATE logical_sources SET historical_only=1",
                "control": "UPDATE engine_generations SET control_epoch=control_epoch+1",
                "lifecycle": (
                    "UPDATE source_lifecycle_state SET lifecycle_version=lifecycle_version+1"
                ),
            }[change]
            connection.execute(statement)
    # Original receipt remains immutable evidence despite lost eligibility.
    sink.verify_receipt(delivery, receipt)
    with (
        pytest.raises(T03Error, match="revision_changed"),
        sink.revision_page_checkpoint(((delivery, result),), selection_generation="selected.v1"),
    ):
        pytest.fail("stale normal witness reached persistence")


def test_mixed_historical_and_normal_page_uses_exact_same_fence(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    first = _adopt(engine)
    normal_delivery = _successor(first)
    normal_sink = engine.sources.public_revision_sink(normal_delivery.binding)
    receipt = normal_sink.submit(normal_delivery)
    normal = normal_sink.lookup_checkpoint(
        normal_delivery, receipt, selection_generation="selected.v1"
    )
    second = replace(
        _baseline(engine, retained_delivery_id="owner.second"), expected_claim_generation=1
    )
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )
    with exclusive_runtime_admission(engine.profile) as admission:
        adopt_historical_baseline(
            engine.profile,
            second,
            authority=owner,
            admission=admission,
            validate_before_write=lambda: None,
        )
    historical_sink = engine.sources.public_revision_sink(second.observed_delivery.binding)
    historical = historical_sink.lookup_baseline(
        second.observed_delivery, selection_generation="selected.v1"
    )
    assert historical is not None
    entries = ((normal_delivery, normal), (second.observed_delivery, historical))
    with normal_sink.revision_page_checkpoint(entries, selection_generation="selected.v1"):
        pass
    with (
        pytest.raises(T03Error, match="revision_changed"),
        normal_sink.revision_page_checkpoint(entries, selection_generation="selected.reset"),
    ):
        pytest.fail("reset selection reached mixed-page persistence")


@pytest.mark.parametrize("change", ["root", "source_cas", "receipt", "selection"])
def test_modified_normal_witness_cannot_authorize_checkpoint(tmp_path: Path, change: str) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    baseline = _adopt(engine)
    delivery = _successor(baseline)
    sink = engine.sources.public_revision_sink(delivery.binding)
    receipt = sink.submit(delivery)
    result = sink.lookup_checkpoint(delivery, receipt, selection_generation="selected.v1")
    if change == "root":
        device, inode = result.root_identity
        altered = replace(result, root_identity=(device, inode + 1))
    elif change == "source_cas":
        altered = replace(result, source_cas=replace(result.source_cas, expected_route_version=999))
    elif change == "receipt":
        altered = replace(result, receipt=replace(receipt, envelope_sha256="0" * 64))
    else:
        altered = replace(result, selection_generation="selected.old")
    with (
        pytest.raises(T03Error),
        sink.revision_page_checkpoint(((delivery, altered),), selection_generation="selected.v1"),
    ):
        pytest.fail("modified witness reached persistence")


def test_missing_historical_authority_refuses_normal_checkpoint(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    delivery = _successor(_adopt(engine))
    sink = engine.sources.public_revision_sink(delivery.binding)
    receipt = sink.submit(delivery)
    registry = engine.profile.root / ".open-brain/historical-authority/historical-claims.v1.json"
    registry.rename(registry.with_suffix(".retained"))
    sink.verify_receipt(delivery, receipt)
    with pytest.raises(T03Error, match="revision_changed"):
        sink.lookup_checkpoint(delivery, receipt, selection_generation="selected.v1")

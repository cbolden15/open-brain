"""A historical fact is a checkpoint witness only while exact eligibility holds."""

from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from open_brain_engine.engine import BrainEngine
from open_brain_engine.engine.historical_recovery import _historical_transaction
from open_brain_engine.engine.source_intake import SourceRevisionDeliveryReceipt
from open_brain_engine.engine.t03_contracts import T03Error
from open_brain_engine.storage.locks import FileLease, LockBusyError

from open_brain.profile import compile_single_user_local
from packages.app.tests.unit.engine.test_historical_continuity import _adopt, _successor


def test_exact_baseline_lookup_is_read_only_and_not_a_normal_delivery(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    baseline = _adopt(engine)
    sink = engine.sources.public_revision_sink(baseline.observed_delivery.binding)
    with closing(engine._store.connect()) as connection:
        before = list(connection.iterdump())
    result = sink.lookup_baseline(baseline.observed_delivery, selection_generation="selected.v1")
    assert result is not None
    assert not isinstance(result, SourceRevisionDeliveryReceipt)
    assert result.capture_id == baseline.retained_original.capture_id
    assert result.source_id == baseline.source_cas.source_id
    assert result.receipt.operation_id == baseline.operation_id
    assert result.observed_envelope_sha256 == baseline.observed_delivery.envelope_sha256
    assert (
        sink.lookup_baseline(baseline.observed_delivery, selection_generation="selected.v1")
        == result
    )
    with closing(engine._store.connect()) as connection:
        assert list(connection.iterdump()) == before
        assert connection.execute("SELECT COUNT(*) FROM captures").fetchone()[0] == 1
        assert (
            connection.execute("SELECT COUNT(*) FROM managed_source_deliveries").fetchone()[0] == 0
        )


@pytest.mark.parametrize(
    "field", ["head", "route", "lifecycle", "availability", "historical", "control"]
)
def test_changed_current_source_refuses_old_checkpoint_witness(tmp_path: Path, field: str) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    baseline = _adopt(engine)
    sink = engine.sources.public_revision_sink(baseline.observed_delivery.binding)
    result = sink.lookup_baseline(baseline.observed_delivery, selection_generation="selected.v1")
    assert result is not None
    if field == "head":
        sink.submit(_successor(baseline))
    else:
        with _historical_transaction(engine.profile, lambda: None) as connection:
            statement = {
                "route": "UPDATE logical_sources SET route_version=route_version+1",
                "lifecycle": (
                    "UPDATE source_lifecycle_state SET lifecycle_version=lifecycle_version+1"
                ),
                "availability": "UPDATE logical_sources SET availability='missing'",
                "historical": "UPDATE logical_sources SET historical_only=1",
                "control": "UPDATE engine_generations SET control_epoch=control_epoch+1",
            }[field]
            connection.execute(statement)
    with pytest.raises(T03Error, match="revision_changed"):
        sink.lookup_baseline(baseline.observed_delivery, selection_generation="selected.v1")
    with (
        pytest.raises(T03Error, match="revision_changed"),
        sink.baseline_checkpoint(
            baseline.observed_delivery, result, selection_generation="selected.v1"
        ),
    ):
        pytest.fail("stale witness reached checkpoint body")


def test_changed_observation_is_not_a_baseline_duplicate(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    baseline = _adopt(engine)
    sink = engine.sources.public_revision_sink(baseline.observed_delivery.binding)
    assert sink.lookup_baseline(_successor(baseline), selection_generation="selected.v1") is None


def test_selection_reset_cannot_use_previous_lookup(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    baseline = _adopt(engine)
    sink = engine.sources.public_revision_sink(baseline.observed_delivery.binding)
    result = sink.lookup_baseline(baseline.observed_delivery, selection_generation="selected.v1")
    assert result is not None
    with (
        pytest.raises(T03Error, match="revision_changed"),
        sink.baseline_checkpoint(
            baseline.observed_delivery, result, selection_generation="selected.v2"
        ),
    ):
        pytest.fail("selection reset reached checkpoint body")


def test_changed_receipt_cannot_use_checkpoint_context(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    baseline = _adopt(engine)
    sink = engine.sources.public_revision_sink(baseline.observed_delivery.binding)
    result = sink.lookup_baseline(baseline.observed_delivery, selection_generation="selected.v1")
    assert result is not None
    with (
        pytest.raises(T03Error, match="revision_changed"),
        sink.baseline_checkpoint(
            baseline.observed_delivery,
            replace(result, observed_envelope_sha256="0" * 64),
            selection_generation="selected.v1",
        ),
    ):
        pytest.fail("altered witness reached checkpoint body")


def test_checkpoint_holds_writer_exclusion_and_releases_on_failure(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    baseline = _adopt(engine)
    sink = engine.sources.public_revision_sink(baseline.observed_delivery.binding)
    result = sink.lookup_baseline(baseline.observed_delivery, selection_generation="selected.v1")
    assert result is not None
    contender = FileLease(
        engine.profile.root / ".open-brain",
        "synthetic-checkpoint-contender",
        parent_root_identity=engine.profile.root_identity,
    )
    reached = False
    with (
        pytest.raises(RuntimeError, match="synthetic checkpoint failure"),
        sink.baseline_checkpoint(
            baseline.observed_delivery, result, selection_generation="selected.v1"
        ),
    ):
        reached = True
        with pytest.raises(LockBusyError), contender.acquire_shared_writer():
            pytest.fail("competing writer admitted during checkpoint")
        raise RuntimeError("synthetic checkpoint failure")
    assert reached
    with contender.acquire_shared_writer():
        pass
    assert (
        sink.lookup_baseline(baseline.observed_delivery, selection_generation="selected.v1")
        == result
    )


@pytest.mark.parametrize("generation", ["", "bad\x00generation", "x" * 1025, "\ud800"])
def test_lookup_generation_is_bounded_and_control_free(tmp_path: Path, generation: str) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    baseline = _adopt(engine)
    sink = engine.sources.public_revision_sink(baseline.observed_delivery.binding)
    with pytest.raises(T03Error, match="invalid_arguments"):
        sink.lookup_baseline(baseline.observed_delivery, selection_generation=generation)


@pytest.mark.parametrize("file_name", ["historical-claims.v1.json", "historical-fence.v1.json"])
def test_missing_authority_is_not_an_unbound_namespace(tmp_path: Path, file_name: str) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    baseline = _adopt(engine)
    sink = engine.sources.public_revision_sink(baseline.observed_delivery.binding)
    authority_root = engine.profile.root / ".open-brain/historical-authority"
    path = authority_root / file_name
    assert path.is_file()
    path.rename(path.with_suffix(".retained"))
    with pytest.raises(T03Error, match="revision_changed"):
        sink.lookup_baseline(baseline.observed_delivery, selection_generation="selected.v1")


def test_wrong_binding_cannot_lookup_another_baseline(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    baseline = _adopt(engine)
    delivery = baseline.observed_delivery
    other = replace(delivery.binding, accepted_source_id="different-selection")
    sink = engine.sources.public_revision_sink(other)
    with pytest.raises(T03Error, match="invalid_arguments"):
        sink.lookup_baseline(delivery, selection_generation="selected.v1")


def test_root_witness_cannot_be_replaced_at_checkpoint(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    baseline = _adopt(engine)
    sink = engine.sources.public_revision_sink(baseline.observed_delivery.binding)
    result = sink.lookup_baseline(baseline.observed_delivery, selection_generation="selected.v1")
    assert result is not None
    device, inode = engine.profile.root_identity
    with (
        pytest.raises(T03Error, match="revision_changed"),
        sink.baseline_checkpoint(
            baseline.observed_delivery,
            replace(result, root_identity=(device, inode + 1)),
            selection_generation="selected.v1",
        ),
    ):
        pytest.fail("replaced root witness reached checkpoint body")

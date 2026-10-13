"""A historical fact is a checkpoint witness only while exact eligibility holds."""

from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from open_brain_engine.engine import BrainEngine
from open_brain_engine.engine.historical_checkpoint import HistoricalBaselineDuplicate
from open_brain_engine.engine.historical_recovery import _historical_transaction
from open_brain_engine.engine.source_intake import (
    SourceRevisionDeliveryReceipt,
    SourceRevisionObservedDelivery,
)
from open_brain_engine.engine.t03_contracts import T03Error
from open_brain_engine.storage.locks import FileLease, LockBusyError

from open_brain.profile import compile_single_user_local
from packages.app.tests.unit.engine.test_historical_continuity import _adopt, _successor


@pytest.mark.parametrize("count", [2, 25])
def test_multi_source_page_revalidates_all_entries_under_one_fence(
    tmp_path: Path, count: int
) -> None:
    from open_brain_engine.engine.historical_tasks import adopt_historical_baseline
    from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission
    from open_brain_engine.engine.t03_contracts import EffectiveAuthority

    from packages.app.tests.unit.engine.test_historical_baseline import _baseline

    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    baselines = [_adopt(engine)]
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )
    for index in range(1, count):
        baseline = replace(
            _baseline(engine, retained_delivery_id=f"owner.item{index}"),
            expected_claim_generation=index,
        )
        with exclusive_runtime_admission(engine.profile) as admission:
            adopt_historical_baseline(
                engine.profile,
                baseline,
                authority=owner,
                admission=admission,
                validate_before_write=lambda: None,
            )
        baselines.append(baseline)
    sinks = tuple(
        engine.sources.public_revision_sink(item.observed_delivery.binding) for item in baselines
    )
    entries = []
    for sink, baseline in zip(sinks, baselines, strict=True):
        result = sink.lookup_baseline(
            baseline.observed_delivery, selection_generation="selected.v1"
        )
        assert result is not None
        entries.append((baseline.observed_delivery, result))
    with sinks[0].baseline_page_checkpoint(tuple(entries), selection_generation="selected.v1"):
        pass
    # The last item becoming stale must prevent the entire page body.
    sinks[-1].submit(_successor(baselines[-1]))
    with (
        pytest.raises(T03Error, match="revision_changed"),
        sinks[0].baseline_page_checkpoint(tuple(entries), selection_generation="selected.v1"),
    ):
        pytest.fail("page committed despite stale final source")


def test_template_reconstructs_exact_baseline_without_returning_capture_body(
    tmp_path: Path,
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    baseline = _adopt(engine)
    delivery = baseline.observed_delivery
    sink = engine.sources.public_revision_sink(delivery.binding)
    template = sink.baseline_template()
    assert template is not None
    assert not hasattr(template, "capture")
    assert not hasattr(template, "observation")
    rebuilt = replace(
        delivery,
        delivery_id=template.delivery_id,
        expected_lifecycle_version=template.expected_lifecycle_version,
        submission=replace(
            delivery.submission,
            capture=replace(delivery.submission.capture, delivery_id=template.capture_delivery_id),
            revision_key=template.revision_key,
            expected_head=template.expected_head,
            ordering=template.ordering,
            expected_control_epoch=template.expected_control_epoch,
        ),
    )
    assert rebuilt.custody_bytes() == delivery.custody_bytes()
    assert rebuilt.envelope_sha256 == template.observed_envelope_sha256
    assert sink.lookup_baseline(rebuilt, selection_generation="selected.v1") is not None
    with pytest.raises(TypeError):
        template.ordering["kind"] = "unordered"  # type: ignore[index]


def test_template_is_historical_not_current_eligibility(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    baseline = _adopt(engine)
    sink = engine.sources.public_revision_sink(baseline.observed_delivery.binding)
    template = sink.baseline_template()
    sink.submit(_successor(baseline))
    assert sink.baseline_template() == template
    with pytest.raises(T03Error, match="revision_changed"):
        sink.lookup_baseline(baseline.observed_delivery, selection_generation="selected.v1")


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

    with (
        pytest.raises(RuntimeError, match="synthetic page persistence failure"),
        sink.baseline_page_checkpoint(
            ((baseline.observed_delivery, result),), selection_generation="selected.v1"
        ),
    ):
        raise RuntimeError("synthetic page persistence failure")
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
        sink.baseline_template()
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


def test_page_checkpoint_holds_one_writer_fence_through_caller_persistence(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    baseline = _adopt(engine)
    delivery = baseline.observed_delivery
    sink = engine.sources.public_revision_sink(delivery.binding)
    result = sink.lookup_baseline(delivery, selection_generation="selected.v1")
    assert result is not None
    contender = FileLease(
        engine.profile.root / ".open-brain",
        "synthetic-page-contender",
        parent_root_identity=engine.profile.root_identity,
    )
    with (
        sink.baseline_page_checkpoint(((delivery, result),), selection_generation="selected.v1"),
        pytest.raises(LockBusyError),
        contender.acquire_shared_writer(),
    ):
        pytest.fail("competing writer reached checkpoint persistence")
    with contender.acquire_shared_writer():
        pass


@pytest.mark.parametrize("case", ["empty", "oversized", "duplicate", "wrong_resource", "stale"])
def test_page_checkpoint_refuses_invalid_or_stale_page_before_body(
    tmp_path: Path, case: str
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    baseline = _adopt(engine)
    delivery = baseline.observed_delivery
    sink = engine.sources.public_revision_sink(delivery.binding)
    result = sink.lookup_baseline(delivery, selection_generation="selected.v1")
    assert result is not None
    entries: tuple[tuple[SourceRevisionObservedDelivery, HistoricalBaselineDuplicate], ...] = (
        (delivery, result),
    )
    code = "invalid_arguments"
    if case == "empty":
        entries = ()
    elif case == "oversized":
        entries *= 26
    elif case == "duplicate":
        entries *= 2
    elif case == "wrong_resource":
        binding = replace(
            delivery.binding, namespace=dict(delivery.binding.namespace, resource_id="other")
        )
        entries = (
            (
                replace(
                    delivery,
                    binding=binding,
                    submission=replace(delivery.submission, namespace=binding.namespace),
                ),
                result,
            ),
        )
    else:
        entries = ((delivery, replace(result, selection_generation="selected.old")),)
        code = "revision_changed"
    with (
        pytest.raises(T03Error, match=code),
        sink.baseline_page_checkpoint(entries, selection_generation="selected.v1"),
    ):
        pytest.fail("invalid page reached checkpoint body")

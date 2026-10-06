"""Saved adapter observations reuse adopted identity without normal intake."""

from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from open_brain_engine.engine import BrainEngine, CaptureSubmission, PublicJobCaptureContext
from open_brain_engine.engine.historical_tasks import adopt_historical_baseline
from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission
from open_brain_engine.engine.source_intake import SourceRevisionObservedDelivery
from open_brain_engine.engine.t03_contracts import EffectiveAuthority, T03Error

from open_brain.profile import compile_single_user_local
from open_brain_collector.lifecycle import EngineRevisionSink
from open_brain_connectors.runtime.live_storage import PrivateJsonStore
from open_brain_connectors.runtime.source_intake import SourceRecordIntake
from packages.app.tests.unit.engine.test_historical_baseline import _baseline
from packages.collector.tests.integration.test_saved_markdown import _context, _intake, _root


def _adopt_saved(
    tmp_path: Path,
) -> tuple[BrainEngine, EngineRevisionSink, SourceRecordIntake, SourceRevisionObservedDelivery]:
    intake = _intake(_root(tmp_path))
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    context = _context(engine.profile)
    sink = EngineRevisionSink(
        engine.sources.public_revision_sink,
        engine.capture.public_job_sink(context),
        store=PrivateJsonStore(tmp_path / "revisions"),
    )
    observed = _adopt_saved_intake(engine, sink, intake)
    return engine, sink, intake, observed


def _adopt_saved_intake(
    engine: BrainEngine,
    sink: EngineRevisionSink,
    intake: SourceRecordIntake,
    *,
    slot: int = 0,
    root_fingerprint: str | None = None,
    context: PublicJobCaptureContext | None = None,
) -> SourceRevisionObservedDelivery:
    context = _context(engine.profile) if context is None else context
    baseline = _baseline(
        engine,
        retained_text=intake.text,
        retained_delivery_id="owner.original" if slot == 0 else f"owner.saved.{slot}",
    )
    baseline = replace(baseline, expected_claim_generation=slot)
    binding, _ = sink._revision_capability(intake)
    if root_fingerprint is not None:
        binding = replace(binding, root_fingerprint=root_fingerprint)
    capture = CaptureSubmission.for_public_job(
        context=context,
        payload=intake.payload(),
        delivery_id=f"synthetic.saved.adopted.capture.{slot}",
        source_origin="third_party",
        source_reference=intake.source_reference,
        provenance=intake.provenance(),
        privacy=intake.privacy,
        intent="reference",
        title=intake.title,
    )
    assert intake.observation is not None
    observed = replace(
        baseline.observed_delivery,
        delivery_id=f"synthetic.saved.baseline.delivery.{slot}",
        binding=binding,
        submission=replace(
            baseline.observed_delivery.submission,
            capture=capture,
            canonical_sha256=capture.request_sha256(),
            namespace=binding.namespace,
            revision_key=f"synthetic.saved.baseline.key.{slot}",
        ),
        observation=intake.observation,
    )
    baseline = replace(baseline, observed_delivery=observed)
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )
    with exclusive_runtime_admission(engine.profile) as admission:
        adopt_historical_baseline(
            engine.profile,
            baseline,
            authority=owner,
            admission=admission,
            validate_before_write=lambda: None,
        )
    return observed


def test_actual_saved_adapter_reconstructs_baseline_without_new_custody(tmp_path: Path) -> None:
    engine, sink, intake, observed = _adopt_saved(tmp_path)
    with closing(engine._store.connect()) as connection:
        before = list(connection.iterdump())
    result = sink.lookup_baseline(intake, selection_generation="selected.v1")
    assert result is not None
    assert result.delivery.custody_bytes() == observed.custody_bytes()
    assert result.result.observed_envelope_sha256 == observed.envelope_sha256
    # A new collector process has no revision cache to seed or fabricate.
    restarted = EngineRevisionSink(
        engine.sources.public_revision_sink,
        engine.capture.public_job_sink(_context(engine.profile)),
        store=PrivateJsonStore(tmp_path / "restarted-revisions"),
    )
    assert restarted.lookup_baseline(intake, selection_generation="selected.v1") == result
    assert PrivateJsonStore(tmp_path / "revisions").names("saved-revision-") == ()
    with closing(engine._store.connect()) as connection:
        assert list(connection.iterdump()) == before
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 1
        assert (
            connection.execute("SELECT count(*) FROM managed_source_deliveries").fetchone()[0] == 0
        )


@pytest.mark.parametrize("change", ["private_raw", "title", "normalization", "namespace"])
def test_changed_saved_intake_is_not_historical_duplicate(tmp_path: Path, change: str) -> None:
    _, sink, intake, _ = _adopt_saved(tmp_path)
    assert intake.observation is not None
    if change == "private_raw":
        path = tmp_path / "selected/saved.md"
        path.write_text(path.read_text().replace("private", "changed private"))
        changed = _intake(tmp_path / "selected")
        assert changed.text == intake.text
    elif change == "title":
        changed = replace(intake, title="changed title")
    elif change == "normalization":
        changed = replace(
            intake, observation=replace(intake.observation, normalization_version="changed.v3")
        )
    else:
        changed = replace(intake, key=replace(intake.key, external_id="different-item"))
    assert sink.lookup_baseline(changed, selection_generation="selected.v1") is None


def test_saved_lookup_refuses_withdrawn_adopted_baseline(tmp_path: Path) -> None:
    from open_brain_engine.engine.source_lifecycle_contracts import SourceWithdrawRequest

    engine, sink, intake, _ = _adopt_saved(tmp_path)
    result = sink.lookup_baseline(intake, selection_generation="selected.v1")
    assert result is not None
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )
    engine.sources.withdraw(
        SourceWithdrawRequest(
            source_id=result.result.source_id,
            expected_head=result.result.capture_id,
            expected_lifecycle_version=result.result.source_cas.expected_lifecycle_version,
            operation_id="withdraw.synthetic.saved",
            brain_id=result.result.receipt.destination.brain_id,
            issuer_epoch=result.result.receipt.destination.issuer_epoch,
            reason_code="owner_request",
        ),
        authority=owner,
    )
    with pytest.raises(T03Error, match="revision_changed"):
        sink.lookup_baseline(intake, selection_generation="selected.v1")


def test_changed_file_uses_real_successor_and_preserves_original_identity(tmp_path: Path) -> None:
    engine, sink, intake, _ = _adopt_saved(tmp_path)
    baseline = sink.lookup_baseline(intake, selection_generation="selected.v1")
    assert baseline is not None
    (tmp_path / "selected/saved.md").write_text("# Changed saved item\nchanged body\n")
    changed = _intake(tmp_path / "selected")
    assert sink.lookup_baseline(changed, selection_generation="selected.v1") is None
    successor = sink.submit(changed)
    assert successor.source_receipt is not None
    assert successor.source_receipt.source_id == baseline.result.source_id
    assert successor.source_receipt.capture_id != baseline.result.capture_id
    with closing(engine._store.connect()) as connection:
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 2
        assert (
            connection.execute(
                "SELECT revision_key FROM source_revisions WHERE capture_id=?",
                (baseline.result.capture_id,),
            ).fetchone()[0]
            is None
        )
    with pytest.raises(T03Error, match="revision_changed"):
        sink.lookup_baseline(intake, selection_generation="selected.v1")

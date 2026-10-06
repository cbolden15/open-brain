"""Real Saved reconstruction preserves historical coordinates after root renumbering."""

from contextlib import closing
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.engine import BrainEngine
from open_brain_engine.engine.historical_contracts import HistoricalDestination
from open_brain_engine.engine.historical_root_checkpoint import HistoricalRootContinuity
from open_brain_engine.engine.source_intake import SourceRevisionObservedDelivery
from open_brain_engine.engine.t03_contracts import EffectiveAuthority, T03Error

from open_brain.profile import compile_single_user_local
from open_brain_collector.historical_checkpoint import collector_historical_checkpoint_sink
from open_brain_collector.runner import collector_revision_sink
from open_brain_connectors.runtime.source_intake import SourceRecordIntake
from packages.collector.tests.integration.test_saved_markdown import _intake, _root
from packages.collector.tests.integration.test_saved_markdown_historical import _adopt_saved_intake


def _fixture(
    tmp_path: Path,
) -> tuple[
    BrainEngine,
    SourceRecordIntake,
    SourceRevisionObservedDelivery,
    EffectiveAuthority,
    HistoricalRootContinuity,
]:
    intake = _intake(_root(tmp_path))
    profile = compile_single_user_local(tmp_path / "brain")
    engine = BrainEngine.open(profile)
    ordinary = collector_revision_sink(profile.root, tmp_path / "revisions")
    with closing(engine._store.connect()) as connection:
        brain_id, epoch = connection.execute(
            "SELECT brain_id,issuer_epoch FROM brain_identity"
        ).fetchone()
    device, inode = profile.root_identity
    continuity = HistoricalRootContinuity(
        profile.root,
        (device + 1, inode),
        (device, inode),
        profile.tenant_id,
        HistoricalDestination(brain_id=brain_id, issuer_epoch=epoch),
    )
    observed = _adopt_saved_intake(
        engine,
        ordinary,
        intake,
        root_fingerprint=continuity.fingerprint(continuity.previous_root_identity),
        context=ordinary._capture_sink.context,
    )
    owner = EffectiveAuthority(profile.owner_actor_id, "session", frozenset(), None, owner=True)
    return engine, intake, observed, owner, continuity


def test_actual_saved_historical_checkpoint_never_opens_ordinary_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, intake, observed, owner, continuity = _fixture(tmp_path)
    with closing(engine._store.connect()) as connection:
        before = tuple(connection.iterdump())
    monkeypatch.setattr(BrainEngine, "open", lambda *a, **k: pytest.fail("ordinary startup replay"))
    sink = collector_historical_checkpoint_sink(
        engine.profile.root, continuity, authority=owner, validate_continuity=lambda: None
    )
    assert not hasattr(sink, "submit") and not hasattr(sink, "_capture_sink")
    assert sink.brain_binding == continuity.fingerprint(continuity.current_root_identity)
    baseline = sink.lookup_baseline(intake, selection_generation="selection")
    assert baseline is not None and baseline.delivery.custody_bytes() == observed.custody_bytes()
    assert baseline.result.root_identity == continuity.current_root_identity
    with sink.page_checkpoint(
        ((intake, baseline.result.capture_id),), selection_generation="selection"
    ) as terminal:
        assert terminal == (baseline,)
    with closing(engine._store.connect()) as connection:
        assert tuple(connection.iterdump()) == before


@pytest.mark.parametrize("change", ["raw", "title", "normalization", "privacy", "namespace"])
def test_changed_saved_content_cannot_become_a_checkpoint_or_capture(
    tmp_path: Path,
    change: str,
) -> None:
    engine, intake, _, owner, continuity = _fixture(tmp_path)
    sink = collector_historical_checkpoint_sink(
        engine.profile.root, continuity, authority=owner, validate_continuity=lambda: None
    )
    baseline = sink.lookup_baseline(intake, selection_generation="selection")
    assert baseline is not None and intake.observation is not None
    if change == "raw":
        changed = replace(intake, observation=replace(intake.observation, original_sha256="0" * 64))
    elif change == "title":
        changed = replace(intake, title="changed")
    elif change == "normalization":
        changed = replace(
            intake, observation=replace(intake.observation, normalization_version="v3")
        )
    elif change == "privacy":
        privacy = replace(intake.privacy, policy_version="changed")
        changed = replace(
            intake,
            privacy=privacy,
            observation=replace(
                intake.observation,
                privacy_policy_version=privacy.policy_version,
                privacy_policy_sha256=sha256(
                    portable_canonical_json_bytes(privacy.to_dict())
                ).hexdigest(),
            ),
        )
    else:
        changed = replace(intake, key=replace(intake.key, external_id="unknown"))
    if change == "namespace":
        with pytest.raises(T03Error, match="revision_changed"):
            sink.lookup_baseline(changed, selection_generation="selection")
    else:
        assert sink.lookup_baseline(changed, selection_generation="selection") is None
    with (
        pytest.raises(T03Error, match="revision_changed"),
        sink.page_checkpoint(
            ((changed, baseline.result.capture_id),), selection_generation="selection"
        ),
    ):
        pytest.fail("changed content reached checkpoint persistence")
    assert not hasattr(sink, "submit")


def test_empty_saved_scan_page_still_uses_admission_and_writer_fence(tmp_path: Path) -> None:
    engine, _, _, owner, continuity = _fixture(tmp_path)
    validations: list[bool] = []
    sink = collector_historical_checkpoint_sink(
        engine.profile.root,
        continuity,
        authority=owner,
        validate_continuity=lambda: validations.append(True),
    )
    with sink.page_checkpoint((), selection_generation="selection") as terminal:
        assert terminal == ()
    assert len(validations) >= 5
    from open_brain_engine.storage.locks import LockBusyError

    with (
        engine._writer_lease.acquire_shared_writer(),
        pytest.raises(LockBusyError),
        sink.page_checkpoint((), selection_generation="selection"),
    ):
        pytest.fail("empty page crossed an existing canonical writer")

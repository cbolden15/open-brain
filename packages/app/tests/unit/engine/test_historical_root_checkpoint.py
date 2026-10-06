"""Device continuity grants checkpointing, never startup replay or new capture."""

from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from open_brain_engine.engine import BrainEngine, PublicJobCaptureSink
from open_brain_engine.engine.historical_contracts import HistoricalBaselineRequest
from open_brain_engine.engine.historical_recovery import _historical_transaction
from open_brain_engine.engine.historical_root_checkpoint import (
    HistoricalRootCheckpointHost,
    HistoricalRootContinuity,
)
from open_brain_engine.engine.historical_tasks import adopt_historical_baseline
from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission
from open_brain_engine.engine.sharing_contracts import SharingError
from open_brain_engine.engine.source_intake import SourceRevisionBinding
from open_brain_engine.engine.t03_contracts import EffectiveAuthority, T03Error
from open_brain_engine.storage.locks import LockBusyError

from open_brain.profile import compile_single_user_local
from packages.app.tests.unit.engine.test_historical_baseline import _baseline
from packages.app.tests.unit.engine.test_historical_continuity import _successor


def _fixture(
    tmp_path: Path,
) -> tuple[
    BrainEngine,
    HistoricalBaselineRequest,
    EffectiveAuthority,
    HistoricalRootContinuity,
    SourceRevisionBinding,
]:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    request = _baseline(engine)
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )
    device, inode = engine.profile.root_identity
    continuity = HistoricalRootContinuity(
        engine.profile.root,
        (device + 1, inode),
        (device, inode),
        engine.profile.tenant_id,
        request.destination,
    )
    old_binding = replace(
        request.observed_delivery.binding,
        root_fingerprint=continuity.fingerprint(continuity.previous_root_identity),
    )
    request = replace(
        request,
        observed_delivery=replace(
            request.observed_delivery,
            binding=old_binding,
        ),
    )
    with exclusive_runtime_admission(engine.profile) as admission:
        adopt_historical_baseline(
            engine.profile,
            request,
            authority=owner,
            admission=admission,
            validate_before_write=lambda: None,
        )
    current_binding = replace(
        old_binding, root_fingerprint=continuity.fingerprint(engine.profile.root_identity)
    )
    return engine, request, owner, continuity, current_binding


def test_checkpoint_preserves_history_and_sql_without_engine_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, request, owner, continuity, binding = _fixture(tmp_path)
    with closing(engine._store.connect()) as connection:
        before = tuple(connection.iterdump())
    historical = engine.profile.root / ".open-brain/historical-authority"
    files = {
        p.relative_to(historical): p.read_bytes() for p in historical.rglob("*") if p.is_file()
    }
    monkeypatch.setattr(BrainEngine, "open", lambda *a, **k: pytest.fail("startup recovery"))
    validations: list[bool] = []
    host = HistoricalRootCheckpointHost(
        engine.profile,
        continuity,
        authority=owner,
        validate_continuity=lambda: validations.append(True),
    )
    capability = host.capability(binding)
    assert capability.binding == request.observed_delivery.binding
    assert binding.root_fingerprint != capability.binding.root_fingerprint
    assert capability.baseline_template() is not None
    witness = capability.lookup_baseline(
        request.observed_delivery, selection_generation="selection"
    )
    assert witness is not None and witness.root_identity == engine.profile.root_identity
    with capability.revision_page_checkpoint(
        ((request.observed_delivery, witness),), selection_generation="selection"
    ):
        assert witness.capture_id == request.retained_original.capture_id
    assert len(validations) >= 8
    assert not hasattr(capability, "submit") and not hasattr(host, "capture")
    with closing(engine._store.connect()) as connection:
        assert tuple(connection.iterdump()) == before
    assert {
        p.relative_to(historical): p.read_bytes() for p in historical.rglob("*") if p.is_file()
    } == files
    # The ordinary current-bound capability still refuses immutable old history.
    with pytest.raises(T03Error, match="revision_changed"):
        engine.sources.public_revision_sink(binding).baseline_template()
    assert (
        PublicJobCaptureSink.fingerprint_for(
            str(engine.profile.root), engine.profile.root_identity, engine.profile.tenant_id
        )
        == binding.root_fingerprint
    )


@pytest.mark.parametrize("changed", ["path", "inode", "tenant", "destination", "epoch"])
def test_incompatible_root_or_logical_identity_is_refused(tmp_path: Path, changed: str) -> None:
    engine, _, owner, continuity, _ = _fixture(tmp_path)
    if changed == "path":
        continuity = replace(continuity, root=tmp_path)
    elif changed == "inode":
        continuity = replace(continuity, previous_root_identity=(0, 1))
    elif changed == "tenant":
        continuity = replace(continuity, tenant_id="other")
    elif changed == "destination":
        continuity = replace(
            continuity,
            destination=replace(continuity.destination, brain_id="brn_aaaaaaaaabaabaaaaaaaaaaaai"),
        )
    else:
        continuity = replace(
            continuity,
            destination=replace(
                continuity.destination, issuer_epoch=continuity.destination.issuer_epoch + 1
            ),
        )
    with pytest.raises((T03Error, SharingError)):
        HistoricalRootCheckpointHost(
            engine.profile, continuity, authority=owner, validate_continuity=lambda: None
        )


def test_owner_and_fresh_external_continuity_are_required(tmp_path: Path) -> None:
    engine, request, owner, continuity, binding = _fixture(tmp_path)
    with pytest.raises(SharingError, match="unsupported_capability"):
        HistoricalRootCheckpointHost(
            engine.profile,
            continuity,
            authority=replace(owner, owner=False),
            validate_continuity=lambda: None,
        )
    admitted = True

    def validate() -> None:
        if not admitted:
            raise T03Error("operation_pending")

    host = HistoricalRootCheckpointHost(
        engine.profile, continuity, authority=owner, validate_continuity=validate
    )
    capability = host.capability(binding)
    witness = capability.lookup_baseline(
        request.observed_delivery, selection_generation="selection"
    )
    assert witness is not None
    admitted = False
    with pytest.raises(T03Error, match="operation_pending"):
        capability.baseline_template()
    with pytest.raises(T03Error, match="operation_pending"):
        capability.lookup_baseline(request.observed_delivery, selection_generation="selection")
    with (
        pytest.raises(T03Error, match="operation_pending"),
        capability.revision_page_checkpoint(
            ((request.observed_delivery, witness),), selection_generation="selection"
        ),
    ):
        pytest.fail("checkpoint reached after continuity withdrawal")


def test_checkpoint_revalidates_current_cas_and_complete_content(tmp_path: Path) -> None:
    engine, request, owner, continuity, binding = _fixture(tmp_path)
    host = HistoricalRootCheckpointHost(
        engine.profile, continuity, authority=owner, validate_continuity=lambda: None
    )
    capability = host.capability(binding)
    witness = capability.lookup_baseline(
        request.observed_delivery, selection_generation="selection"
    )
    assert witness is not None
    altered = replace(
        request.observed_delivery,
        observation=replace(
            request.observed_delivery.observation,
            original_sha256="0" * 64,
        ),
    )
    assert capability.lookup_baseline(altered, selection_generation="selection") is None
    engine.sources.public_revision_sink(request.observed_delivery.binding).submit(
        _successor(request)
    )
    with pytest.raises(T03Error, match="revision_changed"):
        capability.lookup_baseline(request.observed_delivery, selection_generation="selection")
    with (
        pytest.raises(T03Error, match="revision_changed"),
        capability.revision_page_checkpoint(
            ((request.observed_delivery, witness),), selection_generation="selection"
        ),
    ):
        pytest.fail("checkpoint reached after head change")


@pytest.mark.parametrize("field", ["route", "lifecycle", "availability", "historical", "control"])
def test_current_cas_denial_refuses_the_narrow_checkpoint(tmp_path: Path, field: str) -> None:
    engine, request, owner, continuity, binding = _fixture(tmp_path)
    host = HistoricalRootCheckpointHost(
        engine.profile, continuity, authority=owner, validate_continuity=lambda: None
    )
    capability = host.capability(binding)
    witness = capability.lookup_baseline(
        request.observed_delivery, selection_generation="selection"
    )
    assert witness is not None
    with _historical_transaction(engine.profile, lambda: None) as connection:
        statement = {
            "route": "UPDATE logical_sources SET route_version=route_version+1",
            "lifecycle": "UPDATE source_lifecycle_state SET lifecycle_version=lifecycle_version+1",
            "availability": "UPDATE logical_sources SET availability='missing'",
            "historical": "UPDATE logical_sources SET historical_only=1",
            "control": "UPDATE engine_generations SET control_epoch=control_epoch+1",
        }[field]
        connection.execute(statement)
    with pytest.raises(T03Error, match="revision_changed"):
        capability.lookup_baseline(request.observed_delivery, selection_generation="selection")
    with (
        pytest.raises(T03Error, match="revision_changed"),
        capability.revision_page_checkpoint(
            ((request.observed_delivery, witness),), selection_generation="selection"
        ),
    ):
        pytest.fail("checkpoint reached after current CAS denial")


def test_narrow_checkpoint_uses_the_ordinary_writer_fence(tmp_path: Path) -> None:
    engine, request, owner, continuity, binding = _fixture(tmp_path)
    host = HistoricalRootCheckpointHost(
        engine.profile, continuity, authority=owner, validate_continuity=lambda: None
    )
    capability = host.capability(binding)
    witness = capability.lookup_baseline(
        request.observed_delivery, selection_generation="selection"
    )
    assert witness is not None
    with (
        engine._writer_lease.acquire_shared_writer(),
        pytest.raises(LockBusyError),
        capability.revision_page_checkpoint(
            ((request.observed_delivery, witness),), selection_generation="selection"
        ),
    ):
        pytest.fail("checkpoint crossed an existing canonical writer")


def test_v2_import_history_keeps_original_bytes_and_current_root_witness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from packages.app.tests.unit.engine._historical_compatibility_fixtures import (
        baseline_v2,
        portable5_import_engine,
    )

    engine = portable5_import_engine(tmp_path, monkeypatch)
    request = baseline_v2(engine)
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )
    device, inode = engine.profile.root_identity
    continuity = HistoricalRootContinuity(
        engine.profile.root,
        (device + 1, inode),
        (device, inode),
        engine.profile.tenant_id,
        request.destination,
    )
    request = replace(
        request,
        observed_delivery=replace(
            request.observed_delivery,
            binding=replace(
                request.observed_delivery.binding,
                root_fingerprint=continuity.fingerprint(continuity.previous_root_identity),
            ),
        ),
    )
    with exclusive_runtime_admission(engine.profile) as admission:
        adopt_historical_baseline(
            engine.profile,
            request,
            authority=owner,
            admission=admission,
            validate_before_write=lambda: None,
        )
    binding = replace(
        request.observed_delivery.binding,
        root_fingerprint=continuity.fingerprint(continuity.current_root_identity),
    )
    host = HistoricalRootCheckpointHost(
        engine.profile, continuity, authority=owner, validate_continuity=lambda: None
    )
    capability = host.capability(binding)
    witness = capability.lookup_baseline(
        request.observed_delivery, selection_generation="selection"
    )
    assert witness is not None and witness.receipt.dto_version == 2
    assert witness.root_identity == continuity.current_root_identity
    with capability.revision_page_checkpoint(
        ((request.observed_delivery, witness),), selection_generation="selection"
    ):
        assert capability.binding == request.observed_delivery.binding

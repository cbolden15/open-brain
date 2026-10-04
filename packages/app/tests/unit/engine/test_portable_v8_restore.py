"""Actual clean imports preserve historical authority, replay and owner history."""

import json
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import (
    BrainEngine,
    InjectedFault,
    PortabilityFault,
    TextPayload,
    portable_v8_restore,
)
from open_brain_engine.engine.historical_contracts import HistoricalBaselineRequest
from open_brain_engine.engine.historical_recovery import _historical_transaction
from open_brain_engine.engine.historical_tasks import revoke_historical_copy
from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission
from open_brain_engine.engine.sharing_contracts import SharingError
from open_brain_engine.engine.source_lifecycle_contracts import SourceWithdrawRequest
from open_brain_engine.engine.t03_contracts import EffectiveAuthority, RecordReadRequest, T03Error
from open_brain_engine.portable.v1 import PortableSnapshot
from open_brain_engine.portable.v8 import HISTORICAL_AUTHORITY_PATH, validate_historical_authority
from open_brain_engine.portable.versioned import validated_portable_snapshot
from open_brain_engine.storage.filesystem import RootIdentity

from open_brain.profile import compile_single_user_local
from packages.app.tests.unit.engine.test_historical_continuity import _adopt, _successor
from packages.app.tests.unit.engine.test_historical_revocation import _request as _revocation
from packages.app.tests.unit.engine.test_paging import external_authority
from packages.app.tests.unit.engine.test_portable_v8_historical import _linked


@pytest.mark.parametrize(
    "state", ["empty", "baseline", "successor", "linked", "revoked", "withdrawn", "advanced"]
)
def test_actual_clean_restore_reexport_retains_authority_and_history(
    tmp_path: Path, state: str
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )
    baseline = None
    successor = None
    relation = None
    if state in ("baseline", "successor"):
        baseline = _adopt(engine)
        if state == "successor":
            successor = _successor(baseline)
            engine.sources.public_revision_sink(successor.binding).submit(successor)
    elif state != "empty":
        relation = _linked(engine)
        if state == "revoked":
            with exclusive_runtime_admission(engine.profile) as admission:
                revoke_historical_copy(
                    engine.profile,
                    _revocation(engine, relation),
                    authority=owner,
                    admission=admission,
                    validate_before_write=lambda: None,
                )
        elif state == "withdrawn":
            engine.sources.withdraw(
                SourceWithdrawRequest(
                    operation_id="withdraw.restore",
                    source_id=relation.source_cas.source_id,
                    expected_head=relation.source_cas.expected_head,
                    expected_lifecycle_version=relation.source_cas.expected_lifecycle_version,
                    brain_id=relation.destination.brain_id,
                    issuer_epoch=relation.destination.issuer_epoch,
                    reason_code="synthetic_withdrawal",
                ),
                authority=owner,
            )
    source = tmp_path / "export"
    engine.portability.export(source, export_id="export_" + str(uuid4()))
    original = validated_portable_snapshot(source)
    if state == "advanced":
        retained_baseline = validate_historical_authority(original.files).records[0].request
        assert isinstance(retained_baseline, HistoricalBaselineRequest)
        baseline = retained_baseline
        successor = _successor(baseline)
        engine.sources.public_revision_sink(successor.binding).submit(successor)
        source = tmp_path / "advanced-export"
        engine.portability.export(source, export_id="export_" + str(uuid4()))
        original = validated_portable_snapshot(source)
    destination = tmp_path / "restored"
    import_id = "import_" + str(uuid4())
    result = engine.portability.import_clean(source, destination, import_id=import_id)
    assert result.schema_version == 8 and not result.duplicate
    assert engine.portability.import_clean(source, destination, import_id=import_id).duplicate
    assert validated_portable_snapshot(destination).files == original.files
    restored = BrainEngine.open(compile_single_user_local(destination))
    if state != "empty":
        history = validate_historical_authority(original.files)
        retained_baseline = history.records[0].request
        assert isinstance(retained_baseline, HistoricalBaselineRequest)
        replay = restored.capture.accept(
            TextPayload("synthetic retained"), delivery_id="owner.original"
        )
        assert replay.duplicate
        assert replay.capture_id == retained_baseline.retained_original.capture_id
    with closing(engine._store.connect()) as before, closing(restored._store.connect()) as after:
        for table in (
            "historical_registry_state",
            "historical_operations",
            "historical_claims",
            "historical_baselines",
            "historical_relations",
            "historical_revocations",
        ):
            assert [tuple(row) for row in before.execute(f"SELECT * FROM {table} ORDER BY 1")] == [
                tuple(row) for row in after.execute(f"SELECT * FROM {table} ORDER BY 1")
            ]
    if successor is not None:
        assert baseline is not None
        source_replay = restored.sources.public_revision_sink(successor.binding).submit(successor)
        assert source_replay.source_receipt is not None
        assert source_replay.source_receipt.source_id == baseline.source_cas.source_id
    if relation is not None:
        read = RecordReadRequest(
            record_id=relation.retained_copy.capture_id,
            expected_revision_id=relation.retained_copy.capture_id,
        )
        reader = replace(
            external_authority(restored),
            provider_id="openai",
            allowed_read_tiers=frozenset({PrivacyTier.PUBLIC}),
        )
        for current in (restored, BrainEngine.open(restored.profile)):
            current.history.read_history(read, authority=owner)
            if state != "linked":
                with pytest.raises(T03Error, match="not_found"):
                    current.retrieval.read_record(read, authority=reader)
            assert current.portability.rebuild_index().schema_version == 8
    assert restored.portability.rebuild_index().schema_version == 8
    reexport = tmp_path / "reexport"
    restored.portability.export(reexport, export_id="export_" + str(uuid4()))
    latest = validated_portable_snapshot(reexport)
    assert {
        path: data for path, data in latest.files.items() if path != "portable-manifest.json"
    } == {path: data for path, data in original.files.items() if path != "portable-manifest.json"}
    assert b"consent" not in latest.files[HISTORICAL_AUTHORITY_PATH]
    assert not list((destination / ".open-brain").rglob("*provider*consent*"))
    assert json.loads(latest.files[HISTORICAL_AUTHORITY_PATH])["schema_version"] == 1


@pytest.mark.parametrize(
    "stage",
    [
        "historical_intent_restored",
        "historical_registry_advanced",
        "historical_sql_committed",
        "historical_completed",
        "historical_audited",
    ],
)
def test_interrupted_hidden_restore_never_promotes_and_retries_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    _linked(engine)
    source = tmp_path / "export"
    engine.portability.export(source, export_id="export_" + str(uuid4()))
    original = validated_portable_snapshot(source)
    destination = tmp_path / "restored"
    import_id = "import_" + str(uuid4())
    restore = portable_v8_restore.restore_portable_v8_root

    def checkpoint(current: str) -> None:
        if current == stage:
            raise RuntimeError("synthetic interrupted restore")

    def fail_restore(
        root: Path, *, snapshot: PortableSnapshot, expected_root_identity: RootIdentity
    ) -> object:
        return restore(
            root,
            snapshot=snapshot,
            expected_root_identity=expected_root_identity,
            checkpoint=checkpoint,
        )

    with monkeypatch.context() as fault:
        fault.setattr(portable_v8_restore, "restore_portable_v8_root", fail_restore)
        with pytest.raises(RuntimeError, match="synthetic interrupted restore"):
            engine.portability.import_clean(source, destination, import_id=import_id)
    assert not destination.exists()
    assert validated_portable_snapshot(source).files == original.files
    result = engine.portability.import_clean(source, destination, import_id=import_id)
    assert not result.duplicate
    assert engine.portability.import_clean(source, destination, import_id=import_id).duplicate
    assert validate_historical_authority(validated_portable_snapshot(destination).files) == (
        validate_historical_authority(original.files)
    )


def test_lost_response_after_promotion_returns_duplicate_without_reinstall(tmp_path: Path) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    engine = BrainEngine.open(profile)
    _linked(engine)
    source = tmp_path / "export"
    engine.portability.export(source, export_id="export_" + str(uuid4()))
    destination = tmp_path / "restored"
    import_id = "import_" + str(uuid4())
    faulted = BrainEngine.open(profile, faults={PortabilityFault.AFTER_PROMOTION})
    with pytest.raises(InjectedFault):
        faulted.portability.import_clean(source, destination, import_id=import_id)
    assert destination.is_dir()
    with closing(
        BrainEngine.open(compile_single_user_local(destination))._store.connect()
    ) as connection:
        before = [tuple(row) for row in connection.execute("SELECT * FROM historical_operations")]
    assert engine.portability.import_clean(source, destination, import_id=import_id).duplicate
    with closing(
        BrainEngine.open(compile_single_user_local(destination))._store.connect()
    ) as connection:
        assert [
            tuple(row) for row in connection.execute("SELECT * FROM historical_operations")
        ] == before


@pytest.mark.parametrize("damage", ["registry", "transition", "fence", "relation_projection"])
def test_retry_detects_authority_damage_before_engine_recovery_without_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    relation = _linked(engine)
    source = tmp_path / "export"
    engine.portability.export(source, export_id="export_" + str(uuid4()))
    destination = tmp_path / "restored"
    import_id = "import_" + str(uuid4())
    engine.portability.import_clean(source, destination, import_id=import_id)
    restored = BrainEngine.open(compile_single_user_local(destination))
    authority_root = destination / ".open-brain/historical-authority"
    if damage == "relation_projection":
        with _historical_transaction(restored.profile, lambda: None) as connection:
            trigger = connection.execute(
                "SELECT sql FROM sqlite_master WHERE name='historical_relations_delete_immutable'"
            ).fetchone()[0]
            connection.execute("DROP TRIGGER historical_relations_delete_immutable")
            connection.execute("DELETE FROM historical_relations")
            connection.execute(trigger)
    else:
        if damage == "registry":
            path = authority_root / "historical-claims.v1.json"
        elif damage == "fence":
            path = authority_root / "historical-fence.v1.json"
        else:
            path = next((authority_root / "historical-transitions.v1").glob("*.json"))
        path.rename(path.with_suffix(".retained"))
    before = validated_portable_snapshot(destination)

    def forbidden_open(*_args: object, **_kwargs: object) -> BrainEngine:
        raise AssertionError("ordinary engine recovery must not precede archive-bound audit")

    with monkeypatch.context() as fault:
        fault.setattr(BrainEngine, "open", forbidden_open)
        with pytest.raises(SharingError, match="binding_mismatch"):
            engine.portability.import_clean(source, destination, import_id=import_id)
    assert validated_portable_snapshot(destination).files == before.files
    read = RecordReadRequest(
        record_id=relation.retained_copy.capture_id,
        expected_revision_id=relation.retained_copy.capture_id,
    )
    reader = replace(
        external_authority(restored),
        provider_id="openai",
        allowed_read_tiers=frozenset({PrivacyTier.PUBLIC}),
    )
    with pytest.raises(T03Error, match="not_found"):
        restored.retrieval.read_record(read, authority=reader)
    owner = EffectiveAuthority(
        restored.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )
    restored.history.read_history(read, authority=owner)
    with closing(restored._store.connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM captures").fetchone()[0] == 2
        if damage == "relation_projection":
            assert (
                connection.execute("SELECT COUNT(*) FROM historical_relations").fetchone()[0] == 0
            )

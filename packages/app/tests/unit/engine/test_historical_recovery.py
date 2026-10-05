"""Only current owner/exclusive admission can finish exact pending history."""

from pathlib import Path

import pytest
from open_brain_engine.engine import BrainEngine
from open_brain_engine.engine.consent_contracts import EgressMode
from open_brain_engine.engine.historical_fence import HistoricalPendingFence
from open_brain_engine.engine.historical_projection import verify_historical_projection
from open_brain_engine.engine.historical_recovery import (
    _historical_transaction,
    recover_historical_pending,
    recover_historical_profile,
)
from open_brain_engine.engine.historical_registry import HistoricalRegistryStore
from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission
from open_brain_engine.engine.sharing_contracts import SharingError
from open_brain_engine.engine.t03_contracts import EffectiveAuthority, T03Error
from open_brain_engine.storage.sqlite import SchemaError

from open_brain.profile import compile_single_user_local
from packages.app.tests.unit.engine.test_historical_migration import _schema_twelve
from packages.app.tests.unit.engine.test_historical_projection import _claim_transition


def test_profile_recovery_refuses_old_state_before_writable_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _schema_twelve(tmp_path, monkeypatch)
    owner = EffectiveAuthority("synthetic-owner", "session", frozenset(), None, owner=True)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("writable connection invoked for old state")

    monkeypatch.setattr("open_brain_engine.engine.historical_recovery.connect_database", forbidden)
    with (
        exclusive_runtime_admission(profile) as admission,
        pytest.raises(SchemaError, match="schema13 or schema14"),
    ):
        recover_historical_profile(
            profile, authority=owner, admission=admission, validate_before_write=lambda: None
        )
    assert not (profile.root / ".open-brain" / "historical-authority").exists()


def test_profile_recovery_refuses_nonowner_before_validation_or_storage(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    record = _claim_transition(engine)
    fence = HistoricalPendingFence(engine.profile.root, engine.profile.root_identity)
    fence.prepare(record)
    scoped = EffectiveAuthority("synthetic-agent", "session", frozenset({"admin"}), None)

    def forbidden() -> None:
        raise AssertionError("nonowner reached owner mutation validation")

    with (
        exclusive_runtime_admission(engine.profile) as admission,
        pytest.raises(SharingError, match="unsupported_capability"),
    ):
        recover_historical_profile(
            engine.profile, authority=scoped, admission=admission, validate_before_write=forbidden
        )
    assert fence.pending() == record


@pytest.mark.parametrize(
    "stage",
    [
        "historical_recovery_preflight",
        "historical_registry_advanced",
        "historical_sql_projected",
        "historical_sql_committed",
        "historical_recovery_complete",
    ],
)
def test_profile_recovery_never_bootstraps_or_runs_ordinary_writer_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    record = _claim_transition(engine)
    profile = engine.profile
    fence = HistoricalPendingFence(profile.root, profile.root_identity)
    fence.prepare(record)
    owner = EffectiveAuthority("synthetic-owner", "session", frozenset(), None, owner=True)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("ordinary startup or writer hook invoked")

    monkeypatch.setattr(BrainEngine, "open", forbidden)
    monkeypatch.setattr("open_brain_engine.engine.local_store.open_local_database", forbidden)
    for hook in (
        "publish_source_metadata",
        "register_completed_captures",
        "register_publication_members",
    ):
        monkeypatch.setattr(f"open_brain_engine.engine.source_store.{hook}", forbidden)

    def crash(observed: str) -> None:
        if observed == stage:
            raise RuntimeError("synthetic profile interruption")

    with (
        exclusive_runtime_admission(profile) as admission,
        pytest.raises(RuntimeError, match="synthetic profile interruption"),
    ):
        recover_historical_profile(
            profile,
            authority=owner,
            admission=admission,
            validate_before_write=lambda: None,
            checkpoint=crash,
        )
    with exclusive_runtime_admission(profile) as admission:
        result = recover_historical_profile(
            profile, authority=owner, admission=admission, validate_before_write=lambda: None
        )
        assert result == (None if stage == "historical_recovery_complete" else record.receipt)
        assert (
            recover_historical_profile(
                profile, authority=owner, admission=admission, validate_before_write=lambda: None
            )
            is None
        )
    from open_brain_engine.engine.local_schema import open_local_database_read_only

    connection = open_local_database_read_only(profile)
    try:
        assert verify_historical_projection(connection, profile, record.proposed) == (record,)
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM source_intakes").fetchone()[0] == 0
    finally:
        connection.close()


@pytest.mark.parametrize(
    "revocation_stage",
    [
        "historical_recovery_preflight",
        "historical_registry_advanced",
        "historical_sql_projected",
        "historical_sql_committed",
    ],
)
def test_profile_recovery_revalidates_authority_before_writes_and_commit(
    tmp_path: Path, revocation_stage: str
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    record = _claim_transition(engine)
    fence = HistoricalPendingFence(engine.profile.root, engine.profile.root_identity)
    fence.prepare(record)
    owner = EffectiveAuthority("synthetic-owner", "session", frozenset(), None, owner=True)
    revoked = False

    def validate() -> None:
        if revoked:
            raise SharingError("operation_pending")

    def revoke(stage: str) -> None:
        nonlocal revoked
        if stage == revocation_stage:
            revoked = True

    with (
        exclusive_runtime_admission(engine.profile) as admission,
        pytest.raises(SharingError, match="operation_pending"),
    ):
        recover_historical_profile(
            engine.profile,
            authority=owner,
            admission=admission,
            validate_before_write=validate,
            checkpoint=revoke,
        )
    assert fence.pending() == record
    assert HistoricalRegistryStore(engine.profile.root, engine.profile.root_identity).read(
        record.request.destination
    ) == (
        record.previous if revocation_stage == "historical_recovery_preflight" else record.proposed
    )
    # Revocation never completes a receipt. A later freshly validated owner
    # finishes only the retained transition, including after an SQL rollback.
    revoked = False
    with exclusive_runtime_admission(engine.profile) as admission:
        assert (
            recover_historical_profile(
                engine.profile, authority=owner, admission=admission, validate_before_write=validate
            )
            == record.receipt
        )
    fence.assert_settled(record.proposed)


@pytest.mark.parametrize(
    "stage",
    [
        "historical_recovery_preflight",
        "historical_registry_advanced",
        "historical_sql_projected",
        "historical_sql_committed",
        "historical_recovery_complete",
    ],
)
def test_forward_recovery_survives_each_boundary_without_recapture(
    tmp_path: Path, stage: str
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    record = _claim_transition(engine)
    fence = HistoricalPendingFence(engine.profile.root, engine.profile.root_identity)
    fence.prepare(record)  # Simulate an already admitted, durably retained intent.
    owner = EffectiveAuthority("synthetic-owner", "session", frozenset(), None, owner=True)

    def crash(observed: str) -> None:
        if observed == stage:
            raise RuntimeError("synthetic recovery interruption")

    with (
        exclusive_runtime_admission(engine.profile) as admission,
        pytest.raises(RuntimeError, match="synthetic recovery interruption"),
    ):
        recover_historical_pending(engine, authority=owner, admission=admission, checkpoint=crash)
    if stage != "historical_recovery_complete":
        with pytest.raises(SharingError, match="operation_pending"):
            BrainEngine.open(engine.profile)
    with exclusive_runtime_admission(engine.profile) as admission:
        result = recover_historical_profile(
            engine.profile, authority=owner, admission=admission, validate_before_write=lambda: None
        )
        assert result == (None if stage == "historical_recovery_complete" else record.receipt)
        assert recover_historical_pending(engine, authority=owner, admission=admission) is None
    reopened = BrainEngine.open(engine.profile)
    registry = HistoricalRegistryStore(engine.profile.root, engine.profile.root_identity).read(
        record.request.destination
    )
    assert registry == record.proposed
    fence.assert_settled(registry)
    with _historical_transaction(reopened.profile, lambda: None) as connection:
        assert verify_historical_projection(connection, reopened.profile, registry) == (record,)
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM source_intakes").fetchone()[0] == 0


def test_nonowner_recovery_refuses_without_changing_pending_intent(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    record = _claim_transition(engine)
    fence = HistoricalPendingFence(engine.profile.root, engine.profile.root_identity)
    fence.prepare(record)
    scoped = EffectiveAuthority("synthetic-agent", "session", frozenset({"admin"}), None)
    with (
        exclusive_runtime_admission(engine.profile) as admission,
        pytest.raises(SharingError, match="unsupported_capability"),
    ):
        recover_historical_pending(engine, authority=scoped, admission=admission)
    assert fence.pending() == record
    with _historical_transaction(engine.profile, lambda: None) as connection:
        assert verify_historical_projection(connection, engine.profile, record.previous) == ()


@pytest.mark.parametrize("damage", ["stale_cas", "missing_alias", "incomplete_capture"])
def test_recovery_refuses_changed_uncommitted_evidence_and_retains_fence(
    tmp_path: Path, damage: str
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    record = _claim_transition(engine)
    fence = HistoricalPendingFence(engine.profile.root, engine.profile.root_identity)
    fence.prepare(record)
    with _historical_transaction(engine.profile, lambda: None) as connection:
        if damage == "stale_cas":
            connection.execute("UPDATE logical_sources SET route_version=route_version+1")
        elif damage == "missing_alias":
            connection.execute("DELETE FROM source_aliases WHERE delivery_id='owner.original'")
        else:
            connection.execute("UPDATE captures SET stage=2")
    owner = EffectiveAuthority("synthetic-owner", "session", frozenset(), None, owner=True)
    with (
        exclusive_runtime_admission(engine.profile) as admission,
        pytest.raises(SharingError, match="revision_changed|binding_mismatch"),
    ):
        recover_historical_pending(engine, authority=owner, admission=admission)
    assert fence.pending() == record
    assert (
        HistoricalRegistryStore(engine.profile.root, engine.profile.root_identity).read(
            record.request.destination
        )
        == record.previous
    )


def test_recovery_refuses_expired_admission_and_external_owner(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    record = _claim_transition(engine)
    fence = HistoricalPendingFence(engine.profile.root, engine.profile.root_identity)
    fence.prepare(record)
    owner = EffectiveAuthority("synthetic-owner", "session", frozenset(), None, owner=True)
    with exclusive_runtime_admission(engine.profile) as admission:
        pass
    with pytest.raises(T03Error, match="operation_pending"):
        recover_historical_pending(engine, authority=owner, admission=admission)
    external = EffectiveAuthority(
        "synthetic-owner",
        "session",
        frozenset(),
        None,
        owner=True,
        egress_mode=EgressMode.EXTERNAL_PROVIDER,
        provider_id="openai",
        consent_id="consent_00000000000000000000000000000000",
        brain_id=record.request.destination.brain_id,
        issuer_epoch=record.request.destination.issuer_epoch,
    )
    with (
        exclusive_runtime_admission(engine.profile) as admission,
        pytest.raises(SharingError, match="unsupported_capability"),
    ):
        recover_historical_pending(engine, authority=external, admission=admission)
    assert fence.pending() == record

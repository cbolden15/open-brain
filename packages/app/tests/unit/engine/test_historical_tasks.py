"""Actual owner claim admission cannot manufacture content or publication."""

import json
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.engine import BrainEngine, TextPayload
from open_brain_engine.engine.historical_contracts import (
    HistoricalClaimRequest,
    HistoricalSourceCAS,
    RetainedCaptureEvidence,
)
from open_brain_engine.engine.historical_fence import HistoricalPendingFence
from open_brain_engine.engine.historical_projection import verify_historical_projection
from open_brain_engine.engine.historical_registry import HistoricalRegistryStore
from open_brain_engine.engine.historical_tasks import register_historical_claim
from open_brain_engine.engine.historical_transition import HistoricalTransitionStore
from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission
from open_brain_engine.engine.sharing_contracts import SharingError
from open_brain_engine.engine.source_lifecycle_contracts import SourceWithdrawRequest
from open_brain_engine.engine.t03_contracts import EffectiveAuthority

from open_brain.profile import compile_single_user_local
from packages.app.tests.unit.engine.test_historical_projection import _claim_transition


def _request(engine: BrainEngine) -> HistoricalClaimRequest:
    request = _claim_transition(engine).request
    assert isinstance(request, HistoricalClaimRequest)
    return request


def test_orphan_intent_before_pending_marker_retains_receipt_on_exact_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    request = _request(engine)
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )

    def crash(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic marker interruption")

    with (
        monkeypatch.context() as fault,
        exclusive_runtime_admission(engine.profile) as admission,
        pytest.raises(RuntimeError, match="synthetic marker interruption"),
    ):
        fault.setattr(HistoricalPendingFence, "_replace", crash)
        register_historical_claim(
            engine.profile,
            request,
            authority=owner,
            admission=admission,
            validate_before_write=lambda: None,
            clock=lambda: datetime(2026, 10, 3, tzinfo=UTC),
        )
    assert (
        HistoricalPendingFence(engine.profile.root, engine.profile.root_identity).pending() is None
    )
    orphan = HistoricalTransitionStore(engine.profile.root, engine.profile.root_identity).read(
        request.operation_id
    )
    with exclusive_runtime_admission(engine.profile) as admission:
        assert (
            register_historical_claim(
                engine.profile,
                request,
                authority=owner,
                admission=admission,
                validate_before_write=lambda: None,
                clock=lambda: datetime(2026, 10, 4, tzinfo=UTC),
            )
            == orphan.receipt
        )


def test_pending_claim_refuses_changed_bytes_and_competing_operation(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    request = _request(engine)
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )

    def crash(stage: str) -> None:
        if stage == "historical_claim_pending":
            raise RuntimeError("synthetic pending interruption")

    with exclusive_runtime_admission(engine.profile) as admission:
        with pytest.raises(RuntimeError, match="synthetic pending interruption"):
            register_historical_claim(
                engine.profile,
                request,
                authority=owner,
                admission=admission,
                validate_before_write=lambda: None,
                checkpoint=crash,
            )
        before = HistoricalPendingFence(engine.profile.root, engine.profile.root_identity).pending()
        with pytest.raises(SharingError, match="binding_mismatch"):
            register_historical_claim(
                engine.profile,
                replace(request, expected_claim_generation=1),
                authority=owner,
                admission=admission,
                validate_before_write=lambda: None,
            )
        with pytest.raises(SharingError, match="operation_pending"):
            register_historical_claim(
                engine.profile,
                replace(request, operation_id="claim.competing"),
                authority=owner,
                admission=admission,
                validate_before_write=lambda: None,
            )
        assert (
            HistoricalPendingFence(engine.profile.root, engine.profile.root_identity).pending()
            == before
        )
        register_historical_claim(
            engine.profile,
            request,
            authority=owner,
            admission=admission,
            validate_before_write=lambda: None,
        )


def test_owner_claim_admission_preserves_capture_and_returns_exact_receipt(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    request = _request(engine)
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )
    with closing(engine._store.connect()) as connection:
        before = tuple(connection.execute("SELECT * FROM captures").fetchone())
    with exclusive_runtime_admission(engine.profile) as admission:
        receipt = register_historical_claim(
            engine.profile,
            request,
            authority=owner,
            admission=admission,
            validate_before_write=lambda: None,
        )
        assert (
            register_historical_claim(
                engine.profile,
                request,
                authority=owner,
                admission=admission,
                validate_before_write=lambda: None,
            )
            == receipt
        )
    registry = HistoricalRegistryStore(engine.profile.root, engine.profile.root_identity).read(
        request.destination
    )
    HistoricalPendingFence(engine.profile.root, engine.profile.root_identity).assert_settled(
        registry
    )
    with closing(engine._store.connect()) as connection:
        assert tuple(connection.execute("SELECT * FROM captures").fetchone()) == before
        assert connection.execute("SELECT count(*) FROM source_intakes").fetchone()[0] == 0
        records = verify_historical_projection(connection, engine.profile, registry)
        assert records[0].receipt == receipt
        assert receipt.outcome == "denied_claim_recorded"


@pytest.mark.parametrize(
    "stage",
    [
        "historical_claim_validated",
        "historical_claim_pending",
        "historical_registry_advanced",
        "historical_sql_projected",
        "historical_sql_committed",
    ],
)
def test_owner_claim_admission_exact_retry_after_each_interruption(
    tmp_path: Path, stage: str
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    request = _request(engine)
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )

    def crash(observed: str) -> None:
        if stage == observed:
            raise RuntimeError("synthetic claim interruption")

    with (
        exclusive_runtime_admission(engine.profile) as admission,
        pytest.raises(RuntimeError, match="synthetic claim interruption"),
    ):
        register_historical_claim(
            engine.profile,
            request,
            authority=owner,
            admission=admission,
            validate_before_write=lambda: None,
            checkpoint=crash,
        )
    with exclusive_runtime_admission(engine.profile) as admission:
        receipt = register_historical_claim(
            engine.profile,
            request,
            authority=owner,
            admission=admission,
            validate_before_write=lambda: None,
        )
        assert receipt.claim_generation == 1
    with closing(engine._store.connect()) as connection:
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM historical_operations").fetchone()[0] == 1


def test_changed_same_operation_request_refuses_without_new_claim(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    request = _request(engine)
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )
    with exclusive_runtime_admission(engine.profile) as admission:
        register_historical_claim(
            engine.profile,
            request,
            authority=owner,
            admission=admission,
            validate_before_write=lambda: None,
        )
        with pytest.raises(SharingError, match="binding_mismatch"):
            register_historical_claim(
                engine.profile,
                replace(request, expected_claim_generation=1),
                authority=owner,
                admission=admission,
                validate_before_write=lambda: None,
            )


def test_nonowner_admin_cannot_admit_historical_claim(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    request = _request(engine)
    scoped = EffectiveAuthority("synthetic-agent", "session", frozenset({"admin"}), None)
    with (
        exclusive_runtime_admission(engine.profile) as admission,
        pytest.raises(SharingError, match="unsupported_capability"),
    ):
        register_historical_claim(
            engine.profile,
            request,
            authority=scoped,
            admission=admission,
            validate_before_write=lambda: None,
        )
    assert (
        HistoricalPendingFence(engine.profile.root, engine.profile.root_identity).pending() is None
    )


def _current_cas(engine: BrainEngine, source_id: str) -> HistoricalSourceCAS:
    with closing(engine._store.connect()) as connection:
        row = connection.execute(
            "SELECT s.*,l.lifecycle_version,g.control_epoch FROM logical_sources s "
            "JOIN source_lifecycle_state l USING(source_id) CROSS JOIN engine_generations g "
            "WHERE s.source_id=? AND g.singleton=1",
            (source_id,),
        ).fetchone()
    return HistoricalSourceCAS(
        source_id=source_id,
        expected_head=row["head_capture_id"],
        expected_head_version=row["head_version"],
        expected_route_version=row["route_version"],
        expected_lifecycle_version=row["lifecycle_version"],
        expected_control_epoch=row["control_epoch"],
        expected_lifecycle=row["lifecycle"],
        expected_availability=row["availability"],
        expected_historical_only=bool(row["historical_only"]),
    )


def _withdraw(
    engine: BrainEngine, request: HistoricalClaimRequest, owner: EffectiveAuthority
) -> None:
    engine.sources.withdraw(
        SourceWithdrawRequest(
            operation_id="withdraw.synthetic",
            source_id=request.source_cas.source_id,
            expected_head=request.source_cas.expected_head,
            expected_lifecycle_version=request.source_cas.expected_lifecycle_version,
            brain_id=request.destination.brain_id,
            issuer_epoch=request.destination.issuer_epoch,
            reason_code="synthetic_owner_withdrawal",
        ),
        authority=owner,
    )


@pytest.mark.parametrize("state", ["withdrawn", "unavailable", "historical_only"])
def test_denial_claim_admits_exact_inactive_witness_without_reactivation(
    tmp_path: Path, state: str
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    request = _request(engine)
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )
    if state == "withdrawn":
        _withdraw(engine, request, owner)
    else:
        with engine._store.transaction() as connection:
            if state == "unavailable":
                connection.execute("UPDATE logical_sources SET availability='missing'")
            else:
                connection.execute("UPDATE logical_sources SET historical_only=1")
    witness = _current_cas(engine, request.source_cas.source_id)
    with pytest.raises(SharingError, match="revision_changed"):
        witness.require_active()
    changed = replace(request, source_cas=witness, capture_source_cas=witness)
    with exclusive_runtime_admission(engine.profile) as admission:
        receipt = register_historical_claim(
            engine.profile,
            changed,
            authority=owner,
            admission=admission,
            validate_before_write=lambda: None,
        )
    assert receipt.outcome == "denied_claim_recorded"
    assert _current_cas(engine, witness.source_id) == witness
    with closing(engine._store.connect()) as connection:
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM historical_baselines").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM historical_relations").fetchone()[0] == 0


def test_exact_completed_claim_replay_after_withdrawal_returns_original_receipt(
    tmp_path: Path,
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    request = _request(engine)
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )
    with exclusive_runtime_admission(engine.profile) as admission:
        receipt = register_historical_claim(
            engine.profile,
            request,
            authority=owner,
            admission=admission,
            validate_before_write=lambda: None,
            clock=lambda: datetime(2026, 10, 3, tzinfo=UTC),
        )
    _withdraw(engine, request, owner)
    with exclusive_runtime_admission(engine.profile) as admission:
        assert (
            register_historical_claim(
                engine.profile,
                request,
                authority=owner,
                admission=admission,
                validate_before_write=lambda: None,
                clock=lambda: datetime(2026, 10, 4, tzinfo=UTC),
            )
            == receipt
        )
    assert _current_cas(engine, request.source_cas.source_id).expected_lifecycle == "retired"


def test_repeated_membership_new_operation_advances_once_and_retains_old_receipt(
    tmp_path: Path,
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    request = _request(engine)
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )
    with exclusive_runtime_admission(engine.profile) as admission:
        first = register_historical_claim(
            engine.profile,
            request,
            authority=owner,
            admission=admission,
            validate_before_write=lambda: None,
        )
        second = register_historical_claim(
            engine.profile,
            replace(request, operation_id="claim.confirm", expected_claim_generation=1),
            authority=owner,
            admission=admission,
            validate_before_write=lambda: None,
        )
        assert second.claim_generation == first.claim_generation + 1
        assert (
            register_historical_claim(
                engine.profile,
                request,
                authority=owner,
                admission=admission,
                validate_before_write=lambda: None,
            )
            == first
        )
    with closing(engine._store.connect()) as connection:
        assert connection.execute("SELECT count(*) FROM historical_operations").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM historical_claims").fetchone()[0] == 1


def test_copy_claim_preserves_two_sources_without_baseline_or_publication(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    original = _request(engine)
    copy = engine.capture.accept(
        TextPayload("synthetic copy discrepancy"), delivery_id="owner.copy"
    )
    with closing(engine._store.connect()) as connection:
        row = connection.execute(
            "SELECT r.source_id,r.source_sha256,r.request_sha256,c.privacy_json "
            "FROM source_revisions r JOIN captures c USING(capture_id) WHERE r.capture_id=?",
            (copy.capture_id,),
        ).fetchone()
        captures = [
            tuple(item) for item in connection.execute("SELECT * FROM captures ORDER BY capture_id")
        ]
        sources = [
            tuple(item)
            for item in connection.execute("SELECT * FROM logical_sources ORDER BY source_id")
        ]
        aliases = [
            tuple(item)
            for item in connection.execute("SELECT * FROM source_aliases ORDER BY delivery_id")
        ]
    assert row["source_id"] != original.source_cas.source_id
    request = HistoricalClaimRequest(
        operation_id="claim.copy",
        destination=original.destination,
        source_cas=_current_cas(engine, original.source_cas.source_id),
        capture_source_cas=_current_cas(engine, row["source_id"]),
        retained_capture=RetainedCaptureEvidence(
            capture_id=copy.capture_id,
            source_sha256=row["source_sha256"],
            retained_delivery_id="owner.copy",
            retained_request_sha256=row["request_sha256"],
            privacy_sha256=sha256(
                portable_canonical_json_bytes(json.loads(row["privacy_json"]))
            ).hexdigest(),
        ),
        claim_role="historical_copy",
        expected_claim_generation=0,
    )
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )
    with exclusive_runtime_admission(engine.profile) as admission:
        receipt = register_historical_claim(
            engine.profile,
            request,
            authority=owner,
            admission=admission,
            validate_before_write=lambda: None,
        )
    assert receipt.copy_capture_id == copy.capture_id
    with closing(engine._store.connect()) as connection:
        assert [
            tuple(item) for item in connection.execute("SELECT * FROM captures ORDER BY capture_id")
        ] == captures
        assert [
            tuple(item)
            for item in connection.execute("SELECT * FROM logical_sources ORDER BY source_id")
        ] == sources
        assert [
            tuple(item)
            for item in connection.execute("SELECT * FROM source_aliases ORDER BY delivery_id")
        ] == aliases
        assert connection.execute("SELECT count(*) FROM historical_baselines").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM historical_relations").fetchone()[0] == 0


@pytest.mark.parametrize("damage", ["generation", "source_cas", "privacy_digest", "destination"])
def test_changed_uncommitted_claim_refuses_without_pending_intent(
    tmp_path: Path, damage: str
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    request = _request(engine)
    if damage == "generation":
        request = replace(request, expected_claim_generation=1)
    elif damage == "source_cas":
        witness = replace(
            request.source_cas, expected_head_version=request.source_cas.expected_head_version + 1
        )
        request = replace(request, source_cas=witness, capture_source_cas=witness)
    elif damage == "privacy_digest":
        request = replace(
            request, retained_capture=replace(request.retained_capture, privacy_sha256="a" * 64)
        )
    else:
        request = replace(request, destination=replace(request.destination, issuer_epoch=2))
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )
    with (
        exclusive_runtime_admission(engine.profile) as admission,
        pytest.raises(SharingError, match="binding_mismatch|revision_changed"),
    ):
        register_historical_claim(
            engine.profile,
            request,
            authority=owner,
            admission=admission,
            validate_before_write=lambda: None,
        )
    assert (
        HistoricalPendingFence(engine.profile.root, engine.profile.root_identity).pending() is None
    )
    with closing(engine._store.connect()) as connection:
        assert connection.execute("SELECT count(*) FROM historical_operations").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 1

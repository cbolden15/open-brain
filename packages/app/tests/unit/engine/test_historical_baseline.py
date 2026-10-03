"""Positive adoption preserves old owner identity and the separate observation."""

from contextlib import closing
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import BrainEngine, ReferencePayload
from open_brain_engine.engine.historical_contracts import (
    HistoricalBaselineRequest,
    HistoricalClaimRequest,
)
from open_brain_engine.engine.historical_fence import HistoricalPendingFence
from open_brain_engine.engine.historical_recovery import _historical_transaction
from open_brain_engine.engine.historical_registry import HistoricalRegistryStore
from open_brain_engine.engine.historical_tasks import adopt_historical_baseline
from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission
from open_brain_engine.engine.sharing import EligibilityMode, sharing_eligible
from open_brain_engine.engine.sharing_contracts import SharingError
from open_brain_engine.engine.source_intake import (
    SourceRevisionBinding,
    SourceRevisionObservedDelivery,
    SourceRevisionSubmission,
)
from open_brain_engine.engine.source_lifecycle_contracts import SourceWithdrawRequest
from open_brain_engine.engine.source_observation import SourceRevisionObservation
from open_brain_engine.engine.t03_contracts import EffectiveAuthority, RecordReadRequest, T03Error

from open_brain.profile import compile_single_user_local
from packages.app.tests.unit.engine.test_foundation_contracts import _public_submission
from packages.app.tests.unit.engine.test_historical_projection import _claim_transition
from packages.app.tests.unit.engine.test_paging import scoped_authority


def _baseline(
    engine: BrainEngine, *, privacy_tier: PrivacyTier | None = None
) -> HistoricalBaselineRequest:
    claim = _claim_transition(engine, privacy_tier=privacy_tier).request
    assert isinstance(claim, HistoricalClaimRequest)
    capture = _public_submission(engine.tasks)
    capture = replace(
        capture, payload=ReferencePayload(capture.source_reference, "synthetic retained")
    )
    namespace = {
        "connector_name": "synthetic",
        "connection_id": "one",
        "resource_id": "one",
        "external_id": "one",
    }
    observed = SourceRevisionObservedDelivery(
        binding=SourceRevisionBinding(
            destination_brain_id=claim.destination.brain_id,
            issuer_epoch=claim.destination.issuer_epoch,
            root_fingerprint="synthetic-upstream-not-canonical-root",
            accepted_source_id="synthetic-selection",
            namespace=namespace,
        ),
        submission=SourceRevisionSubmission(
            namespace=namespace,
            capture=capture,
            revision_key="synthetic.baseline.key",
            canonical_sha256=capture.request_sha256(),
            expected_head=claim.source_cas.expected_head,
            ordering={"kind": "unordered"},
            expected_control_epoch=claim.source_cas.expected_control_epoch,
        ),
        expected_lifecycle_version=claim.source_cas.expected_lifecycle_version,
        delivery_id="synthetic.baseline.observed",
        observation=SourceRevisionObservation(
            original_sha256=sha256(b"synthetic upstream raw file").hexdigest(),
            transformed_sha256=sha256(b"synthetic retained").hexdigest(),
            normalization_version="synthetic.text.v1",
            privacy_policy_version=capture.privacy.policy_version,
            privacy_policy_sha256=sha256(
                portable_canonical_json_bytes(capture.privacy.to_dict())
            ).hexdigest(),
            admitted_payload_sha256=sha256(
                portable_canonical_json_bytes(capture.payload.to_dict())
            ).hexdigest(),
        ),
    )
    return HistoricalBaselineRequest(
        operation_id="baseline.synthetic",
        destination=claim.destination,
        source_cas=claim.source_cas,
        retained_original=claim.retained_capture,
        observed_delivery=observed,
        expected_claim_generation=0,
    )


def test_public_original_becomes_owner_only_without_privacy_rewrite(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    request = _baseline(engine, privacy_tier=PrivacyTier.PUBLIC)
    read = RecordReadRequest(
        record_id=request.retained_original.capture_id,
        expected_revision_id=request.retained_original.capture_id,
    )
    reader = scoped_authority(PrivacyTier.PUBLIC)
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )
    engine.retrieval.read_record(read, authority=reader)
    with closing(engine._store.connect()) as connection:
        before = tuple(
            connection.execute("SELECT payload_json,privacy_json FROM captures").fetchone()
        )
    with exclusive_runtime_admission(engine.profile) as admission:
        adopt_historical_baseline(
            engine.profile,
            request,
            authority=owner,
            admission=admission,
            validate_before_write=lambda: None,
        )
    with pytest.raises(T03Error, match="not_found"):
        engine.retrieval.read_record(read, authority=reader)
    engine.retrieval.read_record(read, authority=owner)
    with closing(engine._store.connect()) as connection:
        assert (
            tuple(connection.execute("SELECT payload_json,privacy_json FROM captures").fetchone())
            == before
        )
        assert connection.execute("SELECT COUNT(*) FROM captures").fetchone()[0] == 1


def test_nonowner_admin_cannot_adopt_baseline(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    request = _baseline(engine)
    scoped = EffectiveAuthority("synthetic-agent", "session", frozenset({"admin"}), None)
    with (
        exclusive_runtime_admission(engine.profile) as admission,
        pytest.raises(SharingError, match="unsupported_capability"),
    ):
        adopt_historical_baseline(
            engine.profile,
            request,
            authority=scoped,
            admission=admission,
            validate_before_write=lambda: None,
        )
    assert (
        HistoricalPendingFence(engine.profile.root, engine.profile.root_identity).pending() is None
    )
    with closing(engine._store.connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM historical_operations").fetchone()[0] == 0


@pytest.mark.parametrize("damage", ["missing_baseline", "corrupt_envelope", "missing_claim"])
def test_public_original_stays_denied_when_baseline_projection_is_damaged(
    tmp_path: Path, damage: str
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    request = _baseline(engine, privacy_tier=PrivacyTier.PUBLIC)
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )
    with exclusive_runtime_admission(engine.profile) as admission:
        adopt_historical_baseline(
            engine.profile,
            request,
            authority=owner,
            admission=admission,
            validate_before_write=lambda: None,
        )
    registry = HistoricalRegistryStore(engine.profile.root, engine.profile.root_identity).read(
        request.destination
    )
    assert registry.memberships[0].claim_role == "baseline_original"
    with (
        pytest.raises(RuntimeError, match="discard synthetic damage"),
        _historical_transaction(engine.profile, lambda: None) as connection,
    ):
        # Deliberately simulate storage loss inside a rolled-back test transaction.
        # Supported writes cannot remove or change these immutable facts.
        connection.execute("PRAGMA defer_foreign_keys=ON")
        table = "historical_claims" if damage == "missing_claim" else "historical_baselines"
        action = "update" if damage == "corrupt_envelope" else "delete"
        connection.execute(f"DROP TRIGGER {table}_{action}_immutable")
        if damage == "corrupt_envelope":
            connection.execute("UPDATE historical_baselines SET envelope_bytes='{}'")
        else:
            connection.execute(f"DELETE FROM {table}")
        for mode in (
            EligibilityMode.EXTERNAL_READ,
            EligibilityMode.LOCAL_CURRENT,
            EligibilityMode.LOCAL_HISTORY,
        ):
            assert not sharing_eligible(
                connection,
                request.retained_original.capture_id,
                mode=mode,
                profile=engine.profile,
            )
        assert sharing_eligible(
            connection,
            request.retained_original.capture_id,
            mode=EligibilityMode.OWNER_HISTORY,
            profile=engine.profile,
        )
        raise RuntimeError("discard synthetic damage")


def test_duplicate_baseline_with_new_operation_cannot_advance_generation(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    request = _baseline(engine)
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )
    with exclusive_runtime_admission(engine.profile) as admission:
        first = adopt_historical_baseline(
            engine.profile,
            request,
            authority=owner,
            admission=admission,
            validate_before_write=lambda: None,
        )
        duplicate = replace(request, operation_id="baseline.duplicate", expected_claim_generation=1)
        with pytest.raises(SharingError, match="binding_mismatch"):
            adopt_historical_baseline(
                engine.profile,
                duplicate,
                authority=owner,
                admission=admission,
                validate_before_write=lambda: None,
            )
    registry = HistoricalRegistryStore(engine.profile.root, engine.profile.root_identity).read(
        request.destination
    )
    assert registry.generation == first.claim_generation == 1
    assert (
        HistoricalPendingFence(engine.profile.root, engine.profile.root_identity).pending() is None
    )
    with closing(engine._store.connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM historical_operations").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM historical_baselines").fetchone()[0] == 1


def test_baseline_adopts_without_capture_namespace_or_old_revision_rewrite(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    request = _baseline(engine)
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )
    with closing(engine._store.connect()) as connection:
        before = {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in (
                "captures",
                "source_revisions",
                "source_aliases",
                "logical_sources",
                "source_namespaces",
            )
        }
    with exclusive_runtime_admission(engine.profile) as admission:
        receipt = adopt_historical_baseline(
            engine.profile,
            request,
            authority=owner,
            admission=admission,
            validate_before_write=lambda: None,
        )
        assert (
            adopt_historical_baseline(
                engine.profile,
                request,
                authority=owner,
                admission=admission,
                validate_before_write=lambda: None,
            )
            == receipt
        )
    assert receipt.outcome == "baseline_adopted"
    registry = HistoricalRegistryStore(engine.profile.root, engine.profile.root_identity).read(
        request.destination
    )
    assert registry.memberships[0].claim_role == "baseline_original"
    with closing(engine._store.connect()) as connection:
        for table, rows in before.items():
            assert [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")] == rows
        assert connection.execute("SELECT count(*) FROM source_intakes").fetchone()[0] == 0
        assert (
            connection.execute("SELECT count(*) FROM managed_source_deliveries").fetchone()[0] == 0
        )
        assert connection.execute("SELECT revision_key FROM source_revisions").fetchone()[0] is None
        assert (
            bytes(
                connection.execute("SELECT envelope_bytes FROM historical_baselines").fetchone()[0]
            )
            == request.observed_delivery.custody_bytes()
        )


@pytest.mark.parametrize(
    "stage",
    [
        "historical_baseline_validated",
        "historical_baseline_pending",
        "historical_registry_advanced",
        "historical_sql_projected",
        "historical_sql_committed",
    ],
)
def test_baseline_exact_retry_survives_interruption(tmp_path: Path, stage: str) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    request = _baseline(engine)
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )

    def crash(observed: str) -> None:
        if observed == stage:
            raise RuntimeError("synthetic baseline interruption")

    with exclusive_runtime_admission(engine.profile) as admission:
        with pytest.raises(RuntimeError, match="synthetic baseline interruption"):
            adopt_historical_baseline(
                engine.profile,
                request,
                authority=owner,
                admission=admission,
                validate_before_write=lambda: None,
                checkpoint=crash,
            )
        assert (
            adopt_historical_baseline(
                engine.profile,
                request,
                authority=owner,
                admission=admission,
                validate_before_write=lambda: None,
            ).claim_generation
            == 1
        )


@pytest.mark.parametrize("damage", ["text", "transform_digest", "profile"])
def test_baseline_refuses_false_correspondence_before_pending(tmp_path: Path, damage: str) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    request = _baseline(engine)
    observed = request.observed_delivery
    capture = observed.submission.capture
    observation = observed.observation
    if damage == "text":
        capture = replace(
            capture, payload=ReferencePayload(capture.source_reference, "different text")
        )
        observation = replace(
            observation,
            transformed_sha256=sha256(b"different text").hexdigest(),
            admitted_payload_sha256=sha256(
                portable_canonical_json_bytes(capture.payload.to_dict())
            ).hexdigest(),
        )
    elif damage == "transform_digest":
        observation = replace(observation, transformed_sha256="a" * 64)
    else:
        other = BrainEngine.open(compile_single_user_local(tmp_path / "other"))
        capture = replace(_public_submission(other.tasks), payload=capture.payload)
    observed = replace(
        observed,
        submission=replace(
            observed.submission, capture=capture, canonical_sha256=capture.request_sha256()
        ),
        observation=observation,
    )
    changed = replace(request, observed_delivery=observed)
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )
    with (
        exclusive_runtime_admission(engine.profile) as admission,
        pytest.raises(SharingError, match="binding_mismatch"),
    ):
        adopt_historical_baseline(
            engine.profile,
            changed,
            authority=owner,
            admission=admission,
            validate_before_write=lambda: None,
        )
    assert (
        HistoricalPendingFence(engine.profile.root, engine.profile.root_identity).pending() is None
    )


def test_completed_baseline_replay_after_withdrawal_is_truth_not_reactivation(
    tmp_path: Path,
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    request = _baseline(engine)
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )
    with exclusive_runtime_admission(engine.profile) as admission:
        receipt = adopt_historical_baseline(
            engine.profile,
            request,
            authority=owner,
            admission=admission,
            validate_before_write=lambda: None,
        )
    engine.sources.withdraw(
        SourceWithdrawRequest(
            operation_id="withdraw.baseline",
            source_id=request.source_cas.source_id,
            expected_head=request.source_cas.expected_head,
            expected_lifecycle_version=request.source_cas.expected_lifecycle_version,
            brain_id=request.destination.brain_id,
            issuer_epoch=request.destination.issuer_epoch,
            reason_code="synthetic_baseline_withdrawal",
        ),
        authority=owner,
    )
    with exclusive_runtime_admission(engine.profile) as admission:
        assert (
            adopt_historical_baseline(
                engine.profile,
                request,
                authority=owner,
                admission=admission,
                validate_before_write=lambda: None,
            )
            == receipt
        )
    with closing(engine._store.connect()) as connection:
        assert (
            connection.execute("SELECT lifecycle FROM logical_sources").fetchone()[0] == "retired"
        )

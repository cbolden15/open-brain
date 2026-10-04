"""Revocation admits denial over exact retained relation history, never publication."""

import json
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.engine import BrainEngine, TextPayload
from open_brain_engine.engine.historical_contracts import (
    HistoricalClaimRequest,
    HistoricalCopyRelationRequest,
    HistoricalRevocationRequest,
    RetainedCaptureEvidence,
)
from open_brain_engine.engine.historical_fence import HistoricalPendingFence
from open_brain_engine.engine.historical_projection import _append_historical_projection
from open_brain_engine.engine.historical_recovery import _historical_transaction
from open_brain_engine.engine.historical_registry import HistoricalRegistryStore
from open_brain_engine.engine.historical_tasks import (
    adopt_historical_baseline,
    register_historical_claim,
    revoke_historical_copy,
)
from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission
from open_brain_engine.engine.sharing_contracts import SharingError
from open_brain_engine.engine.source_lifecycle_contracts import SourceWithdrawRequest
from open_brain_engine.engine.t03_contracts import EffectiveAuthority

from open_brain.profile import compile_single_user_local
from packages.app.tests.unit.engine.test_historical_baseline import _baseline
from packages.app.tests.unit.engine.test_historical_projection_chain import _record
from packages.app.tests.unit.engine.test_historical_tasks import _current_cas


def _retained_relation(engine: BrainEngine) -> HistoricalCopyRelationRequest:
    """Seed synthetic prior relation history, not proof of link admission.

    Both actual captures keep UNKNOWN privacy and never gain public output. The
    relation's synthetic attestation is not real approval or consent. Eligible
    linking has separate admission tests before this extension can ship.
    """
    baseline = _baseline(engine)
    copy = engine.capture.accept(TextPayload("synthetic retained"), delivery_id="owner.copy")
    with closing(engine._store.connect()) as connection:
        row = connection.execute(
            "SELECT r.*,c.privacy_json FROM source_revisions r JOIN captures c USING(capture_id) "
            "WHERE r.capture_id=?",
            (copy.capture_id,),
        ).fetchone()
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )
    claim = HistoricalClaimRequest(
        operation_id="claim.copy",
        destination=baseline.destination,
        source_cas=_current_cas(engine, baseline.source_cas.source_id),
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
        expected_claim_generation=1,
    )
    with exclusive_runtime_admission(engine.profile) as admission:
        adopt_historical_baseline(
            engine.profile,
            baseline,
            authority=owner,
            admission=admission,
            validate_before_write=lambda: None,
        )
        register_historical_claim(
            engine.profile,
            claim,
            authority=owner,
            admission=admission,
            validate_before_write=lambda: None,
        )
    relation = HistoricalCopyRelationRequest(
        operation_id="relation.synthetic",
        destination=baseline.destination,
        source_cas=claim.source_cas,
        copy_source_cas=claim.capture_source_cas,
        baseline_operation_id=baseline.operation_id,
        copy_claim_operation_id=claim.operation_id,
        retained_copy=claim.retained_capture,
        approval_evidence_sha256="d" * 64,
        provider_ids=("openai",),
        expected_relation_version=0,
        expected_claim_generation=2,
    )
    registry = HistoricalRegistryStore(engine.profile.root, engine.profile.root_identity)
    record = _record(relation, registry.read(relation.destination))
    fence = HistoricalPendingFence(engine.profile.root, engine.profile.root_identity)
    fence.prepare(record)
    registry.advance(record.previous, record.proposed)
    with _historical_transaction(engine.profile, lambda: None) as connection:
        _append_historical_projection(connection, engine.profile, record)
    fence.mark_complete(record, record.proposed)
    return relation


def _request(
    engine: BrainEngine, relation: HistoricalCopyRelationRequest
) -> HistoricalRevocationRequest:
    return HistoricalRevocationRequest(
        operation_id="revoke.synthetic",
        destination=relation.destination,
        source_cas=_current_cas(engine, relation.source_cas.source_id),
        relation_operation_id=relation.operation_id,
        expected_relation_version=1,
        expected_claim_generation=3,
        reason_code="synthetic_owner_revocation",
    )


@pytest.mark.parametrize("withdrawn", [False, True])
def test_revocation_preserves_two_claims_captures_sources_and_exact_receipt(
    tmp_path: Path, withdrawn: bool
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    relation = _retained_relation(engine)
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )
    if withdrawn:
        engine.sources.withdraw(
            SourceWithdrawRequest(
                operation_id="withdraw.original",
                source_id=relation.source_cas.source_id,
                expected_head=relation.source_cas.expected_head,
                expected_lifecycle_version=relation.source_cas.expected_lifecycle_version,
                brain_id=relation.destination.brain_id,
                issuer_epoch=relation.destination.issuer_epoch,
                reason_code="synthetic_withdrawal",
            ),
            authority=owner,
        )
    request = _request(engine, relation)
    with closing(engine._store.connect()) as connection:
        before = {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in (
                "captures",
                "logical_sources",
                "source_revisions",
                "source_aliases",
                "historical_claims",
            )
        }
    with exclusive_runtime_admission(engine.profile) as admission:
        receipt = revoke_historical_copy(
            engine.profile,
            request,
            authority=owner,
            admission=admission,
            validate_before_write=lambda: None,
        )
        assert (
            revoke_historical_copy(
                engine.profile,
                request,
                authority=owner,
                admission=admission,
                validate_before_write=lambda: None,
            )
            == receipt
        )
    assert receipt.outcome == "historical_copy_revoked"
    assert receipt.relation_version == 2 and receipt.claim_generation == 4
    assert receipt.original_capture_id == relation.source_cas.expected_head
    assert receipt.copy_capture_id == relation.retained_copy.capture_id
    with closing(engine._store.connect()) as connection:
        for table, rows in before.items():
            assert [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")] == rows
        assert connection.execute("SELECT COUNT(*) FROM historical_revocations").fetchone()[0] == 1
    registry = HistoricalRegistryStore(engine.profile.root, engine.profile.root_identity).read(
        request.destination
    )
    assert len(registry.memberships) == 2
    assert (
        HistoricalPendingFence(engine.profile.root, engine.profile.root_identity).pending() is None
    )


@pytest.mark.parametrize(
    "stage",
    [
        "historical_revocation_validated",
        "historical_revocation_pending",
        "historical_registry_advanced",
        "historical_sql_projected",
        "historical_sql_committed",
    ],
)
def test_revocation_exact_retry_after_each_interruption(tmp_path: Path, stage: str) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    relation = _retained_relation(engine)
    request = _request(engine, relation)
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )

    def crash(point: str) -> None:
        if point == stage:
            raise RuntimeError("synthetic interruption")

    with exclusive_runtime_admission(engine.profile) as admission:
        with pytest.raises(RuntimeError, match="synthetic interruption"):
            revoke_historical_copy(
                engine.profile,
                request,
                authority=owner,
                admission=admission,
                validate_before_write=lambda: None,
                checkpoint=crash,
            )
        receipt = revoke_historical_copy(
            engine.profile,
            request,
            authority=owner,
            admission=admission,
            validate_before_write=lambda: None,
        )
        assert receipt.claim_generation == 4
        assert (
            revoke_historical_copy(
                engine.profile,
                request,
                authority=owner,
                admission=admission,
                validate_before_write=lambda: None,
            )
            == receipt
        )


def test_completed_revocation_replays_after_withdrawal_but_cannot_revoke_twice(
    tmp_path: Path,
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    relation = _retained_relation(engine)
    request = _request(engine, relation)
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )
    with exclusive_runtime_admission(engine.profile) as admission:
        receipt = revoke_historical_copy(
            engine.profile,
            request,
            authority=owner,
            admission=admission,
            validate_before_write=lambda: None,
        )
    engine.sources.withdraw(
        SourceWithdrawRequest(
            operation_id="withdraw.after_revocation",
            source_id=relation.source_cas.source_id,
            expected_head=relation.source_cas.expected_head,
            expected_lifecycle_version=relation.source_cas.expected_lifecycle_version,
            brain_id=relation.destination.brain_id,
            issuer_epoch=relation.destination.issuer_epoch,
            reason_code="synthetic_withdrawal",
        ),
        authority=owner,
    )
    with exclusive_runtime_admission(engine.profile) as admission:
        assert (
            revoke_historical_copy(
                engine.profile,
                request,
                authority=owner,
                admission=admission,
                validate_before_write=lambda: None,
            )
            == receipt
        )
        duplicate = replace(
            _request(engine, relation), operation_id="revoke.twice", expected_claim_generation=4
        )
        with pytest.raises(SharingError, match="binding_mismatch"):
            revoke_historical_copy(
                engine.profile,
                duplicate,
                authority=owner,
                admission=admission,
                validate_before_write=lambda: None,
            )
    with closing(engine._store.connect()) as connection:
        assert (
            connection.execute(
                "SELECT lifecycle FROM logical_sources WHERE source_id=?",
                (relation.source_cas.source_id,),
            ).fetchone()[0]
            == "retired"
        )
        assert connection.execute("SELECT COUNT(*) FROM historical_revocations").fetchone()[0] == 1


def test_changed_or_competing_pending_revocation_refuses_without_losing_exact_retry(
    tmp_path: Path,
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    relation = _retained_relation(engine)
    request = _request(engine, relation)
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )

    def crash(point: str) -> None:
        if point == "historical_revocation_pending":
            raise RuntimeError("synthetic interruption")

    with exclusive_runtime_admission(engine.profile) as admission:
        with pytest.raises(RuntimeError, match="synthetic interruption"):
            revoke_historical_copy(
                engine.profile,
                request,
                authority=owner,
                admission=admission,
                validate_before_write=lambda: None,
                checkpoint=crash,
            )
        for changed, code in (
            (replace(request, reason_code="changed_reason"), "binding_mismatch"),
            (replace(request, operation_id="competing.operation"), "operation_pending"),
        ):
            with pytest.raises(SharingError, match=code):
                revoke_historical_copy(
                    engine.profile,
                    changed,
                    authority=owner,
                    admission=admission,
                    validate_before_write=lambda: None,
                )
        assert (
            revoke_historical_copy(
                engine.profile,
                request,
                authority=owner,
                admission=admission,
                validate_before_write=lambda: None,
            ).claim_generation
            == 4
        )


@pytest.mark.parametrize("damage", ["relation", "version", "generation", "cas", "nonowner"])
def test_revocation_refuses_bad_authority_or_witness_without_pending(
    tmp_path: Path, damage: str
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    relation = _retained_relation(engine)
    request = _request(engine, relation)
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )
    code = "binding_mismatch"
    if damage == "relation":
        request = replace(request, relation_operation_id="absent.relation")
    elif damage == "version":
        request = replace(request, expected_relation_version=2)
    elif damage == "generation":
        request = replace(request, expected_claim_generation=2)
        code = "revision_changed"
    elif damage == "cas":
        request = replace(request, source_cas=replace(request.source_cas, expected_head_version=99))
        code = "revision_changed"
    else:
        owner = EffectiveAuthority("synthetic-agent", "session", frozenset({"admin"}), None)
        code = "unsupported_capability"
    with (
        exclusive_runtime_admission(engine.profile) as admission,
        pytest.raises(SharingError, match=code),
    ):
        revoke_historical_copy(
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
        assert connection.execute("SELECT COUNT(*) FROM historical_revocations").fetchone()[0] == 0

"""Projection loss must not become ordinary unlinked public eligibility."""

import json
from hashlib import sha256
from pathlib import Path

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import BrainEngine, TextPayload
from open_brain_engine.engine.historical_contracts import (
    HistoricalClaimRequest,
    HistoricalDestination,
    HistoricalReceipt,
    HistoricalSourceCAS,
    RetainedCaptureEvidence,
)
from open_brain_engine.engine.historical_fence import HistoricalPendingFence
from open_brain_engine.engine.historical_projection import (
    _append_historical_projection,
    verify_historical_projection,
)
from open_brain_engine.engine.historical_recovery import _historical_transaction
from open_brain_engine.engine.historical_registry import (
    HistoricalClaimMembership,
    HistoricalRegistryStore,
)
from open_brain_engine.engine.historical_transition import HistoricalTransition
from open_brain_engine.engine.sharing_contracts import SharingError

from open_brain.profile import compile_single_user_local
from packages.app.tests.unit.engine.test_historical_receipts import _value


def _claim_transition(
    engine: BrainEngine,
    *,
    privacy_tier: PrivacyTier | None = None,
    retained_text: str = "synthetic retained",
) -> HistoricalTransition:
    receipt = engine.capture.accept(
        TextPayload(retained_text),
        delivery_id="owner.original",
        privacy_tier=privacy_tier,
    )
    with _historical_transaction(engine.profile, lambda: None) as connection:
        row = connection.execute(
            "SELECT s.*,r.request_sha256,r.source_sha256,c.privacy_json,l.lifecycle_version,"
            "g.control_epoch FROM logical_sources s JOIN source_revisions r USING(source_id) "
            "JOIN captures c USING(capture_id) JOIN source_lifecycle_state l USING(source_id) "
            "CROSS JOIN engine_generations g WHERE r.capture_id=?",
            (receipt.capture_id,),
        ).fetchone()
        identity = connection.execute("SELECT brain_id,issuer_epoch FROM brain_identity").fetchone()
    destination = HistoricalDestination(brain_id=identity[0], issuer_epoch=identity[1])
    cas = HistoricalSourceCAS(
        source_id=row["source_id"],
        expected_head=receipt.capture_id,
        expected_head_version=row["head_version"],
        expected_route_version=row["route_version"],
        expected_lifecycle_version=row["lifecycle_version"],
        expected_control_epoch=row["control_epoch"],
        expected_lifecycle=row["lifecycle"],
        expected_availability=row["availability"],
        expected_historical_only=bool(row["historical_only"]),
    )
    request = HistoricalClaimRequest(
        operation_id="claim.original",
        destination=destination,
        source_cas=cas,
        capture_source_cas=cas,
        claim_role="baseline_original",
        expected_claim_generation=0,
        retained_capture=RetainedCaptureEvidence(
            capture_id=receipt.capture_id,
            source_sha256=row["source_sha256"],
            retained_delivery_id="owner.original",
            retained_request_sha256=row["request_sha256"],
            privacy_sha256=sha256(canonical(json.loads(row["privacy_json"]))).hexdigest(),
        ),
    )
    previous = HistoricalRegistryStore(engine.profile.root, engine.profile.root_identity).read(
        destination
    )
    proposed = previous.register(
        HistoricalClaimMembership(
            capture_id=receipt.capture_id,
            source_id=cas.source_id,
            capture_source_id=cas.source_id,
            claim_role="baseline_original",
        )
    )
    result = HistoricalReceipt.from_value(
        _value(
            destination=destination.value(),
            operation_id=request.operation_id,
            request_sha256=request.request_sha256,
            source_id=cas.source_id,
            original_capture_id=receipt.capture_id,
            outcome="denied_claim_recorded",
        )
    )
    return HistoricalTransition.create(
        request=request, previous=previous, proposed=proposed, receipt=result
    )


def test_projection_append_requires_exact_retained_intent_and_preserves_capture(
    tmp_path: Path,
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    record = _claim_transition(engine)
    with _historical_transaction(engine.profile, lambda: None) as connection:
        assert verify_historical_projection(connection, engine.profile, record.previous) == ()
        with pytest.raises(SharingError, match="binding_mismatch"):
            _append_historical_projection(connection, engine.profile, record)
    fence = HistoricalPendingFence(engine.profile.root, engine.profile.root_identity)
    fence.prepare(record)
    with _historical_transaction(engine.profile, lambda: None) as connection:
        _append_historical_projection(connection, engine.profile, record)
        assert verify_historical_projection(connection, engine.profile, record.proposed) == (
            record,
        )
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM source_intakes").fetchone()[0] == 0
    with pytest.raises(SharingError, match="operation_pending"):
        fence.assert_settled(record.proposed)
    registry = HistoricalRegistryStore(engine.profile.root, engine.profile.root_identity)
    registry.advance(record.previous, record.proposed)
    fence.mark_complete(record, record.proposed)
    fence.assert_settled(record.proposed)


@pytest.mark.parametrize("damage", ["claim", "operation", "receipt", "unexpected_revocation"])
def test_projection_damage_refuses_even_when_registry_digest_matches(
    tmp_path: Path, damage: str
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    record = _claim_transition(engine)
    HistoricalPendingFence(engine.profile.root, engine.profile.root_identity).prepare(record)
    with (
        pytest.raises(RuntimeError, match="discard synthetic damage"),
        _historical_transaction(engine.profile, lambda: None) as connection,
    ):
        _append_historical_projection(connection, engine.profile, record)
        connection.execute("PRAGMA defer_foreign_keys=ON")
        # Simulate storage damage, not a supported mutation. SQL guards forbid
        # these changes during ordinary operation; retained history detects loss.
        if damage in ("claim", "operation", "receipt"):
            table = "historical_claims" if damage == "claim" else "historical_operations"
            action = "update" if damage == "receipt" else "delete"
            connection.execute(f"DROP TRIGGER {table}_{action}_immutable")
            if damage == "receipt":
                connection.execute("UPDATE historical_operations SET receipt_json='{}'")
            else:
                connection.execute(f"DELETE FROM {table}")
        else:
            connection.execute("PRAGMA defer_foreign_keys=ON")
            connection.execute("INSERT INTO historical_revocations VALUES('wrong','absent',2)")
        with pytest.raises(SharingError, match="binding_mismatch"):
            verify_historical_projection(connection, engine.profile, record.proposed)
        # Keep damaged synthetic state transaction-local, avoiding FK commit.
        raise RuntimeError("discard synthetic damage")

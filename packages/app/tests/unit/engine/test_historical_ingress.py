"""Already-open ordinary writers cannot bypass historical maintenance."""

from contextlib import closing
from pathlib import Path

import pytest
from open_brain_engine.engine import (
    BrainEngine,
    CaptureCustodyReceipt,
    CaptureSubmission,
    TextPayload,
)
from open_brain_engine.engine.historical_fence import HistoricalPendingFence
from open_brain_engine.engine.historical_recovery import recover_historical_profile
from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission
from open_brain_engine.engine.sharing_contracts import SharingError
from open_brain_engine.engine.t03_contracts import EffectiveAuthority, RecordReadRequest

from open_brain.profile import compile_single_user_local
from packages.app.tests.unit.engine.test_historical_projection import _claim_transition


@pytest.mark.parametrize(
    "operation", ["capture", "replay", "retry", "space", "transaction", "recover"]
)
def test_pending_maintenance_refuses_already_open_writers_before_custody(
    tmp_path: Path, operation: str
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    record = _claim_transition(engine)
    fence = HistoricalPendingFence(engine.profile.root, engine.profile.root_identity)
    fence.prepare(record)
    with pytest.raises(SharingError, match="operation_pending"):
        if operation == "capture":
            engine.capture.accept(TextPayload("new synthetic body"), delivery_id="maintenance.new")
        elif operation == "replay":
            engine.capture.accept(TextPayload("synthetic retained"), delivery_id="owner.original")
        elif operation == "retry":
            engine.ingestion.retry("maintenance.new")
        elif operation == "space":
            engine.inbox.create_space("Maintenance new space", delivery_id="maintenance.space")
        elif operation == "recover":
            engine.recover()
        else:
            with engine._store.transaction():
                pytest.fail("ordinary transaction body admitted")
    assert fence.pending() == record
    with closing(engine._store.connect()) as connection:
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM capture_ingestion_items").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM source_intakes").fetchone()[0] == 0
    owner = EffectiveAuthority("synthetic-owner", "session", frozenset(), None, owner=True)
    engine.retrieval.read_record(
        RecordReadRequest(
            record_id=record.receipt.original_capture_id,
            expected_revision_id=record.receipt.original_capture_id,
        ),
        authority=owner,
    )
    with exclusive_runtime_admission(engine.profile) as admission:
        assert (
            recover_historical_profile(
                engine.profile,
                authority=owner,
                admission=admission,
                validate_before_write=lambda: None,
            )
            == record.receipt
        )
    receipt = engine.capture.accept(
        TextPayload("after maintenance"), delivery_id="maintenance.after"
    )
    assert receipt.capture_id != record.receipt.original_capture_id


def test_pending_marker_before_commit_rolls_back_ordinary_sql(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    record = _claim_transition(engine)
    fence = HistoricalPendingFence(engine.profile.root, engine.profile.root_identity)
    with (
        pytest.raises(SharingError, match="operation_pending"),
        engine._store.transaction() as connection,
    ):
        connection.execute("UPDATE captures SET stage=2")
        fence.prepare(record)  # Synthetic race between admission and commit.
    assert fence.pending() == record
    with closing(engine._store.connect()) as connection:
        assert connection.execute("SELECT stage FROM captures").fetchone()[0] == 3
    owner = EffectiveAuthority("synthetic-owner", "session", frozenset(), None, owner=True)
    with exclusive_runtime_admission(engine.profile) as admission:
        assert (
            recover_historical_profile(
                engine.profile,
                authority=owner,
                admission=admission,
                validate_before_write=lambda: None,
            )
            == record.receipt
        )


@pytest.mark.parametrize("operation", ["compact", "discard", "drain"])
def test_pending_maintenance_preserves_preexisting_queued_envelope_and_receipt(
    tmp_path: Path, operation: str
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    record = _claim_transition(engine)
    queued = engine.ingestion.enqueue(
        CaptureSubmission.for_local_owner(
            profile=engine.profile,
            payload=TextPayload("synthetic queued custody"),
            delivery_id="maintenance.queued",
        )
    )
    assert isinstance(queued, CaptureCustodyReceipt)
    with closing(engine._store.connect()) as connection:
        before = tuple(
            connection.execute("SELECT envelope_bytes FROM capture_ingestion_payloads").fetchone()
        )
    fence = HistoricalPendingFence(engine.profile.root, engine.profile.root_identity)
    fence.prepare(record)
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )
    assert engine.journal.summary(authority=owner).pending_count == 1
    with pytest.raises(SharingError, match="operation_pending"):
        if operation == "compact":
            engine.ingestion.compact("maintenance.queued")
        elif operation == "discard":
            engine.ingestion.discard("maintenance.queued", reason="synthetic maintenance")
        else:
            engine.journal.drain(authority=owner)
    with closing(engine._store.connect()) as connection:
        assert (
            tuple(
                connection.execute(
                    "SELECT envelope_bytes FROM capture_ingestion_payloads"
                ).fetchone()
            )
            == before
        )
        assert connection.execute("SELECT count(*) FROM capture_ingestion_items").fetchone()[0] == 1
    with exclusive_runtime_admission(engine.profile) as admission:
        recover_historical_profile(
            engine.profile, authority=owner, admission=admission, validate_before_write=lambda: None
        )
    result = engine.journal.drain(authority=owner)
    assert result.materialized_count == 1
    with closing(engine._store.connect()) as connection:
        row = connection.execute(
            "SELECT delivery_id,request_sha256 FROM captures WHERE capture_id=?",
            (result.receipts[0].capture_id,),
        ).fetchone()
        assert tuple(row) == (queued.delivery_id, queued.request_sha256)
    replay = engine.capture.accept(
        TextPayload("synthetic queued custody"), delivery_id=queued.delivery_id
    )
    assert replay.capture_id == result.receipts[0].capture_id
    assert replay.duplicate
    assert engine.journal.summary(authority=owner).pending_count == 0


@pytest.mark.parametrize("name", ["historical-claims.v1.json", "historical-fence.v1.json"])
def test_missing_authority_refuses_already_open_writer_and_custody(
    tmp_path: Path, name: str
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    path = engine.profile.root / ".open-brain/historical-authority" / name
    path.rename(path.with_suffix(".retained"))  # Synthetic evidence-loss case only.
    with pytest.raises(SharingError, match="binding_mismatch"):
        engine.capture.accept(TextPayload("unadmitted body"), delivery_id="missing.authority")
    with closing(engine._store.connect()) as connection:
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM capture_ingestion_items").fetchone()[0] == 0

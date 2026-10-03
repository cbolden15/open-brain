"""Original owner delivery identity must survive derived capture reconstruction."""

from pathlib import Path

import pytest
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import BrainEngine, CaptureReceipt, TextPayload
from open_brain_engine.engine.capture import DeliveryConflict
from open_brain_engine.engine.local_schema import open_local_database_read_only

from open_brain.profile import compile_single_user_local, open_existing_single_user_local


@pytest.mark.parametrize("tier", [PrivacyTier.UNKNOWN, PrivacyTier.PUBLIC])
@pytest.mark.parametrize("rebuild", [False, True])
def test_original_owner_delivery_replays_after_clean_restore(
    tmp_path: Path, tier: PrivacyTier, rebuild: bool
) -> None:
    tmp_path.chmod(0o700)
    original = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    payload = TextPayload("Retained synthetic owner baseline")
    delivery = "synthetic.retained.owner-delivery"
    receipt = original.capture.accept(payload, delivery_id=delivery, privacy_tier=tier)
    assert isinstance(receipt, CaptureReceipt)
    portable = tmp_path / "portable"
    original.portability.export(portable, export_id="export_00000000-0000-4000-8000-000000000081")
    target = tmp_path / "restored"
    original.portability.import_clean(
        portable, target, import_id="import_00000000-0000-4000-8000-000000000081"
    )
    restored = BrainEngine.open(open_existing_single_user_local(target))
    if rebuild:
        restored.portability.rebuild_index()
    with open_local_database_read_only(restored.profile) as db:
        before = db.execute("SELECT count(*) FROM captures").fetchone()[0]
        alias = db.execute(
            "SELECT evidence_sha256 FROM source_aliases WHERE delivery_id=?", (delivery,)
        ).fetchone()
        assert alias is not None
    replay = restored.capture.accept(payload, delivery_id=delivery, privacy_tier=tier)
    assert isinstance(replay, CaptureReceipt), "retained delivery must not create new custody"
    assert replay.capture_id == receipt.capture_id
    assert replay.duplicate
    assert replay.final_admitted_tier == receipt.final_admitted_tier
    with open_local_database_read_only(restored.profile) as db:
        assert db.execute("SELECT count(*) FROM captures").fetchone()[0] == before
        assert db.execute("SELECT count(*) FROM capture_ingestion_pending").fetchone()[0] == 0


@pytest.fixture
def restored_owner(tmp_path: Path) -> tuple[BrainEngine, CaptureReceipt]:
    tmp_path.chmod(0o700)
    original = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    receipt = original.capture.accept(
        TextPayload("Retained synthetic owner baseline"),
        delivery_id="synthetic.retained.owner-delivery",
        privacy_tier=PrivacyTier.UNKNOWN,
    )
    assert isinstance(receipt, CaptureReceipt)
    original.portability.export(
        tmp_path / "portable", export_id="export_00000000-0000-4000-8000-000000000082"
    )
    original.portability.import_clean(
        tmp_path / "portable",
        tmp_path / "restored",
        import_id="import_00000000-0000-4000-8000-000000000082",
    )
    return BrainEngine.open(open_existing_single_user_local(tmp_path / "restored")), receipt


def test_retained_delivery_changed_payload_is_not_new_custody(
    restored_owner: tuple[BrainEngine, CaptureReceipt],
) -> None:
    engine, _ = restored_owner
    with pytest.raises(DeliveryConflict):
        engine.capture.accept(
            TextPayload("Changed synthetic owner baseline"),
            delivery_id="synthetic.retained.owner-delivery",
        )
    with open_local_database_read_only(engine.profile) as db:
        assert db.execute("SELECT count(*) FROM captures").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM capture_ingestion_items").fetchone()[0] == 0


def test_retained_owner_digest_cannot_widen_admitted_privacy(
    restored_owner: tuple[BrainEngine, CaptureReceipt],
) -> None:
    engine, original = restored_owner
    replay = engine.capture.accept(
        TextPayload("Retained synthetic owner baseline"),
        delivery_id="synthetic.retained.owner-delivery",
        privacy_tier=PrivacyTier.PUBLIC,
    )
    assert isinstance(replay, CaptureReceipt)
    assert replay.capture_id == original.capture_id
    assert replay.duplicate
    assert replay.final_admitted_tier == PrivacyTier.UNKNOWN


@pytest.mark.parametrize("damage", ["admission", "completion"])
def test_retained_alias_with_missing_terminal_evidence_refuses_new_admission(
    restored_owner: tuple[BrainEngine, CaptureReceipt],
    damage: str,
) -> None:
    engine, original = restored_owner
    with engine._store.transaction() as db:
        if damage == "admission":
            db.execute(
                "UPDATE source_revisions SET request_sha256=NULL WHERE capture_id=?",
                (original.capture_id,),
            )
        else:
            db.execute("UPDATE captures SET stage=2 WHERE capture_id=?", (original.capture_id,))
    with pytest.raises(RuntimeError, match="retained replay evidence unavailable or ambiguous"):
        engine.capture.accept(
            TextPayload("Retained synthetic owner baseline"),
            delivery_id="synthetic.retained.owner-delivery",
        )
    with open_local_database_read_only(engine.profile) as db:
        assert db.execute("SELECT count(*) FROM captures").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM capture_ingestion_items").fetchone()[0] == 0

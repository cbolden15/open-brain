"""Original alias, immutable source and privacy are independent admission facts."""

import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.engine import BrainEngine, TextPayload, local_schema, open_local_engine
from open_brain_engine.engine.historical_admission import verify_retained_capture
from open_brain_engine.engine.historical_contracts import RetainedCaptureEvidence
from open_brain_engine.engine.local_schema import open_local_database_read_only
from open_brain_engine.engine.local_schema_catalog import LOCAL_MIGRATIONS
from open_brain_engine.engine.sharing_contracts import SharingError
from open_brain_engine.portable.versioned import validated_portable_snapshot

from open_brain.profile import compile_single_user_local, open_existing_single_user_local


@pytest.mark.parametrize(
    "field",
    [
        None,
        "capture_id",
        "source_sha256",
        "retained_delivery_id",
        "retained_request_sha256",
        "privacy_sha256",
        "source_id",
    ],
)
def test_retained_admission_matches_source_alias_and_privacy_without_recapture(
    tmp_path: Path,
    field: str | None,
) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    tasks = open_local_engine(profile)
    receipt = tasks.capture.accept(
        TextPayload("synthetic retained body"), delivery_id="synthetic.original"
    )
    with open_local_database_read_only(profile) as connection:
        row = connection.execute(
            "SELECT r.*,c.privacy_json FROM source_revisions r "
            "JOIN captures c USING(capture_id) WHERE capture_id=?",
            (receipt.capture_id,),
        ).fetchone()
        witness = RetainedCaptureEvidence(
            capture_id=receipt.capture_id,
            source_sha256=row["source_sha256"],
            retained_delivery_id="synthetic.original",
            retained_request_sha256=row["request_sha256"],
            privacy_sha256=sha256(
                portable_canonical_json_bytes(json.loads(row["privacy_json"]))
            ).hexdigest(),
        )
        source_id = row["source_id"]
        if field is None:
            record = verify_retained_capture(connection, profile, witness, source_id)
            assert record["capture_id"] == receipt.capture_id
            assert record["payload"] == {"family": "text", "text": "synthetic retained body"}
        else:
            if field == "source_id":
                source_id = "source_other"
            else:
                change = (
                    "capture_other"
                    if field == "capture_id"
                    else ("synthetic.wrong" if field == "retained_delivery_id" else "0" * 64)
                )
                witness = replace(witness, **{field: change})
            with pytest.raises(SharingError, match="binding_mismatch"):
                verify_retained_capture(connection, profile, witness, source_id)
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM source_intakes").fetchone()[0] == 0


@pytest.mark.parametrize("damage", [None, "alias", "source_bytes", "completion", "privacy"])
@pytest.mark.parametrize("portable_version", [7, 8])
def test_retained_evidence_survives_genuine_portable_restore(
    tmp_path: Path,
    damage: str | None,
    portable_version: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.chmod(0o700)
    # Generate v7 from its real catalog, not by deleting mandatory v8 sidecars.
    with monkeypatch.context() as historical:
        if portable_version == 7:
            historical.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 12)
            historical.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:12])
        engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
        receipt = engine.capture.accept(
            TextPayload("synthetic retained original"), delivery_id="synthetic.old.owner"
        )
        with open_local_database_read_only(engine.profile) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == (
                12 if portable_version == 7 else 13
            )
            row = connection.execute(
                "SELECT r.*,c.privacy_json FROM source_revisions r "
                "JOIN captures c USING(capture_id) WHERE capture_id=?",
                (receipt.capture_id,),
            ).fetchone()
            source_id = row["source_id"]
            witness = RetainedCaptureEvidence(
                capture_id=receipt.capture_id,
                source_sha256=row["source_sha256"],
                retained_delivery_id="synthetic.old.owner",
                retained_request_sha256=row["request_sha256"],
                privacy_sha256=sha256(
                    portable_canonical_json_bytes(json.loads(row["privacy_json"]))
                ).hexdigest(),
            )
        engine.portability.export(
            tmp_path / "portable", export_id="export_00000000-0000-4000-8000-000000000092"
        )
    assert validated_portable_snapshot(tmp_path / "portable").manifest["schema_version"] == (
        portable_version
    )
    engine.portability.import_clean(
        tmp_path / "portable",
        tmp_path / "restored",
        import_id="import_00000000-0000-4000-8000-000000000092",
    )
    restored = BrainEngine.open(open_existing_single_user_local(tmp_path / "restored"))
    if damage is not None:
        with restored._store.transaction() as connection:
            if damage == "alias":
                connection.execute(
                    "DELETE FROM source_aliases WHERE delivery_id=?",
                    (witness.retained_delivery_id,),
                )
            elif damage == "source_bytes":
                connection.execute(
                    "UPDATE source_revisions SET source_bytes=? WHERE capture_id=?",
                    (b"synthetic corrupted bytes", receipt.capture_id),
                )
            elif damage == "completion":
                connection.execute(
                    "UPDATE captures SET stage=2 WHERE capture_id=?", (receipt.capture_id,)
                )
            else:
                connection.execute(
                    "UPDATE captures SET privacy_json='{}' WHERE capture_id=?",
                    (receipt.capture_id,),
                )
    with open_local_database_read_only(restored.profile) as connection:
        restored_digest = connection.execute(
            "SELECT request_sha256 FROM captures WHERE capture_id=?", (receipt.capture_id,)
        ).fetchone()[0]
        if portable_version == 7:
            assert restored_digest != witness.retained_request_sha256
        else:
            assert restored_digest == witness.retained_request_sha256
        if damage is None:
            record = verify_retained_capture(connection, restored.profile, witness, source_id)
            assert record["capture_id"] == receipt.capture_id
        else:
            with pytest.raises(SharingError, match="binding_mismatch"):
                verify_retained_capture(connection, restored.profile, witness, source_id)
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM source_intakes").fetchone()[0] == 0

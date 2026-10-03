"""Current Portable recovery preserves original capture admission, not aliases."""

import json
from pathlib import Path
from uuid import uuid4

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import (
    BrainEngine,
    CaptureAction,
    CaptureSubmission,
    FilePayload,
    TextPayload,
)
from open_brain_engine.portable.v1 import PortableValidationError
from open_brain_engine.portable.v8 import validate_portable_file_set_v8
from open_brain_engine.portable.v8_capture_metadata import CAPTURE_METADATA_PATH
from open_brain_engine.portable.versioned import validated_portable_snapshot

from open_brain.profile import compile_single_user_local


@pytest.mark.parametrize("canonical,file_payload", ((False, False), (True, False), (False, True)))
def test_v8_restore_preserves_exact_original_capture_metadata(
    tmp_path: Path,
    canonical: bool,
    file_payload: bool,
) -> None:
    primary = BrainEngine.open(
        compile_single_user_local(
            tmp_path / "primary",
            starter_spaces=("Recovery",),
        )
    )
    submission = CaptureSubmission.for_local_owner(
        profile=primary.profile,
        payload=(
            FilePayload("synthetic.txt", "text/plain", b"Synthetic baseline file body")
            if file_payload
            else TextPayload("Synthetic baseline owner body")
        ),
        delivery_id="baseline.owner.original",
        title="Original supplied title",
        privacy_tier=PrivacyTier.PERSONAL,
        action=CaptureAction.CANONICAL_NOTE if canonical else CaptureAction.QUICK,
        space_id=primary.inbox.spaces()[0].space_id if canonical else None,
    )
    receipt = primary.capture.submit(submission)
    with primary._store.connect() as connection:
        original = dict(
            connection.execute(
                "SELECT * FROM captures WHERE delivery_id=?",
                (submission.delivery_id,),
            ).fetchone()
        )
    archive = tmp_path / "baseline"
    primary.portability.export(archive, export_id="export_" + str(uuid4()))
    destination = tmp_path / "restored"
    primary.portability.import_clean(archive, destination, import_id="import_" + str(uuid4()))
    restored = BrainEngine.open(compile_single_user_local(destination))
    with restored._store.connect() as connection:
        result = dict(connection.execute("SELECT * FROM captures").fetchone())
    assert result == original
    replay = restored.capture.submit(submission)
    assert replay.duplicate and replay.capture_id == receipt.capture_id


@pytest.mark.parametrize(
    "case",
    ("missing", "extra", "duplicate", "capture_id", "payload", "privacy", "receipt", "path"),
)
def test_v8_rejects_incomplete_or_misbound_capture_metadata(tmp_path: Path, case: str) -> None:
    primary = BrainEngine.open(compile_single_user_local(tmp_path / "primary"))
    primary.capture.accept(TextPayload("Synthetic sidecar body"), delivery_id="baseline.synthetic")
    archive = tmp_path / "archive"
    primary.portability.export(archive, export_id="export_" + str(uuid4()))
    files = dict(validated_portable_snapshot(archive).files)
    files.pop("portable-manifest.json")
    value = json.loads(files[CAPTURE_METADATA_PATH])
    row = value["captures"][0]
    if case == "missing":
        files.pop(CAPTURE_METADATA_PATH)
    else:
        if case == "extra":
            row["unexpected"] = True
        elif case == "duplicate":
            value["captures"].append(dict(row))
        elif case == "capture_id":
            row["capture_id"] = "capture_" + str(uuid4())
        elif case == "payload":
            row["payload_json"] = "AAAA"
        elif case == "privacy":
            row["privacy_json"] = "{}"
        elif case == "receipt":
            row["accepted_receipt_id"] = "receipt_" + str(uuid4())
        else:
            row["source_path"] = "sources/captures/absent.json"
        files[CAPTURE_METADATA_PATH] = portable_canonical_json_bytes(value)
    with pytest.raises(PortableValidationError):
        validate_portable_file_set_v8(files, tenant_id=primary.profile.tenant_id)

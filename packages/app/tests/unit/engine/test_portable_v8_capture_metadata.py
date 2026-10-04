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
from open_brain_engine.portable.v5 import (
    EFFECTIVE_PRIVACY_PATH,
    decode_retained_privacy_value,
    encode_retained_privacy_value,
)
from open_brain_engine.portable.v8 import validate_portable_file_set_v8
from open_brain_engine.portable.v8_capture_metadata import (
    CAPTURE_METADATA_PATH,
    capture_metadata_bytes,
    validate_capture_metadata,
)
from open_brain_engine.portable.versioned import validated_portable_snapshot

from open_brain.profile import compile_single_user_local
from packages.engine.tests.contract.test_portable_brain_v5 import _fixture, _privacy_state, _write


@pytest.mark.parametrize("original,changed", ((1, 1.0), (1.0, 1)))
def test_v8_metadata_refuses_equal_numeric_value_with_changed_storage_type(
    tmp_path: Path,
    original: int | float,
    changed: int | float,
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "primary"))
    engine.capture.accept(TextPayload("Synthetic type binding"), delivery_id="privacy.type")
    archive = tmp_path / "archive"
    engine.portability.export(archive, export_id="export_" + str(uuid4()))
    files = dict(validated_portable_snapshot(archive).files)
    immutable = json.loads(files[EFFECTIVE_PRIVACY_PATH])
    immutable["retained_privacy"][0]["privacy_json"] = encode_retained_privacy_value(original)
    files[EFFECTIVE_PRIVACY_PATH] = portable_canonical_json_bytes(immutable)
    metadata = json.loads(files[CAPTURE_METADATA_PATH])
    metadata["captures"][0]["privacy_json"] = encode_retained_privacy_value(changed)
    files[CAPTURE_METADATA_PATH] = portable_canonical_json_bytes(metadata)
    # Exercise original-metadata cross-binding directly. Numeric restore
    # admission remains independently refused by the existing importer.
    with pytest.raises(PortableValidationError):
        validate_capture_metadata(files)


@pytest.mark.parametrize("privacy", (None, b"\x00invalid\xff", "malformed text", 123, 1.25))
def test_v8_metadata_privacy_codec_preserves_original_storage_type(
    tmp_path: Path,
    privacy: object,
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "primary"))
    engine.capture.accept(TextPayload("Synthetic codec body"), delivery_id="privacy.codec")
    with engine._store.connect() as connection:
        row = dict(connection.execute("SELECT * FROM captures").fetchone())
    row["privacy_json"] = privacy
    encoded = json.loads(capture_metadata_bytes([row]))
    assert encoded["schema_version"] == 2
    tagged = encoded["captures"][0]["privacy_json"]
    assert tagged == encode_retained_privacy_value(privacy)
    restored = decode_retained_privacy_value(tagged)
    assert type(restored) is type(privacy) and restored == privacy


@pytest.mark.parametrize("privacy", (None, b"\x00invalid\xff", "malformed text"))
def test_v8_clean_restore_preserves_historical_privacy_storage_value(
    tmp_path: Path,
    privacy: object,
) -> None:
    files = _fixture()
    state = _privacy_state(files, privacy)
    expected = {
        row["capture_id"]: decode_retained_privacy_value(row["privacy_json"])
        for row in state["retained_privacy"]
    }
    historical = tmp_path / "historical"
    _write(historical, files)
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "control"))
    engine.portability.import_clean(
        historical, tmp_path / "upgraded", import_id="import_" + str(uuid4())
    )
    upgraded = BrainEngine.open(compile_single_user_local(tmp_path / "upgraded"))
    archive = tmp_path / "current"
    upgraded.portability.export(archive, export_id="export_" + str(uuid4()))
    engine.portability.import_clean(
        archive, tmp_path / "restored", import_id="import_" + str(uuid4())
    )
    restored = BrainEngine.open(compile_single_user_local(tmp_path / "restored"))
    with restored._store.connect() as connection:
        values = dict(connection.execute("SELECT capture_id,privacy_json FROM captures"))
    assert values.keys() == expected.keys()
    assert all(
        type(value) is type(expected[key]) and value == expected[key]
        for key, value in values.items()
    )


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


@pytest.mark.parametrize(
    "field",
    (
        "auto_proposal_id",
        "auto_proposal_receipt_id",
        "auto_decision_id",
        "page_id",
        "publication_id",
        "canonical_path",
        "publication_path",
        "action",
    ),
)
def test_v8_rejects_original_allocation_disagreeing_with_archived_proposal(
    tmp_path: Path,
    field: str,
) -> None:
    primary = BrainEngine.open(
        compile_single_user_local(
            tmp_path / "primary",
            starter_spaces=("Recovery",),
        )
    )
    primary.capture.submit(
        CaptureSubmission.for_local_owner(
            profile=primary.profile,
            payload=TextPayload("Synthetic canonical allocation body"),
            delivery_id="baseline.canonical",
            action=CaptureAction.CANONICAL_NOTE,
            space_id=primary.inbox.spaces()[0].space_id,
        )
    )
    archive = tmp_path / "archive"
    primary.portability.export(archive, export_id="export_" + str(uuid4()))
    files = dict(validated_portable_snapshot(archive).files)
    files.pop("portable-manifest.json")
    value = json.loads(files[CAPTURE_METADATA_PATH])
    if field in {"canonical_path", "publication_path"}:
        replacement = "brain.toml"
    elif field == "action":
        replacement = "quick"
    else:
        prefix = {
            "auto_proposal_id": "proposal",
            "auto_proposal_receipt_id": "receipt",
            "auto_decision_id": "decision",
            "page_id": "page",
            "publication_id": "publication",
        }[field]
        replacement = prefix + "_" + str(uuid4())
    value["captures"][0][field] = replacement
    files[CAPTURE_METADATA_PATH] = portable_canonical_json_bytes(value)
    with pytest.raises(PortableValidationError):
        validate_portable_file_set_v8(files, tenant_id=primary.profile.tenant_id)

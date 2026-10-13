"""Portable8 retains exact capture journal custody before ordinary recovery."""

import json
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.engine import (
    BrainEngine,
    CaptureFault,
    CaptureSubmission,
    InjectedFault,
    TextPayload,
    local_schema,
)
from open_brain_engine.engine.contracts import LocalEngineContext
from open_brain_engine.engine.local_schema import open_local_database_read_only
from open_brain_engine.engine.local_schema_catalog import LOCAL_MIGRATIONS
from open_brain_engine.portable.v1 import PortableValidationError
from open_brain_engine.portable.v8_custody import CUSTODY_PATH, validate_capture_custody
from open_brain_engine.portable.versioned import validated_portable_snapshot

from open_brain.profile import compile_single_user_local

_TABLES = (
    ("capture_ingestion_items", "journal_sequence"),
    ("capture_ingestion_payloads", "delivery_id"),
    ("capture_ingestion_events", "event_sequence"),
    ("capture_ingestion_tombstones", "delivery_id"),
)


@pytest.fixture(autouse=True)
def frozen_schema_thirteen(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise actual Portable8/schema13 behavior after the current format advances."""
    monkeypatch.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 13)
    monkeypatch.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:13])


def _journal_state(profile: LocalEngineContext) -> dict[str, object]:
    with open_local_database_read_only(profile) as connection:
        return {
            **{
                table: [
                    dict(row)
                    for row in connection.execute(f"SELECT * FROM {table} ORDER BY {order}")
                ]
                for table, order in _TABLES
            },
            "sequences": [
                tuple(row)
                for row in connection.execute(
                    "SELECT name,seq FROM sqlite_sequence WHERE name IN "
                    "('capture_ingestion_items','capture_ingestion_events') ORDER BY name",
                )
            ],
        }


@pytest.mark.parametrize(
    "state",
    (
        "queued",
        "queued-managed",
        "failed",
        "quarantined",
        "retried",
        "discarded",
        "compacted",
        "terminal",
    ),
)
def test_v8_restores_original_journal_and_watermarks_before_drain(
    tmp_path: Path, state: str
) -> None:
    primary = BrainEngine.open(compile_single_user_local(tmp_path / "primary"))
    if state == "queued-managed":
        workspace = tmp_path / "workspace"
        workspace.mkdir(mode=0o700)
        primary.managed_workspace.setup(str(workspace), operation_id="custody.workspace")
    submission = CaptureSubmission.for_local_owner(
        profile=primary.profile,
        payload=TextPayload("Synthetic original pending envelope"),
        delivery_id="custody.synthetic.original",
        title="Supplied pending title",
    )
    if state == "compacted":
        primary.capture.submit(submission)
    elif state == "terminal":
        primary._faults.add(CaptureFault.AFTER_JOURNAL_TERMINAL_EVENT)
        with pytest.raises(InjectedFault):
            primary.capture.submit(submission)
        primary._faults.clear()
    else:
        primary.ingestion.enqueue(submission)
        if state == "failed":
            primary.ingestion._terminal_metadata(
                submission.delivery_id, "attempt_failed", 1, "retryable"
            )
        if state in {"quarantined", "retried", "discarded"}:
            primary.ingestion._terminal_metadata(
                submission.delivery_id, "quarantined", 0, "invalid"
            )
            if state == "discarded":
                primary.ingestion.discard(submission.delivery_id, reason="Synthetic owner discard")
            elif state == "retried":
                primary.ingestion.retry(submission.delivery_id)
    original = _journal_state(primary.profile)
    archive = tmp_path / "archive"
    primary.portability.export(archive, export_id="export_" + str(uuid4()))
    destination = tmp_path / "restored"
    import_id = "import_" + str(uuid4())
    primary.portability.import_clean(archive, destination, import_id=import_id)
    restored_profile = compile_single_user_local(destination)
    assert _journal_state(restored_profile) == original
    primary.portability.import_clean(archive, destination, import_id=import_id)
    assert _journal_state(restored_profile) == original
    restored = BrainEngine.open(restored_profile)
    if state == "quarantined":
        assert _journal_state(restored_profile) == original
        restored.ingestion.retry(submission.delivery_id)
        restored.recover()
    if state != "discarded":
        replay = restored.capture.submit(submission)
        assert replay.duplicate
    next_submission = CaptureSubmission.for_local_owner(
        profile=restored_profile,
        payload=TextPayload("Synthetic next envelope"),
        delivery_id="custody.synthetic.next",
    )
    restored.ingestion.enqueue(next_submission)
    with open_local_database_read_only(restored_profile) as connection:
        sequence = connection.execute(
            "SELECT journal_sequence FROM capture_ingestion_items WHERE delivery_id=?",
            (next_submission.delivery_id,),
        ).fetchone()[0]
    original_high = dict(cast(list[tuple[str, int]], original["sequences"]))[
        "capture_ingestion_items"
    ]
    assert sequence > original_high


@pytest.mark.parametrize("mutation", ("payload", "missing_payload", "sequence", "epoch", "event"))
def test_v8_rejects_changed_pending_custody(tmp_path: Path, mutation: str) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "primary"))
    engine.ingestion.enqueue(
        CaptureSubmission.for_local_owner(
            profile=engine.profile,
            payload=TextPayload("Synthetic queued body"),
            delivery_id="custody.tamper",
        )
    )
    archive = tmp_path / "archive"
    engine.portability.export(archive, export_id="export_" + str(uuid4()))
    files = dict(validated_portable_snapshot(archive).files)
    value = json.loads(files[CUSTODY_PATH])
    if mutation == "payload":
        value["capture_ingestion_payloads"][0]["envelope_bytes"] = "e30="
    elif mutation == "missing_payload":
        value["capture_ingestion_payloads"] = []
    elif mutation == "sequence":
        value["sequences"]["capture_ingestion_items"] = 0
    elif mutation == "epoch":
        event = value["capture_ingestion_events"][0]
        wrapped = json.loads(event["receipt_json"])
        wrapped["receipt"]["issuer_epoch"] += 1
        event["receipt_json"] = portable_canonical_json_bytes(wrapped).decode()
    else:
        value["capture_ingestion_events"][0]["event_kind"] = "accepted"
    files[CUSTODY_PATH] = portable_canonical_json_bytes(value)
    with pytest.raises(PortableValidationError, match="capture custody"):
        validate_capture_custody(files)

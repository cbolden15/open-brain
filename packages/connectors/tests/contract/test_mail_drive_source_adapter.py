from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from open_brain_engine.engine import PublicJobCaptureContext, open_local_engine

from open_brain.profile import compile_single_user_local
from open_brain_connectors.runtime.connectors import (
    ConnectorBudget,
    ConnectorBudgetLimits,
    ConnectorCaptureSink,
    ConnectorContractError,
    ConnectorOutcome,
    ConnectorRunEvidence,
)
from open_brain_connectors.runtime.mail_drive import (
    DriveFileCheckpoint,
    DriveFileCheckpointStore,
    DriveSourceAdapter,
    GmailSourceAdapter,
    MailCheckpoint,
    MailCheckpointStore,
    MailPage,
    MailPageStatus,
    MicrosoftMailSourceAdapter,
)

from .test_source_intake import _privacy


def test_gmail_adapter_builds_selected_label_preview_without_bodies() -> None:
    adapter = GmailSourceAdapter()
    selection = adapter.selection(
        connection_id="account:gmail-fixture",
        resource_id="label:OpenBrain",
    )

    page = adapter.page_from_rest(
        selection,
        (
            {
                "id": "msg_1",
                "resource_id": "label:OpenBrain",
                "web_url": "https://mail.google.com/mail/u/0/#inbox/msg_1",
                "subject": "Fixture Gmail message",
                "body_text": "Synthetic Gmail body must not appear in preview.",
                "updated_at": "2026-09-15T12:00:00Z",
                "attachment_count": 2,
            },
        ),
        privacy=_privacy(),
        next_cursor="cursor:gmail2",
    )

    assert page.status is MailPageStatus.READY
    assert page.preview is not None
    assert page.preview.selection == selection
    assert page.preview.records[0].content_type == "mail_message"
    assert "must not appear" not in repr(page.preview.to_dict())
    assert selection.resource_id == "mail_label:label:OpenBrain"


def test_microsoft_mail_import_refuses_unordered_revision_and_replays_safely(
    tmp_path: Path,
) -> None:
    adapter = MicrosoftMailSourceAdapter()
    selection = adapter.selection(
        connection_id="account:m365-fixture",
        resource_id="folder:Inbox",
    )
    original = adapter.record_from_rest(
        {
            "id": "message-a",
            "resource_id": "folder:Inbox",
            "web_url": "https://outlook.office.com/mail/inbox/id/message-a",
            "subject": "Fixture Microsoft 365 message",
            "body_text": "Synthetic original Microsoft mail body.",
            "updated_at": "2026-09-15T12:00:00Z",
        }
    )
    original_page = adapter.preview(selection, (original,), privacy=_privacy())
    original_intake = adapter.intake(selection, original, privacy=_privacy())
    sink = _capture_sink(tmp_path)

    checkpoint, receipt = adapter.import_page(
        MailCheckpoint.initial(selection),
        MailPage(
            status=MailPageStatus.READY,
            preview=original_page,
            connector_name="microsoft_mail",
        ),
        (original_intake,),
        sink,
    )

    assert receipt.outcome is ConnectorOutcome.COMPLETED
    assert checkpoint.committed_revision_identities == (
        original_intake.key.revision_identity(),
    )

    replayed_checkpoint, replayed_receipt = adapter.import_page(
        checkpoint,
        MailPage(
            status=MailPageStatus.READY,
            preview=original_page,
            connector_name="microsoft_mail",
        ),
        (original_intake,),
        sink,
    )

    assert replayed_checkpoint == checkpoint
    assert replayed_receipt.outcome is ConnectorOutcome.EMPTY

    changed = adapter.record_from_rest(
        {
            "id": "message-a",
            "resource_id": "folder:Inbox",
            "web_url": "https://outlook.office.com/mail/inbox/id/message-a",
            "subject": "Fixture Microsoft 365 message",
            "body_text": "Synthetic changed Microsoft mail body.",
            "updated_at": "2026-09-15T12:30:00Z",
        }
    )
    changed_page = adapter.preview(selection, (changed,), privacy=_privacy())
    changed_intake = adapter.intake(selection, changed, privacy=_privacy())
    checkpoint_before = checkpoint.to_dict()
    source_bytes = {p: p.read_bytes() for p in tmp_path.glob("brain-*/sources/**/*")
                    if p.is_file()}
    assert source_bytes
    # Legacy deliveries lack ordering and head-CAS evidence for revision replacement.
    with pytest.raises(ValueError, match="^conflicting delivery$"):
        adapter.import_page(
            checkpoint,
            MailPage(
                status=MailPageStatus.READY,
                preview=changed_page,
                connector_name="microsoft_mail",
            ),
            (changed_intake,),
            sink,
        )
    assert checkpoint.to_dict() == checkpoint_before
    assert {p: p.read_bytes() for p in tmp_path.glob("brain-*/sources/**/*")
            if p.is_file()} == source_bytes



def test_google_drive_adapter_imports_selected_text_like_file_and_rejects_binary(
    tmp_path: Path,
) -> None:
    adapter = DriveSourceAdapter()
    selection = adapter.file_selection(
        connection_id="account:drive-fixture",
        file_id="drive-file-123",
    )
    record = adapter.record_from_rest(
        {
            "id": "drive-file-123",
            "revision_id": "rev-1",
            "web_url": "https://drive.google.com/file/d/drive-file-123/view",
            "name": "Fixture Drive document",
            "text": "Synthetic Google Drive text body.",
            "mime_type": "application/vnd.google-apps.document",
        }
    )
    page = adapter.preview(selection, (record,), privacy=_privacy(), next_cursor="cursor:drive2")
    intake = adapter.intake(selection, record, privacy=_privacy())
    checkpoint, receipt = adapter.import_page(
        DriveFileCheckpoint.initial(selection),
        adapter.page_from_rest(
            selection,
            (
                {
                    "id": "drive-file-123",
                    "revision_id": "rev-1",
                    "web_url": "https://drive.google.com/file/d/drive-file-123/view",
                    "name": "Fixture Drive document",
                    "text": "Synthetic Google Drive text body.",
                    "mime_type": "application/vnd.google-apps.document",
                },
            ),
            privacy=_privacy(),
            next_cursor="cursor:drive2",
        ),
        (intake,),
        _capture_sink(tmp_path),
    )

    assert receipt.outcome is ConnectorOutcome.COMPLETED
    assert checkpoint.next_cursor == "cursor:drive2"
    assert page.records[0].content_type == "drive_file"
    with pytest.raises(ConnectorContractError, match="unsupported google drive file"):
        adapter.record_from_rest(
            {
                "id": "drive-file-123",
                "revision_id": "rev-1",
                "web_url": "https://drive.google.com/file/d/drive-file-123/view",
                "name": "Binary fixture",
                "text": "",
                "mime_type": "application/octet-stream",
            }
        )


def test_mail_and_drive_checkpoint_stores_are_metadata_only_and_atomic(tmp_path: Path) -> None:
    mail = GmailSourceAdapter()
    mail_selection = mail.selection(
        connection_id="account:gmail-fixture",
        resource_id="label:OpenBrain",
    )
    mail_record = mail.record_from_rest(
        {
            "id": "msg_1",
            "resource_id": "label:OpenBrain",
            "web_url": "https://mail.google.com/mail/u/0/#inbox/msg_1",
            "subject": "Fixture Gmail message",
            "body_text": "Synthetic body excluded from checkpoint.",
            "updated_at": "2026-09-15T12:00:00Z",
        }
    )
    mail_page = mail.preview(mail_selection, (mail_record,), privacy=_privacy())
    mail_intake = mail.intake(mail_selection, mail_record, privacy=_privacy())
    mail_checkpoint = MailCheckpoint.initial(mail_selection).advance(
        mail_page,
        committed_delivery_ids=tuple(record.delivery_id for record in mail_page.records),
        committed_revision_identities=(mail_intake.key.revision_identity(),),
    )

    mail_path = MailCheckpointStore(tmp_path / "checkpoints").save(mail_checkpoint)

    assert MailCheckpointStore(tmp_path / "checkpoints").load(mail_selection) == mail_checkpoint
    assert mail_path.read_text(encoding="utf-8").endswith("\n")
    assert "Synthetic body" not in mail_path.read_text(encoding="utf-8")

    drive = DriveSourceAdapter()
    drive_selection = drive.file_selection(
        connection_id="account:drive-fixture",
        file_id="drive-file-123",
    )
    drive_record = drive.record_from_rest(
        {
            "id": "drive-file-123",
            "revision_id": "rev-1",
            "web_url": "https://drive.google.com/file/d/drive-file-123/view",
            "name": "Fixture Drive document",
            "text": "Synthetic Drive body excluded from checkpoint.",
            "mime_type": "text/plain",
        }
    )
    drive_page = drive.preview(drive_selection, (drive_record,), privacy=_privacy())
    drive_intake = drive.intake(drive_selection, drive_record, privacy=_privacy())
    drive_checkpoint = DriveFileCheckpoint.initial(drive_selection).advance(
        drive_page,
        committed_delivery_ids=tuple(record.delivery_id for record in drive_page.records),
        committed_revision_identities=(drive_intake.key.revision_identity(),),
    )

    drive_path = DriveFileCheckpointStore(tmp_path / "checkpoints").save(drive_checkpoint)

    assert (
        DriveFileCheckpointStore(tmp_path / "checkpoints").load(drive_selection)
        == drive_checkpoint
    )
    assert drive_path.read_text(encoding="utf-8").endswith("\n")
    assert "Synthetic Drive body" not in drive_path.read_text(encoding="utf-8")


def test_mail_and_drive_model_rate_limit_reauth_lost_access_and_attachment_exclusion() -> None:
    gmail = GmailSourceAdapter()
    drive = DriveSourceAdapter()

    assert gmail.rate_limited_page(retry_after_seconds=60).status is MailPageStatus.RATE_LIMITED
    assert gmail.needs_sign_in_page().status is MailPageStatus.NEEDS_SIGN_IN
    assert gmail.not_allowed_page().status is MailPageStatus.NOT_ALLOWED
    assert gmail.unsupported_attachment_page().status is MailPageStatus.UNSUPPORTED_ATTACHMENT
    assert drive.rate_limited_page(retry_after_seconds=60).status is MailPageStatus.RATE_LIMITED
    assert drive.needs_sign_in_page().status is MailPageStatus.NEEDS_SIGN_IN
    assert drive.not_allowed_page().status is MailPageStatus.NOT_ALLOWED


def test_mail_adapter_rejects_cross_label_or_folder_records() -> None:
    gmail = GmailSourceAdapter()
    selection = gmail.selection(
        connection_id="account:gmail-fixture",
        resource_id="label:OpenBrain",
    )
    record = gmail.record_from_rest(
        {
            "id": "msg_1",
            "resource_id": "label:Other",
            "web_url": "https://mail.google.com/mail/u/0/#inbox/msg_1",
            "subject": "Wrong Gmail message",
            "body_text": "Synthetic cross-label body.",
            "updated_at": "2026-09-15T12:00:00Z",
        }
    )

    with pytest.raises(ConnectorContractError, match="invalid gmail record"):
        gmail.preview(selection, (record,), privacy=_privacy())

    microsoft = MicrosoftMailSourceAdapter()
    folder_selection = microsoft.selection(
        connection_id="account:m365-fixture",
        resource_id="folder:Inbox",
    )
    folder_record = microsoft.record_from_rest(
        {
            "id": "message-a",
            "resource_id": "folder:Archive",
            "web_url": "https://outlook.office.com/mail/inbox/id/message-a",
            "subject": "Wrong Microsoft 365 message",
            "body_text": "Synthetic cross-folder body.",
            "updated_at": "2026-09-15T12:00:00Z",
        }
    )

    with pytest.raises(ConnectorContractError, match="invalid microsoft_mail record"):
        microsoft.preview(folder_selection, (folder_record,), privacy=_privacy())


def test_drive_adapter_rejects_cross_file_records() -> None:
    adapter = DriveSourceAdapter()
    selection = adapter.file_selection(
        connection_id="account:drive-fixture",
        file_id="drive-file-123",
    )
    record = adapter.record_from_rest(
        {
            "id": "other-drive-file",
            "revision_id": "rev-1",
            "web_url": "https://drive.google.com/file/d/other-drive-file/view",
            "name": "Wrong Drive document",
            "text": "Synthetic cross-file body.",
            "mime_type": "text/plain",
        }
    )

    with pytest.raises(ConnectorContractError, match="invalid google drive record"):
        adapter.preview(selection, (record,), privacy=_privacy())


def _capture_sink(tmp_path: Path) -> ConnectorCaptureSink:
    tasks = open_local_engine(compile_single_user_local(tmp_path / f"brain-{uuid4()}"))
    actor_id = f"actor_{uuid4()}"
    context = PublicJobCaptureContext.create(
        profile=tasks.profile,
        actor_id=actor_id,
        role_claim={
            "actor_id": actor_id,
            "capabilities": ["capture.accept"],
            "role_claim_id": f"role_claim_{uuid4()}",
            "role_id": f"role_{uuid4()}",
            "tenant_id": tasks.profile.tenant_id,
        },
    )
    sink = tasks.capture.public_job_sink(context)
    return ConnectorCaptureSink(
        sink,
        ConnectorBudget(ConnectorBudgetLimits(max_submissions=8)),
        ConnectorRunEvidence(),
    )

from __future__ import annotations

import json
from pathlib import Path

import pytest

from open_brain_connectors.runtime.connectors import (
    ConnectorContractError,
    ConnectorFailureCode,
    ConnectorOutcome,
)
from open_brain_connectors.runtime.meeting_transcript import (
    MeetingTranscriptCheckpoint,
    MeetingTranscriptCheckpointStore,
    MeetingTranscriptPage,
    MeetingTranscriptPageStatus,
    MeetingTranscriptRecord,
    MeetingTranscriptSourceAdapter,
)
from open_brain_connectors.runtime.source_intake import SourceRecordIntake
from open_brain_connectors.runtime.source_registry import SourcePreviewPage, SourcePreviewRecord

from .test_agent_session_source_adapter import _capture_sink
from .test_source_intake import _privacy

_MEETING_ID = "meeting:weekly-sync"
_TRANSCRIPT_ID = "transcript:weekly-sync-v1"


def test_meeting_transcript_adapter_builds_metadata_preview_without_body() -> None:
    adapter = MeetingTranscriptSourceAdapter()
    selection = adapter.meeting_selection(
        connection_id="account:meeting-fixture",
        meeting_id=_MEETING_ID,
        provider="zoom",
    )

    page = adapter.page_from_transcripts(
        selection,
        (
            _transcript(
                transcript="Synthetic meeting transcript body must not appear in preview.",
                speaker_segments=("Ada 00:01 Synthetic segment must not appear.",),
            ),
        ),
        privacy=_privacy(),
        selected_meeting_ids=(_MEETING_ID,),
        next_cursor="cursor:meeting2",
    )

    assert page.status is MeetingTranscriptPageStatus.READY
    assert page.preview is not None
    assert page.preview.selection == selection
    assert page.preview.next_cursor == "cursor:meeting2"
    assert [record.content_type for record in page.preview.records] == ["meeting_transcript"]
    assert page.preview.records[0].title == "Weekly Sync"
    assert "must not appear" not in repr(page.preview.to_dict())


def test_meeting_transcript_intake_uses_stable_reference_without_provider_link() -> None:
    adapter = MeetingTranscriptSourceAdapter()
    selection = adapter.meeting_selection(
        connection_id="account:meeting-fixture",
        meeting_id=_MEETING_ID,
        provider="zoom",
    )
    intake = adapter.intake(
        selection,
        adapter.record_from_transcript(
            _transcript(source_link="https://zoom.example.invalid/recording/weekly-sync")
        ),
        privacy=_privacy(),
    )

    assert intake.url.startswith("https://local.openbrain.invalid/meeting-transcripts/zoom/")
    assert "zoom.example.invalid" not in intake.text
    assert "Synthetic transcript." in intake.text


def test_meeting_transcript_delivery_identity_can_follow_provider_artifact() -> None:
    adapter = MeetingTranscriptSourceAdapter()
    selection = adapter.meeting_selection(
        connection_id="account:meeting-fixture",
        meeting_id=_MEETING_ID,
        provider="zoom",
    )
    google_selection = adapter.meeting_selection(
        connection_id="account:meeting-fixture",
        meeting_id=_MEETING_ID,
        provider="google_meet",
    )
    first = adapter.intake(
        selection,
        adapter.record_from_transcript(
            _transcript(
                transcript_id="transcript:provider-generated-1",
                source_link="https://zoom.example.invalid/recording/shared-artifact",
            )
        ),
        privacy=_privacy(),
    )
    same_provider_artifact_from_google_meet = adapter.intake(
        google_selection,
        adapter.record_from_transcript(
            _transcript(
                provider="google_meet",
                transcript_id="transcript:provider-generated-1",
                source_link="https://zoom.example.invalid/recording/shared-artifact",
            )
        ),
        privacy=_privacy(),
    )
    document_overlap = adapter.intake(
        selection,
        adapter.record_from_transcript(
            _transcript(
                revision_id="document-extract-rev-99",
                transcript_id="transcript:document-imported-copy",
                source_link="https://zoom.example.invalid/recording/shared-artifact",
            )
        ),
        privacy=_privacy(),
    )
    changed = adapter.intake(
        selection,
        adapter.record_from_transcript(
            _transcript(
                revision_id="rev-2",
                transcript="Synthetic changed transcript.",
                transcript_id="transcript:provider-generated-1",
                source_link="https://zoom.example.invalid/recording/shared-artifact",
            )
        ),
        privacy=_privacy(),
    )

    assert first.key.delivery_id() == document_overlap.key.delivery_id()
    assert first.key.revision_identity() == document_overlap.key.revision_identity()
    assert first.key.revision_identity() != changed.key.revision_identity()
    assert first.key.delivery_id() != same_provider_artifact_from_google_meet.key.delivery_id()
    assert (
        first.key.revision_identity()
        != same_provider_artifact_from_google_meet.key.revision_identity()
    )
    assert first.source_reference == document_overlap.source_reference
    assert "zoom.example.invalid" not in first.source_reference


def test_meeting_transcript_document_overlap_replays_same_artifact_identity(
    tmp_path: Path,
) -> None:
    adapter = MeetingTranscriptSourceAdapter()
    selection = adapter.meeting_selection(
        connection_id="account:meeting-fixture",
        meeting_id=_MEETING_ID,
        provider="zoom",
    )
    provider_payload = _transcript(
        transcript_id="transcript:provider-generated-1",
        source_link="https://zoom.example.invalid/recording/shared-artifact",
    )
    document_payload = _transcript(
        revision_id="document-extract-rev-99",
        transcript_id="transcript:document-imported-copy",
        source_link="https://zoom.example.invalid/recording/shared-artifact",
    )
    provider_record = adapter.record_from_transcript(provider_payload)
    document_record = adapter.record_from_transcript(document_payload)
    provider_intake = adapter.intake(selection, provider_record, privacy=_privacy())
    document_intake = adapter.intake(selection, document_record, privacy=_privacy())
    sink = _capture_sink(tmp_path)

    checkpoint, receipt = adapter.import_page(
        MeetingTranscriptCheckpoint.initial(selection),
        adapter.page_from_transcripts(
            selection,
            (provider_payload,),
            privacy=_privacy(),
            selected_meeting_ids=(_MEETING_ID,),
        ),
        (provider_intake,),
        sink,
    )
    replayed_checkpoint, replayed_receipt = adapter.import_page(
        checkpoint,
        adapter.page_from_transcripts(
            selection,
            (document_payload,),
            privacy=_privacy(),
            selected_meeting_ids=(_MEETING_ID,),
        ),
        (document_intake,),
        sink,
    )

    assert receipt.outcome is ConnectorOutcome.COMPLETED
    assert provider_intake.key.delivery_id() == document_intake.key.delivery_id()
    assert provider_intake.key.revision_identity() == document_intake.key.revision_identity()
    assert provider_intake.source_reference == document_intake.source_reference
    assert replayed_checkpoint == checkpoint
    assert replayed_receipt.outcome is ConnectorOutcome.EMPTY
    assert "zoom.example.invalid" not in provider_intake.source_reference
    assert "zoom.example.invalid" not in document_intake.source_reference


def test_meeting_transcript_preview_coalesces_same_page_document_overlap() -> None:
    adapter = MeetingTranscriptSourceAdapter()
    selection = adapter.meeting_selection(
        connection_id="account:meeting-fixture",
        meeting_id=_MEETING_ID,
        provider="zoom",
    )

    page = adapter.page_from_transcripts(
        selection,
        (
            _transcript(
                transcript_id="transcript:provider-generated-1",
                source_link="https://zoom.example.invalid/recording/shared-artifact",
                title="Provider Transcript",
            ),
            _transcript(
                revision_id="document-extract-rev-99",
                transcript_id="transcript:document-imported-copy",
                source_link="https://zoom.example.invalid/recording/shared-artifact",
                title="Document Imported Transcript",
            ),
        ),
        privacy=_privacy(),
        selected_meeting_ids=(_MEETING_ID,),
    )

    assert page.preview is not None
    assert len(page.preview.records) == 1
    assert page.preview.records[0].title == "Document Imported Transcript"
    assert "zoom.example.invalid" not in page.preview.records[0].source_reference


def test_meeting_transcript_import_updates_revision_checkpoint_and_replays_safely(
    tmp_path: Path,
) -> None:
    adapter = MeetingTranscriptSourceAdapter()
    selection = adapter.meeting_selection(
        connection_id="account:meeting-fixture",
        meeting_id=_MEETING_ID,
        provider="google_meet",
    )
    original = adapter.record_from_transcript(
        _transcript(
            provider="google_meet", transcript="Synthetic original transcript.", revision_id="rev-1"
        )
    )
    original_page = adapter.preview(selection, (original,), privacy=_privacy())
    original_intake = adapter.intake(selection, original, privacy=_privacy())
    sink = _capture_sink(tmp_path)

    checkpoint, receipt = adapter.import_page(
        MeetingTranscriptCheckpoint.initial(selection),
        adapter.page_from_transcripts(
            selection,
            (
                _transcript(
                    provider="google_meet",
                    transcript="Synthetic original transcript.",
                    revision_id="rev-1",
                ),
            ),
            privacy=_privacy(),
            selected_meeting_ids=(_MEETING_ID,),
        ),
        (original_intake,),
        sink,
    )

    assert receipt.outcome is ConnectorOutcome.COMPLETED
    assert checkpoint.committed_revision_identities == (original_intake.key.revision_identity(),)

    replayed_checkpoint, replayed_receipt = adapter.import_page(
        checkpoint,
        adapter.page_from_transcripts(
            selection,
            (
                _transcript(
                    provider="google_meet",
                    transcript="Synthetic original transcript.",
                    revision_id="rev-1",
                ),
            ),
            privacy=_privacy(),
            selected_meeting_ids=(_MEETING_ID,),
        ),
        (original_intake,),
        sink,
    )

    assert replayed_checkpoint == checkpoint
    assert replayed_receipt.outcome is ConnectorOutcome.EMPTY

    changed = adapter.record_from_transcript(
        _transcript(
            provider="google_meet", transcript="Synthetic changed transcript.", revision_id="rev-2"
        )
    )
    changed_page = adapter.preview(selection, (changed,), privacy=_privacy())
    changed_intake = adapter.intake(selection, changed, privacy=_privacy())
    changed_checkpoint, changed_receipt = adapter.import_page(
        checkpoint,
        adapter.page_from_transcripts(
            selection,
            (
                _transcript(
                    provider="google_meet",
                    transcript="Synthetic changed transcript.",
                    revision_id="rev-2",
                ),
            ),
            privacy=_privacy(),
            selected_meeting_ids=(_MEETING_ID,),
        ),
        (changed_intake,),
        sink,
    )

    assert original_page.records[0].delivery_id == changed_page.records[0].delivery_id
    assert changed_receipt.outcome is ConnectorOutcome.COMPLETED
    assert changed_receipt.created_count == 1
    assert changed_checkpoint.committed_delivery_ids == checkpoint.committed_delivery_ids
    assert changed_checkpoint.committed_revision_identities == (
        changed_intake.key.revision_identity(),
    )


def test_meeting_transcript_checkpoint_retains_prior_pages_without_resubmitting(
    tmp_path: Path,
) -> None:
    adapter = MeetingTranscriptSourceAdapter()
    selection = adapter.meeting_selection(
        connection_id="account:meeting-fixture",
        meeting_id=_MEETING_ID,
        provider="zoom",
    )
    first = adapter.record_from_transcript(
        _transcript(
            transcript_id="transcript:first",
            revision_id="rev-first",
            source_link=None,
            title="First transcript",
        )
    )
    second = adapter.record_from_transcript(
        _transcript(
            transcript_id="transcript:second",
            revision_id="rev-second",
            source_link=None,
            title="Second transcript",
            transcript="Synthetic second transcript.",
        )
    )
    first_page = MeetingTranscriptPage(
        MeetingTranscriptPageStatus.READY,
        adapter.preview(selection, (first,), privacy=_privacy(), next_cursor="cursor:page2"),
    )
    second_page = MeetingTranscriptPage(
        MeetingTranscriptPageStatus.READY,
        adapter.preview(selection, (second,), privacy=_privacy(), next_cursor="cursor:page3"),
    )
    first_intake = adapter.intake(selection, first, privacy=_privacy())
    second_intake = adapter.intake(selection, second, privacy=_privacy())
    sink = _capture_sink(tmp_path)

    first_checkpoint, first_receipt = adapter.import_page(
        MeetingTranscriptCheckpoint.initial(selection),
        first_page,
        (first_intake,),
        sink,
    )
    second_checkpoint, second_receipt = adapter.import_page(
        first_checkpoint,
        second_page,
        (second_intake,),
        sink,
    )
    replay_checkpoint, replay_receipt = adapter.import_page(
        second_checkpoint,
        first_page,
        (first_intake,),
        sink,
    )

    assert first_receipt.outcome is ConnectorOutcome.COMPLETED
    assert second_receipt.outcome is ConnectorOutcome.COMPLETED
    assert second_checkpoint.next_cursor == "cursor:page3"
    assert second_checkpoint.observed_page_cursors == ("cursor:page2", "cursor:page3")
    assert second_checkpoint.committed_delivery_ids == (
        first_intake.key.delivery_id(),
        second_intake.key.delivery_id(),
    )
    assert second_checkpoint.committed_revision_identities == (
        first_intake.key.revision_identity(),
        second_intake.key.revision_identity(),
    )
    assert replay_checkpoint == second_checkpoint
    assert replay_receipt.outcome is ConnectorOutcome.EMPTY
    assert replay_receipt.submitted_count == 0


def test_meeting_transcript_duplicate_only_later_page_advances_cursor_without_rewind(
    tmp_path: Path,
) -> None:
    adapter = MeetingTranscriptSourceAdapter()
    selection = adapter.meeting_selection(
        connection_id="account:meeting-fixture",
        meeting_id=_MEETING_ID,
        provider="zoom",
    )
    first = adapter.record_from_transcript(
        _transcript(
            transcript_id="transcript:first",
            revision_id="rev-first",
            source_link=None,
            title="First transcript",
        )
    )
    second = adapter.record_from_transcript(
        _transcript(
            transcript_id="transcript:second",
            revision_id="rev-second",
            source_link=None,
            title="Second transcript",
            transcript="Synthetic second transcript.",
        )
    )
    first_page = MeetingTranscriptPage(
        MeetingTranscriptPageStatus.READY,
        adapter.preview(selection, (first,), privacy=_privacy(), next_cursor="cursor:page2"),
    )
    second_page = MeetingTranscriptPage(
        MeetingTranscriptPageStatus.READY,
        adapter.preview(selection, (second,), privacy=_privacy(), next_cursor="cursor:page3"),
    )
    duplicate_later_page = MeetingTranscriptPage(
        MeetingTranscriptPageStatus.READY,
        adapter.preview(selection, (first,), privacy=_privacy(), next_cursor="cursor:page4"),
    )
    first_intake = adapter.intake(selection, first, privacy=_privacy())
    second_intake = adapter.intake(selection, second, privacy=_privacy())
    sink = _capture_sink(tmp_path)

    first_checkpoint, _first_receipt = adapter.import_page(
        MeetingTranscriptCheckpoint.initial(selection),
        first_page,
        (first_intake,),
        sink,
    )
    second_checkpoint, _second_receipt = adapter.import_page(
        first_checkpoint,
        second_page,
        (second_intake,),
        sink,
    )
    advanced_checkpoint, advanced_receipt = adapter.import_page(
        second_checkpoint,
        duplicate_later_page,
        (first_intake,),
        sink,
    )

    assert advanced_checkpoint.next_cursor == "cursor:page4"
    assert advanced_checkpoint.committed_delivery_ids == second_checkpoint.committed_delivery_ids
    assert (
        advanced_checkpoint.committed_revision_identities
        == second_checkpoint.committed_revision_identities
    )
    assert advanced_receipt.outcome is ConnectorOutcome.COMPLETED
    assert advanced_receipt.submitted_count == 0
    assert advanced_receipt.checkpoint_committed is True


def test_meeting_transcript_changed_stale_page_does_not_rewind_cursor(
    tmp_path: Path,
) -> None:
    adapter = MeetingTranscriptSourceAdapter()
    selection = adapter.meeting_selection(
        connection_id="account:meeting-fixture",
        meeting_id=_MEETING_ID,
        provider="zoom",
    )
    first = adapter.record_from_transcript(
        _transcript(
            transcript_id="transcript:first",
            revision_id="rev-first",
            source_link=None,
            title="First transcript",
        )
    )
    second = adapter.record_from_transcript(
        _transcript(
            transcript_id="transcript:second",
            revision_id="rev-second",
            source_link=None,
            title="Second transcript",
            transcript="Synthetic second transcript.",
        )
    )
    changed_first = adapter.record_from_transcript(
        _transcript(
            transcript_id="transcript:first",
            revision_id="rev-first-updated",
            source_link=None,
            title="First transcript",
            transcript="Synthetic updated first transcript.",
        )
    )
    first_page = MeetingTranscriptPage(
        MeetingTranscriptPageStatus.READY,
        adapter.preview(selection, (first,), privacy=_privacy(), next_cursor="cursor:page2"),
    )
    second_page = MeetingTranscriptPage(
        MeetingTranscriptPageStatus.READY,
        adapter.preview(selection, (second,), privacy=_privacy(), next_cursor="cursor:page3"),
    )
    changed_first_replay = MeetingTranscriptPage(
        MeetingTranscriptPageStatus.READY,
        adapter.preview(
            selection,
            (changed_first,),
            privacy=_privacy(),
            next_cursor="cursor:page2",
        ),
    )
    first_intake = adapter.intake(selection, first, privacy=_privacy())
    second_intake = adapter.intake(selection, second, privacy=_privacy())
    changed_first_intake = adapter.intake(selection, changed_first, privacy=_privacy())
    sink = _capture_sink(tmp_path)

    first_checkpoint, _first_receipt = adapter.import_page(
        MeetingTranscriptCheckpoint.initial(selection),
        first_page,
        (first_intake,),
        sink,
    )
    second_checkpoint, _second_receipt = adapter.import_page(
        first_checkpoint,
        second_page,
        (second_intake,),
        sink,
    )
    changed_checkpoint, changed_receipt = adapter.import_page(
        second_checkpoint,
        changed_first_replay,
        (changed_first_intake,),
        sink,
    )

    assert second_checkpoint.next_cursor == "cursor:page3"
    assert second_checkpoint.observed_page_cursors == ("cursor:page2", "cursor:page3")
    assert changed_checkpoint.next_cursor == "cursor:page3"
    assert changed_checkpoint.observed_page_cursors == second_checkpoint.observed_page_cursors
    assert changed_checkpoint.committed_delivery_ids == second_checkpoint.committed_delivery_ids
    assert changed_checkpoint.committed_revision_identities == (
        changed_first_intake.key.revision_identity(),
        second_intake.key.revision_identity(),
    )
    assert changed_receipt.outcome is ConnectorOutcome.COMPLETED
    assert changed_receipt.submitted_count == 1


def test_meeting_transcript_duplicate_only_page_advances_cursor_without_submit(
    tmp_path: Path,
) -> None:
    adapter = MeetingTranscriptSourceAdapter()
    selection = adapter.meeting_selection(
        connection_id="account:meeting-fixture",
        meeting_id=_MEETING_ID,
        provider="zoom",
    )
    provider_payload = _transcript(
        transcript_id="transcript:provider-generated-1",
        source_link="https://zoom.example.invalid/recording/shared-artifact",
    )
    document_payload = _transcript(
        revision_id="document-extract-rev-99",
        transcript_id="transcript:document-imported-copy",
        source_link="https://zoom.example.invalid/recording/shared-artifact",
    )
    provider_record = adapter.record_from_transcript(provider_payload)
    document_record = adapter.record_from_transcript(document_payload)
    provider_intake = adapter.intake(selection, provider_record, privacy=_privacy())
    document_intake = adapter.intake(selection, document_record, privacy=_privacy())
    provider_page = MeetingTranscriptPage(
        MeetingTranscriptPageStatus.READY,
        adapter.preview(
            selection,
            (provider_record,),
            privacy=_privacy(),
            next_cursor="cursor:document-page",
        ),
    )
    document_page = MeetingTranscriptPage(
        MeetingTranscriptPageStatus.READY,
        adapter.preview(
            selection,
            (document_record,),
            privacy=_privacy(),
            next_cursor="cursor:after-document-page",
        ),
    )
    sink = _capture_sink(tmp_path)

    checkpoint, receipt = adapter.import_page(
        MeetingTranscriptCheckpoint.initial(selection),
        provider_page,
        (provider_intake,),
        sink,
    )
    duplicate_checkpoint, duplicate_receipt = adapter.import_page(
        checkpoint,
        document_page,
        (document_intake,),
        sink,
    )

    assert receipt.outcome is ConnectorOutcome.COMPLETED
    assert provider_intake.key.delivery_id() == document_intake.key.delivery_id()
    assert provider_intake.key.revision_identity() == document_intake.key.revision_identity()
    assert duplicate_checkpoint.next_cursor == "cursor:after-document-page"
    assert duplicate_receipt.outcome is ConnectorOutcome.COMPLETED
    assert duplicate_receipt.submitted_count == 0
    assert duplicate_receipt.checkpoint_committed is True


def test_meeting_transcript_import_treats_all_deselected_preview_as_empty(
    tmp_path: Path,
) -> None:
    adapter = MeetingTranscriptSourceAdapter()
    selection = adapter.meeting_selection(
        connection_id="account:meeting-fixture",
        meeting_id=_MEETING_ID,
        provider="zoom",
    )
    record = adapter.record_from_transcript(_transcript())
    intake = adapter.intake(selection, record, privacy=_privacy())
    preview = adapter.preview(selection, (record,), privacy=_privacy())
    deselected_page = SourcePreviewPage(
        selection=selection,
        records=tuple(
            SourcePreviewRecord(
                connector_name=item.connector_name,
                connection_id=item.connection_id,
                resource_id=item.resource_id,
                delivery_id=item.delivery_id,
                source_reference=item.source_reference,
                title=item.title,
                content_type=item.content_type,
                selected=False,
            )
            for item in preview.records
        ),
        next_cursor=preview.next_cursor,
    )

    checkpoint, receipt = adapter.import_page(
        MeetingTranscriptCheckpoint.initial(selection),
        MeetingTranscriptPage(MeetingTranscriptPageStatus.READY, deselected_page),
        (),
        _capture_sink(tmp_path),
    )

    assert checkpoint == MeetingTranscriptCheckpoint.initial(selection)
    assert receipt.outcome is ConnectorOutcome.EMPTY
    assert receipt.metadata_count == 1
    assert intake.key.delivery_id() == preview.records[0].delivery_id


def test_meeting_transcript_adapter_requires_selected_meeting_and_provider() -> None:
    adapter = MeetingTranscriptSourceAdapter()
    selection = adapter.meeting_selection(
        connection_id="account:meeting-fixture",
        meeting_id=_MEETING_ID,
        provider="zoom",
    )

    page = adapter.page_from_transcripts(
        selection,
        (
            _transcript(meeting_id="meeting:other", transcript="api_key = excluded"),
            _transcript(title="Selected Meeting"),
        ),
        privacy=_privacy(),
        selected_meeting_ids=(_MEETING_ID,),
    )

    assert page.preview is not None
    assert len(page.preview.records) == 1
    assert page.preview.records[0].title == "Selected Meeting"

    poisoned_wrong_provider = _transcript(
        provider="google_meet",
        title="Wrong Provider",
        transcript="client_secret = wrong-provider-body-must-not-be-read",
        transcript_secret_scan="finding",
    )
    filtered_page = adapter.page_from_transcripts(
        selection,
        (
            poisoned_wrong_provider,
            _transcript(title="Selected Zoom Meeting"),
        ),
        privacy=_privacy(),
        selected_meeting_ids=(_MEETING_ID,),
    )

    assert filtered_page.preview is not None
    assert len(filtered_page.preview.records) == 1
    assert filtered_page.preview.records[0].title == "Selected Zoom Meeting"
    assert "wrong-provider-body" not in repr(filtered_page.preview.to_dict())

    with pytest.raises(ConnectorContractError, match="invalid meeting transcripts"):
        adapter.page_from_transcripts(
            selection,
            (_transcript(provider="google_meet"),),
            privacy=_privacy(),
            selected_meeting_ids=(_MEETING_ID,),
        )
    with pytest.raises(ConnectorContractError, match="invalid meeting transcripts"):
        adapter.page_from_transcripts(
            selection,
            (_transcript(),),
            privacy=_privacy(),
            selected_meeting_ids=(),
        )
    with pytest.raises(ConnectorContractError, match="invalid meeting transcripts"):
        adapter.page_from_transcripts(
            selection,
            (_transcript(),),
            privacy=_privacy(),
            selected_meeting_ids=(_MEETING_ID, "meeting:other"),
        )


def test_meeting_transcript_import_rejects_spoofed_intake_source_reference(
    tmp_path: Path,
) -> None:
    adapter = MeetingTranscriptSourceAdapter()
    selection = adapter.meeting_selection(
        connection_id="account:meeting-fixture",
        meeting_id=_MEETING_ID,
        provider="zoom",
    )
    record = adapter.record_from_transcript(_transcript())
    preview = adapter.preview(selection, (record,), privacy=_privacy())
    page = adapter.page_from_transcripts(
        selection,
        (_transcript(),),
        privacy=_privacy(),
        selected_meeting_ids=(_MEETING_ID,),
    )
    valid_intake = adapter.intake(selection, record, privacy=_privacy())
    spoofed_intake = SourceRecordIntake(
        key=valid_intake.key,
        url="https://example.invalid/spoofed",
        title=valid_intake.title,
        text=valid_intake.text,
        privacy=valid_intake.privacy,
    )

    with pytest.raises(ConnectorContractError, match="invalid meeting transcript import"):
        adapter.import_page(
            MeetingTranscriptCheckpoint.initial(selection),
            page,
            (spoofed_intake,),
            _capture_sink(tmp_path),
        )

    assert page.preview == preview


def test_meeting_transcript_adapter_rejects_secret_feedback_and_invalid_provider() -> None:
    adapter = MeetingTranscriptSourceAdapter()

    with pytest.raises(ConnectorContractError, match="invalid meeting transcript"):
        adapter.record_from_transcript(_transcript(source_kind="open_brain_result"))
    with pytest.raises(ConnectorContractError, match="invalid meeting transcript"):
        adapter.record_from_transcript(_transcript(transcript_secret_scan="finding"))
    with pytest.raises(ConnectorContractError, match="invalid meeting transcript"):
        adapter.record_from_transcript(_transcript(transcript="client_secret = synthetic-secret"))
    with pytest.raises(ConnectorContractError, match="invalid meeting transcript"):
        adapter.record_from_transcript(_transcript(transcript=""))
    with pytest.raises(ConnectorContractError, match="invalid meeting transcript"):
        adapter.record_from_transcript(_transcript(transcript="  \n\t  "))
    missing_transcript = _transcript()
    del missing_transcript["transcript"]
    with pytest.raises(ConnectorContractError, match="invalid meeting transcript"):
        adapter.record_from_transcript(missing_transcript)
    with pytest.raises(ConnectorContractError, match="invalid meeting transcript"):
        MeetingTranscriptRecord(
            meeting_id=_MEETING_ID,
            transcript_id=_TRANSCRIPT_ID,
            revision_id="rev-1",
            provider="teams",
            title="Weekly Sync",
            transcript="Synthetic transcript.",
            started_at="2026-09-15T15:00:00Z",
        )
    with pytest.raises(ConnectorContractError, match="invalid meeting transcript"):
        MeetingTranscriptRecord(
            meeting_id=_MEETING_ID,
            transcript_id=_TRANSCRIPT_ID,
            revision_id="rev-1",
            provider="zoom",
            title="Weekly Sync",
            transcript="",
            started_at="2026-09-15T15:00:00Z",
        )


def test_meeting_transcript_adapter_models_permission_and_availability_statuses(
    tmp_path: Path,
) -> None:
    adapter = MeetingTranscriptSourceAdapter()
    checkpoint = MeetingTranscriptCheckpoint.initial(
        adapter.meeting_selection(
            connection_id="account:meeting-fixture",
            meeting_id=_MEETING_ID,
            provider="zoom",
        )
    )

    assert adapter.not_allowed_page().status is MeetingTranscriptPageStatus.NOT_ALLOWED
    assert (
        adapter.transcript_unavailable_page().status
        is MeetingTranscriptPageStatus.TRANSCRIPT_UNAVAILABLE
    )
    assert adapter.token_invalidated_page().status is MeetingTranscriptPageStatus.TOKEN_INVALIDATED
    assert (
        adapter.unsupported_provider_page().status
        is MeetingTranscriptPageStatus.UNSUPPORTED_PROVIDER
    )
    assert (
        adapter.import_page(
            checkpoint,
            adapter.not_allowed_page(),
            (),
            _capture_sink(tmp_path),
        )[1].failure_code
        is ConnectorFailureCode.NOT_ALLOWED
    )
    assert (
        adapter.import_page(
            checkpoint,
            adapter.transcript_unavailable_page(),
            (),
            _capture_sink(tmp_path),
        )[1].failure_code
        is ConnectorFailureCode.NOT_ALLOWED
    )
    assert (
        adapter.import_page(
            checkpoint,
            adapter.token_invalidated_page(),
            (),
            _capture_sink(tmp_path),
        )[1].failure_code
        is ConnectorFailureCode.NOT_ALLOWED
    )
    assert (
        adapter.import_page(
            checkpoint,
            adapter.unsupported_provider_page(),
            (),
            _capture_sink(tmp_path),
        )[1].failure_code
        is ConnectorFailureCode.UNSUPPORTED_CAPABILITY
    )


def test_meeting_transcript_unavailable_statuses_preserve_checkpoint(
    tmp_path: Path,
) -> None:
    adapter = MeetingTranscriptSourceAdapter()
    selection = adapter.meeting_selection(
        connection_id="account:meeting-fixture",
        meeting_id=_MEETING_ID,
        provider="zoom",
    )
    record = adapter.record_from_transcript(_transcript())
    page = adapter.preview(selection, (record,), privacy=_privacy(), next_cursor="cursor:after")
    intake = adapter.intake(selection, record, privacy=_privacy())
    checkpoint = MeetingTranscriptCheckpoint.initial(selection).advance(
        page,
        committed_delivery_ids=tuple(record.delivery_id for record in page.records),
        committed_revision_identities=(intake.key.revision_identity(),),
    )
    sink = _capture_sink(tmp_path)

    for unavailable_page, expected_code in (
        (adapter.not_allowed_page(), ConnectorFailureCode.NOT_ALLOWED),
        (adapter.transcript_unavailable_page(), ConnectorFailureCode.NOT_ALLOWED),
        (adapter.token_invalidated_page(), ConnectorFailureCode.NOT_ALLOWED),
        (adapter.unsupported_provider_page(), ConnectorFailureCode.UNSUPPORTED_CAPABILITY),
    ):
        retained_checkpoint, receipt = adapter.import_page(
            checkpoint,
            unavailable_page,
            (),
            sink,
        )

        assert retained_checkpoint == checkpoint
        assert receipt.outcome is ConnectorOutcome.FAILED
        assert receipt.failure_code is expected_code
        assert receipt.submitted_count == 0
        assert receipt.checkpoint_committed is False


def test_meeting_transcript_checkpoint_store_is_metadata_only_and_rejects_mismatch(
    tmp_path: Path,
) -> None:
    adapter = MeetingTranscriptSourceAdapter()
    selection = adapter.meeting_selection(
        connection_id="account:meeting-fixture",
        meeting_id=_MEETING_ID,
        provider="zoom",
    )
    other_selection = adapter.meeting_selection(
        connection_id="account:meeting-fixture",
        meeting_id="meeting:other",
        provider="zoom",
    )
    same_meeting_google_selection = adapter.meeting_selection(
        connection_id="account:meeting-fixture",
        meeting_id=_MEETING_ID,
        provider="google_meet",
    )
    record = adapter.record_from_transcript(_transcript(transcript="Synthetic excluded text."))
    page = adapter.preview(selection, (record,), privacy=_privacy())
    intake = adapter.intake(selection, record, privacy=_privacy())
    checkpoint = MeetingTranscriptCheckpoint.initial(selection).advance(
        page,
        committed_delivery_ids=tuple(record.delivery_id for record in page.records),
        committed_revision_identities=(intake.key.revision_identity(),),
    )
    store = MeetingTranscriptCheckpointStore(tmp_path / "checkpoints")

    path = store.save(checkpoint)

    assert store.load(selection) == checkpoint
    assert store.load(same_meeting_google_selection) == MeetingTranscriptCheckpoint.initial(
        same_meeting_google_selection
    )
    assert store._path(selection) != store._path(same_meeting_google_selection)
    assert path.read_text(encoding="utf-8").endswith("\n")
    assert "Synthetic excluded" not in path.read_text(encoding="utf-8")

    path.write_text(
        json.dumps(MeetingTranscriptCheckpoint.initial(other_selection).to_dict()),
        encoding="utf-8",
    )
    with pytest.raises(
        ConnectorContractError,
        match="invalid meeting transcript checkpoint store",
    ):
        store.load(selection)


def _transcript(
    *,
    meeting_id: str = _MEETING_ID,
    transcript_id: str = _TRANSCRIPT_ID,
    revision_id: str = "rev-1",
    provider: str = "zoom",
    title: str = "Weekly Sync",
    transcript: str = "Synthetic transcript.",
    started_at: str = "2026-09-15T15:00:00Z",
    speaker_segments: tuple[str, ...] = ("Ada 00:01 Synthetic transcript segment.",),
    source_link: str | None = "https://zoom.example.invalid/recording/weekly-sync",
    source_kind: object = "meeting_transcript",
    transcript_secret_scan: object = "clean",
) -> dict[str, object]:
    payload: dict[str, object] = {
        "meeting_id": meeting_id,
        "provider": provider,
        "revision_id": revision_id,
        "source_kind": source_kind,
        "speaker_segments": list(speaker_segments),
        "started_at": started_at,
        "title": title,
        "transcript": transcript,
        "transcript_id": transcript_id,
        "transcript_secret_scan": transcript_secret_scan,
    }
    if source_link is not None:
        payload["source_link"] = source_link
    return payload

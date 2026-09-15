from __future__ import annotations

import json
from pathlib import Path

import pytest

from open_brain_connectors.runtime.connectors import ConnectorContractError, ConnectorOutcome
from open_brain_connectors.runtime.local_document import (
    LocalDocumentCheckpoint,
    LocalDocumentCheckpointStore,
    LocalDocumentPageStatus,
    LocalDocumentRecord,
    LocalDocumentSourceAdapter,
)

from .test_agent_session_source_adapter import _capture_sink
from .test_source_intake import _privacy

_DOCUMENT_ID = "document:0123456789abcdef0123456789abcdef"


def test_local_document_adapter_builds_metadata_preview_without_body() -> None:
    adapter = LocalDocumentSourceAdapter()
    selection = adapter.file_selection(
        connection_id="account:local-doc-fixture",
        document_id=_DOCUMENT_ID,
        file_kind="text_pdf",
    )

    page = adapter.page_from_documents(
        selection,
        (
            _document(
                text="Synthetic extracted PDF body must not appear in preview.",
                title="Fixture PDF",
            ),
        ),
        privacy=_privacy(),
        selected_document_ids=(_DOCUMENT_ID,),
        next_cursor="cursor:doc2",
    )

    assert page.status is LocalDocumentPageStatus.READY
    assert page.preview is not None
    assert page.preview.selection == selection
    assert page.preview.next_cursor == "cursor:doc2"
    assert [record.content_type for record in page.preview.records] == ["document_text"]
    assert page.preview.records[0].title == "Fixture PDF"
    assert "must not appear" not in repr(page.preview.to_dict())


def test_local_document_import_updates_revision_checkpoint_and_replays_safely(
    tmp_path: Path,
) -> None:
    adapter = LocalDocumentSourceAdapter()
    selection = adapter.file_selection(
        connection_id="account:local-doc-fixture",
        document_id=_DOCUMENT_ID,
        file_kind="docx_file",
    )
    original = adapter.record_from_extracted(
        _document(
            file_kind="docx_file",
            text="Synthetic original document text.",
            revision_id="rev-1",
        )
    )
    original_page = adapter.preview(selection, (original,), privacy=_privacy())
    original_intake = adapter.intake(selection, original, privacy=_privacy())
    sink = _capture_sink(tmp_path)

    checkpoint, receipt = adapter.import_page(
        LocalDocumentCheckpoint.initial(selection),
        adapter.page_from_documents(
            selection,
            (
                _document(
                    file_kind="docx_file",
                    text="Synthetic original document text.",
                    revision_id="rev-1",
                ),
            ),
            privacy=_privacy(),
            selected_document_ids=(_DOCUMENT_ID,),
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
        adapter.page_from_documents(
            selection,
            (
                _document(
                    file_kind="docx_file",
                    text="Synthetic original document text.",
                    revision_id="rev-1",
                ),
            ),
            privacy=_privacy(),
            selected_document_ids=(_DOCUMENT_ID,),
        ),
        (original_intake,),
        sink,
    )

    assert replayed_checkpoint == checkpoint
    assert replayed_receipt.outcome is ConnectorOutcome.EMPTY

    changed = adapter.record_from_extracted(
        _document(
            file_kind="docx_file",
            text="Synthetic changed document text.",
            revision_id="rev-2",
        )
    )
    changed_page = adapter.preview(selection, (changed,), privacy=_privacy())
    changed_intake = adapter.intake(selection, changed, privacy=_privacy())
    changed_checkpoint, changed_receipt = adapter.import_page(
        checkpoint,
        adapter.page_from_documents(
            selection,
            (
                _document(
                    file_kind="docx_file",
                    text="Synthetic changed document text.",
                    revision_id="rev-2",
                ),
            ),
            privacy=_privacy(),
            selected_document_ids=(_DOCUMENT_ID,),
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


def test_local_document_checkpoint_store_is_metadata_only_and_atomic(tmp_path: Path) -> None:
    adapter = LocalDocumentSourceAdapter()
    selection = adapter.file_selection(
        connection_id="account:local-doc-fixture",
        document_id=_DOCUMENT_ID,
        file_kind="text_pdf",
    )
    record = adapter.record_from_extracted(
        _document(text="Synthetic checkpoint body excluded.")
    )
    page = adapter.preview(selection, (record,), privacy=_privacy(), next_cursor="cursor:doc2")
    intake = adapter.intake(selection, record, privacy=_privacy())
    checkpoint = LocalDocumentCheckpoint.initial(selection).advance(
        page,
        committed_delivery_ids=tuple(record.delivery_id for record in page.records),
        committed_revision_identities=(intake.key.revision_identity(),),
    )

    path = LocalDocumentCheckpointStore(tmp_path / "checkpoints").save(checkpoint)

    assert LocalDocumentCheckpointStore(tmp_path / "checkpoints").load(selection) == checkpoint
    assert path.read_text(encoding="utf-8").endswith("\n")
    assert "Synthetic checkpoint body" not in path.read_text(encoding="utf-8")


def test_local_document_adapter_requires_selected_file_and_omits_others() -> None:
    adapter = LocalDocumentSourceAdapter()
    selection = adapter.file_selection(
        connection_id="account:local-doc-fixture",
        document_id=_DOCUMENT_ID,
        file_kind="text_pdf",
    )

    page = adapter.page_from_documents(
        selection,
        (
            _document(
                document_id="document:fedcba9876543210fedcba9876543210",
                text="access_token = synthetic-secret",
            ),
            _document(title="Selected PDF"),
        ),
        privacy=_privacy(),
        selected_document_ids=(_DOCUMENT_ID,),
    )

    assert page.preview is not None
    assert len(page.preview.records) == 1
    assert page.preview.records[0].title == "Selected PDF"

    with pytest.raises(ConnectorContractError, match="invalid local document records"):
        adapter.page_from_documents(
            selection,
            (_document(file_kind="docx_file"),),
            privacy=_privacy(),
            selected_document_ids=(_DOCUMENT_ID,),
        )
    with pytest.raises(ConnectorContractError, match="invalid local document records"):
        adapter.page_from_documents(
            selection,
            (_document(),),
            privacy=_privacy(),
            selected_document_ids=(),
        )


def test_local_document_adapter_rejects_secret_feedback_and_source_reference() -> None:
    adapter = LocalDocumentSourceAdapter()

    with pytest.raises(ConnectorContractError, match="invalid local document record"):
        adapter.record_from_extracted(_document(source_kind="open_brain_result"))
    with pytest.raises(ConnectorContractError, match="invalid local document record"):
        adapter.record_from_extracted(
            _document(source_reference="https://local.openbrain-result.invalid/doc/1")
        )
    with pytest.raises(ConnectorContractError, match="invalid local document record"):
        adapter.record_from_extracted(_document(title="access_token = synthetic-secret"))
    with pytest.raises(ConnectorContractError, match="invalid local document record"):
        adapter.record_from_extracted(_document(text_secret_scan="finding"))
    with pytest.raises(ConnectorContractError, match="invalid local document record"):
        LocalDocumentRecord(
            document_id=_DOCUMENT_ID,
            revision_id="rev-1",
            file_kind="text_pdf",
            title="Fixture PDF",
            text="client_secret = synthetic-secret",
        )


def test_local_document_adapter_models_denied_unsupported_and_encrypted_statuses() -> None:
    adapter = LocalDocumentSourceAdapter()

    assert adapter.not_allowed_page().status is LocalDocumentPageStatus.NOT_ALLOWED
    assert adapter.unsupported_format_page().status is LocalDocumentPageStatus.UNSUPPORTED_FORMAT
    assert adapter.encrypted_page().status is LocalDocumentPageStatus.ENCRYPTED


def test_local_document_checkpoint_store_rejects_mismatched_embedded_selection(
    tmp_path: Path,
) -> None:
    adapter = LocalDocumentSourceAdapter()
    selection = adapter.file_selection(
        connection_id="account:local-doc-fixture",
        document_id=_DOCUMENT_ID,
        file_kind="text_pdf",
    )
    other_selection = adapter.file_selection(
        connection_id="account:local-doc-fixture",
        document_id=_DOCUMENT_ID,
        file_kind="docx_file",
    )
    store = LocalDocumentCheckpointStore(tmp_path / "checkpoints")
    path = store.save(LocalDocumentCheckpoint.initial(selection))
    path.write_text(
        json.dumps(LocalDocumentCheckpoint.initial(other_selection).to_dict()),
        encoding="utf-8",
    )

    with pytest.raises(ConnectorContractError, match="invalid local document checkpoint store"):
        store.load(selection)


def _document(
    *,
    document_id: str = _DOCUMENT_ID,
    revision_id: str = "rev-1",
    file_kind: str = "text_pdf",
    title: str = "Fixture PDF",
    text: str = "Synthetic extracted document text.",
    source_kind: object = "local_document",
    text_secret_scan: object = "clean",
    source_reference: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "document_id": document_id,
        "file_kind": file_kind,
        "revision_id": revision_id,
        "source_kind": source_kind,
        "text": text,
        "text_secret_scan": text_secret_scan,
        "title": title,
    }
    if source_reference is not None:
        payload["source_reference"] = source_reference
    return payload

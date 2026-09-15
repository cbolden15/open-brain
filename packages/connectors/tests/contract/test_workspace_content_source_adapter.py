from __future__ import annotations

from pathlib import Path

import pytest

from open_brain_connectors.runtime.connectors import (
    ConnectorContractError,
    ConnectorFailureCode,
    ConnectorOutcome,
)
from open_brain_connectors.runtime.workspace_content import (
    WorkspaceContentCheckpoint,
    WorkspaceContentCheckpointStore,
    WorkspaceContentPage,
    WorkspaceContentPageStatus,
    WorkspaceContentSourceAdapter,
)

from .test_agent_session_source_adapter import _capture_sink
from .test_source_intake import _privacy

_NOTION_PAGE_ID = "notion:page/weekly-plan"
_NOTION_DATA_SOURCE_ID = "notion:data-source/roadmap"
_CONFLUENCE_SPACE_ID = "confluence:space/ENG"


def test_workspace_content_preview_is_metadata_only_for_selected_notion_page() -> None:
    adapter = WorkspaceContentSourceAdapter()
    selection = adapter.resource_selection(
        connector_name="notion",
        connection_id="account:notion-fixture",
        resource_id=_NOTION_PAGE_ID,
        resource_type="page",
    )

    page = adapter.page_from_items(
        selection,
        (
            _item(
                body="Synthetic nested Notion body must not appear in preview.",
                content_id=_NOTION_PAGE_ID,
                connector_name="notion",
                title="Weekly Plan",
            ),
        ),
        privacy=_privacy(),
        selected_content_ids=(_NOTION_PAGE_ID,),
        next_cursor="cursor:notion2",
    )

    assert page.status is WorkspaceContentPageStatus.READY
    assert page.preview is not None
    assert page.preview.selection == selection
    assert page.preview.next_cursor == "cursor:notion2"
    assert [record.content_type for record in page.preview.records] == ["page"]
    assert page.preview.records[0].title == "Weekly Plan"
    assert "must not appear" not in repr(page.preview.to_dict())


def test_workspace_content_import_updates_revision_checkpoint_and_replays_safely(
    tmp_path: Path,
) -> None:
    adapter = WorkspaceContentSourceAdapter()
    selection = adapter.resource_selection(
        connector_name="confluence",
        connection_id="account:confluence-fixture",
        resource_id=_CONFLUENCE_SPACE_ID,
        resource_type="cloud_space",
    )
    original = adapter.record_from_item(
        _item(
            connector_name="confluence",
            content_id="confluence:page/runbook",
            parent_id=_CONFLUENCE_SPACE_ID,
            revision_id="rev-1",
            title="Runbook",
        )
    )
    changed = adapter.record_from_item(
        _item(
            connector_name="confluence",
            content_id="confluence:page/runbook",
            parent_id=_CONFLUENCE_SPACE_ID,
            revision_id="rev-2",
            title="Runbook",
            body="Synthetic updated Confluence body.",
        )
    )
    original_page = WorkspaceContentPage(
        WorkspaceContentPageStatus.READY,
        adapter.preview(selection, (original,), privacy=_privacy()),
    )
    original_intake = adapter.intake(selection, original, privacy=_privacy())
    changed_page = WorkspaceContentPage(
        WorkspaceContentPageStatus.READY,
        adapter.preview(selection, (changed,), privacy=_privacy()),
    )
    changed_intake = adapter.intake(selection, changed, privacy=_privacy())
    sink = _capture_sink(tmp_path)

    checkpoint, receipt = adapter.import_page(
        WorkspaceContentCheckpoint.initial(selection),
        original_page,
        (original_intake,),
        sink,
    )
    replayed_checkpoint, replayed_receipt = adapter.import_page(
        checkpoint,
        original_page,
        (original_intake,),
        sink,
    )
    changed_checkpoint, changed_receipt = adapter.import_page(
        checkpoint,
        changed_page,
        (changed_intake,),
        sink,
    )

    assert receipt.outcome is ConnectorOutcome.COMPLETED
    assert replayed_checkpoint == checkpoint
    assert replayed_receipt.outcome is ConnectorOutcome.EMPTY
    assert original_intake.key.delivery_id() == changed_intake.key.delivery_id()
    assert changed_receipt.outcome is ConnectorOutcome.COMPLETED
    assert changed_receipt.created_count == 1
    assert changed_checkpoint.committed_delivery_ids == checkpoint.committed_delivery_ids
    assert changed_checkpoint.committed_revision_identities == (
        changed_intake.key.revision_identity(),
    )


def test_workspace_content_selected_page_allows_child_blocks_and_comments_without_id_leak() -> None:
    adapter = WorkspaceContentSourceAdapter()
    notion_selection = adapter.resource_selection(
        connector_name="notion",
        connection_id="account:notion-fixture",
        resource_id=_NOTION_PAGE_ID,
        resource_type="page",
    )
    confluence_selection = adapter.resource_selection(
        connector_name="confluence",
        connection_id="account:confluence-fixture",
        resource_id="confluence:page/runbook",
        resource_type="cloud_page",
    )
    adapter.record_from_item(
        _item(
            content_id="notion:block/user-authored-heading",
            content_type="block",
            parent_id=_NOTION_PAGE_ID,
            title="Nested Heading",
        )
    )
    adapter.record_from_item(
        _item(
            connector_name="confluence",
            content_id="confluence:comment/user-authored-reply",
            content_type="comment",
            parent_id="confluence:page/runbook",
            title="Review Comment",
        )
    )

    notion_page = adapter.page_from_items(
        notion_selection,
        (_item(content_id="notion:page/other"), _item(
            content_id="notion:block/user-authored-heading",
            content_type="block",
            parent_id=_NOTION_PAGE_ID,
            title="Nested Heading",
        )),
        privacy=_privacy(),
        selected_content_ids=(_NOTION_PAGE_ID,),
    )
    confluence_page = adapter.page_from_items(
        confluence_selection,
        (_item(
            connector_name="confluence",
            content_id="confluence:comment/user-authored-reply",
            content_type="comment",
            parent_id="confluence:page/runbook",
            title="Review Comment",
        ),),
        privacy=_privacy(),
        selected_content_ids=("confluence:page/runbook",),
    )

    assert notion_page.preview is not None
    assert confluence_page.preview is not None
    assert [record.content_type for record in notion_page.preview.records] == ["block"]
    assert [record.content_type for record in confluence_page.preview.records] == ["comment"]
    assert "user-authored-heading" not in notion_page.preview.records[0].source_reference
    assert "user-authored-reply" not in confluence_page.preview.records[0].source_reference


def test_workspace_content_checkpoint_retains_prior_pages_without_resubmitting(
    tmp_path: Path,
) -> None:
    adapter = WorkspaceContentSourceAdapter()
    selection = adapter.resource_selection(
        connector_name="notion",
        connection_id="account:notion-fixture",
        resource_id=_NOTION_DATA_SOURCE_ID,
        resource_type="data_source",
    )
    first = adapter.record_from_item(
        _item(content_id="notion:page/first", parent_id=_NOTION_DATA_SOURCE_ID, title="First")
    )
    second = adapter.record_from_item(
        _item(
            content_id="notion:page/second",
            parent_id=_NOTION_DATA_SOURCE_ID,
            title="Second",
            body="Synthetic second page.",
        )
    )
    first_page = WorkspaceContentPage(
        WorkspaceContentPageStatus.READY,
        adapter.preview(selection, (first,), privacy=_privacy(), next_cursor="cursor:page2"),
    )
    second_page = WorkspaceContentPage(
        WorkspaceContentPageStatus.READY,
        adapter.preview(selection, (second,), privacy=_privacy(), next_cursor="cursor:page3"),
    )
    first_intake = adapter.intake(selection, first, privacy=_privacy())
    second_intake = adapter.intake(selection, second, privacy=_privacy())
    sink = _capture_sink(tmp_path)

    first_checkpoint, _first_receipt = adapter.import_page(
        WorkspaceContentCheckpoint.initial(selection),
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
    replay_checkpoint, replay_receipt = adapter.import_page(
        second_checkpoint,
        first_page,
        (first_intake,),
        sink,
    )

    assert second_checkpoint.next_cursor == "cursor:page3"
    assert second_checkpoint.observed_page_cursors == ("cursor:page2", "cursor:page3")
    assert second_checkpoint.committed_delivery_ids == (
        first_intake.key.delivery_id(),
        second_intake.key.delivery_id(),
    )
    assert replay_checkpoint == second_checkpoint
    assert replay_receipt.outcome is ConnectorOutcome.EMPTY
    assert replay_receipt.submitted_count == 0


def test_workspace_content_checkpoint_store_is_partitioned_by_connector(
    tmp_path: Path,
) -> None:
    adapter = WorkspaceContentSourceAdapter()
    notion = adapter.resource_selection(
        connector_name="notion",
        connection_id="account:workspace-fixture",
        resource_id="notion:page/shared",
        resource_type="page",
    )
    confluence = adapter.resource_selection(
        connector_name="confluence",
        connection_id="account:workspace-fixture",
        resource_id="confluence:page/shared",
        resource_type="cloud_page",
    )
    notion_record = adapter.record_from_item(
        _item(content_id="notion:page/shared", connector_name="notion")
    )
    notion_intake = adapter.intake(notion, notion_record, privacy=_privacy())
    store = WorkspaceContentCheckpointStore(tmp_path / "checkpoints")

    notion_path = store.save(
        WorkspaceContentCheckpoint.initial(notion).advance(
            adapter.preview(notion, (notion_record,), privacy=_privacy()),
            committed_delivery_ids=(notion_intake.key.delivery_id(),),
            committed_revision_identities=(notion_intake.key.revision_identity(),),
        )
    )

    assert notion_path != store._path(confluence)
    assert store.load(confluence) == WorkspaceContentCheckpoint.initial(confluence)


def test_workspace_content_import_surfaces_permission_and_resource_failures(
    tmp_path: Path,
) -> None:
    adapter = WorkspaceContentSourceAdapter()
    selection = adapter.resource_selection(
        connector_name="notion",
        connection_id="account:notion-fixture",
        resource_id=_NOTION_PAGE_ID,
        resource_type="page",
    )
    checkpoint = WorkspaceContentCheckpoint.initial(selection)

    not_allowed_checkpoint, not_allowed = adapter.import_page(
        checkpoint,
        adapter.not_allowed_page(),
        (),
        _capture_sink(tmp_path),
    )
    unsupported_checkpoint, unsupported = adapter.import_page(
        checkpoint,
        adapter.unsupported_resource_page(),
        (),
        _capture_sink(tmp_path),
    )

    assert not_allowed_checkpoint == checkpoint
    assert not_allowed.failure_code is ConnectorFailureCode.NOT_ALLOWED
    assert unsupported_checkpoint == checkpoint
    assert unsupported.failure_code is ConnectorFailureCode.UNSUPPORTED_CAPABILITY


def test_workspace_content_rejects_unselected_or_secret_flagged_content() -> None:
    adapter = WorkspaceContentSourceAdapter()
    selection = adapter.resource_selection(
        connector_name="notion",
        connection_id="account:notion-fixture",
        resource_id=_NOTION_PAGE_ID,
        resource_type="page",
    )

    with pytest.raises(ConnectorContractError, match="invalid workspace contents"):
        adapter.page_from_items(
            selection,
            (_item(content_id="notion:page/other"),),
            privacy=_privacy(),
            selected_content_ids=(_NOTION_PAGE_ID,),
        )
    with pytest.raises(ConnectorContractError, match="invalid workspace content"):
        adapter.record_from_item(
            {
                **_item(content_id=_NOTION_PAGE_ID),
                "content_secret_scan": "finding",
            }
        )
    with pytest.raises(ConnectorContractError, match="invalid workspace content"):
        adapter.record_from_item(
            {
                **_item(content_id=_NOTION_PAGE_ID),
                "source_link": "https://notion.example.invalid/source\nInjected: value",
            }
        )


def _item(
    *,
    connector_name: str = "notion",
    content_id: str = _NOTION_PAGE_ID,
    parent_id: str | None = None,
    revision_id: str = "rev-1",
    content_type: str = "page",
    title: str = "Workspace Page",
    body: str = "Synthetic workspace body.",
) -> dict[str, object]:
    return {
        "body": body,
        "connector_name": connector_name,
        "content_id": content_id,
        "content_secret_scan": "clean",
        "content_type": content_type,
        "parent_id": parent_id,
        "revision_id": revision_id,
        "source_kind": connector_name,
        "source_link": f"https://{connector_name}.example.invalid/source/item",
        "title": title,
    }

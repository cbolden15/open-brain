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
from open_brain_connectors.runtime.gitlab_jira import (
    GitLabProjectCheckpoint,
    GitLabProjectCheckpointStore,
    GitLabProjectPage,
    GitLabSourceAdapter,
    ProjectPageStatus,
)

from .test_source_intake import _privacy


def test_gitlab_adapter_builds_selected_project_preview_without_bodies() -> None:
    adapter = GitLabSourceAdapter()
    selection = adapter.project_selection(
        connection_id="account:gitlab-fixture",
        host="gitlab.com",
        project_path="open-brain/fixture",
    )
    page = adapter.project_page_from_rest(
        selection,
        (
            {
                "iid": 4,
                "web_url": "https://gitlab.com/open-brain/fixture/-/issues/4",
                "title": "Fixture issue",
                "description": "Synthetic GitLab issue body must not appear in preview.",
                "updated_at": "2026-09-15T12:00:00Z",
            },
            {
                "iid": 5,
                "web_url": "https://gitlab.com/open-brain/fixture/-/merge_requests/5",
                "title": "Fixture merge request",
                "description": "Synthetic GitLab MR body must not appear in preview.",
                "updated_at": "2026-09-15T12:01:00Z",
                "source_branch": "fixture",
            },
            {
                "id": 99,
                "noteable_iid": 4,
                "noteable_type": "Issue",
                "web_url": "https://gitlab.com/open-brain/fixture/-/issues/4#note_99",
                "body": "Synthetic GitLab note body must not appear in preview.",
                "updated_at": "2026-09-15T12:02:00Z",
            },
        ),
        privacy=_privacy(),
        next_cursor="cursor:gitlab2",
    )

    assert page.status is ProjectPageStatus.READY
    assert page.preview is not None
    assert page.preview.selection == selection
    assert [record.content_type for record in page.preview.records] == [
        "issue",
        "merge_request",
        "comment",
    ]
    assert "must not appear" not in repr(page.preview.to_dict())
    assert selection.resource_id == "project:gitlab.com/open-brain/fixture"


def test_gitlab_project_import_updates_revision_checkpoint_and_replays_safely(
    tmp_path: Path,
) -> None:
    adapter = GitLabSourceAdapter()
    selection = adapter.project_selection(
        connection_id="account:gitlab-fixture",
        host="gitlab.com",
        project_path="open-brain/fixture",
    )
    original = adapter.issue_or_mr_from_rest(
        {
            "iid": 4,
            "web_url": "https://gitlab.com/open-brain/fixture/-/issues/4",
            "title": "Fixture issue",
            "description": "Synthetic original GitLab issue body.",
            "updated_at": "2026-09-15T12:00:00Z",
        }
    )
    original_page = adapter.preview_project(selection, (original,), privacy=_privacy())
    original_intake = adapter.intake(selection, original, privacy=_privacy())
    sink = _capture_sink(tmp_path)

    checkpoint, receipt = adapter.import_project_page(
        GitLabProjectCheckpoint.initial(selection),
        GitLabProjectPage(status=ProjectPageStatus.READY, preview=original_page),
        (original_intake,),
        sink,
    )

    assert receipt.outcome is ConnectorOutcome.COMPLETED
    assert checkpoint.committed_revision_identities == (
        original_intake.key.revision_identity(),
    )

    replayed_checkpoint, replayed_receipt = adapter.import_project_page(
        checkpoint,
        GitLabProjectPage(status=ProjectPageStatus.READY, preview=original_page),
        (original_intake,),
        sink,
    )

    assert replayed_checkpoint == checkpoint
    assert replayed_receipt.outcome is ConnectorOutcome.EMPTY

    changed = adapter.issue_or_mr_from_rest(
        {
            "iid": 4,
            "web_url": "https://gitlab.com/open-brain/fixture/-/issues/4",
            "title": "Fixture issue",
            "description": "Synthetic changed GitLab issue body.",
            "updated_at": "2026-09-15T12:30:00Z",
        }
    )
    changed_page = adapter.preview_project(selection, (changed,), privacy=_privacy())
    changed_intake = adapter.intake(selection, changed, privacy=_privacy())
    changed_checkpoint, changed_receipt = adapter.import_project_page(
        checkpoint,
        GitLabProjectPage(status=ProjectPageStatus.READY, preview=changed_page),
        (changed_intake,),
        sink,
    )

    assert changed_receipt.outcome is ConnectorOutcome.COMPLETED
    assert changed_receipt.created_count == 1
    assert changed_checkpoint.committed_delivery_ids == checkpoint.committed_delivery_ids
    assert changed_checkpoint.committed_revision_identities == (
        changed_intake.key.revision_identity(),
    )


def test_gitlab_checkpoint_store_is_metadata_only_and_atomic(tmp_path: Path) -> None:
    adapter = GitLabSourceAdapter()
    selection = adapter.project_selection(
        connection_id="account:gitlab-fixture",
        host="gitlab.com",
        project_path="open-brain/fixture",
    )
    record = adapter.issue_or_mr_from_rest(
        {
            "iid": 4,
            "web_url": "https://gitlab.com/open-brain/fixture/-/issues/4",
            "title": "Fixture issue",
            "description": "Synthetic body excluded from checkpoint.",
            "updated_at": "2026-09-15T12:00:00Z",
        }
    )
    page = adapter.preview_project(selection, (record,), privacy=_privacy(), next_cursor="cursor:2")
    intake = adapter.intake(selection, record, privacy=_privacy())
    checkpoint = GitLabProjectCheckpoint.initial(selection).advance(
        page,
        committed_delivery_ids=tuple(record.delivery_id for record in page.records),
        committed_revision_identities=(intake.key.revision_identity(),),
    )
    store = GitLabProjectCheckpointStore(tmp_path / "checkpoints")

    path = store.save(checkpoint)

    assert store.load(selection) == checkpoint
    assert path.read_text(encoding="utf-8").endswith("\n")
    assert "Synthetic body" not in path.read_text(encoding="utf-8")


def test_gitlab_adapter_models_rate_limit_and_lost_access_without_preview() -> None:
    adapter = GitLabSourceAdapter()

    assert (
        adapter.rate_limited_page(retry_after_seconds=60).status
        is ProjectPageStatus.RATE_LIMITED
    )
    assert adapter.needs_sign_in_page().status is ProjectPageStatus.NEEDS_SIGN_IN
    assert adapter.not_allowed_page().status is ProjectPageStatus.NOT_ALLOWED


def test_gitlab_adapter_rejects_cross_project_records() -> None:
    adapter = GitLabSourceAdapter()
    selection = adapter.project_selection(
        connection_id="account:gitlab-fixture",
        host="gitlab.com",
        project_path="open-brain/fixture",
    )
    record = adapter.issue_or_mr_from_rest(
        {
            "iid": 4,
            "web_url": "https://gitlab.com/other/fixture/-/issues/4",
            "title": "Wrong issue",
            "description": "Synthetic cross-project body.",
            "updated_at": "2026-09-15T12:00:00Z",
        }
    )

    with pytest.raises(ConnectorContractError, match="invalid gitlab record"):
        adapter.preview_project(selection, (record,), privacy=_privacy())


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

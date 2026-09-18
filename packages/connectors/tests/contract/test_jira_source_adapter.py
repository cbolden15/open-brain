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
    JiraProjectCheckpoint,
    JiraProjectCheckpointStore,
    JiraProjectPage,
    JiraSourceAdapter,
    ProjectPageStatus,
)

from .test_source_intake import _privacy


def test_jira_adapter_builds_selected_project_preview_without_bodies() -> None:
    adapter = JiraSourceAdapter()
    selection = adapter.project_selection(
        connection_id="account:jira-fixture",
        site="fixture.atlassian.net",
        project_key="OB",
    )
    page = adapter.project_page_from_rest(
        selection,
        (
            {
                "key": "OB-7",
                "self_web_url": "https://fixture.atlassian.net/browse/OB-7",
                "fields": {
                    "summary": "Fixture Jira issue",
                    "description": "Synthetic Jira issue body must not appear in preview.",
                    "updated": "2026-09-15T12:00:00Z",
                },
            },
            {
                "id": "10001",
                "issue_key": "OB-7",
                "self_web_url": "https://fixture.atlassian.net/browse/OB-7?focusedCommentId=10001",
                "body": "Synthetic Jira comment body must not appear in preview.",
                "updated": "2026-09-15T12:02:00Z",
            },
        ),
        privacy=_privacy(),
        next_cursor="cursor:jira2",
    )

    assert page.status is ProjectPageStatus.READY
    assert page.preview is not None
    assert [record.content_type for record in page.preview.records] == ["issue", "comment"]
    assert "must not appear" not in repr(page.preview.to_dict())
    assert selection.resource_id == "project:fixture.atlassian.net/OB"


def test_jira_project_import_refuses_unordered_revision_and_replays_safely(
    tmp_path: Path,
) -> None:
    adapter = JiraSourceAdapter()
    selection = adapter.project_selection(
        connection_id="account:jira-fixture",
        site="fixture.atlassian.net",
        project_key="OB",
    )
    original = adapter.issue_from_rest(
        {
            "key": "OB-7",
            "self_web_url": "https://fixture.atlassian.net/browse/OB-7",
            "fields": {
                "summary": "Fixture Jira issue",
                "description": "Synthetic original Jira issue body.",
                "updated": "2026-09-15T12:00:00Z",
            },
        }
    )
    original_page = adapter.preview_project(selection, (original,), privacy=_privacy())
    original_intake = adapter.intake(selection, original, privacy=_privacy())
    sink = _capture_sink(tmp_path)

    checkpoint, receipt = adapter.import_project_page(
        JiraProjectCheckpoint.initial(selection),
        JiraProjectPage(status=ProjectPageStatus.READY, preview=original_page),
        (original_intake,),
        sink,
    )

    assert receipt.outcome is ConnectorOutcome.COMPLETED
    assert checkpoint.committed_revision_identities == (
        original_intake.key.revision_identity(),
    )

    replayed_checkpoint, replayed_receipt = adapter.import_project_page(
        checkpoint,
        JiraProjectPage(status=ProjectPageStatus.READY, preview=original_page),
        (original_intake,),
        sink,
    )

    assert replayed_checkpoint == checkpoint
    assert replayed_receipt.outcome is ConnectorOutcome.EMPTY

    changed = adapter.issue_from_rest(
        {
            "key": "OB-7",
            "self_web_url": "https://fixture.atlassian.net/browse/OB-7",
            "fields": {
                "summary": "Fixture Jira issue",
                "description": "Synthetic changed Jira issue body.",
                "updated": "2026-09-15T12:30:00Z",
            },
        }
    )
    changed_page = adapter.preview_project(selection, (changed,), privacy=_privacy())
    changed_intake = adapter.intake(selection, changed, privacy=_privacy())
    checkpoint_before = checkpoint.to_dict()
    source_bytes = {p: p.read_bytes() for p in tmp_path.glob("brain-*/sources/**/*")
                    if p.is_file()}
    assert source_bytes
    # Legacy deliveries lack ordering and head-CAS evidence for revision replacement.
    with pytest.raises(ValueError, match="^conflicting delivery$"):
        adapter.import_project_page(
            checkpoint,
            JiraProjectPage(status=ProjectPageStatus.READY, preview=changed_page),
            (changed_intake,),
            sink,
        )
    assert checkpoint.to_dict() == checkpoint_before
    assert {p: p.read_bytes() for p in tmp_path.glob("brain-*/sources/**/*")
            if p.is_file()} == source_bytes



def test_jira_checkpoint_store_is_metadata_only_and_atomic(tmp_path: Path) -> None:
    adapter = JiraSourceAdapter()
    selection = adapter.project_selection(
        connection_id="account:jira-fixture",
        site="fixture.atlassian.net",
        project_key="OB",
    )
    record = adapter.issue_from_rest(
        {
            "key": "OB-7",
            "self_web_url": "https://fixture.atlassian.net/browse/OB-7",
            "fields": {
                "summary": "Fixture Jira issue",
                "description": "Synthetic body excluded from checkpoint.",
                "updated": "2026-09-15T12:00:00Z",
            },
        }
    )
    page = adapter.preview_project(selection, (record,), privacy=_privacy(), next_cursor="cursor:2")
    intake = adapter.intake(selection, record, privacy=_privacy())
    checkpoint = JiraProjectCheckpoint.initial(selection).advance(
        page,
        committed_delivery_ids=tuple(record.delivery_id for record in page.records),
        committed_revision_identities=(intake.key.revision_identity(),),
    )
    store = JiraProjectCheckpointStore(tmp_path / "checkpoints")

    path = store.save(checkpoint)

    assert store.load(selection) == checkpoint
    assert path.read_text(encoding="utf-8").endswith("\n")
    assert "Synthetic body" not in path.read_text(encoding="utf-8")


def test_jira_adapter_models_rate_limit_and_lost_access_without_preview() -> None:
    adapter = JiraSourceAdapter()

    assert (
        adapter.rate_limited_page(retry_after_seconds=60).status
        is ProjectPageStatus.RATE_LIMITED
    )
    assert adapter.needs_sign_in_page().status is ProjectPageStatus.NEEDS_SIGN_IN
    assert adapter.not_allowed_page().status is ProjectPageStatus.NOT_ALLOWED


def test_jira_adapter_rejects_cross_project_records() -> None:
    adapter = JiraSourceAdapter()
    selection = adapter.project_selection(
        connection_id="account:jira-fixture",
        site="fixture.atlassian.net",
        project_key="OB",
    )
    record = adapter.issue_from_rest(
        {
            "key": "BAD-7",
            "self_web_url": "https://fixture.atlassian.net/browse/BAD-7",
            "fields": {
                "summary": "Wrong issue",
                "description": "Synthetic cross-project body.",
                "updated": "2026-09-15T12:00:00Z",
            },
        }
    )

    with pytest.raises(ConnectorContractError, match="invalid jira record"):
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

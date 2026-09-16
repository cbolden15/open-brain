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
from open_brain_connectors.runtime.slack import (
    SlackChannelCheckpoint,
    SlackChannelCheckpointStore,
    SlackChannelPageStatus,
    SlackSourceAdapter,
)

from .test_source_intake import _privacy


def test_slack_adapter_builds_selected_channel_preview_with_thread_context() -> None:
    adapter = SlackSourceAdapter()
    selection = adapter.channel_selection(
        connection_id="account:slack-fixture",
        channel_id="COPENBRAIN",
    )

    page = adapter.page_from_rest(
        selection,
        (
            _slack_payload(text="Synthetic Slack message must not appear in preview."),
            _slack_payload(
                ts="1790000002.000300",
                thread_ts="1790000000.000100",
                permalink="https://workspace.slack.com/archives/COPENBRAIN/p1790000002000300",
                text="Synthetic Slack thread reply body must not appear in preview.",
                edited_ts="1790000002.000301",
            ),
        ),
        privacy=_privacy(),
        next_cursor="cursor:slack2",
    )

    assert page.status is SlackChannelPageStatus.READY
    assert page.preview is not None
    assert page.preview.selection == selection
    assert page.preview.records[0].content_type == "message"
    assert page.preview.records[1].content_type == "thread_reply"
    assert "must not appear" not in repr(page.preview.to_dict())
    assert selection.resource_id == "channel:COPENBRAIN"


def test_slack_channel_import_updates_revision_checkpoint_and_replays_safely(
    tmp_path: Path,
) -> None:
    adapter = SlackSourceAdapter()
    selection = adapter.channel_selection(
        connection_id="account:slack-fixture",
        channel_id="COPENBRAIN",
    )
    original = adapter.record_from_rest(
        _slack_payload(text="Synthetic original Slack body.", edited_ts="1790000000.000101")
    )
    original_page = adapter.preview(selection, (original,), privacy=_privacy())
    original_intake = adapter.intake(selection, original, privacy=_privacy())
    sink = _capture_sink(tmp_path)

    checkpoint, receipt = adapter.import_page(
        SlackChannelCheckpoint.initial(selection),
        adapter.page_from_rest(
            selection,
            (_slack_payload(text="Synthetic original Slack body.", edited_ts="1790000000.000101"),),
            privacy=_privacy(),
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
        adapter.page_from_rest(
            selection,
            (_slack_payload(text="Synthetic original Slack body.", edited_ts="1790000000.000101"),),
            privacy=_privacy(),
        ),
        (original_intake,),
        sink,
    )

    assert replayed_checkpoint == checkpoint
    assert replayed_receipt.outcome is ConnectorOutcome.EMPTY

    changed = adapter.record_from_rest(
        _slack_payload(text="Synthetic changed Slack body.", edited_ts="1790000000.000200")
    )
    changed_page = adapter.preview(selection, (changed,), privacy=_privacy())
    changed_intake = adapter.intake(selection, changed, privacy=_privacy())
    changed_checkpoint, changed_receipt = adapter.import_page(
        checkpoint,
        adapter.page_from_rest(
            selection,
            (_slack_payload(text="Synthetic changed Slack body.", edited_ts="1790000000.000200"),),
            privacy=_privacy(),
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


def test_slack_checkpoint_store_is_metadata_only_and_atomic(tmp_path: Path) -> None:
    adapter = SlackSourceAdapter()
    selection = adapter.channel_selection(
        connection_id="account:slack-fixture",
        channel_id="COPENBRAIN",
    )
    record = adapter.record_from_rest(
        _slack_payload(text="Synthetic Slack body excluded from checkpoint.")
    )
    page = adapter.preview(selection, (record,), privacy=_privacy(), next_cursor="cursor:slack2")
    intake = adapter.intake(selection, record, privacy=_privacy())
    checkpoint = SlackChannelCheckpoint.initial(selection).advance(
        page,
        committed_delivery_ids=tuple(record.delivery_id for record in page.records),
        committed_revision_identities=(intake.key.revision_identity(),),
    )

    path = SlackChannelCheckpointStore(tmp_path / "checkpoints").save(checkpoint)

    assert SlackChannelCheckpointStore(tmp_path / "checkpoints").load(selection) == checkpoint
    assert path.read_text(encoding="utf-8").endswith("\n")
    assert "Synthetic Slack body" not in path.read_text(encoding="utf-8")


def test_slack_adapter_models_rate_limit_reauth_lost_access_and_retention() -> None:
    adapter = SlackSourceAdapter()

    assert (
        adapter.rate_limited_page(retry_after_seconds=60).status
        is SlackChannelPageStatus.RATE_LIMITED
    )
    assert adapter.needs_sign_in_page().status is SlackChannelPageStatus.NEEDS_SIGN_IN
    assert adapter.not_allowed_page().status is SlackChannelPageStatus.NOT_ALLOWED
    assert adapter.retention_expired_page().status is SlackChannelPageStatus.RETENTION_EXPIRED


def test_slack_adapter_rejects_cross_channel_records() -> None:
    adapter = SlackSourceAdapter()
    selection = adapter.channel_selection(
        connection_id="account:slack-fixture",
        channel_id="COPENBRAIN",
    )
    record = adapter.record_from_rest(
        _slack_payload(
            channel_id="COTHER",
            permalink="https://workspace.slack.com/archives/COTHER/p1790000000000100",
            text="Synthetic cross-channel Slack body.",
        )
    )

    with pytest.raises(ConnectorContractError, match="invalid slack record"):
        adapter.preview(selection, (record,), privacy=_privacy())


def _slack_payload(
    *,
    channel_id: str = "COPENBRAIN",
    ts: str = "1790000000.000100",
    thread_ts: str | None = None,
    permalink: str = "https://workspace.slack.com/archives/COPENBRAIN/p1790000000000100",
    text: str = "Synthetic Slack body.",
    edited_ts: str = "1790000000.000101",
) -> dict[str, object]:
    payload: dict[str, object] = {
        "channel_id": channel_id,
        "ts": ts,
        "permalink": permalink,
        "user": "UOPENBRAIN",
        "text": text,
        "edited_ts": edited_ts,
    }
    if thread_ts is not None:
        payload["thread_ts"] = thread_ts
    return payload


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

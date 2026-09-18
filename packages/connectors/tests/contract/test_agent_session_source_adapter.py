from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from open_brain_engine.engine import PublicJobCaptureContext, open_local_engine

from open_brain.profile import compile_single_user_local
from open_brain_connectors.runtime.agent_session import (
    AgentSessionCheckpoint,
    AgentSessionCheckpointStore,
    AgentSessionPageStatus,
    AgentSessionRecord,
    AgentSessionSourceAdapter,
)
from open_brain_connectors.runtime.connectors import (
    ConnectorBudget,
    ConnectorBudgetLimits,
    ConnectorCaptureSink,
    ConnectorContractError,
    ConnectorOutcome,
    ConnectorRunEvidence,
)

from .test_source_intake import _privacy

_PROJECT_ID = "project:0123456789abcdef0123456789abcdef"


def test_agent_session_adapter_builds_metadata_preview_with_separate_transcript_opt_in() -> None:
    adapter = AgentSessionSourceAdapter()
    selection = adapter.project_selection(
        connection_id="account:agent-fixture",
        project_id=_PROJECT_ID,
    )

    summary_only = adapter.page_from_events(
        selection,
        (_agent_event(summary="Synthetic summary body must not appear in preview."),),
        privacy=_privacy(),
        include_transcripts=False,
        selected_session_ids=("codex-session-1",),
        next_cursor="cursor:agent2",
    )
    with_transcript = adapter.page_from_events(
        selection,
        (_agent_event(transcript="Synthetic transcript body must not appear in preview."),),
        privacy=_privacy(),
        include_transcripts=True,
        selected_session_ids=("codex-session-1",),
    )

    assert summary_only.status is AgentSessionPageStatus.READY
    assert summary_only.preview is not None
    assert summary_only.preview.selection == selection
    assert [record.content_type for record in summary_only.preview.records] == [
        "session_summary"
    ]
    assert "must not appear" not in repr(summary_only.preview.to_dict())
    assert with_transcript.preview is not None
    assert [record.content_type for record in with_transcript.preview.records] == [
        "session_summary",
        "session_transcript",
    ]


def test_agent_session_source_reference_encodes_session_path_token() -> None:
    adapter = AgentSessionSourceAdapter()
    selection = adapter.project_selection(
        connection_id="account:agent-fixture",
        project_id=_PROJECT_ID,
    )

    page = adapter.page_from_events(
        selection,
        (_agent_event(session_id="codex:session:frag"),),
        privacy=_privacy(),
        include_transcripts=False,
        selected_session_ids=("codex:session:frag",),
    )

    assert page.preview is not None
    assert page.preview.records[0].source_reference.endswith(
        "/sessions/codex%3Asession%3Afrag"
    )


def test_agent_session_import_refuses_unordered_revision_and_replays_safely(
    tmp_path: Path,
) -> None:
    adapter = AgentSessionSourceAdapter()
    selection = adapter.project_selection(
        connection_id="account:agent-fixture",
        project_id=_PROJECT_ID,
    )
    original = adapter.record_from_event(
        _agent_event(summary="Synthetic original agent summary.", revision_id="rev-1"),
        include_transcript=False,
    )
    original_page = adapter.preview(selection, (original,), privacy=_privacy())
    original_intakes = adapter.intakes(selection, original, privacy=_privacy())
    sink = _capture_sink(tmp_path)

    checkpoint, receipt = adapter.import_page(
        AgentSessionCheckpoint.initial(selection),
        adapter.page_from_events(
            selection,
            (_agent_event(summary="Synthetic original agent summary.", revision_id="rev-1"),),
            privacy=_privacy(),
            include_transcripts=False,
            selected_session_ids=("codex-session-1",),
        ),
        original_intakes,
        sink,
    )

    assert receipt.outcome is ConnectorOutcome.COMPLETED
    assert checkpoint.committed_revision_identities == (
        original_intakes[0].key.revision_identity(),
    )

    replayed_checkpoint, replayed_receipt = adapter.import_page(
        checkpoint,
        adapter.page_from_events(
            selection,
            (_agent_event(summary="Synthetic original agent summary.", revision_id="rev-1"),),
            privacy=_privacy(),
            include_transcripts=False,
            selected_session_ids=("codex-session-1",),
        ),
        original_intakes,
        sink,
    )

    assert replayed_checkpoint == checkpoint
    assert replayed_receipt.outcome is ConnectorOutcome.EMPTY

    changed = adapter.record_from_event(
        _agent_event(summary="Synthetic changed agent summary.", revision_id="rev-2"),
        include_transcript=False,
    )
    changed_page = adapter.preview(selection, (changed,), privacy=_privacy())
    changed_intakes = adapter.intakes(selection, changed, privacy=_privacy())
    checkpoint_before = checkpoint.to_dict()
    source_bytes = {p: p.read_bytes() for p in tmp_path.glob("brain-*/sources/**/*")
                    if p.is_file()}
    assert source_bytes
    # Legacy deliveries lack ordering and head-CAS evidence for revision replacement.
    with pytest.raises(ValueError, match="^conflicting delivery$"):
        adapter.import_page(
            checkpoint,
            adapter.page_from_events(
                selection,
                (_agent_event(summary="Synthetic changed agent summary.", revision_id="rev-2"),),
                privacy=_privacy(),
                include_transcripts=False,
                selected_session_ids=("codex-session-1",),
            ),
            changed_intakes,
            sink,
        )
    assert checkpoint.to_dict() == checkpoint_before
    assert {p: p.read_bytes() for p in tmp_path.glob("brain-*/sources/**/*")
            if p.is_file()} == source_bytes

    assert original_page.records[0].delivery_id == changed_page.records[0].delivery_id


def test_agent_session_checkpoint_store_is_metadata_only_and_atomic(tmp_path: Path) -> None:
    adapter = AgentSessionSourceAdapter()
    selection = adapter.project_selection(
        connection_id="account:agent-fixture",
        project_id=_PROJECT_ID,
    )
    record = adapter.record_from_event(
        _agent_event(summary="Synthetic checkpoint body excluded."),
        include_transcript=True,
    )
    page = adapter.preview(selection, (record,), privacy=_privacy(), next_cursor="cursor:agent2")
    intakes = adapter.intakes(selection, record, privacy=_privacy())
    checkpoint = AgentSessionCheckpoint.initial(selection).advance(
        page,
        committed_delivery_ids=tuple(record.delivery_id for record in page.records),
        committed_revision_identities=tuple(
            intake.key.revision_identity() for intake in intakes
        ),
    )

    path = AgentSessionCheckpointStore(tmp_path / "checkpoints").save(checkpoint)

    assert AgentSessionCheckpointStore(tmp_path / "checkpoints").load(selection) == checkpoint
    assert path.read_text(encoding="utf-8").endswith("\n")
    assert "Synthetic checkpoint body" not in path.read_text(encoding="utf-8")


def test_agent_session_checkpoint_advances_legacy_delivery_only_checkpoint() -> None:
    adapter = AgentSessionSourceAdapter()
    selection = adapter.project_selection(
        connection_id="account:agent-fixture",
        project_id=_PROJECT_ID,
    )
    legacy = AgentSessionCheckpoint(
        schema_version=1,
        selection=selection,
        next_cursor=None,
        committed_delivery_ids=("connector.agent_session.legacy",),
    )
    record = adapter.record_from_event(
        _agent_event(summary="Synthetic post-legacy agent summary.", revision_id="rev-2"),
        include_transcript=False,
    )
    page = adapter.preview(selection, (record,), privacy=_privacy())
    intakes = adapter.intakes(selection, record, privacy=_privacy())

    advanced = legacy.advance(
        page,
        committed_delivery_ids=tuple(preview.delivery_id for preview in page.records),
        committed_revision_identities=tuple(
            intake.key.revision_identity() for intake in intakes
        ),
    )

    assert advanced.committed_delivery_ids == (page.records[0].delivery_id,)
    assert advanced.committed_revision_identities == (
        intakes[0].key.revision_identity(),
    )


def test_agent_session_checkpoint_store_loads_legacy_delivery_only_file(
    tmp_path: Path,
) -> None:
    adapter = AgentSessionSourceAdapter()
    selection = adapter.project_selection(
        connection_id="account:agent-fixture",
        project_id=_PROJECT_ID,
    )
    store = AgentSessionCheckpointStore(tmp_path / "checkpoints")
    current = AgentSessionCheckpoint(
        schema_version=1,
        selection=selection,
        next_cursor="cursor:agent2",
        committed_delivery_ids=("connector.agent_session.legacy",),
    )
    path = store.save(current)
    legacy_payload = current.to_dict()
    del legacy_payload["committed_revision_identities"]
    path.write_text(json.dumps(legacy_payload), encoding="utf-8")

    loaded = store.load(selection)

    assert loaded.committed_delivery_ids == ("connector.agent_session.legacy",)
    assert loaded.committed_revision_identities == ()


def test_agent_session_checkpoint_store_rejects_mismatched_embedded_selection(
    tmp_path: Path,
) -> None:
    adapter = AgentSessionSourceAdapter()
    selection = adapter.project_selection(
        connection_id="account:agent-fixture",
        project_id=_PROJECT_ID,
    )
    other_selection = adapter.project_selection(
        connection_id="account:other-fixture",
        project_id=_PROJECT_ID,
    )
    store = AgentSessionCheckpointStore(tmp_path / "checkpoints")
    path = store.save(AgentSessionCheckpoint.initial(selection))
    path.write_text(
        json.dumps(AgentSessionCheckpoint.initial(other_selection).to_dict()),
        encoding="utf-8",
    )

    with pytest.raises(ConnectorContractError, match="invalid agent session checkpoint store"):
        store.load(selection)


def test_agent_session_adapter_models_opt_out_denied_loop_and_unsupported_statuses() -> None:
    adapter = AgentSessionSourceAdapter()

    assert adapter.opted_out_page().status is AgentSessionPageStatus.OPTED_OUT
    assert adapter.not_allowed_page().status is AgentSessionPageStatus.NOT_ALLOWED
    assert adapter.loop_prevented_page().status is AgentSessionPageStatus.LOOP_PREVENTED
    assert adapter.unsupported_format_page().status is AgentSessionPageStatus.UNSUPPORTED_FORMAT


def test_agent_session_adapter_rejects_cross_project_and_brain_feedback_records() -> None:
    adapter = AgentSessionSourceAdapter()
    selection = adapter.project_selection(
        connection_id="account:agent-fixture",
        project_id=_PROJECT_ID,
    )
    cross_project = adapter.record_from_event(
        _agent_event(project_id="project:fedcba9876543210fedcba9876543210"),
        include_transcript=False,
    )

    with pytest.raises(ConnectorContractError, match="invalid agent session record"):
        adapter.preview(selection, (cross_project,), privacy=_privacy())
    with pytest.raises(ConnectorContractError, match="invalid agent session record"):
        adapter.record_from_event(
            _agent_event(source_kind="open_brain_result"),
            include_transcript=False,
        )
    with pytest.raises(ConnectorContractError, match="invalid agent session record"):
        adapter.record_from_event(
            _agent_event(brain_result_reference=True),
            include_transcript=False,
        )
    with pytest.raises(ConnectorContractError, match="invalid agent session record"):
        adapter.record_from_event(
            _agent_event(source_reference="https://local.openbrain-result.invalid/session/1"),
            include_transcript=False,
        )
    with pytest.raises(ConnectorContractError, match="invalid agent session record"):
        adapter.record_from_event(
            _agent_event(
                source_reference=(
                    "https://sessions.example.invalid/"
                    "?transcript=Synthetic%20transcript%20body"
                )
            ),
            include_transcript=False,
        )


def test_agent_session_adapter_requires_explicit_bool_transcript_opt_in() -> None:
    adapter = AgentSessionSourceAdapter()
    selection = adapter.project_selection(
        connection_id="account:agent-fixture",
        project_id=_PROJECT_ID,
    )

    with pytest.raises(ConnectorContractError, match="invalid agent session records"):
        adapter.page_from_events(
            selection,
            (_agent_event(),),
            privacy=_privacy(),
            include_transcripts="false",  # type: ignore[arg-type]
            selected_session_ids=("codex-session-1",),
        )
    with pytest.raises(ConnectorContractError, match="invalid agent session record"):
        adapter.record_from_event(_agent_event(), include_transcript=1)  # type: ignore[arg-type]


def test_agent_session_adapter_requires_selected_sessions_and_omits_others() -> None:
    adapter = AgentSessionSourceAdapter()
    selection = adapter.project_selection(
        connection_id="account:agent-fixture",
        project_id=_PROJECT_ID,
    )

    page = adapter.page_from_events(
        selection,
        (
            _agent_event(session_id="codex-session-1"),
            _agent_event(session_id="codex-session-2"),
        ),
        privacy=_privacy(),
        include_transcripts=False,
        selected_session_ids=("codex-session-2",),
    )

    assert page.preview is not None
    assert len(page.preview.records) == 1
    assert page.preview.records[0].title == "codex session codex-session-2 summary"

    unselected_secret = adapter.page_from_events(
        selection,
        (
            _agent_event(
                session_id="codex-session-1",
                summary="client_secret = synthetic-secret",
                transcript="access_token = synthetic-secret",
            ),
            _agent_event(session_id="codex-session-2"),
        ),
        privacy=_privacy(),
        include_transcripts=True,
        selected_session_ids=("codex-session-2",),
    )

    assert unselected_secret.preview is not None
    assert len(unselected_secret.preview.records) == 2
    assert all(
        "codex-session-2" in record.title
        for record in unselected_secret.preview.records
        if record.title is not None
    )

    unselected_other_project = adapter.page_from_events(
        selection,
        (
            _agent_event(
                project_id="project:fedcba9876543210fedcba9876543210",
                session_id="codex-session-1",
            ),
            _agent_event(session_id="codex-session-2"),
        ),
        privacy=_privacy(),
        include_transcripts=False,
        selected_session_ids=("codex-session-2",),
    )

    assert unselected_other_project.preview is not None
    assert len(unselected_other_project.preview.records) == 1
    assert unselected_other_project.preview.records[0].title == (
        "codex session codex-session-2 summary"
    )

    same_session_other_project = adapter.page_from_events(
        selection,
        (
            _agent_event(
                project_id="project:fedcba9876543210fedcba9876543210",
                session_id="codex-session-2",
            ),
            _agent_event(session_id="codex-session-2"),
        ),
        privacy=_privacy(),
        include_transcripts=False,
        selected_session_ids=("codex-session-2",),
    )

    assert same_session_other_project.preview is not None
    assert len(same_session_other_project.preview.records) == 1
    assert same_session_other_project.preview.records[0].resource_id == _PROJECT_ID

    with pytest.raises(ConnectorContractError, match="invalid agent session records"):
        adapter.page_from_events(
            selection,
            (_agent_event(),),
            privacy=_privacy(),
            include_transcripts=False,
            selected_session_ids=(),
        )
    with pytest.raises(ConnectorContractError, match="invalid agent session records"):
        adapter.page_from_events(
            selection,
            (_agent_event(session_id="codex-session-1"),),
            privacy=_privacy(),
            include_transcripts=False,
            selected_session_ids=("codex-session-2",),
        )


def test_agent_session_summary_only_preview_does_not_inspect_unselected_transcript() -> None:
    adapter = AgentSessionSourceAdapter()
    selection = adapter.project_selection(
        connection_id="account:agent-fixture",
        project_id=_PROJECT_ID,
    )

    page = adapter.page_from_events(
        selection,
        (
            _agent_event(
                summary="Synthetic selected summary.",
                transcript="access_token = synthetic-secret",
            ),
        ),
        privacy=_privacy(),
        include_transcripts=False,
        selected_session_ids=("codex-session-1",),
    )

    assert page.preview is not None
    assert [record.content_type for record in page.preview.records] == ["session_summary"]


def test_agent_session_adapter_requires_agent_session_source_kind() -> None:
    adapter = AgentSessionSourceAdapter()

    for source_kind in (None, "codex_session", "brain_result"):
        with pytest.raises(ConnectorContractError, match="invalid agent session record"):
            adapter.record_from_event(
                _agent_event(source_kind=source_kind),
                include_transcript=False,
            )


def test_agent_session_adapter_rejects_missing_or_secret_content_verdicts() -> None:
    adapter = AgentSessionSourceAdapter()

    for field in ("summary_secret_scan", "transcript_secret_scan"):
        payload = _agent_event()
        del payload[field]
        with pytest.raises(ConnectorContractError, match="invalid agent session record"):
            adapter.record_from_event(payload, include_transcript=True)

    with pytest.raises(ConnectorContractError, match="invalid agent session record"):
        adapter.record_from_event(
            _agent_event(summary="access_token = synthetic-secret"),
            include_transcript=False,
        )
    with pytest.raises(ConnectorContractError, match="invalid agent session record"):
        adapter.record_from_event(
            _agent_event(transcript="client_secret = synthetic-secret"),
            include_transcript=True,
        )


def test_agent_session_record_invariants_reject_direct_secret_and_feedback_bypass() -> None:
    with pytest.raises(ConnectorContractError, match="invalid agent session record"):
        AgentSessionRecord(
            project_id=_PROJECT_ID,
            session_id="codex-session-1",
            revision_id="rev-1",
            client_name="codex",
            summary="access_token = synthetic-secret",
        )
    with pytest.raises(ConnectorContractError, match="invalid agent session record"):
        AgentSessionRecord(
            project_id=_PROJECT_ID,
            session_id="codex-session-1",
            revision_id="rev-1",
            client_name="codex",
            summary="Synthetic agent session summary.",
            transcript="client_secret = synthetic-secret",
        )
    with pytest.raises(ConnectorContractError, match="invalid agent session record"):
        AgentSessionRecord(
            project_id=_PROJECT_ID,
            session_id="codex-session-1",
            revision_id="rev-1",
            client_name="codex",
            summary="Synthetic agent session summary.",
            source_reference="https://local.openbrain-result.invalid/session/1",
        )


def _agent_event(
    *,
    project_id: str = _PROJECT_ID,
    session_id: str = "codex-session-1",
    revision_id: str = "rev-1",
    client_name: str = "codex",
    summary: str = "Synthetic agent session summary.",
    transcript: str | None = "Synthetic transcript body.",
    source_kind: object = "agent_session",
    brain_result_reference: object = False,
    source_reference: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "brain_result_reference": brain_result_reference,
        "client_name": client_name,
        "project_id": project_id,
        "revision_id": revision_id,
        "session_id": session_id,
        "source_kind": source_kind,
        "summary": summary,
        "summary_secret_scan": "clean",
    }
    if transcript is not None:
        payload["transcript"] = transcript
        payload["transcript_secret_scan"] = "clean"
    if source_reference is not None:
        payload["source_reference"] = source_reference
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

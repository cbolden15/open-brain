from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from open_brain_engine.engine import (
    BrainEngine,
    CaptureAction,
    ManagedAccessMode,
    ManagedInferenceRequest,
    ManagedProvider,
    ManagedWorkspaceFailure,
    TextPayload,
)

from open_brain.profile import compile_single_user_local


def _engine(root: Path) -> BrainEngine:
    return BrainEngine.open(
        compile_single_user_local(root, starter_spaces=("Notes",))
    )


def _capture(engine: BrainEngine, text: str, delivery_id: str) -> str:
    engine.capture.accept(
        TextPayload(text),
        delivery_id=delivery_id,
        action=CaptureAction.CANONICAL_NOTE,
        space_id=engine.inbox.spaces()[0].space_id,
    )
    return engine.retrieval.search(text, record_type="canonical")[0].result_id


def _setup(engine: BrainEngine, path: Path, *bodies: str) -> tuple[str, tuple[str, ...]]:
    note_ids = tuple(
        _capture(engine, body, f"managed.inference.capture.{index}")
        for index, body in enumerate(bodies)
    )
    path.mkdir(mode=0o700)
    receipt = engine.managed_workspace.setup(
        str(path), operation_id="managed.inference.setup"
    )
    return receipt.workspace_id, note_ids


def _grant(engine: BrainEngine, workspace_id: str) -> None:
    engine.managed_policy.grant_consent(
        workspace_id,
        ManagedProvider.OPENAI_API,
        ManagedAccessMode.API_KEY,
        operation_id="managed.inference.consent",
    )


def _prepare(
    engine: BrainEngine,
    workspace_id: str,
    note_ids: tuple[str, ...],
    request_id: str,
) -> ManagedInferenceRequest:
    return engine.managed_inference.prepare(
        workspace_id,
        ManagedProvider.OPENAI_API,
        ManagedAccessMode.API_KEY,
        "openai_api:synthetic-v1",
        note_ids,
        request_id=request_id,
        max_output_bytes=4096,
        timeout_seconds=30,
    )


def test_inference_is_revision_bound_and_link_acceptance_requires_explicit_materialization(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path / "brain")
    workspace_id, note_ids = _setup(
        engine,
        tmp_path / "workspace",
        "Solar generation peaks at midday.",
        "Battery storage supplies power after sunset.",
    )
    _grant(engine, workspace_id)
    request_id = "request_00000000-0000-4000-8000-000000000101"

    prepared = _prepare(engine, workspace_id, note_ids, request_id)
    released = engine.managed_inference.release(request_id)
    suggestion = engine.managed_inference.record_suggestion(
        request_id,
        source_note_id=note_ids[0],
        target_note_id=note_ids[1],
        source_quote="peaks at midday",
        target_quote="after sunset",
        model="synthetic-graph-v1",
    )
    source_path = next((tmp_path / "workspace").rglob(f"{note_ids[0]}.md"))
    before_accept = source_path.read_bytes()

    accepted = engine.managed_inference.accept_suggestion(
        workspace_id,
        suggestion.suggestion_id,
        operation_id="managed.inference.accept-link",
    )

    assert prepared == released
    assert prepared.effective_privacy.reason.value == "personal_confirmed"
    assert prepared.effective_privacy.authority.cloud
    assert suggestion.source_revision_id == prepared.sources[0].revision_id
    assert suggestion.target_revision_id == prepared.sources[1].revision_id
    assert all(note_id not in prepared.prompt for note_id in note_ids)
    assert source_path.read_bytes() == before_accept
    assert accepted.status == "suggestion_accepted"

    engine.managed_workspace.materialize(
        workspace_id,
        note_ids[0],
        operation_id="managed.inference.materialize-link",
    )

    assert source_path.read_bytes() != before_accept
    assert f"[[{note_ids[1]}]]" in source_path.read_text(encoding="utf-8")
    with sqlite3.connect(tmp_path / "brain/.open-brain/state/phase1.sqlite3") as connection:
        link = connection.execute(
            "SELECT provenance_json FROM managed_links WHERE source_note_id = ?",
            (note_ids[0],),
        ).fetchone()
    assert link is not None
    assert json.loads(link[0]) == {
        "model": "synthetic-graph-v1",
        "provider": "openai_api",
        "request_id": request_id,
        "suggestion_id": suggestion.suggestion_id,
    }


def test_policy_change_invalidates_pending_work_before_adapter_release(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    workspace_id, note_ids = _setup(
        engine,
        tmp_path / "workspace",
        "First policy source.",
        "Second policy source.",
    )
    _grant(engine, workspace_id)
    request_id = "request_00000000-0000-4000-8000-000000000102"
    _prepare(engine, workspace_id, note_ids, request_id)

    engine.managed_policy.set_exclusion(
        workspace_id,
        "note",
        note_ids[0],
        excluded=True,
        operation_id="managed.inference.exclude",
    )

    with pytest.raises(ManagedWorkspaceFailure, match="stale_request"):
        engine.managed_inference.release(request_id)
    with sqlite3.connect(tmp_path / "brain/.open-brain/state/phase1.sqlite3") as connection:
        request = connection.execute(
            "SELECT status FROM managed_inference_requests WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        budget = connection.execute(
            """SELECT reserved_requests, used_requests, uncertain_requests
            FROM managed_inference_budgets WHERE workspace_id = ?""",
            (workspace_id,),
        ).fetchone()
    assert request == ("cancelled",)
    assert budget == (0, 0, 0)


def test_materialization_conflict_preserves_both_versions_until_owner_resolution(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    workspace = tmp_path / "workspace"
    engine = _engine(root)
    workspace_id, note_ids = _setup(
        engine,
        workspace,
        "First conflict source.",
        "Second conflict source.",
    )
    _grant(engine, workspace_id)
    request_id = "request_00000000-0000-4000-8000-000000000109"
    _prepare(engine, workspace_id, note_ids, request_id)
    engine.managed_inference.release(request_id)
    suggestion = engine.managed_inference.record_suggestion(
        request_id,
        source_note_id=note_ids[0],
        target_note_id=note_ids[1],
        source_quote="First conflict",
        target_quote="Second conflict",
        model="synthetic-graph-v1",
    )
    engine.managed_inference.accept_suggestion(
        workspace_id,
        suggestion.suggestion_id,
        operation_id="managed.conflict.accept-link",
    )
    source_path = next(workspace.rglob(f"{note_ids[0]}.md"))
    external = source_path.read_bytes() + b"\nOwner changed this after inference.\n"
    source_path.write_bytes(external)

    with pytest.raises(ManagedWorkspaceFailure, match="target_changed"):
        engine.managed_workspace.materialize(
            workspace_id,
            note_ids[0],
            operation_id="managed.conflict.first-materialize",
        )
    assert source_path.read_bytes() == external
    with sqlite3.connect(root / ".open-brain/state/phase1.sqlite3") as connection:
        connection.row_factory = sqlite3.Row
        conflict = connection.execute(
            "SELECT * FROM managed_conflicts WHERE note_id = ? AND status = 'open'",
            (note_ids[0],),
        ).fetchone()
        accepted = connection.execute(
            """SELECT body_bytes FROM managed_note_revisions
            WHERE revision_id = (
                SELECT accepted_revision_id FROM managed_notes WHERE note_id = ?
            )""",
            (note_ids[0],),
        ).fetchone()[0]
    assert conflict is not None
    assert conflict["candidate_body_bytes"] == external
    assert accepted != external

    engine.managed_workspace.resolve_conflict(
        workspace_id,
        note_ids[0],
        "accepted",
        operation_id="managed.conflict.resolve",
    )
    assert source_path.read_bytes() == external
    engine.managed_workspace.materialize(
        workspace_id,
        note_ids[0],
        operation_id="managed.conflict.final-materialize",
    )
    assert source_path.read_bytes() == accepted


def test_external_edit_cancels_reserved_request_without_releasing_content(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    workspace_id, note_ids = _setup(
        engine,
        tmp_path / "workspace",
        "First exact source.",
        "Second exact source.",
    )
    _grant(engine, workspace_id)
    request_id = "request_00000000-0000-4000-8000-000000000103"
    _prepare(engine, workspace_id, note_ids, request_id)
    source_path = next((tmp_path / "workspace").rglob(f"{note_ids[0]}.md"))
    source_path.write_bytes(source_path.read_bytes() + b"external edit\n")

    with pytest.raises(ManagedWorkspaceFailure, match="ineligible_source"):
        engine.managed_inference.release(request_id)

    with sqlite3.connect(tmp_path / "brain/.open-brain/state/phase1.sqlite3") as connection:
        assert connection.execute(
            "SELECT status FROM managed_inference_requests WHERE request_id = ?",
            (request_id,),
        ).fetchone() == ("cancelled",)


def test_restart_cancels_reserved_marks_dispatch_uncertain_and_deactivates_consent(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    engine = _engine(root)
    workspace_id, note_ids = _setup(
        engine,
        tmp_path / "workspace",
        "First restart source.",
        "Second restart source.",
    )
    _grant(engine, workspace_id)
    reserved_id = "request_00000000-0000-4000-8000-000000000104"
    dispatch_id = "request_00000000-0000-4000-8000-000000000105"
    _prepare(engine, workspace_id, note_ids, reserved_id)
    _prepare(engine, workspace_id, note_ids, dispatch_id)
    engine.managed_inference.release(dispatch_id)

    reopened = _engine(root)

    with sqlite3.connect(root / ".open-brain/state/phase1.sqlite3") as connection:
        statuses = dict(
            connection.execute(
                "SELECT request_id, status FROM managed_inference_requests ORDER BY request_id"
            )
        )
        consent = connection.execute(
            "SELECT active, revoked_at FROM managed_consents WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()
        budget = connection.execute(
            """SELECT reserved_requests, used_requests, uncertain_requests
            FROM managed_inference_budgets WHERE workspace_id = ?""",
            (workspace_id,),
        ).fetchone()
    assert statuses == {reserved_id: "cancelled", dispatch_id: "uncertain"}
    assert consent is not None and consent[0] == 0 and consent[1] is not None
    assert budget == (0, 0, 1)
    with pytest.raises(ManagedWorkspaceFailure, match="active_consent_required"):
        _prepare(
            reopened,
            workspace_id,
            note_ids,
            "request_00000000-0000-4000-8000-000000000106",
        )


def test_shared_budget_rejects_before_a_request_is_reserved(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    engine = _engine(root)
    workspace_id, note_ids = _setup(
        engine,
        tmp_path / "workspace",
        "First budget source.",
        "Second budget source.",
    )
    _grant(engine, workspace_id)
    first_id = "request_00000000-0000-4000-8000-000000000107"
    _prepare(engine, workspace_id, note_ids, first_id)
    with sqlite3.connect(root / ".open-brain/state/phase1.sqlite3") as connection:
        connection.execute(
            """UPDATE managed_inference_budgets SET request_limit = 1
            WHERE workspace_id = ? AND provider = 'openai_api'""",
            (workspace_id,),
        )

    with pytest.raises(ManagedWorkspaceFailure, match="budget_exhausted"):
        _prepare(
            engine,
            workspace_id,
            note_ids,
            "request_00000000-0000-4000-8000-000000000108",
        )

    with sqlite3.connect(root / ".open-brain/state/phase1.sqlite3") as connection:
        assert connection.execute(
            "SELECT count(*) FROM managed_inference_requests"
        ).fetchone() == (1,)

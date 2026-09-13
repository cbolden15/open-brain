from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from open_brain_engine.engine import (
    BrainEngine,
    CaptureAction,
    InjectedFault,
    ManagedWorkspaceFailure,
    ManagedWorkspaceFault,
    TextPayload,
)
from open_brain_engine.storage.markdown import parse_markdown, render_markdown

from open_brain.profile import compile_single_user_local


def _engine(
    root: Path, *, faults: set[ManagedWorkspaceFault] | None = None
) -> BrainEngine:
    return BrainEngine.open(
        compile_single_user_local(root, starter_spaces=("Notes",)),
        faults=faults or set(),
    )


def _capture_page(engine: BrainEngine, body: str, delivery_id: str) -> str:
    receipt = engine.capture.accept(
        TextPayload(body),
        delivery_id=delivery_id,
        action=CaptureAction.CANONICAL_NOTE,
        space_id=engine.inbox.spaces()[0].space_id,
    )
    result = engine.retrieval.search(body, record_type="canonical")[0]
    assert result.capture_id == receipt.capture_id
    return result.result_id


def _managed_page(workspace: Path, note_id: str) -> Path:
    return next(workspace.rglob(f"{note_id}.md"))


def test_managed_workspace_accepts_an_exact_observed_edit_without_rewriting_it(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path / "brain")
    note_id = _capture_page(engine, "Original managed body", "managed.capture")
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)

    setup = engine.managed_workspace.setup(str(workspace), operation_id="managed.setup")
    original = _managed_page(workspace, note_id)
    renamed = workspace / "renamed" / original.name
    renamed.parent.mkdir()
    original.rename(renamed)
    parsed = parse_markdown(renamed.read_bytes())
    renamed.write_text(
        render_markdown(fields=parsed.fields, body="Accepted owner edit\n"),
        encoding="utf-8",
    )
    expected = renamed.read_bytes()

    observation = engine.managed_workspace.observe(setup.workspace_id)
    changed = next(note for note in observation.notes if note.note_id == note_id)
    receipt = engine.managed_workspace.accept_observed(
        setup.workspace_id,
        note_id,
        generation=observation.generation,
        operation_id="managed.accept",
    )

    assert changed.relative_path == f"renamed/{note_id}.md"
    assert changed.changed
    assert receipt.status == "accepted"
    assert renamed.read_bytes() == expected
    current = engine.managed_workspace.observe(setup.workspace_id)
    rebound = next(note for note in current.notes if note.note_id == note_id)
    assert not rebound.changed


def test_managed_workspace_recovery_finishes_one_previously_authorized_setup_write(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    engine = _engine(root, faults={ManagedWorkspaceFault.AFTER_TARGET_WRITE})
    note_id = _capture_page(engine, "Recovery managed body", "managed.recovery.capture")
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)

    with pytest.raises(InjectedFault) as interrupted:
        engine.managed_workspace.setup(str(workspace), operation_id="managed.recovery.setup")
    assert interrupted.value.point is ManagedWorkspaceFault.AFTER_TARGET_WRITE
    expected = _managed_page(workspace, note_id).read_bytes()

    reopened = _engine(root)

    assert _managed_page(workspace, note_id).read_bytes() == expected
    with sqlite3.connect(root / ".open-brain/state/phase1.sqlite3") as connection:
        operation = connection.execute(
            "SELECT status, stage FROM managed_operations WHERE kind = 'setup'"
        ).fetchone()
        revisions = connection.execute(
            "SELECT count(*) FROM managed_note_revisions WHERE note_id = ?", (note_id,)
        ).fetchone()[0]
    assert operation == ("completed", 3)
    assert revisions == 1
    assert reopened.managed_workspace.recover() == 0


def test_deactivate_and_restore_change_state_without_touching_markdown_or_consent(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    engine = _engine(root)
    note_id = _capture_page(engine, "Inactive managed body", "managed.inactive.capture")
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    setup = engine.managed_workspace.setup(str(workspace), operation_id="managed.inactive.setup")
    page = _managed_page(workspace, note_id)
    expected = page.read_bytes()
    database = root / ".open-brain/state/phase1.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO managed_consents
            (consent_id, workspace_id, provider, operation, note_scope,
             granted_generation, active, granted_at)
            VALUES ('consent_test', ?, 'fake', 'infer', '*', 0, 1,
                    '2026-09-12T00:00:00Z')""",
            (setup.workspace_id,),
        )

    engine.managed_workspace.deactivate(
        setup.workspace_id, note_id, operation_id="managed.deactivate"
    )
    assert engine.managed_workspace.observe(setup.workspace_id).notes == ()
    engine.managed_workspace.restore(
        setup.workspace_id, note_id, operation_id="managed.restore"
    )

    assert page.read_bytes() == expected
    assert len(engine.managed_workspace.observe(setup.workspace_id).notes) == 1
    with sqlite3.connect(database) as connection:
        consent = connection.execute(
            "SELECT active, revoked_at FROM managed_consents WHERE consent_id = 'consent_test'"
        ).fetchone()
    assert consent is not None
    assert consent[0] == 0
    assert consent[1] is not None


def test_managed_workspace_rejects_overlap_and_existing_markdown(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    engine = _engine(root)

    with pytest.raises(ManagedWorkspaceFailure, match="unsafe_workspace"):
        engine.managed_workspace.setup(str(root), operation_id="managed.overlap")

    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    (workspace / "existing.md").write_text("synthetic", encoding="utf-8")
    with pytest.raises(ManagedWorkspaceFailure, match="unsafe_workspace"):
        engine.managed_workspace.setup(str(workspace), operation_id="managed.nonempty")

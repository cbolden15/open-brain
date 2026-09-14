from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from open_brain_engine.engine import (
    BrainEngine,
    CaptureAction,
    InjectedFault,
    ManagedAccessMode,
    ManagedExclusion,
    ManagedProvider,
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


def test_graph_snapshot_uses_accepted_revisions_and_shared_exclusions(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    engine = _engine(root)
    first_id = _capture_page(
        engine, "Solar generation peaks at midday.", "managed.graph.first"
    )
    second_id = _capture_page(
        engine, "Battery storage supplies power after sunset.", "managed.graph.second"
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    setup = engine.managed_workspace.setup(
        str(workspace), operation_id="managed.graph.setup"
    )

    original = engine.managed_workspace.graph_snapshot(setup.workspace_id)
    first_path = _managed_page(workspace, first_id)
    first_path.write_text("Unaccepted workspace edit\n", encoding="utf-8")
    unchanged = engine.managed_workspace.graph_snapshot(setup.workspace_id)

    assert original == unchanged
    assert {source.note_id for source in original.sources} == {first_id, second_id}
    bodies = {source.note_id: source.body for source in original.sources}
    assert bodies[first_id].endswith("\n\nSolar generation peaks at midday.\n")
    assert bodies[second_id].endswith(
        "\n\nBattery storage supplies power after sunset.\n"
    )
    assert "Unaccepted workspace edit" not in bodies[first_id]
    assert all(source.privacy_sha256 for source in original.sources)

    engine.managed_policy.set_exclusion(
        setup.workspace_id,
        "note",
        first_id,
        excluded=True,
        operation_id="managed.graph.exclude",
    )
    excluded = engine.managed_workspace.graph_snapshot(setup.workspace_id)

    assert [source.note_id for source in excluded.sources] == [second_id]
    assert excluded.policy_generation == original.policy_generation + 1
    assert excluded.snapshot_sha256 != original.snapshot_sha256
    relative_path = first_path.relative_to(workspace).as_posix()
    assert engine.managed_workspace.note_id_for_path(setup.workspace_id, relative_path) == first_id
    assert engine.managed_policy.active_exclusions(setup.workspace_id) == (
        ManagedExclusion(kind="note", subject=first_id, relative_path=relative_path),
    )

    engine.managed_policy.set_exclusion(
        setup.workspace_id,
        "note",
        first_id,
        excluded=False,
        operation_id="managed.graph.include",
    )
    assert engine.managed_policy.active_exclusions(setup.workspace_id) == ()


def test_graph_snapshot_rejects_invalid_stored_privacy_before_extraction(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    engine = _engine(root)
    note_id = _capture_page(engine, "Private graph source.", "managed.graph.privacy")
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    setup = engine.managed_workspace.setup(
        str(workspace), operation_id="managed.graph.privacy.setup"
    )
    database = root / ".open-brain/state/phase1.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE managed_note_revisions SET privacy_json = '{}' WHERE note_id = ?",
            (note_id,),
        )

    with pytest.raises(ManagedWorkspaceFailure, match="ineligible_source"):
        engine.managed_workspace.graph_snapshot(setup.workspace_id)


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
    engine.managed_policy.grant_consent(
        setup.workspace_id,
        ManagedProvider.OPENAI_API,
        ManagedAccessMode.API_KEY,
        operation_id="managed.consent",
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
            "SELECT active, revoked_at FROM managed_consents WHERE workspace_id = ?",
            (setup.workspace_id,),
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


def test_explicit_refresh_adds_only_new_canonical_pages(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    setup = engine.managed_workspace.setup(str(workspace), operation_id="managed.empty.setup")
    engine.capture.accept(TextPayload("Quick content"), delivery_id="managed.quick")
    note_id = _capture_page(engine, "Later canonical body", "managed.later.capture")

    first = engine.managed_workspace.refresh(
        setup.workspace_id, operation_id="managed.explicit.refresh"
    )
    repeated = engine.managed_workspace.refresh(
        setup.workspace_id, operation_id="managed.explicit.refresh"
    )

    assert first.status == "refreshed"
    assert not first.duplicate
    assert repeated.duplicate
    assert _managed_page(workspace, note_id).is_file()
    assert len(tuple(workspace.rglob("*.md"))) == 1


def test_missing_file_is_observed_but_requires_explicit_deactivation(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    note_id = _capture_page(engine, "Missing managed body", "managed.missing.capture")
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    setup = engine.managed_workspace.setup(str(workspace), operation_id="managed.missing.setup")
    page = _managed_page(workspace, note_id)
    page.unlink()

    missing = engine.managed_workspace.observe(setup.workspace_id).notes[0]

    assert missing.note_id == note_id
    assert not missing.present
    assert missing.observed_sha256 is None
    assert missing.changed
    with sqlite3.connect(tmp_path / "brain/.open-brain/state/phase1.sqlite3") as connection:
        assert connection.execute(
            "SELECT active FROM managed_notes WHERE note_id = ?", (note_id,)
        ).fetchone() == (1,)

    engine.managed_workspace.deactivate(
        setup.workspace_id, note_id, operation_id="managed.missing.deactivate"
    )
    assert engine.managed_workspace.observe(setup.workspace_id).notes == ()

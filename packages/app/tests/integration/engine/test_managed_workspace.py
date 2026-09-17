from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from open_brain_engine.engine import (
    BrainEngine,
    CaptureAction,
    CaptureFault,
    InjectedFault,
    ManagedAccessMode,
    ManagedExclusion,
    ManagedProvider,
    ManagedWorkspaceFailure,
    ManagedWorkspaceFault,
    PortabilityFault,
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


def _publish(engine: BrainEngine, note_id: str, version: str) -> bytes:
    from open_brain_engine.engine import DecisionOutcome, ProposalDraft

    capture = engine.capture.accept(
        TextPayload(f"Evidence {version}"),
        delivery_id=f"refresh.source.{version}",
        space_id=engine.inbox.spaces()[0].space_id,
    )
    proposal = engine.review.propose(
        (capture.capture_id,),
        (ProposalDraft("Refresh publication", f"Approved {version}"),),
        delivery_id=f"refresh.propose.{version}",
        target_page_id=note_id,
    )[0]
    engine.review.decide(
        proposal.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id=f"refresh.approve.{version}",
        expected_review_digest=proposal.review_digest,
    )
    return next((engine.profile.root / "content/spaces").rglob(f"{note_id}.md")).read_bytes()


def _refresh_fixture(tmp_path: Path) -> tuple[BrainEngine, str, str, Path]:
    engine = _engine(tmp_path / "brain")
    note_id = _capture_page(engine, "Original publication", "refresh.original")
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    setup = engine.managed_workspace.setup(str(workspace), operation_id="refresh.setup")
    return engine, setup.workspace_id, note_id, _managed_page(workspace, note_id)


def _rows(engine: BrainEngine, sql: str, *args: object) -> list[sqlite3.Row]:
    with sqlite3.connect(engine.profile.root / ".open-brain/state/phase1.sqlite3") as connection:
        connection.row_factory = sqlite3.Row
        return list(connection.execute(sql, args))


def _note(engine: BrainEngine, note_id: str) -> sqlite3.Row:
    return _rows(engine, "SELECT * FROM managed_notes WHERE note_id = ?", note_id)[0]


def _revisions(engine: BrainEngine, note_id: str) -> list[sqlite3.Row]:
    return _rows(
        engine,
        "SELECT * FROM managed_note_revisions WHERE note_id = ? ORDER BY recorded_at",
        note_id,
    )


def test_refresh_advances_renamed_note_once_and_binds_replay_to_original_publication(
    tmp_path: Path,
) -> None:
    from hashlib import sha256

    engine, workspace_id, note_id, page = _refresh_fixture(tmp_path)
    original = page.read_bytes()
    initial = _note(engine, note_id)
    renamed = tmp_path / "workspace/renamed.md"
    page.rename(renamed)
    engine.managed_workspace.observe(workspace_id)
    second = _publish(engine, note_id, "second")
    engine.managed_workspace.refresh(workspace_id, operation_id="refresh.second")
    current = _note(engine, note_id)
    revisions = _revisions(engine, note_id)
    assert renamed.read_bytes() == second
    assert not page.exists()
    assert current["relative_path"] == "renamed.md"
    assert current["accepted_revision_id"] == current["materialized_revision_id"]
    assert (
        current["materialized_sha256"] == current["write_base_sha256"] == sha256(second).hexdigest()
    )
    assert revisions[0]["body_bytes"] == original
    assert revisions[1]["parent_revision_id"] == initial["accepted_revision_id"]
    assert revisions[1]["body_sha256"] == sha256(second).hexdigest()
    observation = engine.managed_workspace.observe(workspace_id)
    assert not observation.notes[0].changed
    before_stat = renamed.stat()
    engine.managed_workspace.refresh(workspace_id, operation_id="refresh.unchanged")
    assert len(_revisions(engine, note_id)) == 2
    assert renamed.stat().st_mtime_ns == before_stat.st_mtime_ns
    third = _publish(engine, note_id, "third")
    for reused_id in ("refresh.second", "refresh.unchanged"):
        assert engine.managed_workspace.refresh(workspace_id, operation_id=reused_id).duplicate
    assert renamed.read_bytes() == second
    assert len(_revisions(engine, note_id)) == 2
    engine.managed_workspace.refresh(workspace_id, operation_id="refresh.third")
    assert renamed.read_bytes() == third
    assert len(_revisions(engine, note_id)) == 3


@pytest.mark.parametrize("fault", list(ManagedWorkspaceFault))
@pytest.mark.parametrize("reopen", [False, True])
def test_refresh_interruption_recovers_original_children_exactly_once(
    tmp_path: Path,
    fault: ManagedWorkspaceFault,
    reopen: bool,
) -> None:
    import json

    engine, workspace_id, note_id, page = _refresh_fixture(tmp_path)
    initial = dict(_note(engine, note_id))
    second = _publish(engine, note_id, "second")
    engine._faults.add(fault)
    with pytest.raises(InjectedFault):
        engine.managed_workspace.refresh(workspace_id, operation_id="refresh.interrupted")
    pending = _note(engine, note_id)
    assert pending["accepted_revision_id"] != initial["accepted_revision_id"]
    for field in ("materialized_revision_id", "materialized_sha256", "write_base_sha256"):
        assert pending[field] == initial[field]
    parent = _rows(
        engine, "SELECT * FROM managed_operations WHERE operation_id = ?", "refresh.interrupted"
    )[0]
    assert parent["status"] == "prepared"
    assert len(json.loads(parent["body_bytes"])["children"]) == 1
    # A later publication cannot change the durable authorization, even during recovery.
    _publish(engine, note_id, "third")
    if reopen:
        engine = _engine(engine.profile.root)
    receipt = engine.managed_workspace.refresh(workspace_id, operation_id="refresh.interrupted")
    assert receipt.duplicate
    assert page.read_bytes() == second
    assert len(_revisions(engine, note_id)) == 2
    assert _note(engine, note_id)["materialized_revision_id"] == pending["accepted_revision_id"]
    assert engine.managed_workspace.recover() == 0
    engine.managed_workspace.refresh(workspace_id, operation_id="refresh.fresh")
    assert b"Approved third" in page.read_bytes()


@pytest.mark.parametrize("accepted", [False, True])
def test_refresh_retains_owner_edits_and_open_conflicts_until_explicit_resolution(
    tmp_path: Path,
    accepted: bool,
) -> None:
    engine, workspace_id, note_id, page = _refresh_fixture(tmp_path)
    owner = page.read_bytes() + b"\nOwner edit.\n"
    page.write_bytes(owner)
    if accepted:
        observed = engine.managed_workspace.observe(workspace_id)
        engine.managed_workspace.accept_observed(
            workspace_id,
            note_id,
            generation=observed.generation,
            operation_id="owner.accept",
        )
    old_head = _note(engine, note_id)["accepted_revision_id"]
    engine.managed_workspace.refresh(workspace_id, operation_id="refresh.same")
    assert page.read_bytes() == owner
    assert _note(engine, note_id)["accepted_revision_id"] == old_head
    upstream = _publish(engine, note_id, "second")
    with pytest.raises(ManagedWorkspaceFailure, match="target_changed"):
        engine.managed_workspace.refresh(workspace_id, operation_id="refresh.divergent")
    assert page.read_bytes() == owner
    assert _note(engine, note_id)["accepted_revision_id"] == old_head
    conflict = engine.managed_workspace.review_conflict(workspace_id, note_id)
    assert conflict.accepted_body.encode() == upstream
    assert conflict.workspace_body.encode() == owner
    count = len(_revisions(engine, note_id))
    _publish(engine, note_id, "third")
    engine.managed_workspace.refresh(workspace_id, operation_id="refresh.open")
    assert len(_revisions(engine, note_id)) == count
    assert engine.managed_workspace.review_conflict(workspace_id, note_id) == conflict
    engine.managed_workspace.resolve_conflict(
        workspace_id,
        note_id,
        "workspace",
        operation_id="owner.resolve",
    )
    assert page.read_bytes() == owner
    merge = _note(engine, note_id)["accepted_revision_id"]
    # A workspace-selected merge also retains its authority on the next publication.
    with pytest.raises(ManagedWorkspaceFailure, match="target_changed"):
        engine.managed_workspace.refresh(workspace_id, operation_id="refresh.merge")
    assert _note(engine, note_id)["accepted_revision_id"] == merge
    assert page.read_bytes() == owner
    assert (
        engine.managed_workspace.review_conflict(workspace_id, note_id).workspace_body.encode()
        == owner
    )


@pytest.mark.parametrize("state", ["missing", "inactive", "excluded", "folder_excluded"])
def test_refresh_preserves_ineligible_existing_notes(tmp_path: Path, state: str) -> None:
    engine, workspace_id, note_id, page = _refresh_fixture(tmp_path)
    before = dict(_note(engine, note_id))
    original = page.read_bytes()
    if state == "missing":
        page.unlink()
    elif state == "inactive":
        engine.managed_workspace.deactivate(workspace_id, note_id, operation_id="owner.deactivate")
    else:
        engine.managed_policy.set_exclusion(
            workspace_id,
            "note" if state == "excluded" else "folder",
            note_id
            if state == "excluded"
            else page.parent.relative_to(tmp_path / "workspace").as_posix(),
            excluded=True,
            operation_id="owner.exclude",
        )
    _publish(engine, note_id, "second")
    engine.managed_workspace.refresh(workspace_id, operation_id="refresh.ineligible")
    assert _note(engine, note_id)["accepted_revision_id"] == before["accepted_revision_id"]
    assert len(_revisions(engine, note_id)) == 1
    if state == "missing":
        assert not page.exists()
    else:
        assert page.read_bytes() == original


def test_refresh_fails_closed_without_imported_operation_lineage(tmp_path: Path) -> None:
    engine, workspace_id, note_id, page = _refresh_fixture(tmp_path)
    original = page.read_bytes()
    # Portable import retains revisions but deliberately omits journal authority.
    with engine._store.transaction() as connection:
        connection.execute("DELETE FROM managed_write_authority")
        connection.execute("DELETE FROM managed_operations")
    _publish(engine, note_id, "second")
    with pytest.raises(ManagedWorkspaceFailure, match="ineligible_source"):
        engine.managed_workspace.refresh(workspace_id, operation_id="refresh.imported")
    assert page.read_bytes() == original
    assert len(_revisions(engine, note_id)) == 1


def test_refresh_metadata_only_uses_representative_capture_without_rewriting_body(
    tmp_path: Path,
) -> None:
    import json

    engine, workspace_id, note_id, page = _refresh_fixture(tmp_path)
    before = page.stat()
    original = page.read_bytes()
    representative = _rows(
        engine, "SELECT capture_id FROM search_documents WHERE result_id = ?", note_id
    )[0][0]
    with engine._store.transaction() as connection:
        # Raw source content is not a canonical publication.
        connection.execute(
            "UPDATE captures SET search_text = 'raw update only' WHERE capture_id = ?",
            (representative,),
        )
    engine.managed_workspace.refresh(workspace_id, operation_id="refresh.raw")
    assert len(_revisions(engine, note_id)) == 1
    with engine._store.transaction() as connection:
        provenance = json.loads(
            connection.execute(
                "SELECT provenance_json FROM captures WHERE capture_id = ?", (representative,)
            ).fetchone()[0]
        )
        provenance["source_ref"] = "synthetic-metadata-update"
        metadata = json.dumps(provenance)
        connection.execute(
            "UPDATE captures SET provenance_json = ? WHERE capture_id = ?",
            (metadata, representative),
        )
    engine.managed_workspace.refresh(workspace_id, operation_id="refresh.metadata")
    revisions = _revisions(engine, note_id)
    assert len(revisions) == 2
    assert revisions[1]["provenance_json"] == metadata
    assert revisions[1]["privacy_json"] == revisions[0]["privacy_json"]
    assert revisions[1]["body_bytes"] == original
    assert page.read_bytes() == original
    assert page.stat().st_mtime_ns == before.st_mtime_ns
    assert page.stat().st_ino == before.st_ino


@pytest.mark.parametrize(
    "changed", ["revision", "path", "inactive", "excluded", "preimage", "root", "symlink"]
)
def test_pending_refresh_revalidates_state_before_any_write(tmp_path: Path, changed: str) -> None:
    from open_brain_engine.storage.filesystem import RootConfinementError

    engine, workspace_id, note_id, page = _refresh_fixture(tmp_path)
    initial = _note(engine, note_id)
    original = page.read_bytes()
    _publish(engine, note_id, "second")
    engine._faults.add(ManagedWorkspaceFault.AFTER_OPERATION_PREPARED)
    with pytest.raises(InjectedFault):
        engine.managed_workspace.refresh(workspace_id, operation_id="refresh.stale")
    protected = page
    if changed == "revision":
        with engine._store.transaction() as connection:
            connection.execute(
                "UPDATE managed_notes SET accepted_revision_id = ? WHERE note_id = ?",
                (initial["accepted_revision_id"], note_id),
            )
    elif changed == "path":
        with engine._store.transaction() as connection:
            connection.execute(
                "UPDATE managed_notes SET relative_path = 'elsewhere.md' WHERE note_id = ?",
                (note_id,),
            )
    elif changed == "inactive":
        engine.managed_workspace.deactivate(workspace_id, note_id, operation_id="owner.pause")
    elif changed == "excluded":
        engine.managed_policy.set_exclusion(
            workspace_id, "note", note_id, excluded=True, operation_id="owner.exclude"
        )
    elif changed == "preimage":
        original += b"\nIntervening owner write\n"
        page.write_bytes(original)
    elif changed == "root":
        old_root = tmp_path / "moved-workspace"
        (tmp_path / "workspace").rename(old_root)
        (tmp_path / "workspace").mkdir(mode=0o700)
        protected = old_root / page.relative_to(tmp_path / "workspace")
    else:
        protected = tmp_path / "owner.md"
        page.rename(protected)
        page.symlink_to(protected)
    with pytest.raises((ManagedWorkspaceFailure, RootConfinementError)):
        engine.managed_workspace.refresh(workspace_id, operation_id="refresh.stale")
    assert protected.read_bytes() == original
    assert _note(engine, note_id)["materialized_revision_id"] == initial["materialized_revision_id"]
    engine._faults.clear()
    if changed in {"inactive", "excluded", "preimage"}:
        engine = _engine(engine.profile.root)
        assert engine.managed_workspace.recover() == 0
        assert not _rows(
            engine, "SELECT * FROM managed_operations "
            "WHERE status IN ('prepared', 'writing', 'promoted')",
        )
    else:
        with pytest.raises((ManagedWorkspaceFailure, RootConfinementError)):
            _engine(engine.profile.root)
    assert protected.read_bytes() == original


def test_refresh_guard_catches_mutation_immediately_before_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import open_brain_engine.engine.managed_workspace as module

    engine, workspace_id, note_id, page = _refresh_fixture(tmp_path)
    owner = page.read_bytes() + b"\nRacing owner edit\n"
    _publish(engine, note_id, "second")
    from open_brain_engine.storage.filesystem import atomic_replace

    replace = atomic_replace

    def mutate_then_replace(**kwargs: object) -> None:
        page.write_bytes(owner)
        replace(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(module, "atomic_replace", mutate_then_replace)
    with pytest.raises(ManagedWorkspaceFailure, match="target_changed"):
        engine.managed_workspace.refresh(workspace_id, operation_id="refresh.race")
    assert page.read_bytes() == owner
    assert (
        engine.managed_workspace.review_conflict(workspace_id, note_id).workspace_body.encode()
        == owner
    )


@pytest.mark.parametrize("materialized", [False, True])
@pytest.mark.parametrize("choice", ["workspace", "accepted"])
def test_refresh_preserves_accepted_links_and_resolves_without_implicit_write(
    tmp_path: Path,
    materialized: bool,
    choice: str,
) -> None:
    from open_brain_engine.engine import ManagedAccessMode, ManagedProvider

    engine, workspace_id, note_id, page = _refresh_fixture(tmp_path)
    target_id = _capture_page(engine, "Target link evidence", "refresh.link.target")
    engine.managed_workspace.refresh(workspace_id, operation_id="refresh.add-target")
    engine.managed_policy.grant_consent(
        workspace_id,
        ManagedProvider.OPENAI_API,
        ManagedAccessMode.API_KEY,
        operation_id="refresh.link.consent",
    )
    request_id = "request_00000000-0000-4000-8000-000000000555"
    engine.managed_inference.prepare(
        workspace_id,
        ManagedProvider.OPENAI_API,
        ManagedAccessMode.API_KEY,
        "openai_api:synthetic-v1",
        (note_id, target_id),
        request_id=request_id,
        max_output_bytes=4096,
        timeout_seconds=30,
    )
    engine.managed_inference.release(request_id)
    suggestion = engine.managed_inference.record_suggestion(
        request_id,
        source_note_id=note_id,
        target_note_id=target_id,
        source_quote="Original publication",
        target_quote="Target link evidence",
        model="synthetic-v1",
    )
    engine.managed_inference.accept_suggestion(
        workspace_id,
        suggestion.suggestion_id,
        operation_id="owner.link",
    )
    if materialized:
        engine.managed_workspace.materialize(workspace_id, note_id, operation_id="owner.link.write")
    old_note = dict(_note(engine, note_id))
    link_revision = _revisions(engine, note_id)[-1]
    original_disk = page.read_bytes()
    engine.managed_workspace.refresh(workspace_id, operation_id="refresh.link.unchanged")
    assert dict(_note(engine, note_id)) == old_note
    upstream = _publish(engine, note_id, "second")
    with pytest.raises(ManagedWorkspaceFailure, match="target_changed"):
        engine.managed_workspace.refresh(workspace_id, operation_id="refresh.link.changed")
    assert dict(_note(engine, note_id)) == old_note
    assert page.read_bytes() == original_disk
    conflict = engine.managed_workspace.review_conflict(workspace_id, note_id)
    assert conflict.accepted_body.encode() == upstream
    assert conflict.workspace_body.encode() == link_revision["body_bytes"]
    assert f"[[{target_id}]]" in conflict.workspace_body
    engine = _engine(engine.profile.root)
    engine.managed_workspace.resolve_conflict(
        workspace_id,
        note_id,
        choice,
        operation_id="owner.link.resolve",
    )
    assert page.read_bytes() == original_disk
    resolved = _note(engine, note_id)
    if not materialized or choice == "accepted":
        assert resolved["materialized_revision_id"] == old_note["materialized_revision_id"]
    assert engine.managed_workspace.open_conflicts(workspace_id) == ()
    engine.managed_workspace.materialize(
        workspace_id, note_id, operation_id="owner.resolution.write"
    )
    expected = link_revision["body_bytes"] if choice == "workspace" else upstream
    assert page.read_bytes() == expected
    assert _revisions(engine, note_id)[1]["body_bytes"] == link_revision["body_bytes"]
    # Same upstream stays a no-op after either explicit resolution choice.
    revisions = len(_revisions(engine, note_id))
    engine.managed_workspace.refresh(workspace_id, operation_id="refresh.resolved")
    assert len(_revisions(engine, note_id)) == revisions
    assert page.read_bytes() == expected


def test_refresh_new_note_collision_preserves_owner_file_and_reports_bound_conflict(
    tmp_path: Path,
) -> None:
    engine, workspace_id, _, _ = _refresh_fixture(tmp_path)
    added = _capture_page(engine, "New canonical publication", "refresh.collision")
    canonical = next((engine.profile.root / "content").rglob(f"{added}.md"))
    target = tmp_path / "workspace" / canonical.relative_to(engine.profile.root / "content")
    target.write_bytes(b"Owner collision content")
    with pytest.raises(ManagedWorkspaceFailure, match="target_changed"):
        engine.managed_workspace.refresh(workspace_id, operation_id="refresh.collision")
    assert target.read_bytes() == b"Owner collision content"
    assert _note(engine, added)["materialized_revision_id"] is None
    with pytest.raises(ManagedWorkspaceFailure, match="target_changed"):
        engine.managed_workspace.refresh(workspace_id, operation_id="refresh.collision")
    assert len(_revisions(engine, added)) == 1


def test_refresh_revalidates_canonical_snapshot_and_rolls_back_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, workspace_id, note_id, page = _refresh_fixture(tmp_path)
    original = page.read_bytes()
    _publish(engine, note_id, "second")
    prepare = engine.managed_workspace._prepare_refresh_page

    def mutate_after_snapshot(*args: object, **kwargs: object) -> str | None:
        child = prepare(*args, **kwargs)  # type: ignore[arg-type]
        canonical = next((engine.profile.root / "content/spaces").rglob(f"{note_id}.md"))
        canonical.write_bytes(canonical.read_bytes() + b"\nUnapproved upstream change\n")
        return child

    monkeypatch.setattr(engine.managed_workspace, "_prepare_refresh_page", mutate_after_snapshot)
    with pytest.raises(ManagedWorkspaceFailure, match="ineligible_source"):
        engine.managed_workspace.refresh(workspace_id, operation_id="refresh.snapshot")
    assert page.read_bytes() == original
    assert len(_revisions(engine, note_id)) == 1
    assert not _rows(
        engine, "SELECT * FROM managed_operations WHERE operation_id = ?", "refresh.snapshot"
    )


def test_existing_check_to_rename_external_editor_race_remains_a_documented_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import open_brain_engine.storage.filesystem as filesystem

    engine, workspace_id, note_id, page = _refresh_fixture(tmp_path)
    upstream = _publish(engine, note_id, "second")
    owner = page.read_bytes() + b"\nUncooperative editor after primitive preimage check\n"
    write_all = filesystem._write_all
    raced = False

    def edit_while_staging(descriptor: int, data: bytes) -> None:
        nonlocal raced
        write_all(descriptor, data)
        if data == upstream:
            page.write_bytes(owner)
            raced = True

    monkeypatch.setattr(filesystem, "_write_all", edit_while_staging)
    engine.managed_workspace.refresh(workspace_id, operation_id="refresh.known-race")
    assert raced
    # This explicitly records the existing primitive's limitation, not a safety proof:
    # an editor writing AFTER its digest check can still lose that intervening write.
    assert page.read_bytes() == upstream
    assert not engine.managed_workspace.open_conflicts(workspace_id)


@pytest.mark.parametrize("change", [
    "accepted_edit", "accepted_rename", "excluded", "folder_excluded", "inactive",
])
@pytest.mark.parametrize("resume", ["recover", "reopen", "replay"])
@pytest.mark.parametrize("parent_first", [False, True])
def test_superseded_refresh_settles_reopens_and_preserves_original_outcome(
    tmp_path: Path, change: str, resume: str, parent_first: bool,
) -> None:
    import json

    engine, workspace_id, note_id, page = _refresh_fixture(tmp_path)
    original = page.read_bytes()
    initial = dict(_note(engine, note_id))
    second = _publish(engine, note_id, "second")
    sibling = _capture_page(engine, "Unaffected sibling", "refresh.sibling")
    # All bound operations share created_at; IDs exercise both recovery orderings.
    request = "aaa.refresh.pending" if parent_first else "zzz.refresh.pending"
    engine._faults.add(ManagedWorkspaceFault.AFTER_OPERATION_PREPARED)
    with pytest.raises(InjectedFault):
        engine.managed_workspace.refresh(workspace_id, operation_id=request)
    engine._faults.clear()
    parent = dict(_rows(
        engine, "SELECT * FROM managed_operations WHERE operation_id = ?", request,
    )[0])
    child_ids = json.loads(parent["body_bytes"])["children"]
    assert len(child_ids) == 2
    ordered = _rows(
        engine, "SELECT operation_id FROM managed_operations WHERE status = 'prepared' "
        "ORDER BY created_at, operation_id",
    )
    assert (ordered[0][0] == request) is parent_first
    bound = {row["note_id"]: dict(row) for row in _rows(
        engine, "SELECT * FROM managed_operations "
        "WHERE status = 'prepared' AND note_id IS NOT NULL",
    )}
    owner = original
    accepted_edit = change in {"accepted_edit", "accepted_rename"}
    if accepted_edit:
        if change == "accepted_rename":
            renamed = tmp_path / "workspace/accepted-rename.md"
            page.rename(renamed)
            page = renamed
        owner += b"\nAccepted intervening edit\n"
        page.write_bytes(owner)
        observation = engine.managed_workspace.observe(workspace_id)
        engine.managed_workspace.accept_observed(
            workspace_id, note_id, generation=observation.generation,
            operation_id="owner.intervening-accept",
        )
    elif change == "inactive":
        engine.managed_workspace.deactivate(workspace_id, note_id, operation_id="owner.pause")
    else:
        engine.managed_policy.set_exclusion(
            workspace_id, "folder" if change == "folder_excluded" else "note",
            page.parent.relative_to(tmp_path / "workspace").as_posix()
            if change == "folder_excluded" else note_id,
            excluded=True, operation_id="owner.exclude",
        )
    protected_note = dict(_note(engine, note_id))
    before_stat = page.stat()
    if resume == "recover":
        assert engine.managed_workspace.recover() > 0
    elif resume == "reopen":
        engine = _engine(engine.profile.root)
    else:
        with pytest.raises(ManagedWorkspaceFailure, match="^stale_request$"):
            engine.managed_workspace.refresh(workspace_id, operation_id=request)
    # Normal startup must succeed even when explicit recovery/replay ran first.
    engine = _engine(engine.profile.root)
    assert engine.managed_workspace.recover() == 0
    assert not _rows(
        engine, "SELECT * FROM managed_operations "
        "WHERE status IN ('prepared', 'writing', 'promoted')",
    )
    settled = {row["operation_id"]: dict(row) for row in _rows(
        engine, "SELECT * FROM managed_operations",
    )}
    assert settled[request]["status"] == "cancelled"
    assert settled[request]["body_bytes"] == parent["body_bytes"]
    for child_id in [request, *child_ids]:
        assert settled[child_id]["stage"] == 3
        assert settled[child_id]["completed_at"] is not None
    assert settled[bound[note_id]["operation_id"]]["status"] == "cancelled"
    # Folder exclusion also covers the sibling; other cases must finish its write.
    sibling_cancelled = change == "folder_excluded"
    assert settled[bound[sibling]["operation_id"]]["status"] == (
        "cancelled" if sibling_cancelled else "completed"
    )
    if not sibling_cancelled:
        sibling_page = _managed_page(tmp_path / "workspace", sibling)
        assert sibling_page.read_bytes() == bound[sibling]["body_bytes"]
        sibling_stat = sibling_page.stat()
    assert dict(_note(engine, note_id)) == protected_note
    assert page.read_bytes() == owner
    assert page.stat().st_mtime_ns == before_stat.st_mtime_ns
    assert page.stat().st_ino == before_stat.st_ino
    revisions = _revisions(engine, note_id)
    assert len(revisions) == (3 if accepted_edit else 2)
    assert revisions[0]["body_bytes"] == original
    assert revisions[1]["body_bytes"] == second
    assert revisions[1]["parent_revision_id"] == initial["accepted_revision_id"]
    assert engine.managed_workspace.open_conflicts(workspace_id) == ()

    third = _publish(engine, note_id, "third")
    if not accepted_edit:
        # A fresh request while disabled skips this note without a pending blocker.
        engine.managed_workspace.refresh(workspace_id, operation_id="refresh.still-disabled")
        assert len(_revisions(engine, note_id)) == len(revisions)
        if change == "inactive":
            engine.managed_workspace.restore(workspace_id, note_id, operation_id="owner.resume")
        else:
            engine.managed_policy.set_exclusion(
                workspace_id, "folder" if change == "folder_excluded" else "note",
                page.parent.relative_to(tmp_path / "workspace").as_posix()
                if change == "folder_excluded" else note_id,
                excluded=False, operation_id="owner.unexclude",
            )
    # Restoring eligibility and publishing again cannot revive the old authorization.
    for _ in range(2):
        engine = _engine(engine.profile.root)
        with pytest.raises(ManagedWorkspaceFailure, match="^stale_request$"):
            engine.managed_workspace.refresh(workspace_id, operation_id=request)
        assert page.read_bytes() == owner
        assert len(_revisions(engine, note_id)) == len(revisions)
        for operation_id in [request, *child_ids]:
            assert dict(_rows(
                engine, "SELECT * FROM managed_operations WHERE operation_id = ?", operation_id,
            )[0]) == settled[operation_id]
    if not sibling_cancelled:
        assert sibling_page.stat().st_mtime_ns == sibling_stat.st_mtime_ns
        assert sibling_page.stat().st_ino == sibling_stat.st_ino
    assert len(_revisions(engine, sibling)) == 1
    # A new request binds the third publication but preserves the owner/preimage divergence.
    with pytest.raises(ManagedWorkspaceFailure, match="^target_changed$"):
        engine.managed_workspace.refresh(workspace_id, operation_id="refresh.fresh")
    assert page.read_bytes() == owner
    conflict = engine.managed_workspace.review_conflict(workspace_id, note_id)
    assert conflict.accepted_body.encode() == third
    engine.managed_workspace.resolve_conflict(
        workspace_id, note_id, "accepted", operation_id="owner.resolve-fresh",
    )
    engine.managed_workspace.materialize(workspace_id, note_id, operation_id="owner.write-fresh")
    assert page.read_bytes() == third
    assert len(_revisions(engine, note_id)) == len(revisions) + 1
    engine = _engine(engine.profile.root)
    with pytest.raises(ManagedWorkspaceFailure, match="^stale_request$"):
        engine.managed_workspace.refresh(workspace_id, operation_id=request)
    assert page.read_bytes() == third
    assert not _rows(
        engine, "SELECT * FROM managed_operations "
        "WHERE status IN ('prepared', 'writing', 'promoted')",
    )


def test_refresh_conflict_does_not_prevent_other_bound_children_from_completing(
    tmp_path: Path,
) -> None:
    engine, workspace_id, note_id, page = _refresh_fixture(tmp_path)
    owner = page.read_bytes() + b"\nOwner edit\n"
    page.write_bytes(owner)
    upstream = _publish(engine, note_id, "second")
    added = _capture_page(engine, "Additional page", "refresh.additional")
    with pytest.raises(ManagedWorkspaceFailure, match="target_changed"):
        engine.managed_workspace.refresh(workspace_id, operation_id="refresh.batch")
    assert _managed_page(tmp_path / "workspace", added).is_file()
    assert page.read_bytes() == owner
    engine = _engine(engine.profile.root)
    with pytest.raises(ManagedWorkspaceFailure, match="target_changed"):
        engine.managed_workspace.refresh(workspace_id, operation_id="refresh.batch")
    engine.managed_workspace.resolve_conflict(
        workspace_id, note_id, "accepted", operation_id="owner.batch.resolve",
    )
    assert page.read_bytes() == owner
    engine.managed_workspace.materialize(
        workspace_id, note_id, operation_id="owner.batch.materialize",
    )
    assert page.read_bytes() == upstream
    with pytest.raises(ManagedWorkspaceFailure, match="target_changed"):
        engine.managed_workspace.refresh(workspace_id, operation_id="refresh.batch")
    assert len(_revisions(engine, note_id)) == 2
    assert len(_revisions(engine, added)) == 1


@pytest.mark.parametrize("corrupt", [
    "child_caller", "parent_caller", "child_binding", "manifest", "body", "digest",
    "preimage", "revision", "path", "revision_body", "revision_operation", "acceptance",
    "accepted_preimage", "accepted_path_binding",
])
def test_superseded_refresh_does_not_disguise_journal_corruption(
    tmp_path: Path, corrupt: str,
) -> None:
    engine, workspace_id, note_id, page = _refresh_fixture(tmp_path)
    initial = _note(engine, note_id)
    original = page.read_bytes()
    _publish(engine, note_id, "second")
    engine._faults.add(ManagedWorkspaceFault.AFTER_OPERATION_PREPARED)
    request = "aaa.refresh.corrupt"
    with pytest.raises(InjectedFault):
        engine.managed_workspace.refresh(workspace_id, operation_id=request)
    engine._faults.clear()
    child = _rows(
        engine, "SELECT * FROM managed_operations WHERE note_id = ? AND status = 'prepared'",
        note_id,
    )[0]
    if corrupt in {"acceptance", "accepted_preimage", "accepted_path_binding"}:
        original += b"\nAccepted edit with subsequently corrupted journal\n"
        page.write_bytes(original)
        observed = engine.managed_workspace.observe(workspace_id)
        engine.managed_workspace.accept_observed(
            workspace_id, note_id, generation=observed.generation, operation_id="owner.accept",
        )
    engine.managed_policy.set_exclusion(
        workspace_id, "note", note_id, excluded=True, operation_id="owner.exclude",
    )
    # Deliberate corruption fixtures only; lifecycle tests above use owner APIs exclusively.
    with engine._store.transaction() as connection:
        if corrupt in {"child_caller", "parent_caller", "acceptance"}:
            operation_id = (
                request if corrupt == "parent_caller" else
                "owner.accept" if corrupt == "acceptance" else child["operation_id"]
            )
            connection.execute(
                "UPDATE managed_operations SET caller_actor_id = 'other-actor' "
                "WHERE operation_id = ?", (operation_id,),
            )
        elif corrupt in {"manifest", "body"}:
            connection.execute(
                "UPDATE managed_operations SET body_bytes = ? WHERE operation_id = ?",
                (b"corrupted", request if corrupt == "manifest" else child["operation_id"]),
            )
        elif corrupt in {"child_binding", "digest", "preimage", "accepted_preimage"}:
            column = {
                "child_binding": "request_sha256", "digest": "body_sha256",
                "preimage": "expected_target_sha256",
                "accepted_preimage": "expected_target_sha256",
            }[corrupt]
            connection.execute(
                f"UPDATE managed_operations SET {column} = ? WHERE operation_id = ?",
                ("0" * 64, child["operation_id"]),
            )
        elif corrupt == "accepted_path_binding":
            connection.execute(
                "UPDATE managed_operations SET target_relative_path = 'elsewhere.md' "
                "WHERE operation_id = ?", (child["operation_id"],),
            )
        elif corrupt == "revision":
            connection.execute(
                "UPDATE managed_notes SET accepted_revision_id = ? WHERE note_id = ?",
                (initial["accepted_revision_id"], note_id),
            )
        elif corrupt == "path":
            connection.execute(
                "UPDATE managed_notes SET relative_path = 'elsewhere.md' WHERE note_id = ?",
                (note_id,),
            )
        elif corrupt == "revision_body":
            connection.execute(
                "UPDATE managed_note_revisions SET body_bytes = ? WHERE revision_id = ?",
                (b"corrupted", child["expected_revision_id"]),
            )
        else:
            connection.execute(
                "UPDATE managed_note_revisions SET operation_id = ? WHERE revision_id = ?",
                (request, child["expected_revision_id"]),
            )
    for resume in (engine.managed_workspace.recover, lambda: _engine(engine.profile.root)):
        with pytest.raises(ManagedWorkspaceFailure, match="^operation_replay_mismatch$"):
            resume()
    assert page.read_bytes() == original
    for operation_id in (request, child["operation_id"]):
        row = _rows(
            engine, "SELECT * FROM managed_operations WHERE operation_id = ?", operation_id,
        )[0]
        assert row["status"] == "prepared"
        assert row["completed_at"] is None
    assert engine.managed_workspace.open_conflicts(workspace_id) == ()


def test_cancelled_refresh_settles_conflicted_and_valid_siblings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from open_brain_engine.engine.managed_workspace import _CanonicalPage

    engine, workspace_id, note_id, page = _refresh_fixture(tmp_path)
    conflicted = _capture_page(engine, "Conflict sibling", "refresh.conflicted")
    engine.managed_workspace.refresh(workspace_id, operation_id="refresh.add-sibling")
    conflict_page = _managed_page(tmp_path / "workspace", conflicted)
    owner = conflict_page.read_bytes() + b"\nOwner conflict\n"
    conflict_page.write_bytes(owner)
    _publish(engine, conflicted, "conflict-second")
    _publish(engine, note_id, "second")
    added = _capture_page(engine, "Valid sibling", "refresh.valid")
    original = page.read_bytes()
    canonical_pages = engine.managed_workspace._canonical_pages

    def stale_first(
        connection: sqlite3.Connection | None = None, *, require_publication: bool = False,
    ) -> tuple[_CanonicalPage, ...]:
        return tuple(sorted(
            canonical_pages(connection, require_publication=require_publication),
            key=lambda candidate: candidate.note_id != note_id,
        ))

    monkeypatch.setattr(engine.managed_workspace, "_canonical_pages", stale_first)
    engine._faults.add(ManagedWorkspaceFault.AFTER_OPERATION_PREPARED)
    with pytest.raises(InjectedFault):
        engine.managed_workspace.refresh(workspace_id, operation_id="refresh.mixed")
    engine._faults.clear()
    engine.managed_workspace.deactivate(workspace_id, note_id, operation_id="owner.pause")
    with pytest.raises(ManagedWorkspaceFailure, match="^stale_request$"):
        engine.managed_workspace.refresh(workspace_id, operation_id="refresh.mixed")
    assert page.read_bytes() == original
    assert conflict_page.read_bytes() == owner
    assert _managed_page(tmp_path / "workspace", added).is_file()
    conflict = engine.managed_workspace.review_conflict(workspace_id, conflicted)
    assert conflict.workspace_body.encode() == owner
    engine = _engine(engine.profile.root)
    engine.managed_workspace.resolve_conflict(
        workspace_id, conflicted, "workspace", operation_id="owner.resolve-sibling",
    )
    with pytest.raises(ManagedWorkspaceFailure, match="^stale_request$"):
        engine.managed_workspace.refresh(workspace_id, operation_id="refresh.mixed")
    assert engine.managed_workspace.recover() == 0
    assert len(_revisions(engine, added)) == 1


def test_refreshed_and_resolved_revisions_remain_portable_and_import_fails_closed(
    tmp_path: Path,
) -> None:
    from open_brain_engine.portable import validate_portable_root

    engine, workspace_id, note_id, page = _refresh_fixture(tmp_path)
    _publish(engine, note_id, "second")
    engine.managed_workspace.refresh(workspace_id, operation_id="refresh.portable")
    owner = page.read_bytes() + b"\nPortable owner edit\n"
    page.write_bytes(owner)
    observed = engine.managed_workspace.observe(workspace_id)
    engine.managed_workspace.accept_observed(
        workspace_id, note_id, generation=observed.generation, operation_id="owner.portable",
    )
    _publish(engine, note_id, "third")
    with pytest.raises(ManagedWorkspaceFailure, match="target_changed"):
        engine.managed_workspace.refresh(workspace_id, operation_id="refresh.portable-diverged")
    engine.managed_workspace.resolve_conflict(
        workspace_id, note_id, "workspace", operation_id="owner.portable-resolve",
    )
    exported = tmp_path / "exported"
    engine.portability.export(
        exported, export_id="export_00000000-0000-4000-8000-000000000555",
    )
    assert validate_portable_root(exported)["schema_version"] == 3
    imported_root = tmp_path / "imported"
    engine.portability.import_clean(
        exported, imported_root, import_id="import_00000000-0000-4000-8000-000000000555",
    )
    imported = _engine(imported_root)
    vault = tmp_path / "imported-vault"
    vault.mkdir(mode=0o700)
    imported.managed_workspace.setup(str(vault), operation_id="imported.setup")
    imported_page = _managed_page(vault, note_id)
    assert imported_page.read_bytes() == owner
    with pytest.raises(ManagedWorkspaceFailure, match="ineligible_source"):
        imported.managed_workspace.refresh(workspace_id, operation_id="imported.refresh")
    assert imported_page.read_bytes() == owner


_LIFECYCLE_CELLS = [
    (change, fault)
    for change in ("materialize", "identical", "rename", "accepted_link")
    for fault in (
        ManagedWorkspaceFault.AFTER_OPERATION_PREPARED,
        ManagedWorkspaceFault.AFTER_TARGET_WRITE,
        ManagedWorkspaceFault.AFTER_OPERATION_PROMOTED,
    )
    if not (change == "identical" and fault == ManagedWorkspaceFault.AFTER_OPERATION_PREPARED)
]


@pytest.mark.parametrize(("change", "fault"), _LIFECYCLE_CELLS)
@pytest.mark.parametrize("parent_first", [False, True])
@pytest.mark.parametrize("resume", ["reopen", "recover"])
def test_durable_transition_settlement_matrix(
    tmp_path: Path,
    change: str,
    fault: ManagedWorkspaceFault,
    parent_first: bool,
    resume: str,
) -> None:
    import json

    engine, workspace, note, page = _refresh_fixture(tmp_path)
    target = _capture_page(engine, "Target link evidence", "lifecycle.target")
    engine.managed_workspace.refresh(workspace, operation_id="lifecycle.target-refresh")
    second = _publish(engine, note, "second")
    sibling = _capture_page(engine, "Unaffected lifecycle sibling", "lifecycle.sibling")
    request = "aaa.lifecycle" if parent_first else "zzz.lifecycle"
    # Inject only at this note's leaf, regardless of canonical UUID ordering.
    original_fault = engine._fault

    def fault_at_note(point: CaptureFault | PortabilityFault | ManagedWorkspaceFault) -> None:
        if point == fault:
            rows = _rows(
                engine,
                "SELECT status FROM managed_operations WHERE note_id=? "
                "AND status IN ('prepared','writing','promoted')",
                note,
            )
            expected_status = {
                ManagedWorkspaceFault.AFTER_OPERATION_PREPARED: "prepared",
                ManagedWorkspaceFault.AFTER_TARGET_WRITE: "writing",
                ManagedWorkspaceFault.AFTER_OPERATION_PROMOTED: "promoted",
            }[fault]
            if rows and rows[0][0] == expected_status:
                raise InjectedFault(point)
        original_fault(point)

    engine._fault = fault_at_note  # type: ignore[method-assign]
    with pytest.raises(InjectedFault):
        engine.managed_workspace.refresh(workspace, operation_id=request)
    engine._fault = original_fault  # type: ignore[method-assign]
    parent = dict(
        _rows(engine, "SELECT * FROM managed_operations WHERE operation_id=?", request)[0]
    )
    children = json.loads(parent["body_bytes"])["children"]
    pending = _rows(
        engine,
        "SELECT operation_id FROM managed_operations "
        "WHERE status IN ('prepared','writing','promoted') ORDER BY created_at, operation_id",
    )
    assert (pending[0][0] == request) is parent_first
    child = dict(
        _rows(
            engine,
            "SELECT * FROM managed_operations WHERE note_id=? "
            "AND status IN ('prepared','writing','promoted')",
            note,
        )[0]
    )
    if change == "materialize":
        engine.managed_workspace.materialize(workspace, note, operation_id="lifecycle.explicit")
        assert page.read_bytes() == second
    else:
        if change == "rename":
            moved = tmp_path / "workspace/observed-move.md"
            page.rename(moved)
            page = moved
        elif change == "accepted_link":
            page.write_bytes(page.read_bytes() + b"\nAccepted owner edit\n")
        observed = engine.managed_workspace.observe(workspace)
        if change != "rename":
            engine.managed_workspace.accept_observed(
                workspace, note, generation=observed.generation, operation_id="lifecycle.accept"
            )
        if change == "accepted_link":
            engine.managed_policy.grant_consent(
                workspace,
                ManagedProvider.OPENAI_API,
                ManagedAccessMode.API_KEY,
                operation_id="lifecycle.consent",
            )
            rid = "request_00000000-0000-4000-8000-000000000958"
            engine.managed_inference.prepare(
                workspace,
                ManagedProvider.OPENAI_API,
                ManagedAccessMode.API_KEY,
                "openai_api:synthetic-v1",
                (note, target),
                request_id=rid,
                max_output_bytes=4096,
                timeout_seconds=30,
            )
            engine.managed_inference.release(rid)
            suggestion = engine.managed_inference.record_suggestion(
                rid,
                source_note_id=note,
                target_note_id=target,
                source_quote="Accepted owner edit",
                target_quote="Target link evidence",
                model="synthetic-v1",
            )
            engine.managed_inference.accept_suggestion(
                workspace, suggestion.suggestion_id, operation_id="lifecycle.link"
            )
    protected = page.read_bytes()
    stat = page.stat()
    revisions = [dict(row) for row in _revisions(engine, note)]
    if resume == "recover":
        engine.managed_workspace.recover()
    engine = _engine(engine.profile.root)
    assert engine.managed_workspace.recover() == 0
    assert not _rows(
        engine, "SELECT 1 FROM managed_operations WHERE status IN ('prepared','writing','promoted')"
    )
    completed = change in {"materialize", "identical"}
    expected = "completed" if completed else "cancelled"
    assert (
        _rows(engine, "SELECT status FROM managed_operations WHERE operation_id=?", request)[0][0]
        == expected
    )
    assert (
        _rows(
            engine,
            "SELECT status FROM managed_operations WHERE operation_id=?",
            child["operation_id"],
        )[0][0]
        == expected
    )
    sibling_page = _managed_page(tmp_path / "workspace", sibling)
    sibling_stat = sibling_page.stat()
    frozen = {
        rid: dict(_rows(engine, "SELECT * FROM managed_operations WHERE operation_id=?", rid)[0])
        for rid in [request, *children]
    }
    for _ in range(2):
        engine = _engine(engine.profile.root)
        if completed:
            assert engine.managed_workspace.refresh(workspace, operation_id=request).duplicate
        else:
            with pytest.raises(ManagedWorkspaceFailure, match="^stale_request$"):
                engine.managed_workspace.refresh(workspace, operation_id=request)
        assert page.read_bytes() == protected
        assert (page.stat().st_ino, page.stat().st_mtime_ns) == (stat.st_ino, stat.st_mtime_ns)
        assert [dict(row) for row in _revisions(engine, note)] == revisions
        assert {
            rid: dict(
                _rows(engine, "SELECT * FROM managed_operations WHERE operation_id=?", rid)[0]
            )
            for rid in frozen
        } == frozen
    assert sibling_page.stat().st_mtime_ns == sibling_stat.st_mtime_ns
    assert len(_revisions(engine, sibling)) == 1
    engine.managed_workspace.refresh(workspace, operation_id="lifecycle.fresh")
    assert page.read_bytes() == protected
    assert len(_revisions(engine, note)) == len(revisions)
    assert frozen[request]["body_bytes"] == parent["body_bytes"]
    # Fresh authority must also handle an actual new publication, not only a no-op.
    third = _publish(engine, note, "third")
    if completed:
        engine.managed_workspace.refresh(workspace, operation_id="lifecycle.new-publication")
    else:
        with pytest.raises(ManagedWorkspaceFailure, match="^target_changed$"):
            engine.managed_workspace.refresh(workspace, operation_id="lifecycle.new-publication")
        assert page.read_bytes() == protected
        engine.managed_workspace.resolve_conflict(
            workspace,
            note,
            "accepted",
            operation_id="lifecycle.resolve-new",
        )
        engine.managed_workspace.materialize(workspace, note, operation_id="lifecycle.write-new")
    assert page.read_bytes() == third
    assert len(_revisions(engine, note)) == len(revisions) + 1
    engine = _engine(engine.profile.root)
    if completed:
        assert engine.managed_workspace.refresh(workspace, operation_id=request).duplicate
    else:
        with pytest.raises(ManagedWorkspaceFailure, match="^stale_request$"):
            engine.managed_workspace.refresh(workspace, operation_id=request)
    assert page.read_bytes() == third
    assert engine.managed_workspace.recover() == 0


@pytest.mark.parametrize(
    "fault",
    [ManagedWorkspaceFault.AFTER_TARGET_WRITE, ManagedWorkspaceFault.AFTER_OPERATION_PROMOTED],
)
def test_standalone_materialization_same_revision_acceptance(
    tmp_path: Path, fault: ManagedWorkspaceFault
) -> None:
    engine, workspace, note, page = _refresh_fixture(tmp_path)
    _publish(engine, note, "second")
    engine._faults.add(ManagedWorkspaceFault.AFTER_OPERATION_PREPARED)
    with pytest.raises(InjectedFault):
        engine.managed_workspace.refresh(workspace, operation_id="standalone.parent")
    engine._faults.add(fault)
    with pytest.raises(InjectedFault):
        engine.managed_workspace.materialize(workspace, note, operation_id="standalone.write")
    observed = engine.managed_workspace.observe(workspace)
    engine.managed_workspace.accept_observed(
        workspace, note, generation=observed.generation, operation_id="standalone.accept"
    )
    count = len(_revisions(engine, note))
    stat = page.stat()
    for _ in range(2):
        engine = _engine(engine.profile.root)
        assert engine.managed_workspace.materialize(
            workspace, note, operation_id="standalone.write"
        ).duplicate
        assert engine.managed_workspace.recover() == 0
        assert len(_revisions(engine, note)) == count
        assert page.stat().st_mtime_ns == stat.st_mtime_ns


@pytest.mark.parametrize("later", ["observe_missing", "exclude", "accept_sibling"])
def test_observed_move_settlement_survives_observation_replacement(
    tmp_path: Path, later: str
) -> None:
    engine, workspace, note, page = _refresh_fixture(tmp_path)
    sibling = _capture_page(engine, "Observed sibling", "move.sibling")
    engine.managed_workspace.refresh(workspace, operation_id="move.setup-sibling")
    _publish(engine, note, "second")
    engine._faults.add(ManagedWorkspaceFault.AFTER_OPERATION_PREPARED)
    with pytest.raises(InjectedFault):
        engine.managed_workspace.refresh(workspace, operation_id="move.pending")
    moved = tmp_path / "workspace/moved.md"
    page.rename(moved)
    observed = engine.managed_workspace.observe(workspace)
    protected = moved.read_bytes()
    if later == "observe_missing":
        outside = tmp_path / "removed.md"
        moved.rename(outside)
        moved = outside
        engine.managed_workspace.observe(workspace)
    elif later == "exclude":
        engine.managed_policy.set_exclusion(
            workspace, "note", note, excluded=True, operation_id="move.exclude"
        )
    else:
        engine.managed_workspace.accept_observed(
            workspace, sibling, generation=observed.generation, operation_id="move.accept-sibling"
        )
    engine = _engine(engine.profile.root)
    with pytest.raises(ManagedWorkspaceFailure, match="^stale_request$"):
        engine.managed_workspace.refresh(workspace, operation_id="move.pending")
    assert moved.read_bytes() == protected
    assert not page.exists()
    assert not _rows(
        engine, "SELECT 1 FROM managed_operations WHERE status IN ('prepared','writing','promoted')"
    )


def test_completed_intervening_write_does_not_revive_consumed_preimage(tmp_path: Path) -> None:
    engine, workspace, note, page = _refresh_fixture(tmp_path)
    original = page.read_bytes()
    _publish(engine, note, "second")
    engine._faults.add(ManagedWorkspaceFault.AFTER_OPERATION_PREPARED)
    with pytest.raises(InjectedFault):
        engine.managed_workspace.refresh(workspace, operation_id="consumed.pending")
    engine.managed_workspace.materialize(workspace, note, operation_id="consumed.explicit")
    page.write_bytes(original)  # The owner reverts after the requested bytes were written.
    stat = page.stat()
    engine = _engine(engine.profile.root)
    with pytest.raises(ManagedWorkspaceFailure, match="^stale_request$"):
        engine.managed_workspace.refresh(workspace, operation_id="consumed.pending")
    assert page.read_bytes() == original
    assert page.stat().st_mtime_ns == stat.st_mtime_ns
    assert engine.managed_workspace.recover() == 0


@pytest.mark.parametrize(
    "state", ["refreshed", "resolved", "accepted_edit", "rename", "excluded", "inactive"]
)
def test_settled_retained_history_portable_round_trip(tmp_path: Path, state: str) -> None:
    import json

    from open_brain_engine.portable import validate_portable_root

    engine, workspace, note, page = _refresh_fixture(tmp_path)
    _publish(engine, note, "second")
    if state in {"refreshed", "resolved"}:
        engine.managed_workspace.refresh(workspace, operation_id="portable.refresh")
        if state == "resolved":
            page.write_bytes(page.read_bytes() + b"\nPortable accepted owner edit\n")
            observed = engine.managed_workspace.observe(workspace)
            engine.managed_workspace.accept_observed(
                workspace, note, generation=observed.generation, operation_id="portable.accept"
            )
            _publish(engine, note, "third")
            with pytest.raises(ManagedWorkspaceFailure, match="^target_changed$"):
                engine.managed_workspace.refresh(workspace, operation_id="portable.conflict")
            engine.managed_workspace.resolve_conflict(
                workspace, note, "workspace", operation_id="portable.resolve"
            )
    else:
        engine._faults.add(ManagedWorkspaceFault.AFTER_OPERATION_PREPARED)
        with pytest.raises(InjectedFault):
            engine.managed_workspace.refresh(workspace, operation_id="portable.pending")
        if state == "accepted_edit":
            page.write_bytes(page.read_bytes() + b"\nPortable accepted edit\n")
            observed = engine.managed_workspace.observe(workspace)
            engine.managed_workspace.accept_observed(
                workspace, note, generation=observed.generation, operation_id="portable.accept"
            )
        elif state == "rename":
            moved = tmp_path / "workspace/portable-move.md"
            page.rename(moved)
            page = moved
            engine.managed_workspace.observe(workspace)
        elif state == "excluded":
            engine.managed_policy.set_exclusion(
                workspace, "note", note, excluded=True, operation_id="portable.exclude"
            )
        else:
            engine.managed_workspace.deactivate(workspace, note, operation_id="portable.deactivate")
    engine = _engine(engine.profile.root)
    before = _revisions(engine, note)
    head = _note(engine, note)["accepted_revision_id"]
    members = parse_markdown(before[1]["body_bytes"]).fields["provenance"]
    assert isinstance(members, list) and len(members) == 2
    exported = tmp_path / "exported"
    engine.portability.export(exported, export_id="export_00000000-0000-4000-8000-000000000959")
    assert validate_portable_root(exported)["schema_version"] == 3
    imported_root = tmp_path / "imported"
    engine.portability.import_clean(
        exported, imported_root, import_id="import_00000000-0000-4000-8000-000000000959"
    )
    imported = _engine(imported_root)
    after = _revisions(imported, note)
    assert len(after) == len(before)
    for old, new in zip(before, after, strict=True):
        for key in ("revision_id", "parent_revision_id", "kind", "body_bytes", "body_sha256"):
            assert new[key] == old[key]
        for key in ("privacy_json", "provenance_json"):
            assert json.loads(new[key]) == json.loads(old[key])
        assert (
            parse_markdown(new["body_bytes"]).fields["provenance"]
            == parse_markdown(old["body_bytes"]).fields["provenance"]
        )
    assert _note(imported, note)["accepted_revision_id"] == head
    assert bool(_note(imported, note)["active"]) is (state != "inactive")
    vault = tmp_path / "reattached"
    vault.mkdir(mode=0o700)
    imported.managed_workspace.setup(str(vault), operation_id="portable.attach")
    if state == "inactive":
        assert not list(vault.rglob("*.md"))
        imported.managed_workspace.restore(workspace, note, operation_id="portable.restore")
    else:
        assert _managed_page(vault, note).read_bytes() == next(
            row["body_bytes"] for row in before if row["revision_id"] == head
        )
    if state == "excluded":
        # Portable exclusions are retained sets; a local note toggle does not erase them.
        imported.managed_workspace.refresh(workspace, operation_id="portable.imported-refresh")
        assert len(_revisions(imported, note)) == len(before)
        exclusions = _rows(
            imported,
            "SELECT subject FROM managed_exclusions "
            "WHERE workspace_id=? AND kind='portable_set' AND active=1",
            workspace,
        )
        assert any(note in json.loads(row[0]) for row in exclusions)
    else:
        with pytest.raises(ManagedWorkspaceFailure, match="^ineligible_source$"):
            imported.managed_workspace.refresh(workspace, operation_id="portable.imported-refresh")


@pytest.mark.parametrize("corrupt", ["preimage", "absent_preimage", "path", "observed_path"])
def test_standalone_transition_does_not_disguise_corrupt_binding(
    tmp_path: Path, corrupt: str
) -> None:
    """Integrity negative only; supported transitions precede isolated journal corruption."""
    engine, workspace, note, page = _refresh_fixture(tmp_path)
    _publish(engine, note, "second")
    engine._faults.add(ManagedWorkspaceFault.AFTER_OPERATION_PREPARED)
    with pytest.raises(InjectedFault):
        engine.managed_workspace.refresh(workspace, operation_id="binding.refresh")
    engine._faults.add(ManagedWorkspaceFault.AFTER_OPERATION_PREPARED)
    with pytest.raises(InjectedFault):
        engine.managed_workspace.materialize(workspace, note, operation_id="binding.standalone")
    if corrupt != "observed_path":
        page.write_bytes(page.read_bytes() + b"\nAccepted owner binding edit\n")
    observed = engine.managed_workspace.observe(workspace)
    if corrupt != "observed_path":
        engine.managed_workspace.accept_observed(
            workspace, note, generation=observed.generation, operation_id="binding.accept"
        )
    protected = page.read_bytes()
    with engine._store.transaction() as connection:
        if corrupt in {"preimage", "absent_preimage"}:
            connection.execute(
                "UPDATE managed_operations SET expected_target_sha256=? "
                "WHERE operation_id='binding.standalone'",
                (None if corrupt == "absent_preimage" else "0" * 64,),
            )
        else:
            connection.execute(
                "UPDATE managed_operations SET target_relative_path='wrong.md' "
                "WHERE operation_id='binding.standalone'"
            )
    with pytest.raises(ManagedWorkspaceFailure, match="^operation_replay_mismatch$"):
        engine.managed_workspace.materialize(workspace, note, operation_id="binding.standalone")
    assert page.read_bytes() == protected
    assert (
        _rows(
            engine, "SELECT status FROM managed_operations WHERE operation_id='binding.standalone'"
        )[0][0]
        == "prepared"
    )


def test_standalone_initial_setup_absent_preimage_settles_after_same_head_acceptance(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path / "brain")
    note = _capture_page(engine, "Initial standalone evidence", "initial.capture")
    vault = tmp_path / "workspace"
    vault.mkdir(mode=0o700)
    engine._faults.add(ManagedWorkspaceFault.AFTER_OPERATION_PREPARED)
    with pytest.raises(InjectedFault):
        engine.managed_workspace.setup(str(vault), operation_id="initial.setup")
    status = engine.managed_workspace.status()
    assert status is not None
    workspace = status.workspace_id
    engine._faults.add(ManagedWorkspaceFault.AFTER_TARGET_WRITE)
    with pytest.raises(InjectedFault):
        engine.managed_workspace.materialize(workspace, note, operation_id="initial.standalone")
    observed = engine.managed_workspace.observe(workspace)
    assert engine.managed_workspace.accept_observed(
        workspace,
        note,
        generation=observed.generation,
        operation_id="initial.accept",
    ).duplicate
    page = _managed_page(vault, note)
    stat = page.stat()
    for _ in range(2):
        engine = _engine(engine.profile.root)
        assert engine.managed_workspace.materialize(
            workspace, note, operation_id="initial.standalone"
        ).duplicate
        assert engine.managed_workspace.recover() == 0
        assert page.stat().st_mtime_ns == stat.st_mtime_ns
        assert len(_revisions(engine, note)) == 1


def test_observation_integrity_failure_rolls_back_every_moved_note(tmp_path: Path) -> None:
    engine, workspace, first, first_path = _refresh_fixture(tmp_path)
    second = _capture_page(engine, "Atomic second note", "atomic.second")
    engine.managed_workspace.refresh(workspace, operation_id="atomic.add")
    second_path = _managed_page(tmp_path / "workspace", second)
    for note in (first, second):
        engine._faults.add(ManagedWorkspaceFault.AFTER_OPERATION_PREPARED)
        with pytest.raises(InjectedFault):
            engine.managed_workspace.materialize(workspace, note, operation_id=f"atomic.{note}")
    first_path.rename(tmp_path / "workspace/first.md")
    second_path.rename(tmp_path / "workspace/second.md")
    with engine._store.transaction() as connection:
        connection.execute(
            "UPDATE managed_operations SET body_sha256=? WHERE operation_id=?",
            ("0" * 64, f"atomic.{second}"),
        )
    queries = (
        "SELECT * FROM managed_notes ORDER BY note_id",
        "SELECT * FROM managed_note_observations ORDER BY note_id",
        "SELECT * FROM managed_workspaces",
        "SELECT * FROM managed_operations ORDER BY operation_id",
    )
    before = [list(map(tuple, _rows(engine, sql))) for sql in queries]
    with pytest.raises(ManagedWorkspaceFailure, match="^operation_replay_mismatch$"):
        engine.managed_workspace.observe(workspace)
    assert [list(map(tuple, _rows(engine, sql))) for sql in queries] == before


@pytest.mark.parametrize("boundary", ["before", "after_paths", "after_cancel", "after_commit"])
def test_observation_process_exit_is_atomic(tmp_path: Path, boundary: str) -> None:
    import json
    import subprocess
    import sys

    script = r"""
import contextlib, json, os, runpy, sys
from pathlib import Path
helpers = runpy.run_path(sys.argv[1])
root = Path(sys.argv[2])
boundary = sys.argv[3]
engine, workspace, note, page = helpers["_refresh_fixture"](root)
original = page.read_bytes()
engine._faults.add(helpers["ManagedWorkspaceFault"].AFTER_OPERATION_PREPARED)
try:
    engine.managed_workspace.materialize(workspace, note, operation_id="crash.pending")
except helpers["InjectedFault"]:
    pass
else:
    raise AssertionError("preparation did not interrupt")
moved = root / "workspace/moved.md"
page.rename(moved)
(root / "case.json").write_text(json.dumps({
    "workspace": workspace, "note": note, "old_path": str(page),
    "body_hex": original.hex(),
    "revision_count": len(helpers["_revisions"](engine, note)),
}))
original_transaction = engine._store.transaction
@contextlib.contextmanager
def transaction():
    if boundary == "before":
        os._exit(73)
    with original_transaction() as connection:
        def trace(sql):
            if boundary == "after_paths" and sql.startswith("UPDATE managed_workspaces"):
                os._exit(73)
            if boundary == "after_cancel" and sql == "COMMIT":
                cancelled = connection.execute(
                    "SELECT status FROM managed_operations WHERE operation_id='crash.pending'"
                ).fetchone()[0]
                # An exit after cancellation is only reached if cancellation belongs
                # to this transaction, rather than a later transaction.
                os._exit(73 if cancelled == "cancelled" else 74)
        connection.set_trace_callback(trace)
        yield connection
    if boundary == "after_commit":
        os._exit(73)
engine._store.transaction = transaction
engine.managed_workspace.observe(workspace)
raise AssertionError("exit boundary not reached")
"""
    run = subprocess.run(
        [sys.executable, "-c", script, __file__, str(tmp_path), boundary],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 73, run.stderr
    case = json.loads((tmp_path / "case.json").read_text())
    database = tmp_path / "brain/.open-brain/state/phase1.sqlite3"
    with sqlite3.connect(database) as connection:
        status = connection.execute(
            "SELECT status, stage, completed_at FROM managed_operations "
            "WHERE operation_id='crash.pending'"
        ).fetchone()
        path = connection.execute(
            "SELECT relative_path FROM managed_notes WHERE note_id=?", (case["note"],)
        ).fetchone()[0]
        generation = connection.execute(
            "SELECT observation_generation FROM managed_workspaces"
        ).fetchone()[0]
    committed = boundary == "after_commit"
    assert path == (
        "moved.md"
        if committed
        else Path(case["old_path"]).relative_to(tmp_path / "workspace").as_posix()
    )
    assert status[0:2] == (("cancelled", 3) if committed else ("prepared", 0))
    assert (status[2] is not None) == committed
    assert generation == int(committed)
    for _ in range(2):
        engine = _engine(tmp_path / "brain")
        with pytest.raises(
            ManagedWorkspaceFailure, match="^stale_request$" if committed else "^target_changed$"
        ):
            engine.managed_workspace.materialize(
                case["workspace"], case["note"], operation_id="crash.pending"
            )
        engine.managed_workspace.observe(case["workspace"])
        assert engine.managed_workspace.recover() == 0
        assert (tmp_path / "workspace/moved.md").read_bytes() == bytes.fromhex(case["body_hex"])
        assert not Path(case["old_path"]).exists()
        assert len(_revisions(engine, case["note"])) == case["revision_count"]


@pytest.mark.parametrize(
    "corrupt",
    [
        "missing",
        "digest",
        "descriptor",
        "version",
        "target",
        "preimage",
        "body",
        "created",
        "caller",
    ],
)
def test_new_write_authority_refuses_inconsistent_retained_fields(
    tmp_path: Path, corrupt: str
) -> None:
    engine, workspace, note, page = _refresh_fixture(tmp_path)
    protected = page.read_bytes()
    engine._faults.add(ManagedWorkspaceFault.AFTER_OPERATION_PREPARED)
    with pytest.raises(InjectedFault):
        engine.managed_workspace.materialize(workspace, note, operation_id="authority.pending")
    with engine._store.transaction() as connection:
        if corrupt == "missing":
            connection.execute(
                "DELETE FROM managed_write_authority WHERE operation_id='authority.pending'"
            )
        elif corrupt in {"digest", "descriptor", "version"}:
            connection.execute("PRAGMA ignore_check_constraints=ON")
            column, value = {
                "digest": ("descriptor_sha256", "0" * 64),
                "descriptor": ("descriptor_json", "{}"),
                "version": ("authority_version", 2),
            }[corrupt]
            connection.execute(
                f"UPDATE managed_write_authority SET {column}=? "
                "WHERE operation_id='authority.pending'",
                (value,),
            )
        else:
            column, value = {
                "target": ("target_relative_path", "changed.md"),
                "preimage": ("expected_target_sha256", None),
                "body": ("body_bytes", b"Changed journal payload"),
                "created": ("created_at", "2000-01-01T00:00:00Z"),
                "caller": ("request_sha256", "0" * 64),
            }[corrupt]
            connection.execute(
                f"UPDATE managed_operations SET {column}=? WHERE operation_id='authority.pending'",
                (value,),
            )
    for _ in range(2):
        with pytest.raises(ManagedWorkspaceFailure, match="^operation_replay_mismatch$"):
            engine.managed_workspace.materialize(workspace, note, operation_id="authority.pending")
        assert page.read_bytes() == protected
        assert not (tmp_path / "workspace/changed.md").exists()
        assert (
            _rows(
                engine,
                "SELECT status FROM managed_operations WHERE operation_id='authority.pending'",
            )[0][0]
            == "prepared"
        )


def test_new_write_descriptor_is_inserted_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _engine(tmp_path / "brain")
    _capture_page(engine, "Atomic authority", "authority.capture")
    vault = tmp_path / "workspace"
    vault.mkdir(mode=0o700)

    def refuse(*args: object) -> bytes:
        raise ManagedWorkspaceFailure("operation_replay_mismatch")

    with monkeypatch.context() as scoped:
        scoped.setattr(engine.managed_workspace, "_write_descriptor", refuse)
        with pytest.raises(ManagedWorkspaceFailure, match="^operation_replay_mismatch$"):
            engine.managed_workspace.setup(str(vault), operation_id="authority.setup")
    for table in (
        "managed_operations",
        "managed_write_authority",
        "managed_notes",
        "managed_note_revisions",
    ):
        assert not _rows(engine, f"SELECT * FROM {table}")
    assert not tuple(vault.rglob("*.md"))
    engine.managed_workspace.setup(str(vault), operation_id="authority.setup")
    row = _rows(engine, "SELECT authority_version, descriptor_json FROM managed_write_authority")[0]
    assert row[0] == 1
    assert '"expected_target_sha256":null' in row[1]


def test_atomic_path_swap_settles_all_leaves_without_writing_siblings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine import managed_workspace as workspace_module

    engine, workspace, first, first_path = _refresh_fixture(tmp_path)
    second = _capture_page(engine, "Swap second note", "swap.second")
    sibling = _capture_page(engine, "Swap sibling note", "swap.sibling")
    vault = tmp_path / "workspace"
    engine.managed_workspace.refresh(workspace, operation_id="swap.add")
    second_path = _managed_page(vault, second)
    sibling_path = _managed_page(vault, sibling)
    for note, version in ((first, "first"), (second, "second"), (sibling, "sibling")):
        _publish(engine, note, f"swap-{version}")
    engine._faults.add(ManagedWorkspaceFault.AFTER_OPERATION_PREPARED)
    with pytest.raises(InjectedFault):
        engine.managed_workspace.refresh(workspace, operation_id="swap.refresh")
    for note in (first, second):
        for index in range(2):
            engine._faults.add(ManagedWorkspaceFault.AFTER_OPERATION_PREPARED)
            with pytest.raises(InjectedFault):
                engine.managed_workspace.materialize(
                    workspace, note, operation_id=f"swap.{note}.{index}"
                )
    before = {
        str(path): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (first_path, second_path, sibling_path)
    }
    temporary = vault / "swap.tmp"
    first_path.rename(temporary)
    second_path.rename(first_path)
    temporary.rename(second_path)
    histories = {
        note: list(map(tuple, _revisions(engine, note))) for note in (first, second, sibling)
    }

    def refuse_write(*args: object, **kwargs: object) -> None:
        raise AssertionError("observation must not write any file")

    with monkeypatch.context() as scoped:
        scoped.setattr(workspace_module, "atomic_replace", refuse_write)
        scoped.setattr(workspace_module, "atomic_write_new", refuse_write)
        engine.managed_workspace.observe(workspace)
        engine.managed_workspace.observe(workspace)
    assert _note(engine, first)["relative_path"] == second_path.relative_to(vault).as_posix()
    assert _note(engine, second)["relative_path"] == first_path.relative_to(vault).as_posix()
    assert first_path.read_bytes() == before[str(second_path)][0]
    assert second_path.read_bytes() == before[str(first_path)][0]
    assert (sibling_path.read_bytes(), sibling_path.stat().st_mtime_ns) == before[str(sibling_path)]
    assert (
        _rows(engine, "SELECT status FROM managed_operations WHERE operation_id='swap.refresh'")[0][
            0
        ]
        == "prepared"
    )
    assert len(_rows(engine, "SELECT 1 FROM managed_operations WHERE status='cancelled'")) == 6
    for _ in range(2):
        engine = _engine(engine.profile.root)
        with pytest.raises(ManagedWorkspaceFailure, match="^stale_request$"):
            engine.managed_workspace.refresh(workspace, operation_id="swap.refresh")
        for note in (first, second):
            for index in range(2):
                with pytest.raises(ManagedWorkspaceFailure, match="^stale_request$"):
                    engine.managed_workspace.materialize(
                        workspace, note, operation_id=f"swap.{note}.{index}"
                    )
        assert first_path.read_bytes() == before[str(second_path)][0]
        assert second_path.read_bytes() == before[str(first_path)][0]
        assert sibling_path.read_bytes() != before[str(sibling_path)][0]
        assert {note: list(map(tuple, _revisions(engine, note))) for note in histories} == histories
        assert not _rows(
            engine,
            "SELECT 1 FROM managed_operations WHERE status IN ('prepared','writing','promoted')",
        )
    assert (
        engine.managed_workspace.materialize(workspace, first, operation_id="swap.fresh").status
        == "materialized"
    )
    assert b"Approved swap-first" in second_path.read_bytes()


@pytest.mark.parametrize("state", ["pending", "completed", "cancelled"])
def test_authority_is_required_on_original_id_replay(tmp_path: Path, state: str) -> None:
    engine, workspace, note, page = _refresh_fixture(tmp_path)
    if state != "completed":
        engine._faults.add(ManagedWorkspaceFault.AFTER_OPERATION_PREPARED)
        with pytest.raises(InjectedFault):
            engine.managed_workspace.materialize(workspace, note, operation_id="replay.authority")
    else:
        engine.managed_workspace.materialize(workspace, note, operation_id="replay.authority")
    if state == "cancelled":
        moved = tmp_path / "workspace/moved.md"
        page.rename(moved)
        page = moved
        engine.managed_workspace.observe(workspace)
    protected = page.read_bytes()
    with engine._store.transaction() as connection:
        connection.execute(
            "DELETE FROM managed_write_authority WHERE operation_id='replay.authority'"
        )
    with pytest.raises(ManagedWorkspaceFailure, match="^operation_replay_mismatch$"):
        engine.managed_workspace.materialize(workspace, note, operation_id="replay.authority")
    assert page.read_bytes() == protected


def test_observation_revalidates_notes_selected_before_writer_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, workspace, note, page = _refresh_fixture(tmp_path)
    known_notes = engine.managed_workspace._known_notes

    def concurrent_owner_action(workspace_id: str) -> dict[str, sqlite3.Row]:
        selected = known_notes(workspace_id)
        engine.managed_workspace.deactivate(workspace, note, operation_id="concurrent.deactivate")
        return selected

    monkeypatch.setattr(engine.managed_workspace, "_known_notes", concurrent_owner_action)
    original = page.read_bytes()
    with pytest.raises(ManagedWorkspaceFailure, match="^invalid_observation$"):
        engine.managed_workspace.observe(workspace)
    assert _note(engine, note)["active"] == 0
    assert not _rows(engine, "SELECT * FROM managed_note_observations")
    assert _rows(engine, "SELECT observation_generation FROM managed_workspaces")[0][0] == 0
    assert page.read_bytes() == original


@pytest.mark.parametrize("replace_observation", [False, True])
def test_legacy_marker_cancels_only_with_transaction_local_old_path(
    tmp_path: Path, replace_observation: bool
) -> None:
    engine, workspace, note, page = _refresh_fixture(tmp_path)
    engine._faults.add(ManagedWorkspaceFault.AFTER_OPERATION_PREPARED)
    with pytest.raises(InjectedFault):
        engine.managed_workspace.materialize(workspace, note, operation_id="legacy.pending")
    # Model migration's explicit marker; do not manufacture a version-1 descriptor.
    with engine._store.transaction() as connection:
        connection.execute(
            "UPDATE managed_write_authority SET authority_version=0, "
            "descriptor_json=NULL, descriptor_sha256=NULL WHERE operation_id='legacy.pending'"
        )
    moved = tmp_path / "workspace/moved.md"
    page.rename(moved)
    protected = moved.read_bytes()
    engine.managed_workspace.observe(workspace)
    if replace_observation:
        engine.managed_workspace.deactivate(workspace, note, operation_id="legacy.deactivate")
        engine.managed_workspace.restore(workspace, note, operation_id="legacy.restore")
    for _ in range(2):
        engine = _engine(engine.profile.root)
        with pytest.raises(ManagedWorkspaceFailure, match="^stale_request$"):
            engine.managed_workspace.materialize(workspace, note, operation_id="legacy.pending")
        assert moved.read_bytes() == protected
        assert not page.exists()
    assert tuple(
        _rows(
            engine,
            "SELECT authority_version, descriptor_json, descriptor_sha256 "
            "FROM managed_write_authority WHERE operation_id='legacy.pending'",
        )[0]
    ) == (0, None, None)

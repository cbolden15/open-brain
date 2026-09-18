from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import cast

import open_brain_engine.engine.local_schema as local_schema
import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.engine import (
    BrainEngine,
    CaptureAction,
    DecisionOutcome,
    ManagedAccessMode,
    ManagedProvider,
    ManagedWorkspaceFailure,
    ProposalDraft,
    TextPayload,
)
from open_brain_engine.engine.local_schema_catalog import LOCAL_MIGRATIONS
from open_brain_engine.portable import PortableValidationError, validate_portable_root
from open_brain_engine.portable.managed_v2 import validate_managed_workspace_record
from open_brain_engine.storage.markdown import parse_markdown, render_markdown

from open_brain.profile import compile_single_user_local


@pytest.fixture(autouse=True)
def historical_managed_portable_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retain the historical v2/v3 managed export and restore contract."""
    monkeypatch.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 6)
    monkeypatch.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:6])


def _engine(root: Path) -> BrainEngine:
    return BrainEngine.open(compile_single_user_local(root, starter_spaces=("Notes",)))


def _capture(engine: BrainEngine, body: str, delivery_id: str) -> str:
    engine.capture.accept(
        TextPayload(body),
        delivery_id=delivery_id,
        action=CaptureAction.CANONICAL_NOTE,
        space_id=engine.inbox.spaces()[0].space_id,
    )
    return engine.retrieval.search(body, record_type="canonical")[0].result_id


def _setup_two(engine: BrainEngine, workspace: Path) -> tuple[str, tuple[str, str]]:
    note_ids = (
        _capture(engine, "Alpha source connects to beta.", "managed.portable.alpha"),
        _capture(engine, "Beta target follows alpha.", "managed.portable.beta"),
    )
    workspace.mkdir(mode=0o700)
    receipt = engine.managed_workspace.setup(str(workspace), operation_id="managed.portable.setup")
    return receipt.workspace_id, note_ids


def _grant(engine: BrainEngine, workspace_id: str, operation_id: str) -> None:
    engine.managed_policy.grant_consent(
        workspace_id,
        ManagedProvider.OPENAI_API,
        ManagedAccessMode.API_KEY,
        operation_id=operation_id,
    )


def test_managed_workspace_v2_round_trip_is_path_free_detached_and_reattachable(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    workspace = tmp_path / "workspace"
    engine = _engine(root)
    workspace_id, note_ids = _setup_two(engine, workspace)
    source_path = next(workspace.rglob(f"{note_ids[0]}.md"))
    parsed = parse_markdown(source_path.read_bytes())
    source_path.write_text(
        render_markdown(fields=parsed.fields, body="Alpha owner edit connects to beta.\n"),
        encoding="utf-8",
    )
    observation = engine.managed_workspace.observe(workspace_id)
    engine.managed_workspace.accept_observed(
        workspace_id,
        note_ids[0],
        generation=observation.generation,
        operation_id="managed.portable.accept-edit",
    )
    _grant(engine, workspace_id, "managed.portable.first-consent")
    request_id = "request_00000000-0000-4000-8000-000000000201"
    engine.managed_inference.prepare(
        workspace_id,
        ManagedProvider.OPENAI_API,
        ManagedAccessMode.API_KEY,
        "openai_api:synthetic-v1",
        note_ids,
        request_id=request_id,
        max_output_bytes=4096,
        timeout_seconds=30,
    )
    engine.managed_inference.release(request_id)
    suggestion = engine.managed_inference.record_suggestion(
        request_id,
        source_note_id=note_ids[0],
        target_note_id=note_ids[1],
        source_quote="connects to beta",
        target_quote="follows alpha",
        model="synthetic-graph-v1",
    )
    engine.managed_inference.accept_suggestion(
        workspace_id,
        suggestion.suggestion_id,
        operation_id="managed.portable.accept-link",
    )
    engine.managed_policy.set_exclusion(
        workspace_id,
        "note",
        note_ids[1],
        excluded=True,
        operation_id="managed.portable.exclude",
    )
    engine.managed_workspace.deactivate(
        workspace_id,
        note_ids[1],
        operation_id="managed.portable.deactivate",
    )
    _grant(engine, workspace_id, "managed.portable.active-export-consent")

    exported = tmp_path / "exported"
    receipt = engine.portability.export(
        exported,
        export_id="export_00000000-0000-4000-8000-000000000202",
    )
    manifest = validate_portable_root(exported)
    managed_path = next(
        cast(str, entry["path"])
        for entry in cast(list[dict[str, object]], manifest["files"])
        if cast(str, entry["path"]).startswith("history/managed-workspace/")
    )
    payload = (exported / managed_path).read_bytes()
    record = cast(dict[str, object], json.loads(payload))

    assert receipt.status == "exported"
    assert manifest["schema_version"] == 2
    assert str(root).encode() not in payload
    assert str(workspace).encode() not in payload
    assert b"relative_path" not in payload
    assert b"descriptor_json" not in payload
    assert b"managed_write_authority" not in payload
    assert b"managed_recovery_decisions" not in payload
    assert all(
        budget["reserved_requests"] == 0
        for budget in _database_rows(
            root,
            "SELECT reserved_requests FROM managed_inference_budgets",
        )
    )
    assert any(
        cast(dict[str, object], consent)["active_at_export"]
        for consent in cast(list[object], record["consents"])
    )

    imported_root = tmp_path / "imported"
    imported_receipt = engine.portability.import_clean(
        exported,
        imported_root,
        import_id="import_00000000-0000-4000-8000-000000000203",
    )
    imported = _engine(imported_root)
    for table in ("managed_write_authority", "managed_recovery_decisions"):
        assert not _database_rows(imported_root, f"SELECT * FROM {table}")
    database = imported_root / ".open-brain/state/phase1.sqlite3"
    with sqlite3.connect(database) as connection:
        detached = connection.execute(
            "SELECT workspace_id, root_path, device, inode FROM managed_workspaces"
        ).fetchone()
        active_consents = connection.execute(
            "SELECT count(*) FROM managed_consents WHERE active = 1"
        ).fetchone()[0]
        budget = connection.execute(
            """SELECT used_requests, used_bytes, uncertain_requests, uncertain_bytes
            FROM managed_inference_budgets"""
        ).fetchone()
        link_count = connection.execute("SELECT count(*) FROM managed_links").fetchone()[0]
    assert imported_receipt.history_records == receipt.history_records
    assert detached == (workspace_id, None, None, None)
    assert active_consents == 0
    assert budget is not None and budget[0] == 1 and budget[1] > 0
    assert budget[2:] == (0, 0)
    assert link_count == 1
    with pytest.raises(ManagedWorkspaceFailure, match="unsafe_workspace"):
        imported.managed_workspace.observe(workspace_id)

    restored_workspace = tmp_path / "restored-workspace"
    restored_workspace.mkdir(mode=0o700)
    attached = imported.managed_workspace.setup(
        str(restored_workspace), operation_id="managed.portable.reattach"
    )
    restored_source = next(restored_workspace.rglob(f"{note_ids[0]}.md"))
    assert attached.workspace_id == workspace_id
    assert "Alpha owner edit" in restored_source.read_text(encoding="utf-8")
    assert f"[[{note_ids[1]}]]" in restored_source.read_text(encoding="utf-8")
    assert not tuple(restored_workspace.rglob(f"{note_ids[1]}.md"))
    authorities = _database_rows(imported_root, "SELECT * FROM managed_write_authority")
    assert len(authorities) == 1
    assert authorities[0]["authority_version"] == 1
    descriptor = json.loads(authorities[0]["descriptor_json"])
    assert descriptor["root_path"] == str(restored_workspace)
    assert descriptor["note_id"] == note_ids[0]


def test_managed_v2_rejects_unknown_typed_fields_and_unsettled_export(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    workspace = tmp_path / "workspace"
    engine = _engine(root)
    workspace_id, note_ids = _setup_two(engine, workspace)
    _grant(engine, workspace_id, "managed.portable.pending-consent")
    engine.managed_inference.prepare(
        workspace_id,
        ManagedProvider.OPENAI_API,
        ManagedAccessMode.API_KEY,
        "openai_api:synthetic-v1",
        note_ids,
        request_id="request_00000000-0000-4000-8000-000000000204",
        max_output_bytes=4096,
        timeout_seconds=30,
    )

    with pytest.raises(ValueError, match="settled"):
        engine.portability.export(
            tmp_path / "blocked",
            export_id="export_00000000-0000-4000-8000-000000000205",
        )

    engine.managed_inference.fail("request_00000000-0000-4000-8000-000000000204")
    exported = tmp_path / "exported"
    engine.portability.export(
        exported,
        export_id="export_00000000-0000-4000-8000-000000000206",
    )
    manifest = validate_portable_root(exported)
    path = next(
        cast(str, entry["path"])
        for entry in cast(list[dict[str, object]], manifest["files"])
        if cast(str, entry["path"]).startswith("history/managed-workspace/")
    )
    record = cast(dict[str, object], json.loads((exported / path).read_bytes()))
    record["host_path"] = str(workspace)

    with pytest.raises(PortableValidationError, match="fields"):
        validate_managed_workspace_record(
            portable_canonical_json_bytes(record),
            tenant_id=cast(str, record["tenant_id"]),
            page_ids=set(note_ids),
        )


def test_managed_workspace_and_review_bindings_round_trip_as_v3(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    workspace = tmp_path / "workspace"
    engine = _engine(root)
    source = engine.capture.accept(
        TextPayload("Managed Portable v3 review source"),
        delivery_id="managed.portable.v3.source",
        space_id=engine.inbox.spaces()[0].space_id,
    )
    proposal = engine.review.propose(
        (source.capture_id,),
        (ProposalDraft("Managed v3 page", "Managed Portable v3 review body"),),
        delivery_id="managed.portable.v3.propose",
    )[0]
    engine.review.decide(
        proposal.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="managed.portable.v3.approve",
        expected_review_digest=proposal.review_digest,
    )
    workspace_id, _ = _setup_two(engine, workspace)

    exported = tmp_path / "exported-v3"
    engine.portability.export(
        exported,
        export_id="export_00000000-0000-4000-8000-000000000207",
    )
    manifest = validate_portable_root(exported)
    imported_root = tmp_path / "imported-v3"
    engine.portability.import_clean(
        exported,
        imported_root,
        import_id="import_00000000-0000-4000-8000-000000000208",
    )
    imported = _engine(imported_root)
    for table in ("managed_write_authority", "managed_recovery_decisions"):
        assert not _database_rows(imported_root, f"SELECT * FROM {table}")

    assert manifest["schema_version"] == 3
    assert any(
        cast(str, entry["path"]).startswith("history/managed-workspace/")
        for entry in cast(list[dict[str, object]], manifest["files"])
    )
    assert imported.review.show(proposal.proposal_id).page_id == proposal.page_id
    assert (
        _database_rows(
            imported_root,
            "SELECT workspace_id FROM managed_workspaces WHERE root_path IS NULL",
        )[0]["workspace_id"]
        == workspace_id
    )


def _database_rows(root: Path, query: str) -> list[sqlite3.Row]:
    with sqlite3.connect(root / ".open-brain/state/phase1.sqlite3") as connection:
        connection.row_factory = sqlite3.Row
        return list(connection.execute(query))

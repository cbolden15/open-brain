from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.engine import ReferencePayload, TextPayload, local_schema, open_local_engine
from open_brain_engine.engine.local import BrainEngine
from open_brain_engine.engine.local_schema import open_local_database_read_only
from open_brain_engine.engine.local_schema_catalog import LOCAL_MIGRATIONS
from open_brain_engine.engine.source_intake import SourceRevisionSubmission
from open_brain_engine.engine.t03_contracts import (
    EffectiveAuthority,
    SourceRouteRequest,
    T03Error,
    response_to_wire,
)
from open_brain_engine.portable.v4 import SOURCE_METADATA_PATH, manifest_v4
from open_brain_engine.portable.v5 import V5_SIDECAR_PATHS
from open_brain_engine.portable.versioned import validated_portable_snapshot

from open_brain.profile import compile_single_user_local
from packages.app.tests.unit.engine.test_foundation_contracts import _public_submission


def test_source_route_cas_preserves_capture_and_exports_v5(tmp_path: Path) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    tasks = open_local_engine(profile)
    receipt = tasks.capture.accept(
        TextPayload("synthetic immutable source"), delivery_id="manual.one"
    )
    space = tasks.inbox.create_space("Synthetic", delivery_id="space.one")
    with open_local_database_read_only(profile) as connection:
        row = connection.execute(
            "SELECT source_id,source_path FROM source_revisions WHERE capture_id=?",
            (receipt.capture_id,),
        ).fetchone()
        source_id, path = row["source_id"], row["source_path"]
    before = (profile.root / path).read_bytes()
    request = SourceRouteRequest(
        source_id=source_id,
        expected_head=receipt.capture_id,
        expected_route_version=0,
        space_id=space.space_id,
        operation_id="operation_" + str(uuid4()),
    )
    authority = EffectiveAuthority("synthetic-owner", "session", frozenset(), None, owner=True)
    assert tasks.sources is not None
    result = response_to_wire("source.route", tasks.sources.route(request, authority=authority))
    assert result["route_version"] == 1
    assert (
        response_to_wire("source.route", tasks.sources.route(request, authority=authority))
        == result
    )
    with pytest.raises(T03Error, match="revision_changed"):
        tasks.sources.route(
            SourceRouteRequest(**dict(request.to_wire(), operation_id="operation_" + str(uuid4()))),
            authority=authority,
        )
    assert (profile.root / path).read_bytes() == before
    metadata = json.loads((profile.root / SOURCE_METADATA_PATH).read_bytes())
    assert metadata["sources"][0]["head_capture_id"] == receipt.capture_id
    export = tmp_path / "export"
    exported = tasks.portability.export(export, export_id="export_" + str(uuid4()))
    assert exported.schema_version == 5
    snapshot = validated_portable_snapshot(export)
    assert snapshot.manifest["schema_version"] == 5
    assert snapshot.files.keys() >= V5_SIDECAR_PATHS
    assert snapshot.files[path] == before
    imported = tmp_path / "imported"
    import_receipt = tasks.portability.import_clean(
        export, imported, import_id="import_" + str(uuid4())
    )
    assert import_receipt.schema_version == 5
    assert imported.is_dir()


def test_standalone_v4_import_refusal_uses_valid_v4_fixture(tmp_path: Path) -> None:
    tasks = open_local_engine(compile_single_user_local(tmp_path / "brain"))
    tasks.capture.accept(TextPayload("synthetic legacy fixture"), delivery_id="legacy.v4")
    current = tmp_path / "current-v5"
    tasks.portability.export(current, export_id="export_" + str(uuid4()))
    snapshot = validated_portable_snapshot(current)
    files = {
        relative: payload
        for relative, payload in snapshot.files.items()
        if relative != "portable-manifest.json" and relative not in V5_SIDECAR_PATHS
    }
    legacy = tmp_path / "legacy-v4"
    for relative, payload in files.items():
        destination = legacy / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
    manifest = manifest_v4(
        files,
        tenant_id=str(snapshot.manifest["tenant_id"]),
        export_id="export_" + str(uuid4()),
        created_at=str(snapshot.manifest["created_at"]),
    )
    (legacy / "portable-manifest.json").write_bytes(
        portable_canonical_json_bytes(manifest)
    )
    assert validated_portable_snapshot(legacy).manifest["schema_version"] == 4
    with pytest.raises(ValueError, match="Portable v4 import is not supported"):
        tasks.portability.import_clean(
            legacy, tmp_path / "refused", import_id="import_" + str(uuid4())
        )
    assert not (tmp_path / "refused").exists()


def test_explicit_revision_order_replay_history_route_and_control(tmp_path: Path) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    tasks = open_local_engine(profile)
    assert tasks.sources is not None
    original = _public_submission(tasks)
    namespace = dict(
        connector_name="synthetic", connection_id="one", resource_id="one", external_id="one"
    )

    def submit(key: str, sequence: int, head: str | None, text: str) -> SourceRevisionSubmission:
        capture = replace(original, payload=ReferencePayload(original.source_reference, text))
        return SourceRevisionSubmission(
            capture=capture,
            namespace=namespace,
            revision_key=key,
            canonical_sha256=capture.request_sha256(),
            expected_head=head,
            ordering={
                "kind": "monotonic",
                "provider_namespace": "synthetic",
                "epoch": "one",
                "sequence": sequence,
            },
            expected_control_epoch=0,
        )

    first_request = submit("a1", 1, None, "first")
    first = tasks.sources.submit_revision(first_request)
    assert first.source_id is not None and first.capture_id is not None
    space = tasks.inbox.create_space("Synthetic", delivery_id="space.one")
    authority = EffectiveAuthority("synthetic", "session", frozenset(), None, owner=True)
    tasks.sources.route(
        SourceRouteRequest(
            source_id=first.source_id,
            expected_head=first.capture_id,
            expected_route_version=0,
            space_id=space.space_id,
            operation_id="operation_" + str(uuid4()),
        ),
        authority=authority,
    )
    newest = tasks.sources.submit_revision(submit("a3", 3, first.capture_id, "newest"))
    late = tasks.sources.submit_revision(submit("a2", 2, newest.capture_id, "late"))
    assert newest.capture_id is not None and late.capture_id is not None
    assert late.outcome == "history_only"
    assert [item.capture_id for item in tasks.inbox.list()] == [newest.capture_id]
    assert tasks.sources.submit_revision(first_request) == first
    assert tasks.retrieval.fetch(first.capture_id) is None
    assert tasks.retrieval.fetch(late.capture_id) is None
    fetched = tasks.retrieval.fetch(newest.capture_id)
    assert fetched is not None and fetched.space_id == space.space_id
    with pytest.raises(T03Error, match="source_revision_conflict"):
        tasks.sources.submit_revision(submit("a3", 3, newest.capture_id, "different"))
    with open_local_database_read_only(profile) as connection:
        assert connection.execute("SELECT count(*) FROM source_quarantine").fetchone()[0] == 1
        assert (
            connection.execute("SELECT head_capture_id FROM logical_sources").fetchone()[0]
            == newest.capture_id
        )
    held = submit("a4", 4, newest.capture_id, "held")
    assert tasks.sources.fence_intake(expected_epoch=0, authority=authority) == 1
    with pytest.raises(T03Error, match="revision_changed"):
        tasks.sources.submit_revision(held)


@pytest.mark.parametrize(
    "fault_name", ["AFTER_CAPTURE_RESERVATION", "AFTER_SOURCE_WRITE", "AFTER_BLOB_WRITE"]
)
def test_fenced_reservation_enters_custody_without_new_source_writes_or_startup_poison(
    tmp_path: Path,
    fault_name: str,
) -> None:
    from open_brain_engine.engine import CaptureFault, FilePayload, InjectedFault

    profile = compile_single_user_local(tmp_path / "brain")
    tasks = open_local_engine(profile, faults={CaptureFault[fault_name]})
    assert tasks.sources is not None
    capture = _public_submission(tasks)
    if fault_name == "AFTER_BLOB_WRITE":
        capture = replace(capture, payload=FilePayload("synthetic.txt", "text/plain", b"synthetic"))
    request = SourceRevisionSubmission(
        capture=capture,
        namespace=dict(
            connector_name="synthetic", connection_id="one", resource_id="one", external_id="one"
        ),
        revision_key="one",
        canonical_sha256=capture.request_sha256(),
        expected_head=None,
        ordering={"kind": "unordered"},
        expected_control_epoch=0,
    )
    with pytest.raises(InjectedFault):
        tasks.sources.submit_revision(request)
    before = {
        p.relative_to(profile.root): p.read_bytes()
        for p in (profile.root / "sources").rglob("*")
        if p.is_file() and p.name != "logical-sources.json"
    }
    authority = EffectiveAuthority("synthetic", "session", frozenset(), None, owner=True)
    assert tasks.sources.fence_intake(expected_epoch=0, authority=authority) == 1
    after = {
        p.relative_to(profile.root): p.read_bytes()
        for p in (profile.root / "sources").rglob("*")
        if p.is_file() and p.name != "logical-sources.json"
    }
    assert after == before
    terminal = tasks.sources.submit_revision(replace(request, expected_control_epoch=1))
    assert terminal.outcome == "quarantined" and terminal.custody_id is not None
    reopened = open_local_engine(profile)
    assert reopened.inbox.list() == ()
    reopened.capture.accept(TextPayload("unrelated capture"), delivery_id="unrelated")
    with open_local_database_read_only(profile) as connection:
        assert connection.execute("SELECT count(*) FROM source_quarantine").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM captures WHERE stage<3").fetchone()[0] == 0
    exported = tmp_path / "export"
    reopened.portability.export(exported, export_id="export_" + str(uuid4()))
    validated_portable_snapshot(exported)


@pytest.mark.parametrize("schema", [5, 6])
def test_migrated_alias_adoption_and_automatic_publication_preserve_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    schema: int,
) -> None:
    from open_brain_engine.engine import CaptureAction

    profile = compile_single_user_local(tmp_path / "brain")
    with monkeypatch.context() as legacy:
        legacy.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", schema)
        legacy.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:schema])
        old = open_local_engine(profile)
        capture = _public_submission(old)
        receipt = old.capture.submit(capture)
        space = old.inbox.create_space("Synthetic", delivery_id="space")
        old.capture.accept(
            TextPayload("Automatic publication"),
            action=CaptureAction.CANONICAL_NOTE,
            delivery_id="automatic",
            space_id=space.space_id,
        )
    from open_brain_engine.engine import coordinate_local_migration

    coordinate_local_migration(profile)
    tasks = open_local_engine(profile)
    assert tasks.sources is not None
    adopted = tasks.sources.submit_revision(
        SourceRevisionSubmission(
            capture=capture,
            namespace=dict(
                connector_name="synthetic",
                connection_id="one",
                resource_id="one",
                external_id="one",
            ),
            revision_key="baseline",
            canonical_sha256=capture.request_sha256(),
            expected_head=receipt.capture_id,
            ordering={"kind": "unordered"},
            expected_control_epoch=0,
        )
    )
    assert adopted.capture_id == receipt.capture_id
    assert len(tasks.retrieval.search("Automatic publication", record_type="canonical")) == 1


@pytest.mark.parametrize("version", [1, 2, 3])
def test_schema_seven_imports_legacy_portable_without_changing_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: int
) -> None:
    from open_brain_engine.engine import DecisionOutcome, ProposalDraft

    from packages.app.tests.integration.engine.test_managed_portability import _setup_two

    with monkeypatch.context() as legacy:
        legacy.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 6)
        legacy.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:6])
        historical = BrainEngine.open(
            compile_single_user_local(tmp_path / "historical", starter_spaces=("Notes",))
        )
        if version == 2:
            _setup_two(historical, tmp_path / "vault")
        else:
            capture = historical.capture.accept(
                TextPayload("Synthetic legacy evidence"),
                delivery_id="legacy.one",
                space_id=historical.inbox.spaces()[0].space_id,
            )
            if version == 3:
                proposal = historical.review.propose(
                    (capture.capture_id,),
                    (ProposalDraft("Synthetic", "Synthetic publication"),),
                    delivery_id="legacy.proposal",
                )[0]
                historical.review.decide(
                    proposal.proposal_id,
                    DecisionOutcome.APPROVED,
                    delivery_id="legacy.approval",
                    expected_review_digest=proposal.review_digest,
                )
        export = tmp_path / "legacy-export"
        assert (
            historical.portability.export(export, export_id="export_" + str(uuid4())).schema_version
            == version
        )
    snapshot = validated_portable_snapshot(export)
    control = open_local_engine(compile_single_user_local(tmp_path / "control"))
    destination = tmp_path / "imported"
    import_id = "import_" + str(uuid4())
    assert (
        control.portability.import_clean(export, destination, import_id=import_id).schema_version
        == version
    )
    assert control.portability.import_clean(export, destination, import_id=import_id).duplicate
    assert validated_portable_snapshot(destination).files == snapshot.files
    imported = open_local_engine(compile_single_user_local(destination))
    with open_local_database_read_only(imported.profile) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
        assert connection.execute("SELECT count(*) FROM source_revisions").fetchone()[0] > 0


def test_schema_seven_owner_recovery_retains_current_writer_floor(tmp_path: Path) -> None:
    from open_brain_engine.engine.managed_recovery import (
        abandon_managed_write,
        inspect_managed_recovery,
    )

    from packages.app.tests.integration.engine.test_managed_recovery import _legacy_fixture

    engine, _, _, moved = _legacy_fixture(tmp_path)
    before = moved.read_bytes()
    entry = inspect_managed_recovery(engine.profile, operation_id="legacy.pending").entries[0]
    assert entry.preview_digest is not None
    receipt = abandon_managed_write(
        engine.profile,
        operation_id="legacy.pending",
        expected_digest=entry.preview_digest,
        request_id="source.history.owner.recovery",
        validate_before_write=engine._assert_root,
    )
    assert not receipt.schema_upgraded
    assert moved.read_bytes() == before
    reopened = open_local_engine(engine.profile)
    with open_local_database_read_only(reopened.profile) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
        assert tuple(connection.execute("SELECT * FROM runtime_compatibility").fetchone()) == (
            1,
            4,
            9,
        )

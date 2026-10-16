"""Portable12 proof uses exports from actual Slack intake and sharing lifecycle."""

from __future__ import annotations

import base64
import json
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.engine import open_local_engine
from open_brain_engine.engine.sharing_contracts import (
    SharingDecisionRequest,
    SharingInspectRequest,
    SlackSelectionCreateRequest,
)
from open_brain_engine.portable.slack_v12 import SELECTIONS_PATH
from open_brain_engine.portable.v1 import PortableValidationError
from open_brain_engine.portable.v7 import SHARING_APPROVALS_PATH, validate_sharing_authority
from open_brain_engine.portable.versioned import validated_portable_snapshot

from packages.app.tests.integration.engine.test_slack_sharing import Journey, _revoke
from packages.app.tests.integration.services.test_slack_decision_recall import (
    DECISION,
    PROPOSAL,
    _message,
)
from packages.app.tests.unit.engine.test_sharing_tasks import _managed_source


@pytest.fixture(autouse=True)
def frozen_schema_seventeen(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep Portable12 producers and restores on their frozen schema17 runtime."""
    from open_brain_engine.engine import local_schema, paging
    from open_brain_engine.engine.local_schema_catalog import LOCAL_MIGRATIONS

    monkeypatch.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 17)
    monkeypatch.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:17])
    monkeypatch.setattr(paging, "PHASE1_STATE_SCHEMA_VERSION", 17)


def _export(journey: Journey, path: Path) -> None:
    assert (
        journey.tasks.portability.export(path, export_id="export_" + str(uuid4())).schema_version
        == 12
    )


def _mixed(root: Path) -> tuple[Journey, list[Any]]:
    journey = Journey(root)
    journey.scan(
        [_message("1760000001.000001", DECISION + " 漢字"), _message("1760000000.000001", PROPOSAL)]
    )
    for source in journey.sources():
        journey.route(source)
    sources = journey.sources()
    sharing = journey.tasks.sharing
    assert sharing is not None
    selection = sharing.create_selection(
        SlackSelectionCreateRequest(
            operation_id="selection.portable",
            brain_id=journey.brain_id,
            issuer_epoch=journey.epoch,
            destination_space_id=journey.space,
            member_source_ids=tuple(source["source_id"] for source in sources),
        ),
        authority=journey.owner,
    )
    approvals = [journey.approve(source, selection["selection_id"])[3] for source in sources]
    extra = journey.approve(sources[0], selection["selection_id"])[3]
    _revoke(journey, extra)
    saved_tasks, request, owner, _ = _managed_source(root)
    preview = saved_tasks.sharing.preview(request, authority=owner)
    saved = saved_tasks.sharing.decide(
        SharingDecisionRequest(
            operation_id="saved.portable",
            preview_id=preview.preview_id,
            preview_sha256=preview.preview_sha256,
            brain_id=request.brain_id,
            issuer_epoch=request.issuer_epoch,
            destination_brain_id=request.brain_id,
            expected_decision_version=0,
            decision="approve",
        ),
        authority=owner,
    )
    return journey, [*approvals, extra, saved]


def _rehash(root: Path) -> None:
    manifest = json.loads((root / "portable-manifest.json").read_bytes())
    manifest["files"] = [
        {"path": path.relative_to(root).as_posix(), "sha256": sha256(path.read_bytes()).hexdigest()}
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "portable-manifest.json"
    ]
    (root / "portable-manifest.json").write_bytes(canonical(manifest))


def test_real_mixed_sharing_selection_restore_reopen_reexport_exact(tmp_path: Path) -> None:
    journey, approvals = _mixed(tmp_path)
    exported, restored, again = tmp_path / "exported", tmp_path / "restored", tmp_path / "again"
    first_copy = approvals[0].copy_capture_id
    assert first_copy is not None
    before = journey.read(first_copy)
    inspections = [
        journey.tasks.sharing.inspect(
            SharingInspectRequest(subject_id=approval.approval_id), authority=journey.owner
        )
        for approval in approvals
    ]
    _export(journey, exported)
    snapshot = validated_portable_snapshot(exported)
    with pytest.raises(PortableValidationError):
        validate_sharing_authority(snapshot.files)
    receipt = journey.tasks.portability.import_clean(
        exported, restored, import_id="import_" + str(uuid4())
    )
    assert receipt.schema_version == 12
    from open_brain.profile import open_existing_single_user_local

    journey.tasks = open_local_engine(open_existing_single_user_local(restored))
    assert journey.read(first_copy) == before
    from open_brain.services.mcp_protocol import McpCallError

    revoked_copy = approvals[2].copy_capture_id
    with pytest.raises(McpCallError, match="not_found"):
        journey.read(revoked_copy)
    assert [
        journey.tasks.sharing.inspect(
            SharingInspectRequest(subject_id=approval.approval_id), authority=journey.owner
        )
        for approval in approvals
    ] == inspections
    _export(journey, again)
    second = validated_portable_snapshot(again)
    assert {
        path: raw for path, raw in snapshot.files.items() if path != "portable-manifest.json"
    } == {path: raw for path, raw in second.files.items() if path != "portable-manifest.json"}
    assert SELECTIONS_PATH in snapshot.files
    assert json.loads(snapshot.files[SHARING_APPROVALS_PATH])["sharing_revocations"]


@pytest.mark.parametrize(
    "tamper",
    [
        "selection-remove",
        "selection-members",
        "selection-request",
        "projection",
        "citation",
        "privacy",
        "source-proof",
        "link",
        "approval-remove",
    ],
)
def test_rehashed_v12_tamper_refused_before_destination_write(tmp_path: Path, tamper: str) -> None:
    journey, _ = _mixed(tmp_path)
    exported, destination = tmp_path / "exported", tmp_path / "destination"
    _export(journey, exported)
    path = exported / SELECTIONS_PATH
    if tamper == "selection-remove":
        path.unlink()
    elif tamper.startswith("selection-"):
        value = json.loads(path.read_bytes())
        row = value["selections"][0]
        key = "selection_bytes" if tamper == "selection-members" else "request_bytes"
        document = json.loads(base64.b64decode(row[key]))
        document["member_source_ids"] = document["member_source_ids"][:1]
        row[key] = base64.b64encode(canonical(document)).decode()
        path.write_bytes(canonical(value))
    else:
        path = exported / SHARING_APPROVALS_PATH
        value = json.loads(path.read_bytes())
        if tamper == "link":
            value["sharing_links"].pop()
        elif tamper == "approval-remove":
            value["sharing_decisions"].pop()
        else:
            row = next(
                row
                for row in value["sharing_previews"]
                if json.loads(base64.b64decode(row["preview_bytes"]))["dto_version"] == 2
            )
            preview = json.loads(base64.b64decode(row["preview_bytes"]))
            if tamper == "projection":
                preview["projection_sha256"] = "a" * 64
            elif tamper == "source-proof":
                preview["intake_binding_sha256"] = "a" * 64
            elif tamper == "privacy":
                preview["copy_privacy"]["tier"] = "public"
            else:
                envelope = json.loads(preview["text"])
                envelope["citation"]["permalink"] = "https://evil.example/forged"
                preview["text"] = canonical(envelope).decode()
            raw = canonical(preview)
            row["preview_bytes"] = base64.b64encode(raw).decode()
            row["preview_sha256"] = sha256(b"open-brain-sharing-preview.v1\0" + raw).hexdigest()
        path.write_bytes(canonical(value))
    _rehash(exported)
    with pytest.raises((PortableValidationError, ValueError)):
        journey.tasks.portability.import_clean(
            exported, destination, import_id="import_" + str(uuid4())
        )
    assert not destination.exists()


@pytest.mark.parametrize("version", [7, 8, 9, 10, 11])
def test_rehashed_older_version_refuses_v2_sharing_even_without_selection_sidecar(
    tmp_path: Path, version: int
) -> None:
    from importlib import import_module

    journey, _ = _mixed(tmp_path)
    exported = tmp_path / "exported"
    _export(journey, exported)
    (exported / SELECTIONS_PATH).unlink()
    manifest = json.loads((exported / "portable-manifest.json").read_bytes())
    if version in {7, 8, 9}:
        module = import_module(f"open_brain_engine.portable.v{version}")
    else:
        module = import_module(f"open_brain_engine.portable.v{version}_catalog")
    manifest.update(
        schema_version=version,
        layout_version=version,
        contract_version=str(version),
        compatibility={"maximum_contract_version": str(version), "minimum_contract_version": "1"},
        schema_catalog_digest=getattr(module, f"PORTABLE_V{version}_SCHEMA_CATALOG_DIGEST"),
    )
    (exported / "portable-manifest.json").write_bytes(canonical(manifest))
    _rehash(exported)
    with pytest.raises(PortableValidationError):
        validated_portable_snapshot(exported)


def test_old_archive_audit_and_exact_replay_refuse_new_selection_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine import local_schema
    from open_brain_engine.engine.portable_v5_restore import restored_portable_semantic_state

    with monkeypatch.context() as old:
        old.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 16)
        tasks, _, _, _ = _managed_source(tmp_path / "old")
        archive = tmp_path / "archive"
        assert (
            tasks.portability.export(archive, export_id="export_" + str(uuid4())).schema_version
            == 11
        )
    snapshot = validated_portable_snapshot(archive)
    destination = tmp_path / "restored" / "brain"
    destination.parent.mkdir(mode=0o700)
    import_id = "import_" + str(uuid4())
    tasks.portability.import_clean(archive, destination, import_id=import_id)
    journey = Journey(destination.parent)
    journey.scan([_message("1760000001.000001", DECISION)])
    source = journey.sources()[-1]
    # Resolve the Slack namespace, rather than relying on random source UUID order.
    with journey.tasks.sharing._engine._store.connect() as connection:
        source_id = next(
            row[0]
            for row in connection.execute("SELECT source_id,namespace_json FROM source_namespaces")
            if json.loads(row[1])["connector_name"] == "slack"
        )
    source = next(item for item in journey.sources() if item["source_id"] == source_id)
    journey.route(source)
    journey.tasks.sharing.create_selection(
        SlackSelectionCreateRequest(
            operation_id="selection.after-old-restore",
            brain_id=journey.brain_id,
            issuer_epoch=journey.epoch,
            destination_space_id=journey.space,
            member_source_ids=(source_id,),
        ),
        authority=journey.owner,
    )
    with journey.tasks.sharing._engine._store.connect() as connection:
        if not connection.in_transaction:
            connection.execute("BEGIN")
        with pytest.raises(PortableValidationError, match="Slack selection authority drift"):
            restored_portable_semantic_state(
                connection, profile=journey.tasks.profile, snapshot=snapshot
            )
    with pytest.raises(ValueError):
        tasks.portability.import_clean(archive, destination, import_id=import_id)
    with journey.tasks.sharing._engine._store.connect() as connection:
        assert connection.execute("SELECT count(*) FROM slack_recall_selections").fetchone()[0] == 1


@pytest.mark.parametrize("decision", ["undecided", "reject"])
def test_frozen_v7_refuses_v2_preview_without_a_copy(tmp_path: Path, decision: str) -> None:
    from packages.app.tests.integration.engine.test_slack_sharing import _single

    journey = _single(tmp_path)
    sharing = journey.tasks.sharing
    preview = sharing.preview(journey.request(journey.sources()[0]), authority=journey.owner)
    if decision == "reject":
        sharing.decide(
            SharingDecisionRequest(
                operation_id="reject.v2",
                preview_id=preview.preview_id,
                preview_sha256=preview.preview_sha256,
                brain_id=journey.brain_id,
                issuer_epoch=journey.epoch,
                destination_brain_id=journey.brain_id,
                expected_decision_version=0,
                decision="reject",
            ),
            authority=journey.owner,
        )
    exported = tmp_path / "exported"
    _export(journey, exported)
    snapshot = validated_portable_snapshot(exported)
    with pytest.raises(PortableValidationError, match="sharing authority invalid"):
        validate_sharing_authority(snapshot.files)


def test_unlinked_new_copy_privacy_policy_cannot_be_exported(tmp_path: Path) -> None:
    from dataclasses import replace

    from open_brain_engine.engine import TextPayload
    from open_brain_engine.engine.slack_sharing import copy_privacy

    from packages.app.tests.integration.engine.test_slack_sharing import _single
    from packages.app.tests.unit.engine.test_foundation_contracts import _public_submission

    journey = _single(tmp_path)
    journey.tasks.capture.submit(
        replace(
            _public_submission(journey.tasks),
            payload=TextPayload("Synthetic forged copy policy"),
            delivery_id="synthetic.forged-policy",
            privacy=copy_privacy(),
        )
    )
    with pytest.raises(PortableValidationError, match="sharing authority invalid"):
        _export(journey, tmp_path / "exported")
    assert not (tmp_path / "exported").exists()

from __future__ import annotations

import base64
import json
import os
import shutil
from collections.abc import Callable
from dataclasses import asdict
from hashlib import sha256
from importlib.resources import files
from pathlib import Path
from typing import cast

import open_brain_engine.engine.local_schema as local_schema
import open_brain_engine.engine.portability as portability_module
import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.engine import (
    BrainEngine,
    CaptureAction,
    DecisionOutcome,
    EventPayload,
    InjectedFault,
    MeasurementPayload,
    PortabilityFault,
    ProposalDraft,
    TextPayload,
)
from open_brain_engine.engine.local_schema_catalog import LOCAL_MIGRATIONS
from open_brain_engine.engine.materializer import Materialization, materialize_portable_root
from open_brain_engine.engine.portability_ports import PortableWritePort, portable_write_port
from open_brain_engine.portable import PortableValidationError, validate_portable_root
from open_brain_engine.portable.v1 import PortableSnapshot, validated_portable_snapshot
from open_brain_engine.storage.filesystem import RootConfinementError
from open_brain_engine.storage.locks import FileLease
from open_brain_engine.storage.markdown import parse_markdown, render_markdown
from open_brain_engine.storage.staging import SiblingStage

from open_brain.profile import compile_single_user_local

FIXTURE_ROOT = Path(str(files("open_brain_engine.portable").joinpath("conformance/v1/brain-root")))


@pytest.fixture(autouse=True)
def historical_portable_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep these v1-v3 roundtrip recipes on their unchanged schema-six producer."""
    monkeypatch.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 6)
    monkeypatch.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:6])


def _engine(root: Path, *, faults: set[PortabilityFault] | None = None) -> BrainEngine:
    return BrainEngine.open(
        compile_single_user_local(root),
        faults=set() if faults is None else faults,
    )


def _import_fixture(tmp_path: Path) -> tuple[BrainEngine, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp_path.chmod(0o700)
    control = _engine(tmp_path / "control")
    source = tmp_path / "source"
    control.tasks.portability.import_clean(
        FIXTURE_ROOT,
        source,
        import_id="import_123e4567-e89b-42d3-a456-4266141740d0",
    )
    return _engine(source), source


def _portable_bytes(root: Path) -> dict[str, bytes]:
    manifest = validate_portable_root(root)
    entries = cast(list[dict[str, object]], manifest["files"])
    return {str(entry["path"]): (root / str(entry["path"])).read_bytes() for entry in entries}


def _rewrite_manifest(root: Path) -> None:
    files = [
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": sha256(path.read_bytes()).hexdigest(),
        }
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "portable-manifest.json"
    ]
    manifest_path = root / "portable-manifest.json"
    manifest = cast(dict[str, object], json.loads(manifest_path.read_bytes()))
    manifest["files"] = files
    manifest_path.write_bytes(portable_canonical_json_bytes(manifest))


def _capture_payload(root: Path, capture_id: str) -> dict[str, object]:
    matches = list((root / "sources" / "captures").rglob(f"{capture_id}.json"))
    assert len(matches) == 1
    record = cast(dict[str, object], json.loads(matches[0].read_bytes()))
    return cast(dict[str, object], record["payload"])


def _replace_portable_snapshot(root: Path, snapshot: PortableSnapshot) -> None:
    for name in ("brain.toml", "content", "history", "sources", "portable-manifest.json"):
        target = root / name
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    for relative, payload in snapshot.files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent = target.parent
        while parent != root:
            parent.chmod(0o700)
            parent = parent.parent
        target.write_bytes(payload)
        target.chmod(0o600)


def _opaque_pending_proposal(root: Path, *, proposed_kind: str, media_type: str) -> None:
    proposal_path = (
        root / "history/proposals/2026/08/proposal_123e4567-e89b-42d3-a456-426614174018.json"
    )
    proposal = cast(dict[str, object], json.loads(proposal_path.read_bytes()))
    content_bytes = b"opaque non-JSON Portable proposal content"
    content_digest = sha256(content_bytes).hexdigest()
    proposal["proposed_kind"] = proposed_kind
    proposal["proposed_content"] = {
        "bytes_base64": base64.b64encode(content_bytes).decode("ascii"),
        "media_type": media_type,
        "sha256": content_digest,
    }
    receipt = cast(dict[str, object], proposal["expected_receipt"])
    receipt_payload = cast(dict[str, object], receipt["payload"])
    receipt_payload["proposed_content_sha256"] = content_digest
    receipt["sha256"] = sha256(portable_canonical_json_bytes(receipt_payload)).hexdigest()
    proposal_path.write_bytes(portable_canonical_json_bytes(proposal))
    (root / "history/decisions/2026/08/decision_123e4567-e89b-42d3-a456-426614174019.json").unlink()
    (root / "history/actions/2026/08/action_123e4567-e89b-42d3-a456-42661417400b.json").unlink()
    _rewrite_manifest(root)


@pytest.mark.parametrize(
    ("proposed_kind", "media_type", "suffix"),
    (
        ("page_update", "text/markdown", "1"),
        ("event", "application/octet-stream", "2"),
        ("measurement", "text/plain", "3"),
        ("action", "application/vnd.open-brain+opaque", "4"),
    ),
)
def test_portable_valid_pending_proposals_materialize_for_every_kind(
    tmp_path: Path, proposed_kind: str, media_type: str, suffix: str
) -> None:
    source = tmp_path / proposed_kind
    shutil.copytree(FIXTURE_ROOT, source)
    if proposed_kind != "page_update":
        _opaque_pending_proposal(source, proposed_kind=proposed_kind, media_type=media_type)

    assert validate_portable_root(source)["tenant_id"]
    receipt = _engine(tmp_path / f"control-{proposed_kind}").tasks.portability.import_clean(
        source,
        tmp_path / f"imported-{proposed_kind}",
        import_id=f"import_123e4567-e89b-42d3-a456-4266141740{suffix}1",
    )
    assert receipt.status == "imported"


@pytest.mark.parametrize(
    ("media_type", "bytes_base64"),
    (("", "b3BhcXVl"), ("application/octet-stream", "%%%")),
)
def test_portable_rejects_invalid_pending_proposal_media_or_content(
    tmp_path: Path, media_type: str, bytes_base64: str
) -> None:
    source = tmp_path / "invalid-proposal"
    shutil.copytree(FIXTURE_ROOT, source)
    proposal_path = (
        source / "history/proposals/2026/08/proposal_123e4567-e89b-42d3-a456-426614174018.json"
    )
    proposal = cast(dict[str, object], json.loads(proposal_path.read_bytes()))
    content = cast(dict[str, object], proposal["proposed_content"])
    content["media_type"] = media_type
    content["bytes_base64"] = bytes_base64
    proposal_path.write_bytes(portable_canonical_json_bytes(proposal))
    _rewrite_manifest(source)

    with pytest.raises(PortableValidationError, match="proposal content"):
        validate_portable_root(source)


def test_archived_canonical_page_imports_but_is_not_retrievable(tmp_path: Path) -> None:
    source = tmp_path / "archived-source"
    shutil.copytree(FIXTURE_ROOT, source)
    page_path = next((source / "content" / "spaces").rglob("page_*.md"))
    parsed = parse_markdown(page_path.read_bytes())
    fields = dict(parsed.fields)
    fields["status"] = "archived"
    page_path.write_bytes(render_markdown(fields=fields, body=parsed.body).encode())
    _rewrite_manifest(source)

    imported = tmp_path / "archived-import"
    _engine(tmp_path / "control").tasks.portability.import_clean(
        source,
        imported,
        import_id="import_123e4567-e89b-42d3-a456-4266141740fb",
    )
    page_id = cast(str, fields["page_id"])

    assert validate_portable_root(imported)
    results = _engine(imported).retrieval.search("Synthetic")
    assert all(result.result_id != page_id for result in results)


def test_populated_live_root_round_trips_with_stable_identity_bytes_and_results(
    tmp_path: Path,
) -> None:
    source_engine, source = _import_fixture(tmp_path)
    space = source_engine.inbox.spaces()[0]
    source_engine.inbox.rename_space(
        space.space_id,
        "Renamed Studio",
        delivery_id="delivery.rename",
    )
    capture = source_engine.capture.accept(
        TextPayload("Synthetic round trip phrase"),
        delivery_id="delivery.round-trip",
        space_id=space.space_id,
    )
    event = source_engine.capture.accept(
        EventPayload(
            "synthetic.portability",
            "2026-08-30T12:00:00Z",
            {"source": "integration"},
        ),
        delivery_id="delivery.event",
    )
    siblings = source_engine.review.propose(
        capture.capture_id,
        (
            ProposalDraft("Approved", "# Approved\n"),
            ProposalDraft("Rejected", "# Rejected\n"),
        ),
        delivery_id="delivery.siblings",
    )
    source_engine.review.decide(
        siblings[0].proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="delivery.approve",
    )
    source_engine.review.decide(
        siblings[1].proposal_id,
        DecisionOutcome.REJECTED,
        delivery_id="delivery.reject",
    )
    edited_capture = source_engine.capture.accept(
        TextPayload("Synthetic edited phrase"),
        delivery_id="delivery.edited",
        space_id=space.space_id,
    )
    edited = source_engine.review.propose(
        edited_capture.capture_id,
        (ProposalDraft("Edited", "# Original\n"),),
        delivery_id="delivery.edited-proposal",
    )[0]
    source_engine.review.decide(
        edited.proposal_id,
        DecisionOutcome.EDITED,
        delivery_id="delivery.edited-decision",
        edited_markdown="# Owner edited\n",
    )

    exported = tmp_path / "exported"
    export_receipt = source_engine.tasks.portability.export(
        exported,
        export_id="export_123e4567-e89b-42d3-a456-4266141740d1",
    )
    imported = tmp_path / "imported"
    import_receipt = source_engine.tasks.portability.import_clean(
        exported,
        imported,
        import_id="import_123e4567-e89b-42d3-a456-4266141740d2",
    )
    reopened = _engine(imported)

    assert export_receipt.status == "exported"
    assert import_receipt.status == "imported"
    assert import_receipt.batches == 2
    assert import_receipt.blobs == 1
    assert import_receipt.history_records >= 6
    assert _portable_bytes(exported) == _portable_bytes(imported)
    event_paths = tuple((exported / "sources/captures").rglob(f"{event.capture_id}.json"))
    assert len(event_paths) == 1
    event_record = json.loads(event_paths[0].read_bytes())
    assert event_record["payload_binding"]["kind"] == "inline"
    source_profile = compile_single_user_local(source)
    imported_profile = compile_single_user_local(imported)
    assert source_profile.tenant_id == imported_profile.tenant_id
    assert source_profile.owner_actor_id == imported_profile.owner_actor_id
    assert reopened.inbox.spaces()[0].space_id == space.space_id
    assert reopened.inbox.spaces()[0].name == "Renamed Studio"
    assert reopened.retrieval.search("round trip phrase")[0].capture_id == capture.capture_id
    outcomes = {proposal.status for proposal in reopened.review.list(capture_id=capture.capture_id)}
    assert outcomes == {"approved", "rejected"}
    assert reopened.review.list(capture_id=edited_capture.capture_id)[0].status == "edited"
    assert not (exported / ".open-brain").exists()
    assert not any(".open-brain" in path.parts for path in exported.rglob("*"))
    assert not (imported / ".open-brain" / "identity.json").exists()
    assert (imported / ".open-brain" / "indexes" / "search.sqlite3").is_file()
    public = asdict(import_receipt)
    assert not any("path" in key or "digest" in key or "content" in key for key in public)


def test_bound_page_three_revision_v3_round_trip_is_byte_and_identity_stable(
    tmp_path: Path,
) -> None:
    source = tmp_path / "bound-source"
    engine = _engine(source)
    space_id = engine.inbox.create_space(
        "Portable Bound", delivery_id="delivery.bound.portable.space"
    ).space_id
    captures = tuple(
        engine.capture.accept(
            TextPayload(f"Portable bound source {index}"),
            delivery_id=f"delivery.bound.portable.source.{index}",
            space_id=space_id,
        ).capture_id
        for index in range(3)
    )
    created = engine.review.propose(
        (captures[0],),
        (ProposalDraft("Stable portable page", "Portable revision one"),),
        delivery_id="delivery.bound.portable.create",
    )[0]
    engine.review.decide(
        created.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="delivery.bound.portable.create.approve",
        expected_review_digest=created.review_digest,
    )
    current = created
    for ordinal, capture_id in enumerate(captures[1:], start=2):
        reopened = _engine(source)
        current = reopened.review.propose(
            (capture_id,),
            (ProposalDraft("Stable portable page", f"Portable revision {ordinal} final phrase"),),
            delivery_id=f"delivery.bound.portable.update.{ordinal}",
            target_page_id=created.page_id,
        )[0]
        reopened.review.decide(
            current.proposal_id,
            DecisionOutcome.APPROVED,
            delivery_id=f"delivery.bound.portable.update.{ordinal}.approve",
            expected_review_digest=current.review_digest,
        )

    reopened = _engine(source)
    rebuilt = reopened.tasks.portability.rebuild_index()
    results = reopened.retrieval.search("Portable revision 3 final phrase", record_type="canonical")
    exported = tmp_path / "bound-exported"
    reopened.tasks.portability.export(
        exported,
        export_id="export_123e4567-e89b-42d3-a456-4266141740e1",
    )
    imported = tmp_path / "bound-imported"
    reopened.tasks.portability.import_clean(
        exported,
        imported,
        import_id="import_123e4567-e89b-42d3-a456-4266141740e2",
    )
    reexported = tmp_path / "bound-reexported"
    imported_engine = _engine(imported)
    imported_engine._rederive_live_search_projection()
    imported_rebuild = imported_engine.tasks.portability.rebuild_index()
    imported_engine.tasks.portability.export(
        reexported,
        export_id="export_123e4567-e89b-42d3-a456-4266141740e3",
    )

    assert rebuilt.index_generation is not None
    assert imported_rebuild.index_generation is not None
    assert [result.result_id for result in results] == [created.page_id]
    assert validate_portable_root(exported)["schema_version"] == 3
    assert _portable_bytes(exported) == _portable_bytes(imported)
    assert _portable_bytes(exported) == _portable_bytes(reexported)
    assert len(tuple(exported.glob("history/review-bindings/*/*/*.json"))) == 3
    publications = tuple(exported.glob("history/publications/*/*/*.json"))
    assert (
        len(
            [
                path
                for path in publications
                if json.loads(path.read_bytes()).get("page_id") == created.page_id
            ]
        )
        == 3
    )
    imported_results = imported_engine.retrieval.search(
        "Portable revision 3 final phrase", record_type="canonical"
    )
    assert [result.result_id for result in imported_results] == [created.page_id]
    shown = imported_engine.review.show(current.proposal_id)
    assert shown.capture_ids == captures
    reroute_target = imported_engine.inbox.create_space(
        "Blocked Reroute", delivery_id="delivery.bound.portable.reroute-space"
    )
    with pytest.raises(ValueError, match="published capture cannot be rerouted"):
        imported_engine.inbox.route(
            captures[1],
            reroute_target.space_id,
            delivery_id="delivery.bound.portable.reroute",
        )


def test_imported_legacy_canonical_capture_remains_the_page_owner(tmp_path: Path) -> None:
    source = tmp_path / "legacy-owner-source"
    engine = _engine(source)
    space = engine.inbox.create_space("Legacy Owner", delivery_id="delivery.legacy.owner.space")
    capture = engine.capture.accept(
        TextPayload("Imported legacy owner phrase"),
        delivery_id="delivery.legacy.owner.capture",
        action=CaptureAction.CANONICAL_NOTE,
        space_id=space.space_id,
    )
    expected = engine.retrieval.search("Imported legacy owner phrase", record_type="canonical")[0]
    exported = tmp_path / "legacy-owner-export"
    engine.portability.export(
        exported,
        export_id="export_123e4567-e89b-42d3-a456-4266141740e8",
    )
    imported = tmp_path / "legacy-owner-import"
    engine.portability.import_clean(
        exported,
        imported,
        import_id="import_123e4567-e89b-42d3-a456-4266141740e9",
    )
    reopened = _engine(imported)

    reopened._rederive_live_search_projection()
    results = reopened.retrieval.search("Imported legacy owner phrase", record_type="canonical")

    assert [(result.result_id, result.capture_id) for result in results] == [
        (expected.result_id, capture.capture_id)
    ]


def test_imported_pending_bound_create_can_be_approved(tmp_path: Path) -> None:
    engine, _ = _import_fixture(tmp_path)
    source = engine.capture.accept(
        TextPayload("Imported pending bound phrase"),
        delivery_id="delivery.pending.bound.source",
        space_id=engine.inbox.spaces()[0].space_id,
    )
    proposal = engine.review.propose(
        (source.capture_id,),
        (ProposalDraft("Imported pending bound", "Imported pending bound body"),),
        delivery_id="delivery.pending.bound.propose",
    )[0]
    exported = tmp_path / "pending-bound-export"
    engine.portability.export(
        exported,
        export_id="export_123e4567-e89b-42d3-a456-4266141740e4",
    )
    imported = tmp_path / "pending-bound-import"
    engine.portability.import_clean(
        exported,
        imported,
        import_id="import_123e4567-e89b-42d3-a456-4266141740e5",
    )
    reopened = _engine(imported)

    reopened.review.decide(
        proposal.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="delivery.pending.bound.approve",
        expected_review_digest=proposal.review_digest,
    )

    assert [
        result.result_id
        for result in reopened.retrieval.search(
            "Imported pending bound body", record_type="canonical"
        )
    ] == [proposal.page_id]
    assert tuple(imported.rglob(f"{proposal.page_id}.md"))
    assert len(tuple(imported.glob("history/publications/*/*/*.json"))) >= 2


def test_imported_pending_legacy_page_create_can_be_approved(tmp_path: Path) -> None:
    engine, _ = _import_fixture(tmp_path)
    source = engine.capture.accept(
        TextPayload("Imported pending legacy phrase"),
        delivery_id="delivery.pending.legacy.source",
        space_id=engine.inbox.spaces()[0].space_id,
    )
    proposal = engine.review.propose(
        source.capture_id,
        (ProposalDraft("Imported pending legacy", "Imported pending legacy body"),),
        delivery_id="delivery.pending.legacy.propose",
    )[0]
    exported = tmp_path / "pending-legacy-export"
    engine.portability.export(
        exported,
        export_id="export_123e4567-e89b-42d3-a456-4266141740e6",
    )
    imported = tmp_path / "pending-legacy-import"
    engine.portability.import_clean(
        exported,
        imported,
        import_id="import_123e4567-e89b-42d3-a456-4266141740e7",
    )
    reopened = _engine(imported)

    reopened.review.decide(
        proposal.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="delivery.pending.legacy.approve",
    )

    assert [
        result.result_id
        for result in reopened.retrieval.search(
            "Imported pending legacy body", record_type="canonical"
        )
    ] == [proposal.page_id]
    assert tuple(imported.rglob(f"{proposal.page_id}.md"))


def test_v3_rejects_recomputed_edited_publication_provenance_tamper(
    tmp_path: Path,
) -> None:
    root = tmp_path / "edited-tamper-source"
    engine = _engine(root)
    space = engine.inbox.create_space("Edited Tamper", delivery_id="delivery.edited.tamper.space")
    captures = tuple(
        engine.capture.accept(
            TextPayload(f"Edited tamper source {index}"),
            delivery_id=f"delivery.edited.tamper.source.{index}",
            space_id=space.space_id,
        ).capture_id
        for index in range(2)
    )
    proposal = engine.review.propose(
        captures,
        (ProposalDraft("Edited tamper", "Original reviewed body"),),
        delivery_id="delivery.edited.tamper.propose",
    )[0]
    engine.review.decide(
        proposal.proposal_id,
        DecisionOutcome.EDITED,
        delivery_id="delivery.edited.tamper.approve",
        edited_markdown="Legitimate edited body",
        expected_review_digest=proposal.review_digest,
    )
    exported = tmp_path / "edited-tamper-export"
    engine.portability.export(
        exported,
        export_id="export_123e4567-e89b-42d3-a456-4266141740ea",
    )
    assert validate_portable_root(exported)["schema_version"] == 3

    decision_path = next(
        path
        for path in (exported / "history/decisions").rglob("*.json")
        if json.loads(path.read_bytes())["proposal_id"] == proposal.proposal_id
    )
    decision = cast(dict[str, object], json.loads(decision_path.read_bytes()))
    publication_path = next(
        path
        for path in (exported / "history/publications").rglob("*.json")
        if json.loads(path.read_bytes())["decision_id"] == decision["decision_id"]
    )
    publication = cast(dict[str, object], json.loads(publication_path.read_bytes()))
    page_path = exported / cast(str, publication["published_path"])
    page = parse_markdown(page_path.read_bytes())
    tampered = render_markdown(
        fields={**page.fields, "provenance": [captures[0]]},
        body=page.body,
    ).encode()
    tampered_digest = sha256(tampered).hexdigest()
    decision["edited_content"] = {
        "bytes_base64": base64.b64encode(tampered).decode("ascii"),
        "sha256": tampered_digest,
    }
    decision["terminal_digest"] = sha256(
        portable_canonical_json_bytes(
            {
                "decision_id": decision["decision_id"],
                "edited_content_sha256": tampered_digest,
                "expected_state_digest": decision["expected_state_digest"],
                "outcome": decision["outcome"],
                "proposal_id": decision["proposal_id"],
            }
        )
    ).hexdigest()
    publication["published_bytes_base64"] = base64.b64encode(tampered).decode("ascii")
    publication["published_sha256"] = tampered_digest
    decision_path.write_bytes(portable_canonical_json_bytes(decision))
    publication_path.write_bytes(portable_canonical_json_bytes(publication))
    page_path.write_bytes(tampered)
    _rewrite_manifest(exported)

    with pytest.raises(PortableValidationError, match="published page binding"):
        validate_portable_root(exported)


def test_null_event_and_measurement_occurrence_round_trip_without_fabrication(
    tmp_path: Path,
) -> None:
    source_engine, source = _import_fixture(tmp_path)
    event = source_engine.capture.accept(
        EventPayload(
            "null.occurrence",
            None,
            {"token": "null-event-token"},
        ),
        delivery_id="delivery.null-event",
    )
    measurement = source_engine.capture.accept(
        MeasurementPayload(
            "42.5",
            "ms",
            None,
            {"token": "null-measurement-token"},
        ),
        delivery_id="delivery.null-measurement",
    )

    exported = tmp_path / "exported"
    source_engine.tasks.portability.export(
        exported,
        export_id="export_123e4567-e89b-42d3-a456-4266141740f1",
    )
    imported = tmp_path / "imported"
    source_engine.tasks.portability.import_clean(
        exported,
        imported,
        import_id="import_123e4567-e89b-42d3-a456-4266141740f2",
    )

    for root in (source, exported, imported):
        assert _capture_payload(root, event.capture_id)["occurrence_at"] is None
        assert _capture_payload(root, measurement.capture_id)["occurrence_at"] is None

    reopened = _engine(imported)
    assert reopened.retrieval.search("null-event-token")[0].capture_id == event.capture_id
    assert (
        reopened.retrieval.search("null-measurement-token")[0].capture_id == measurement.capture_id
    )


def test_post_capture_route_chain_round_trips_current_space_membership(tmp_path: Path) -> None:
    source_engine, source = _import_fixture(tmp_path)
    first_space = source_engine.inbox.spaces()[0]
    final_space = source_engine.inbox.create_space(
        "Final Route",
        delivery_id="delivery.route-space",
    )
    capture = source_engine.capture.accept(
        TextPayload("routepreservationquasar"),
        delivery_id="delivery.route-capture",
    )
    source_engine.inbox.route(
        capture.capture_id,
        first_space.space_id,
        delivery_id="delivery.route-first",
    )
    source_engine.inbox.route(
        capture.capture_id,
        final_space.space_id,
        delivery_id="delivery.route-final",
    )

    source_payload = _capture_payload(source, capture.capture_id)
    source_record = next((source / "sources" / "captures").rglob(f"{capture.capture_id}.json"))
    route_records = [
        cast(dict[str, object], json.loads(path.read_bytes()))
        for path in (source / "history" / "routes").rglob("*.json")
        if json.loads(path.read_bytes())["capture_id"] == capture.capture_id
    ]
    route_by_id = {cast(str, record["route_id"]): record for record in route_records}
    terminal = next(
        record for record in route_records if record["space_id"] == final_space.space_id
    )

    assert source_payload["family"] == "text"
    assert json.loads(source_record.read_bytes())["space_id"] is None
    assert len(route_records) == 2
    assert terminal["supersedes"] in route_by_id

    exported = tmp_path / "routed-export"
    source_engine.tasks.portability.export(
        exported,
        export_id="export_123e4567-e89b-42d3-a456-4266141740fc",
    )
    imported = tmp_path / "routed-import"
    source_engine.tasks.portability.import_clean(
        exported,
        imported,
        import_id="import_123e4567-e89b-42d3-a456-4266141740fd",
    )
    reopened = _engine(imported)

    assert (
        reopened.retrieval.search(
            "routepreservationquasar",
            space_id=first_space.space_id,
        )
        == ()
    )
    result = reopened.retrieval.search(
        "routepreservationquasar",
        space_id=final_space.space_id,
    )[0]
    assert result.capture_id == capture.capture_id
    assert result.space_id == final_space.space_id
    inbox_item = next(
        item for item in reopened.inbox.list() if item.capture_id == capture.capture_id
    )
    assert inbox_item.space_id == final_space.space_id


def test_import_materializes_the_one_validated_snapshot_during_a_to_b_to_a_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine_a, _ = _import_fixture(tmp_path / "brain-a")
    capture_a = engine_a.capture.accept(
        TextPayload("asnapshotnebula"),
        delivery_id="delivery.snapshot-a",
    )
    exported_a = tmp_path / "exported-a"
    engine_a.tasks.portability.export(
        exported_a,
        export_id="export_123e4567-e89b-42d3-a456-4266141740f4",
    )

    engine_b, _ = _import_fixture(tmp_path / "brain-b")
    engine_b.capture.accept(
        TextPayload("bcontaminationquasar"),
        delivery_id="delivery.snapshot-b",
    )
    exported_b = tmp_path / "exported-b"
    engine_b.tasks.portability.export(
        exported_b,
        export_id="export_123e4567-e89b-42d3-a456-4266141740f5",
    )
    snapshot_b = validated_portable_snapshot(exported_b)
    original_materialize = materialize_portable_root
    swaps = 0

    def swap_for_materialization(
        root: Path,
        *,
        snapshot: PortableSnapshot,
        expected_root_identity: tuple[int, int],
    ) -> Materialization:
        nonlocal swaps
        swaps += 1
        _replace_portable_snapshot(root, snapshot_b)
        try:
            return original_materialize(
                root,
                snapshot=snapshot,
                expected_root_identity=expected_root_identity,
            )
        finally:
            _replace_portable_snapshot(root, snapshot)

    monkeypatch.setattr(portability_module, "materialize_portable_root", swap_for_materialization)
    imported = tmp_path / "imported-a"
    engine_a.tasks.portability.import_clean(
        exported_a,
        imported,
        import_id="import_123e4567-e89b-42d3-a456-4266141740f6",
    )

    reopened = _engine(imported)
    assert swaps == 1
    assert reopened.retrieval.search("asnapshotnebula")[0].capture_id == capture_a.capture_id
    assert reopened.retrieval.search("bcontaminationquasar") == ()


def test_export_rejects_valid_portable_replacement_after_initial_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_engine, _ = _import_fixture(tmp_path / "source-a")
    replacement_engine, _ = _import_fixture(tmp_path / "source-b")
    replacement_engine.capture.accept(
        TextPayload("replacement-export-token"),
        delivery_id="delivery.replacement-export",
    )
    replacement = tmp_path / "replacement"
    replacement_engine.tasks.portability.export(
        replacement,
        export_id="export_123e4567-e89b-42d3-a456-4266141740f7",
    )
    replacement_snapshot = validated_portable_snapshot(replacement)
    original_promote = SiblingStage.promote

    def replace_before_promotion(
        stage: SiblingStage,
        *,
        pre_rename: Callable[[], None] | None = None,
    ) -> None:
        _replace_portable_snapshot(stage.root, replacement_snapshot)
        original_promote(stage, pre_rename=pre_rename)

    monkeypatch.setattr(SiblingStage, "promote", replace_before_promotion)
    destination = tmp_path / "replaced-export"

    with pytest.raises(ValueError, match="changed before promotion"):
        source_engine.tasks.portability.export(
            destination,
            export_id="export_123e4567-e89b-42d3-a456-4266141740f8",
        )

    assert not destination.exists()
    assert not list(tmp_path.glob(".replaced-export.portable-stage-*"))


def test_clean_import_rejects_conflicts_and_unsafe_portable_sources(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "control")
    conflict = tmp_path / "conflict"
    conflict.mkdir()
    marker = conflict / "keep.txt"
    marker.write_text("preserve", encoding="utf-8")

    with pytest.raises(ValueError, match="conflicts"):
        engine.tasks.portability.import_clean(
            FIXTURE_ROOT,
            conflict,
            import_id="import_123e4567-e89b-42d3-a456-4266141740d3",
        )
    assert marker.read_text(encoding="utf-8") == "preserve"

    unsafe = tmp_path / "unsafe"
    shutil.copytree(FIXTURE_ROOT, unsafe)
    outside = tmp_path / "outside"
    outside.write_text("outside", encoding="utf-8")
    os.symlink(outside, unsafe / "content" / "outside.md")
    target = tmp_path / "unsafe-target"
    with pytest.raises(PortableValidationError, match="symlinks"):
        engine.tasks.portability.import_clean(
            unsafe,
            target,
            import_id="import_123e4567-e89b-42d3-a456-4266141740d4",
        )
    assert not target.exists()

    special = tmp_path / "special"
    shutil.copytree(FIXTURE_ROOT, special)
    os.mkfifo(special / "sources" / "captures" / "2026" / "08" / "unsafe.fifo")
    with pytest.raises(PortableValidationError, match="special files"):
        engine.tasks.portability.import_clean(
            special,
            tmp_path / "special-target",
            import_id="import_123e4567-e89b-42d3-a456-4266141740d8",
        )

    traversal = tmp_path / "traversal"
    shutil.copytree(FIXTURE_ROOT, traversal)
    manifest_path = traversal / "portable-manifest.json"
    manifest = cast(dict[str, object], json.loads(manifest_path.read_bytes()))
    entries = cast(list[dict[str, object]], manifest["files"])
    entries[0]["path"] = "../outside"
    manifest_path.write_bytes(portable_canonical_json_bytes(manifest))
    with pytest.raises(PortableValidationError, match="escapes"):
        engine.tasks.portability.import_clean(
            traversal,
            tmp_path / "traversal-target",
            import_id="import_123e4567-e89b-42d3-a456-4266141740d9",
        )


def test_import_faults_leave_no_partial_target_before_promotion_and_retry_after(
    tmp_path: Path,
) -> None:
    before = _engine(tmp_path / "before", faults={PortabilityFault.AFTER_MANIFEST})
    absent_target = tmp_path / "absent-target"
    with pytest.raises(InjectedFault):
        before.tasks.portability.import_clean(
            FIXTURE_ROOT,
            absent_target,
            import_id="import_123e4567-e89b-42d3-a456-4266141740d5",
        )
    assert not absent_target.exists()
    assert not list(tmp_path.glob(".absent-target.portable-stage-*"))

    after = _engine(tmp_path / "after", faults={PortabilityFault.AFTER_PROMOTION})
    promoted_target = tmp_path / "promoted-target"
    with pytest.raises(InjectedFault):
        after.tasks.portability.import_clean(
            FIXTURE_ROOT,
            promoted_target,
            import_id="import_123e4567-e89b-42d3-a456-4266141740d6",
        )
    assert promoted_target.is_dir()
    retried = after.tasks.portability.import_clean(
        FIXTURE_ROOT,
        promoted_target,
        import_id="import_123e4567-e89b-42d3-a456-4266141740d6",
    )
    assert retried.duplicate is True
    assert retried.status == "imported"


def test_import_retry_requires_complete_canonical_ready_evidence_and_operational_state(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path / "control")
    target = tmp_path / "target"
    import_id = "import_123e4567-e89b-42d3-a456-4266141740e1"
    receipt = engine.tasks.portability.import_clean(FIXTURE_ROOT, target, import_id=import_id)
    manifest = validate_portable_root(FIXTURE_ROOT)
    ready_path = target / ".open-brain" / "state" / "portability-ready.json"
    ready = cast(dict[str, object], json.loads(ready_path.read_bytes()))
    assert ready == {
        "import_id": import_id,
        "index": {"generation": receipt.index_generation, "state": "complete"},
        "materialization": {
            "counts": {
                "batches": receipt.batches,
                "blobs": receipt.blobs,
                "captures": receipt.captures,
                "history_records": receipt.history_records,
            },
            "state": "complete",
        },
        "schema_version": 1,
        "source_manifest": {
            "digest_sha256": sha256(portable_canonical_json_bytes(manifest)).hexdigest(),
            "export_id": manifest["export_id"],
            "tenant_id": manifest["tenant_id"],
        },
    }
    original_ready = ready_path.read_bytes()

    for replacement in (
        None,
        b"not-json",
        portable_canonical_json_bytes({"schema_version": 1}),
        portable_canonical_json_bytes(
            {
                **ready,
                "import_id": "import_123e4567-e89b-42d3-a456-4266141740e2",
            }
        ),
    ):
        if replacement is None:
            ready_path.unlink()
        else:
            ready_path.write_bytes(replacement)
        with pytest.raises(ValueError, match="retry evidence"):
            engine.tasks.portability.import_clean(FIXTURE_ROOT, target, import_id=import_id)
        ready_path.write_bytes(original_ready)

    index = target / ".open-brain" / "indexes" / "search.sqlite3"
    index.unlink()
    with pytest.raises(ValueError, match="retry evidence"):
        engine.tasks.portability.import_clean(FIXTURE_ROOT, target, import_id=import_id)

    with pytest.raises(ValueError, match="retry evidence"):
        engine.tasks.portability.import_clean(
            FIXTURE_ROOT,
            target,
            import_id="import_123e4567-e89b-42d3-a456-4266141740e3",
        )


def test_import_retry_rejects_a_different_source_manifest_with_matching_files(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path / "control")
    target = tmp_path / "target"
    import_id = "import_123e4567-e89b-42d3-a456-4266141740e4"
    engine.tasks.portability.import_clean(FIXTURE_ROOT, target, import_id=import_id)

    changed_source = tmp_path / "changed-source"
    shutil.copytree(FIXTURE_ROOT, changed_source)
    manifest_path = changed_source / "portable-manifest.json"
    manifest = cast(dict[str, object], json.loads(manifest_path.read_bytes()))
    manifest["export_id"] = "export_123e4567-e89b-42d3-a456-4266141740e5"
    manifest_path.write_bytes(portable_canonical_json_bytes(manifest))
    validate_portable_root(changed_source)

    with pytest.raises(ValueError, match="conflicts"):
        engine.tasks.portability.import_clean(changed_source, target, import_id=import_id)
    assert validate_portable_root(target)["export_id"] != manifest["export_id"]


def test_import_rejects_equal_and_nested_real_source_destination_paths_before_staging(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path / "control")
    source = tmp_path / "source"
    shutil.copytree(FIXTURE_ROOT, source)
    original = {
        path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()
    }

    for destination in (source, source / "nested-target", source.parent):
        with pytest.raises(ValueError, match="source and destination"):
            engine.tasks.portability.import_clean(
                source,
                destination,
                import_id="import_123e4567-e89b-42d3-a456-4266141740e6",
            )
        assert {
            path.relative_to(source): path.read_bytes()
            for path in source.rglob("*")
            if path.is_file()
        } == original
        assert not list(source.parent.glob(".source.portable-stage-*"))


def test_export_rejects_intermediate_parent_swap_before_creating_destination_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_engine, source = _import_fixture(tmp_path)
    portable_before = _portable_bytes(source)
    outside = tmp_path / "outside"
    route = outside / "route"
    destination_parent = route / "content"
    destination_parent.mkdir(parents=True, mode=0o700)
    destination = destination_parent / "exported"
    moved_route = source / "content" / "diverted-route"
    original_promotion_lease = portability_module._promotion_lease

    def swap_intermediate_parent(
        lease_destination: Path,
        actor_id: str,
        parent_identity: tuple[int, int],
    ) -> FileLease:
        route.rename(moved_route)
        route.symlink_to(moved_route, target_is_directory=True)
        return original_promotion_lease(lease_destination, actor_id, parent_identity)

    monkeypatch.setattr(portability_module, "_promotion_lease", swap_intermediate_parent)

    with pytest.raises(RootConfinementError, match="storage root"):
        source_engine.tasks.portability.export(
            destination,
            export_id="export_123e4567-e89b-42d3-a456-4266141740f3",
        )

    assert _portable_bytes(source) == portable_before
    assert not (source / "content" / ".open-brain-locks").exists()
    assert not list((source / "content").glob(".exported.portable-stage-*"))
    assert not (moved_route / "content" / ".open-brain-locks").exists()
    assert not list((moved_route / "content").glob(".exported.portable-stage-*"))


def test_export_rejects_a_shared_destination_parent_before_creating_a_lease(
    tmp_path: Path,
) -> None:
    source_engine, _ = _import_fixture(tmp_path)
    shared_parent = tmp_path / "shared"
    shared_parent.mkdir()
    shared_parent.chmod(0o755)

    with pytest.raises(ValueError, match="destination parent is unsafe"):
        source_engine.tasks.portability.export(
            shared_parent / "exported",
            export_id="export_123e4567-e89b-42d3-a456-4266141740f9",
        )

    assert not (shared_parent / ".open-brain-locks").exists()
    assert not list(shared_parent.glob(".exported.portable-stage-*"))


def test_export_rechecks_destination_parent_mode_inside_lease_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_engine, _ = _import_fixture(tmp_path)
    destination_parent = tmp_path / "destination-parent"
    destination_parent.mkdir(mode=0o700)
    destination = destination_parent / "exported"
    original_promotion_lease = portability_module._promotion_lease

    def make_parent_shared_before_lease(
        lease_destination: Path,
        actor_id: str,
        parent_identity: tuple[int, int],
    ) -> FileLease:
        destination_parent.chmod(0o755)
        return original_promotion_lease(lease_destination, actor_id, parent_identity)

    monkeypatch.setattr(
        portability_module,
        "_promotion_lease",
        make_parent_shared_before_lease,
    )

    with pytest.raises(RootConfinementError, match="lease root mode"):
        source_engine.tasks.portability.export(
            destination,
            export_id="export_123e4567-e89b-42d3-a456-4266141740fa",
        )

    assert not (destination_parent / ".open-brain-locks").exists()
    assert not list(destination_parent.glob(".exported.portable-stage-*"))


class _RecordingPortableWrites:
    def __init__(self, delegate: PortableWritePort) -> None:
        self._delegate = delegate
        self.calls: list[str] = []

    def put_capture(self, payload: bytes) -> object:
        self.calls.append("capture")
        return self._delegate.put_capture(payload)

    def put_history(self, family: str, payload: bytes) -> object:
        self.calls.append(f"history:{family}")
        return self._delegate.put_history(family, payload)

    def put_page(self, relative: str, payload: bytes) -> object:
        self.calls.append("page")
        return self._delegate.put_page(relative, payload)

    def put_space(self, payload: bytes, *, replace: bool) -> object:
        self.calls.append("space")
        return self._delegate.put_space(payload, replace=replace)


def test_capture_review_and_space_writers_use_the_injected_portable_write_ports(
    tmp_path: Path,
) -> None:
    engine, _ = _import_fixture(tmp_path)
    recorder = _RecordingPortableWrites(portable_write_port(engine))
    engine.__dict__["_portable_writes"] = recorder
    space = engine.inbox.create_space("Recorded Space", delivery_id="delivery.port-space")
    capture = engine.capture.accept(
        TextPayload("Portable port routing"),
        delivery_id="delivery.port-capture",
        space_id=space.space_id,
    )
    routed_capture = engine.capture.accept(
        TextPayload("Portable route port"),
        delivery_id="delivery.port-route-capture",
    )
    engine.inbox.route(
        routed_capture.capture_id,
        space.space_id,
        delivery_id="delivery.port-route",
    )
    proposal = engine.review.propose(
        capture.capture_id,
        (ProposalDraft("Port page", "# Port page\n"),),
        delivery_id="delivery.port-proposal",
    )[0]
    engine.review.decide(
        proposal.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="delivery.port-decision",
    )

    assert {
        "space",
        "capture",
        "history:proposal",
        "history:decision",
        "history:publication",
        "history:routing",
        "page",
    } <= set(recorder.calls)


def test_export_retry_and_disposable_index_rebuild_are_idempotent(tmp_path: Path) -> None:
    source_engine, source = _import_fixture(tmp_path)
    exported = tmp_path / "exported"
    first = source_engine.tasks.portability.export(
        exported,
        export_id="export_123e4567-e89b-42d3-a456-4266141740d7",
    )
    retried = source_engine.tasks.portability.export(
        exported,
        export_id="export_123e4567-e89b-42d3-a456-4266141740d7",
    )
    assert first.duplicate is False
    assert retried.duplicate is True

    index = source / ".open-brain" / "indexes" / "search.sqlite3"
    index.unlink()
    rebuilt = source_engine.tasks.portability.rebuild_index()
    assert rebuilt.status == "rebuilt"
    assert rebuilt.index_generation == 1
    assert index.is_file()
    reopened = _engine(source)
    assert reopened.retrieval.search("Synthetic")

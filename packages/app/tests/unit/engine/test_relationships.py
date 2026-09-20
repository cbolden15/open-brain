from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.engine import BrainEngine, TextPayload
from open_brain_engine.engine.t03_contracts import (
    DecisionHistoryRequest,
    EffectiveAuthority,
    RelationshipDecideRequest,
    RelationshipListRequest,
    T03Error,
)
from open_brain_engine.portable.relationships_v1 import (
    RELATIONSHIP_METADATA_PATH,
    validate_relationship_metadata,
)
from open_brain_engine.portable.v4 import (
    PORTABLE_V4_RELATIONSHIPS_CATALOG_DIGEST,
    PORTABLE_V4_SCHEMA_CATALOG_DIGEST,
    SOURCE_METADATA_PATH,
    manifest_v4,
)
from open_brain_engine.portable.v5 import PORTABLE_V5_SCHEMA_CATALOG_DIGEST, V5_SIDECAR_PATHS
from open_brain_engine.portable.versioned import validated_portable_snapshot

from open_brain.profile import compile_single_user_local


def owner() -> EffectiveAuthority:
    return EffectiveAuthority("owner", "session", frozenset(), None, owner=True)


def wire(value: Any) -> dict[str, Any]:
    return cast(dict[str, Any], value.to_wire())


def request(left: str, right: str, kind: str = "duplicate_of") -> RelationshipDecideRequest:
    return RelationshipDecideRequest(
        left={"record_id": left, "revision_id": left},
        right={"record_id": right, "revision_id": right},
        kind=kind,
        decision="accept",
        expected_relationship_version=0,
        operation_id="operation_" + str(uuid4()),
    )


def _write_v4_fixture(source: Path, destination: Path) -> dict[str, Any]:
    snapshot = validated_portable_snapshot(source)
    files = {
        relative: payload
        for relative, payload in snapshot.files.items()
        if relative != "portable-manifest.json" and relative not in V5_SIDECAR_PATHS
    }
    for relative, payload in files.items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    manifest = manifest_v4(
        files,
        tenant_id=str(snapshot.manifest["tenant_id"]),
        export_id="export_" + str(uuid4()),
        created_at=str(snapshot.manifest["created_at"]),
    )
    (destination / "portable-manifest.json").write_bytes(
        portable_canonical_json_bytes(manifest)
    )
    return cast(dict[str, Any], validated_portable_snapshot(destination).manifest)


def test_symmetric_relationship_replay_versions_and_hidden_endpoint(tmp_path: Path) -> None:
    engine = BrainEngine.open(
        compile_single_user_local(tmp_path / "brain", starter_spaces=("One", "Two"))
    )
    spaces = engine.inbox.spaces()
    first = engine.capture.accept(
        TextPayload("Independent identical"), delivery_id="one", space_id=spaces[0].space_id
    ).capture_id
    second = engine.capture.accept(
        TextPayload("Independent identical"), delivery_id="two", space_id=spaces[1].space_id
    ).capture_id
    proposed = request(first, second)
    accepted = wire(engine.relationships.decide(proposed, authority=owner()))
    assert wire(engine.relationships.decide(proposed, authority=owner())) == accepted
    with pytest.raises(T03Error, match="invalid_arguments"):
        engine.relationships.decide(replace(proposed, decision="remove"), authority=owner())
    with pytest.raises(T03Error, match="revision_changed"):
        engine.relationships.decide(
            replace(
                proposed,
                left=proposed.right,
                right=proposed.left,
                operation_id="operation_" + str(uuid4()),
            ),
            authority=owner(),
        )
    removed = wire(
        engine.relationships.decide(
            replace(
                proposed,
                decision="remove",
                expected_relationship_version=1,
                operation_id="operation_" + str(uuid4()),
            ),
            authority=owner(),
        )
    )
    assert removed["version"] == 2 and removed["relationship_id"] == accepted["relationship_id"]
    listing = wire(
        engine.relationships.list_relationships(
            RelationshipListRequest(record_id=first), authority=owner()
        )
    )
    assert listing["entries"][0]["status"] == "removed"
    assert listing["entries"][0]["left"]["record_id"] == min(first, second)
    decisions = wire(
        engine.relationships.list_decisions(
            DecisionHistoryRequest(record_id=first, limit=1), authority=owner()
        )
    )
    assert decisions["entries"][0]["decision"] == "remove" and not decisions["complete"]
    tail = wire(
        engine.relationships.list_decisions(
            DecisionHistoryRequest(record_id=first, limit=1, cursor=decisions["next_cursor"]),
            authority=owner(),
        )
    )
    assert tail["entries"][0]["decision"] == "accept" and tail["complete"]
    scoped = EffectiveAuthority(
        "agent", "scope", frozenset({"history-read"}), frozenset({spaces[0].space_id})
    )
    assert (
        wire(
            engine.relationships.list_relationships(
                RelationshipListRequest(record_id=first), authority=scoped
            )
        )["entries"]
        == []
    )
    assert (
        wire(
            engine.relationships.list_decisions(
                DecisionHistoryRequest(record_id=first), authority=scoped
            )
        )["entries"]
        == []
    )
    with pytest.raises(T03Error, match="unsupported_capability"):
        engine.relationships.decide(proposed, authority=scoped)


def test_supersedes_cycle_self_and_export_complete_decisions(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    ids = [
        engine.capture.accept(TextPayload("Separate source"), delivery_id=str(i)).capture_id
        for i in range(3)
    ]
    before = {
        str(p.relative_to(engine.profile.root)): p.read_bytes()
        for p in (engine.profile.root / "sources/captures").rglob("*.json")
    }
    old_export = tmp_path / "old4"
    engine.portability.export(old_export, export_id="export_" + str(uuid4()))
    assert (
        validated_portable_snapshot(old_export).manifest["schema_catalog_digest"]
        == PORTABLE_V5_SCHEMA_CATALOG_DIGEST
    )
    assert (
        _write_v4_fixture(old_export, tmp_path / "legacy-v4-base")["schema_catalog_digest"]
        == PORTABLE_V4_SCHEMA_CATALOG_DIGEST
    )
    engine.relationships.decide(request(ids[0], ids[1], "supersedes"), authority=owner())
    engine.relationships.decide(request(ids[1], ids[2], "supersedes"), authority=owner())
    with pytest.raises(T03Error, match="invalid_arguments"):
        engine.relationships.decide(request(ids[2], ids[0], "supersedes"), authority=owner())
    with pytest.raises(T03Error, match="invalid_arguments"):
        engine.relationships.decide(request(ids[0], ids[0]), authority=owner())
    export = tmp_path / "export"
    engine.portability.export(export, export_id="export_" + str(uuid4()))
    snapshot = validated_portable_snapshot(export)
    assert snapshot.manifest["schema_catalog_digest"] == PORTABLE_V5_SCHEMA_CATALOG_DIGEST
    assert (
        _write_v4_fixture(export, tmp_path / "legacy-v4-relationships")[
            "schema_catalog_digest"
        ]
        == PORTABLE_V4_RELATIONSHIPS_CATALOG_DIGEST
    )
    assert all(snapshot.files[path] == data for path, data in before.items())
    sidecar = json.loads(snapshot.files[RELATIONSHIP_METADATA_PATH])
    assert len(sidecar["decisions"]) == len(sidecar["relationships"]) == 2
    assert "operation_id" not in str(sidecar) and "request_sha256" not in str(sidecar)
    metadata = json.loads(snapshot.files[SOURCE_METADATA_PATH])
    missing = dict(sidecar, decisions=sidecar["decisions"][:-1])
    with pytest.raises(ValueError, match="relationship evidence invalid"):
        validate_relationship_metadata(portable_canonical_json_bytes(missing), metadata)


def test_relationship_commit_recovery_and_cursor_staleness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine import relationship_store

    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    ids = [
        engine.capture.accept(TextPayload("Separate source"), delivery_id=str(i)).capture_id
        for i in range(3)
    ]
    engine.relationships.decide(request(ids[0], ids[1]), authority=owner())
    engine.relationships.decide(request(ids[0], ids[2]), authority=owner())
    listing = wire(
        engine.relationships.list_relationships(
            RelationshipListRequest(record_id=ids[0], limit=1), authority=owner()
        )
    )
    proposed = request(ids[1], ids[2], "contradicts")

    def interrupted(**_kwargs: Any) -> None:
        raise OSError("synthetic sidecar crash after SQL commit")

    with monkeypatch.context() as patch:
        patch.setattr(relationship_store, "atomic_replace", interrupted)
        with pytest.raises(T03Error, match="operation_pending"):
            engine.relationships.decide(proposed, authority=owner())
    reopened = BrainEngine.open(engine.profile)
    receipt = wire(reopened.relationships.decide(proposed, authority=owner()))
    assert receipt["version"] == 1
    with reopened._store.connect() as connection:
        assert connection.execute("SELECT count(*) FROM relationship_decisions").fetchone()[0] == 3
    metadata = json.loads((engine.profile.root / RELATIONSHIP_METADATA_PATH).read_bytes())
    assert len(metadata["decisions"]) == 3
    with pytest.raises(T03Error, match="cursor_stale"):
        reopened.relationships.list_relationships(
            RelationshipListRequest(record_id=ids[0], limit=1, cursor=listing["next_cursor"]),
            authority=owner(),
        )


@pytest.mark.parametrize("kind", ["source", "canonical"])
def test_relationship_stale_endpoint_checks_after_authorization(tmp_path: Path, kind: str) -> None:
    from open_brain_engine.engine import DecisionOutcome, ProposalDraft, ReferencePayload
    from open_brain_engine.engine.source_intake import SourceRevisionSubmission

    from packages.app.tests.unit.engine.test_foundation_contracts import _public_submission

    engine = BrainEngine.open(
        compile_single_user_local(tmp_path / "brain", starter_spaces=("Notes",))
    )
    other = engine.capture.accept(TextPayload("Other endpoint"), delivery_id="other").capture_id
    if kind == "source":
        original = _public_submission(engine.tasks)
        namespace = dict(
            connector_name="synthetic", connection_id="one", resource_id="one", external_id="one"
        )
        capture = replace(original, payload=ReferencePayload(original.source_reference, "First"))
        intake = SourceRevisionSubmission(
            capture=capture,
            namespace=namespace,
            revision_key="first",
            canonical_sha256=capture.request_sha256(),
            expected_head=None,
            ordering={
                "kind": "monotonic",
                "provider_namespace": "synthetic",
                "epoch": "one",
                "sequence": 1,
            },
            expected_control_epoch=0,
        )
        first = engine.sources.submit_revision(intake)
        assert first.capture_id is not None
        record, revision = first.capture_id, first.capture_id
    else:
        space = engine.inbox.spaces()[0]
        source = engine.capture.accept(
            TextPayload("Canonical source"), delivery_id="canonical", space_id=space.space_id
        )
        draft = engine.review.propose(
            (source.capture_id,), (ProposalDraft("First", "First canonical"),), delivery_id="first"
        )[0]
        published = engine.review.decide(
            draft.proposal_id,
            DecisionOutcome.APPROVED,
            delivery_id="first.decision",
            expected_review_digest=draft.review_digest,
        )
        from open_brain_engine.portable.v4 import canonical_revision_id

        assert published.page_id is not None and published.publication_id is not None
        record, revision = published.page_id, canonical_revision_id(published.publication_id)
    proposed = replace(request(other, other), left={"record_id": record, "revision_id": revision})
    receipt = wire(engine.relationships.decide(proposed, authority=owner()))
    if kind == "source":
        capture = replace(capture, payload=ReferencePayload(original.source_reference, "Second"))
        engine.sources.submit_revision(
            replace(
                intake,
                capture=capture,
                canonical_sha256=capture.request_sha256(),
                revision_key="second",
                expected_head=record,
                ordering={
                    "kind": "monotonic",
                    "provider_namespace": "synthetic",
                    "epoch": "one",
                    "sequence": 2,
                },
            )
        )
    else:
        draft = engine.review.propose(
            (source.capture_id,),
            (ProposalDraft("Second", "Second canonical"),),
            delivery_id="second",
            target_page_id=record,
        )[0]
        engine.review.decide(
            draft.proposal_id,
            DecisionOutcome.APPROVED,
            delivery_id="second.decision",
            expected_review_digest=draft.review_digest,
        )
    assert wire(engine.relationships.decide(proposed, authority=owner())) == receipt
    changed = replace(
        proposed,
        operation_id="operation_" + str(uuid4()),
        expected_relationship_version=1,
        decision="remove",
    )
    with pytest.raises(T03Error, match="revision_changed"):
        engine.relationships.decide(changed, authority=owner())
    with pytest.raises(T03Error, match="not_found"):
        engine.relationships.decide(changed, authority=replace(owner(), space_ids=frozenset()))

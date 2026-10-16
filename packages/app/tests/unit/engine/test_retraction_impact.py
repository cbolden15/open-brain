"""Owner-only impact inspection is complete, immutable and read-only."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from open_brain_engine.engine import BrainEngine, CaptureAction, CaptureSubmission, TextPayload
from open_brain_engine.engine.capture_lifecycle_contracts import GroupIntent, LifecycleError, digest
from open_brain_engine.engine.consent_contracts import EgressMode
from open_brain_engine.engine.retraction_impact import _capture_closure
from open_brain_engine.engine.sharing_contracts import SharingDecisionRequest, SharingRevokeRequest
from open_brain_engine.engine.t03_contracts import SessionOrigin
from open_brain_engine.storage.markdown import parse_markdown, render_markdown

from open_brain.profile import compile_single_user_local
from packages.app.tests.unit.engine.test_capture_lifecycle_engine import (
    _activate,
    _group,
    _owner,
    _request,
    _stage,
)
from packages.app.tests.unit.engine.test_sharing_tasks import _managed_source


def test_capture_inspection_is_body_free_immutable_and_does_not_mutate(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    capture = engine.capture.accept(TextPayload("sensitive synthetic body"), delivery_id="impact")
    with closing(engine._store.connect()) as connection:
        before = tuple(connection.iterdump())
    impact = engine.lifecycle.inspect_retraction(
        capture_id=capture.capture_id, authority=_owner(engine)
    )
    assert dict(impact.counts)["selected_captures"] == 1
    assert impact.page()["items"] == [
        {"kind": "capture", "id": capture.capture_id, "effect": "retract"}
    ]
    assert "sensitive" not in repr(impact)
    assert "brain/" not in str(impact.page())
    with pytest.raises(FrozenInstanceError):
        impact.target_id = "changed"  # type: ignore[misc]
    mutated = impact.page()
    mutated["items"] = []
    assert len(impact.page()["items"]) == 1  # type: ignore[arg-type]
    assert (
        engine.lifecycle.inspect_retraction(
            capture_id=capture.capture_id,
            authority=_owner(engine),
            expected_preimage_sha256=impact.preimage_sha256,
        )
        == impact
    )
    with closing(engine._store.connect()) as connection:
        assert tuple(connection.iterdump()) == before
    with engine._store.transaction() as connection:
        raw = connection.execute(
            "SELECT effective_privacy_json FROM source_revision_privacy WHERE capture_id=?",
            (capture.capture_id,),
        ).fetchone()[0]
        privacy = json.loads(raw)
        privacy["tier"] = "public"
        connection.execute(
            "UPDATE source_revision_privacy SET effective_privacy_json=? WHERE capture_id=?",
            (json.dumps(privacy), capture.capture_id),
        )
    with pytest.raises(LifecycleError, match="revision_changed"):
        engine.lifecycle.inspect_retraction(
            capture_id=capture.capture_id,
            authority=_owner(engine),
            expected_preimage_sha256=impact.preimage_sha256,
        )


@pytest.mark.parametrize("size", [99, 100])
def test_complete_session_membership_paging_and_retraction_staleness(
    tmp_path: Path, size: int
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    request, group, segments = _group(engine, tuple(f"synthetic {i}" for i in range(size)))
    _stage(engine, request, group, segments)
    session = _activate(engine, request, group)
    impact = engine.lifecycle.inspect_retraction(session_id=session, authority=_owner(engine))
    assert impact.capture_ids == tuple(segment.identities.capture_id for segment in segments)
    assert impact.membership_version == 1
    assert dict(impact.counts)["current_groups_suppressed"] == 1
    # A whole current group is an additional typed impact, not a capture member.
    assert impact.page()["item_pages"] == (1 if size == 99 else 2)
    assert len(impact.page()["items"]) == 100  # type: ignore[arg-type]
    if size == 100:
        assert len(impact.page(1)["items"]) == 1  # type: ignore[arg-type]
        assert impact.page(1)["next_item_page"] is None
    for invalid in (-1, 2, True, "1"):
        with pytest.raises(LifecycleError, match="invalid_arguments"):
            impact.page(invalid)  # type: ignore[arg-type]
    snapshot = engine.lifecycle.session_snapshot(session, authority=_owner(engine))
    engine.lifecycle.retract(
        _request(engine, "session_retract", snapshot), authority=_owner(engine)
    )
    with pytest.raises(LifecycleError, match="revision_changed"):
        engine.lifecycle.inspect_retraction(
            session_id=session,
            authority=_owner(engine),
            expected_preimage_sha256=impact.preimage_sha256,
        )
    after = engine.lifecycle.inspect_retraction(session_id=session, authority=_owner(engine))
    assert dict(after.counts)["already_retracted"] == size
    assert dict(after.counts)["current_groups_suppressed"] == 0


def test_historical_member_preserves_new_group_and_group_siblings_affect_pages(
    tmp_path: Path,
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    request, group, segments = _group(engine, ("old synthetic",))
    _stage(engine, request, group, segments)
    _activate(engine, request, group)
    replacement, new_group, new_segments = _group(
        engine, ("new one", "new two"), key=group.group_key, expected=group.revision_sha256
    )
    _stage(engine, replacement, new_group, new_segments)
    _activate(engine, replacement, new_group)
    old = engine.lifecycle.inspect_retraction(
        capture_id=segments[0].identities.capture_id, authority=_owner(engine)
    )
    assert dict(old.counts)["current_groups_suppressed"] == 0
    assert dict(old.counts)["newer_heads_preserved"] == 2
    assert any(item.effect == "historical_only" for item in old.items)
    current = engine.lifecycle.inspect_retraction(
        capture_id=new_segments[0].identities.capture_id, authority=_owner(engine)
    )
    assert dict(current.counts)["current_groups_suppressed"] == 1
    # A page depending only on the unselected sibling must still be included.
    page = "page_" + str(uuid4())
    with engine._store.transaction() as connection:
        connection.execute(
            "INSERT INTO canonical_revision_members VALUES(?,?,?,?,?)",
            (
                "revision_synthetic",
                page,
                "publication_synthetic",
                0,
                new_segments[1].identities.capture_id,
            ),
        )
    with pytest.raises(LifecycleError, match="revision_changed"):
        engine.lifecycle.inspect_retraction(
            capture_id=new_segments[0].identities.capture_id,
            authority=_owner(engine),
            expected_preimage_sha256=current.preimage_sha256,
        )
    assert (
        dict(
            engine.lifecycle.inspect_retraction(
                capture_id=new_segments[0].identities.capture_id, authority=_owner(engine)
            ).counts
        )["dependent_pages"]
        == 1
    )

    engine.lifecycle.retract(
        _request(engine, "capture_retract", {"capture_id": segments[0].identities.capture_id}),
        authority=_owner(engine),
    )
    protected = engine.lifecycle.inspect_retraction(
        capture_id=segments[0].identities.capture_id, authority=_owner(engine)
    )
    engine.lifecycle.retract(
        _request(engine, "capture_retract", {"capture_id": new_segments[0].identities.capture_id}),
        authority=_owner(engine),
    )
    with pytest.raises(LifecycleError, match="revision_changed"):
        engine.lifecycle.inspect_retraction(
            capture_id=segments[0].identities.capture_id,
            authority=_owner(engine),
            expected_preimage_sha256=protected.preimage_sha256,
        )


def test_canonical_page_and_effective_privacy_are_bound_but_unrelated_capture_is_not(
    tmp_path: Path,
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    receipt = engine.capture.accept(
        TextPayload("synthetic canonical"),
        delivery_id="canonical",
        action=CaptureAction.CANONICAL_NOTE,
        space_id=engine.inbox.create_space("Synthetic", delivery_id="space").space_id,
    )
    impact = engine.lifecycle.inspect_retraction(
        capture_id=receipt.capture_id, authority=_owner(engine)
    )
    assert dict(impact.counts)["dependent_pages"] == 1
    engine.capture.accept(TextPayload("unrelated"), delivery_id="unrelated")
    assert (
        engine.lifecycle.inspect_retraction(
            capture_id=receipt.capture_id,
            authority=_owner(engine),
            expected_preimage_sha256=impact.preimage_sha256,
        )
        == impact
    )
    with engine._store.transaction() as connection:
        connection.execute(
            "UPDATE logical_sources SET route_version=route_version+1 WHERE head_capture_id=?",
            (receipt.capture_id,),
        )
    with pytest.raises(LifecycleError, match="revision_changed"):
        engine.lifecycle.inspect_retraction(
            capture_id=receipt.capture_id,
            authority=_owner(engine),
            expected_preimage_sha256=impact.preimage_sha256,
        )


def test_approved_copy_is_identified_and_revocation_stales_snapshot(tmp_path: Path) -> None:
    tasks, request, _, _ = _managed_source(tmp_path)
    engine = BrainEngine.open(tasks.profile)
    assert tasks.sharing is not None
    preview = tasks.sharing.preview(request, authority=_owner(engine))
    decision = SharingDecisionRequest(
        operation_id="impact.approve",
        preview_id=preview.preview_id,
        preview_sha256=preview.preview_sha256,
        brain_id=request.brain_id,
        issuer_epoch=request.issuer_epoch,
        destination_brain_id=request.brain_id,
        expected_decision_version=0,
        decision="approve",
    )
    approved = tasks.sharing.decide(decision, authority=_owner(engine))
    impact = engine.lifecycle.inspect_retraction(
        capture_id=request.expected_head, authority=_owner(engine)
    )
    assert dict(impact.counts)["approved_copies"] == 1
    assert any(
        item.id == approved.copy_capture_id and item.kind == "approved_copy"
        for item in impact.items
    )
    tasks.sharing.revoke(
        SharingRevokeRequest(
            operation_id="impact.revoke",
            approval_id=approved.approval_id,
            expected_approval_version=1,
            brain_id=request.brain_id,
            issuer_epoch=request.issuer_epoch,
            destination_brain_id=request.brain_id,
            reason="owner_choice",
        ),
        authority=_owner(engine),
    )
    with pytest.raises(LifecycleError, match="revision_changed"):
        engine.lifecycle.inspect_retraction(
            capture_id=request.expected_head,
            authority=_owner(engine),
            expected_preimage_sha256=impact.preimage_sha256,
        )


def test_pending_unknown_bad_targets_and_nonlocal_authority_refuse(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    request, group, segments = _group(engine, ("unpublished",))
    _stage(engine, request, group, segments)
    capture = segments[0].identities.capture_id
    with pytest.raises(LifecycleError, match="operation_pending"):
        engine.lifecycle.inspect_retraction(capture_id=capture, authority=_owner(engine))
    for kwargs in (
        {},
        {"capture_id": capture, "session_id": "capsess_" + str(uuid4())},
        {"capture_id": "bad"},
        {"session_id": False},
    ):
        with pytest.raises(LifecycleError, match="invalid_arguments"):
            engine.lifecycle.inspect_retraction(authority=_owner(engine), **kwargs)
    with pytest.raises(LifecycleError, match="operation_not_found"):
        engine.lifecycle.inspect_retraction(
            capture_id="capture_" + str(uuid4()), authority=_owner(engine)
        )
    with closing(engine._store.connect()) as connection:
        brain, epoch = connection.execute(
            "SELECT brain_id,issuer_epoch FROM brain_identity"
        ).fetchone()
    for owner in (
        replace(_owner(engine), owner=False),
        replace(_owner(engine), principal_id="someone_else"),
        replace(_owner(engine), session_origin=SessionOrigin.MCP_IMPLICIT),
        replace(
            _owner(engine),
            egress_mode=EgressMode.EXTERNAL_PROVIDER,
            provider_id="openai",
            consent_id="consent_" + "a" * 32,
            brain_id=brain,
            issuer_epoch=epoch,
        ),
        replace(_owner(engine), space_ids=frozenset()),
    ):
        with pytest.raises(LifecycleError, match="invalid_arguments"):
            engine.lifecycle.inspect_retraction(capture_id=capture, authority=owner)


def test_transitive_dependency_closure_includes_supersedes_and_terminates_cycles() -> None:
    with sqlite3.connect(":memory:") as connection:
        connection.execute("CREATE TABLE historical_relations(original_capture_id,copy_capture_id)")
        connection.execute("CREATE TABLE sharing_links(original_capture_id,copy_capture_id)")
        connection.execute("CREATE TABLE development_evidence(derived_capture_id,receipt_json)")
        connection.execute("INSERT INTO historical_relations VALUES('selected','historical')")
        connection.execute("INSERT INTO sharing_links VALUES('historical','approved')")
        connection.execute("INSERT INTO historical_relations VALUES('derived','selected')")
        connection.execute(
            "INSERT INTO development_evidence VALUES(?,?)",
            (
                "derived",
                json.dumps(
                    {
                        "supporting_capture_ids": [],
                        "supports_development_ids": [],
                        "supersedes_development_ids": ["approved"],
                    }
                ),
            ),
        )
        assert _capture_closure(connection, {"selected"}) == {
            "selected",
            "historical",
            "approved",
            "derived",
        }


@pytest.mark.parametrize("change", ["retract", "route", "privacy"])
def test_development_cosupport_changes_stale_without_selecting_cosupport(
    tmp_path: Path,
    change: str,
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    first, other, derived = tuple(
        engine.capture.accept(
            TextPayload(f"synthetic {index}"), delivery_id=f"cosupport.{index}"
        ).capture_id
        for index in range(3)
    )
    engine.developments.bind(
        derived, (first, other), operation_id="cosupport.bind", authority=_owner(engine)
    )
    impact = engine.lifecycle.inspect_retraction(capture_id=first, authority=_owner(engine))
    assert impact.capture_ids == (first,)
    assert all(item.id != other for item in impact.items)
    if change == "retract":
        engine.lifecycle.retract(
            _request(engine, "capture_retract", {"capture_id": other}), authority=_owner(engine)
        )
    else:
        with engine._store.transaction() as connection:
            if change == "route":
                connection.execute(
                    "UPDATE logical_sources SET route_version=route_version+1 "
                    "WHERE head_capture_id=?",
                    (other,),
                )
            else:
                connection.execute(
                    "UPDATE source_revision_privacy SET effective_privacy_json="
                    "json_set(effective_privacy_json,'$.tier','public') WHERE capture_id=?",
                    (other,),
                )
    with pytest.raises(LifecycleError, match="revision_changed"):
        engine.lifecycle.inspect_retraction(
            capture_id=first,
            authority=_owner(engine),
            expected_preimage_sha256=impact.preimage_sha256,
        )


def test_canonical_coprovenance_is_freshness_evidence_not_a_selected_capture(
    tmp_path: Path,
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    first, other = tuple(
        engine.capture.accept(
            TextPayload(f"canonical member {index}"), delivery_id=f"canonical.member.{index}"
        ).capture_id
        for index in range(2)
    )
    page = "page_" + str(uuid4())
    with engine._store.transaction() as connection:
        connection.executemany(
            "INSERT INTO canonical_revision_members VALUES(?,?,?,?,?)",
            (
                ("revision_synthetic", page, "publication_synthetic", index, capture)
                for index, capture in enumerate((first, other))
            ),
        )
    impact = engine.lifecycle.inspect_retraction(capture_id=first, authority=_owner(engine))
    assert dict(impact.counts)["dependent_pages"] == 1
    assert impact.capture_ids == (first,)
    engine.lifecycle.retract(
        _request(engine, "capture_retract", {"capture_id": other}), authority=_owner(engine)
    )
    with pytest.raises(LifecycleError, match="revision_changed"):
        engine.lifecycle.inspect_retraction(
            capture_id=first,
            authority=_owner(engine),
            expected_preimage_sha256=impact.preimage_sha256,
        )


def test_session_append_invalidates_exact_membership_snapshot(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    owner = _owner(engine)
    segments = tuple(
        engine.lifecycle.prepare_segment(
            CaptureSubmission.for_local_owner(
                profile=engine.profile,
                payload=TextPayload(f"segment {index}"),
                delivery_id=f"append.{index}",
            ),
            authority=owner,
        )
        for index in range(2)
    )
    groups = tuple(
        GroupIntent(digest(f"group {index}"), digest(f"revision {index}"), None, (segment.binding,))
        for index, segment in enumerate(segments)
    )
    request = _request(engine, "consumer_import", {"groups": [group.value() for group in groups]})
    engine.lifecycle.begin(request, authority=owner)
    session = "capsess_" + str(uuid4())
    for index, (group, segment) in enumerate(zip(groups, segments, strict=True)):
        engine.lifecycle.stage_segment(request, group.group_key, segment, authority=owner)
        engine.lifecycle.activate_group(
            request, group.group_key, session_id=session, authority=owner
        )
        if index == 0:
            before = engine.lifecycle.inspect_retraction(session_id=session, authority=owner)
    engine.lifecycle.finish(request, authority=owner)
    assert before.capture_ids == (segments[0].identities.capture_id,)
    with pytest.raises(LifecycleError, match="revision_changed"):
        engine.lifecycle.inspect_retraction(
            session_id=session, authority=owner, expected_preimage_sha256=before.preimage_sha256
        )
    after = engine.lifecycle.inspect_retraction(session_id=session, authority=owner)
    assert after.membership_version == 2
    assert len(after.capture_ids) == 2


def test_retained_managed_provenance_and_inactive_reverse_links_are_impacts(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    space = engine.inbox.create_space("Synthetic", delivery_id="managed.space").space_id
    for index in range(2):
        engine.capture.accept(
            TextPayload(f"managed page {index}"),
            delivery_id=f"managed.{index}",
            action=CaptureAction.CANONICAL_NOTE,
            space_id=space,
        )
    selected = engine.capture.accept(TextPayload("retained dependency"), delivery_id="retained")
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    engine.managed_workspace.setup(str(workspace), operation_id="managed.setup")
    with engine._store.transaction() as connection:
        rows = connection.execute(
            "SELECT * FROM managed_note_revisions ORDER BY note_id"
        ).fetchall()
        first, second = rows
        parsed = parse_markdown(bytes(first["body_bytes"]))
        fields = dict(parsed.fields)
        fields["provenance"] = [selected.capture_id]
        body = render_markdown(fields=fields, body="retained synthetic content").encode("utf-8")
        connection.execute(
            """INSERT INTO managed_note_revisions
            SELECT ?,note_id,revision_id,'edit',?,?,accepted_by_actor_id,provenance_json,
                   privacy_json,recorded_at,? FROM managed_note_revisions WHERE revision_id=?""",
            (
                "revision_retained",
                body,
                sha256(body).hexdigest(),
                "managed.retained",
                first["revision_id"],
            ),
        )
        connection.execute(
            "INSERT INTO managed_links VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "link_synthetic",
                second["note_id"],
                second["revision_id"],
                first["note_id"],
                first["revision_id"],
                "source quote",
                "target quote",
                "[]",
                first["recorded_at"],
                0,
            ),
        )
    impact = engine.lifecycle.inspect_retraction(
        capture_id=selected.capture_id, authority=_owner(engine)
    )
    assert {item.id for item in impact.items if item.kind == "dependent_page"} == {
        first["note_id"],
        second["note_id"],
    }
    with closing(engine._store.connect()) as connection:
        co_capture = connection.execute(
            "SELECT capture_id FROM captures WHERE page_id=?", (second["note_id"],)
        ).fetchone()[0]
    engine.lifecycle.retract(
        _request(engine, "capture_retract", {"capture_id": co_capture}), authority=_owner(engine)
    )
    with pytest.raises(LifecycleError, match="revision_changed"):
        engine.lifecycle.inspect_retraction(
            capture_id=selected.capture_id,
            authority=_owner(engine),
            expected_preimage_sha256=impact.preimage_sha256,
        )
    impact = engine.lifecycle.inspect_retraction(
        capture_id=selected.capture_id, authority=_owner(engine)
    )
    with engine._store.transaction() as connection:
        connection.execute("UPDATE managed_links SET active=1 WHERE link_id='link_synthetic'")
    with pytest.raises(LifecycleError, match="revision_changed"):
        engine.lifecycle.inspect_retraction(
            capture_id=selected.capture_id,
            authority=_owner(engine),
            expected_preimage_sha256=impact.preimage_sha256,
        )

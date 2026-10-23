"""Retraction withholds quotes and summary, hides the item from clients, keeps it for the owner."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
from uuid import uuid4

import pytest
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine.followup_contracts import (
    EvidenceRole,
    FollowupError,
    ListFilter,
)
from open_brain_engine.engine.followup_visibility import project_item
from open_brain_engine.engine.records import RecordProjector

from packages.app.tests.unit.engine.test_followup_visibility import (
    _capture,
    _engine,
    _head_text,
    _owner,
    _propose,
    _ref,
    _session,
)
from packages.app.tests.unit.engine.test_followups import _new_source, _revise_source


def test_retraction_withholds_summary_and_quote_and_hides_from_clients(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    kept = _capture(engine, PrivacyTier.WORK, "Synthetic kept promise text")
    gone = _capture(engine, PrivacyTier.WORK, "PRIVATE_SYNTHETIC_RETRACTED promise text")
    refs = (_ref(engine, kept), _ref(engine, gone, role=EvidenceRole.SUPPORTING))
    item = _propose(engine, *refs)
    owner, session = _owner(engine), _session(PrivacyTier.WORK)

    shown, evidence, _decisions = engine.followups.show(item.item_id, authority=session)
    assert shown.summary == "Synthetic follow-up summary"
    assert shown.evidence_retracted is False
    assert [view.quote for view in evidence] == ["Synthetic", "PRIVATE_S"]
    assert {view.role for view in evidence} == {EvidenceRole.ORIGIN, EvidenceRole.SUPPORTING}

    preview = engine.lifecycle.preview_retraction(capture_id=gone, authority=owner)
    engine.lifecycle.commit_retraction(str(preview["preview_id"]), str(uuid4()), authority=owner)

    items, _cursor = engine.followups.list(ListFilter(), authority=session)
    assert item.item_id not in {row.item_id for row in items}
    with pytest.raises(FollowupError) as refused:
        engine.followups.show(item.item_id, authority=session)
    assert refused.value.code == "not_found"

    items, _cursor = engine.followups.list(ListFilter(), authority=owner)
    state_row = next(row for row in items if row.item_id == item.item_id)
    assert state_row.evidence_retracted is True
    assert state_row.summary is None
    shown, evidence, _decisions = engine.followups.show(item.item_id, authority=owner)
    assert shown.evidence_retracted is True and shown.summary is None
    by_capture = {view.capture_id: view.quote for view in evidence}
    assert by_capture == {kept: "Synthetic", gone: None}
    assert "PRIVATE_SYNTHETIC" not in repr((shown, evidence))


def test_project_item_returns_none_when_hidden_and_views_otherwise(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    personal = _capture(engine, PrivacyTier.PERSONAL, "Synthetic personal promise text")
    ref = _ref(engine, personal)
    item = _propose(engine, ref)
    with closing(engine._store.connect()) as connection:
        work = _session(PrivacyTier.WORK)
        assert (
            project_item(
                item,
                ("evidence-1",),
                (ref,),
                connection=connection,
                authority=work,
                projector=RecordProjector(engine.profile, connection, work),
            )
            is None
        )
        owner = _owner(engine)
        projected = project_item(
            item,
            ("evidence-1",),
            (ref,),
            connection=connection,
            authority=owner,
            projector=RecordProjector(engine.profile, connection, owner),
        )
    assert projected is not None
    shown, views = projected
    assert shown.summary == item.summary and shown.evidence_retracted is False
    assert len(views) == 1
    assert (views[0].evidence_id, views[0].capture_id, views[0].quote) == (
        "evidence-1",
        personal,
        "Synthetic",
    )
    assert views[0].status == "current"


def test_show_moves_the_quote_to_the_head_after_an_edit(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    source = _new_source(engine, "Synthetic promise: send the deck by Friday")
    ref = _ref(engine, source.head)
    item = _propose(engine, ref)
    original = _head_text(engine, source.head)[ref.quote_start : ref.quote_end]
    head = _revise_source(
        engine, source, "Edited intro added. Synthetic promise: send the deck by Friday"
    )
    head_text = _head_text(engine, head)
    for authority in (_session(PrivacyTier.WORK), _owner(engine)):
        shown, views, _history = engine.followups.show(item.item_id, authority=authority)
        assert shown.evidence_retracted is False and shown.summary == item.summary
        (view,) = views
        assert (view.status, view.quote, view.head_revision_id) == ("moved", original, head)
        assert head_text[view.head_quote_start : view.head_quote_end] == original
        assert (view.revision_id, view.capture_id) == (ref.revision_id, ref.capture_id)
    items, _cursor = engine.followups.list(ListFilter(), authority=_session(PrivacyTier.WORK))
    assert [entry.item_id for entry in items] == [item.item_id]


def test_show_marks_source_changed_without_text_for_clients(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    source = _new_source(engine, "Synthetic promise: send the deck by Friday")
    ref = _ref(engine, source.head)
    item = _propose(engine, ref)
    original = _head_text(engine, source.head)[ref.quote_start : ref.quote_end]
    _revise_source(engine, source, "Edited: the deck goes out on Monday instead")
    _item, (client_view,), _history = engine.followups.show(
        item.item_id, authority=_session(PrivacyTier.WORK)
    )
    assert (client_view.status, client_view.quote, client_view.head_revision_id) == (
        "source_changed",
        None,
        None,
    )
    owner_item, (owner_view,), _history = engine.followups.show(
        item.item_id, authority=_owner(engine)
    )
    assert (owner_view.status, owner_view.quote) == ("source_changed", original)
    assert owner_item.evidence_retracted is False and owner_item.summary == item.summary
    # Retraction of the cited revision still wins: both sessions lose the quote, the client
    # loses the item, the owner keeps a state row. (Retracting only the newer head leaves the
    # cited revision readable to the owner, so the cited capture itself is retracted here.)
    preview = engine.lifecycle.preview_retraction(
        capture_id=ref.capture_id, authority=_owner(engine)
    )
    engine.lifecycle.commit_retraction(
        str(preview["preview_id"]), str(uuid4()), authority=_owner(engine)
    )
    with pytest.raises(FollowupError, match="not_found"):
        engine.followups.show(item.item_id, authority=_session(PrivacyTier.WORK))
    state_row, (retracted_view,), _history = engine.followups.show(
        item.item_id, authority=_owner(engine)
    )
    assert (retracted_view.status, retracted_view.quote) == ("retracted", None)
    assert state_row.evidence_retracted is True and state_row.summary is None


def test_client_list_keeps_items_citing_edited_sources(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    moved = _new_source(engine, "Synthetic promise: send the deck by Friday")
    changed = _new_source(engine, "Synthetic promise: book the room for Tuesday")
    moved_item = _propose(engine, _ref(engine, moved.head))
    changed_item = _propose(engine, _ref(engine, changed.head))
    _revise_source(engine, moved, "Edited intro added. Synthetic promise: send the deck by Friday")
    _revise_source(engine, changed, "Edited: the room is no longer needed")
    session = _session(PrivacyTier.WORK)
    items, _cursor = engine.followups.list(ListFilter(), authority=session)
    by_id = {entry.item_id: entry for entry in items}
    assert set(by_id) == {moved_item.item_id, changed_item.item_id}
    for entry in by_id.values():
        assert entry.summary == "Synthetic follow-up summary"
        assert entry.evidence_retracted is False
    # The client never receives the cited older revision's text for the changed source.
    _item, (view,), _history = engine.followups.show(changed_item.item_id, authority=session)
    assert (view.status, view.quote) == ("source_changed", None)
    assert "book the room" not in repr(view)

"""Synthetic evidence for lifecycle tasks through real capture and read paths."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from open_brain_engine.engine import BrainEngine, CaptureAction, CaptureSubmission, TextPayload
from open_brain_engine.engine.capture_lifecycle import LifecycleSegment
from open_brain_engine.engine.capture_lifecycle_contracts import (
    GroupIntent,
    LifecycleError,
    LifecycleRequest,
    digest,
)
from open_brain_engine.engine.capture_lifecycle_policy import lifecycle_permits_capture
from open_brain_engine.engine.t03_contracts import (
    EffectiveAuthority,
    HistoryListRequest,
    RecordReadRequest,
    SearchPageRequest,
    T03Error,
)
from open_brain_engine.portable.v4 import canonical_revision_id

from open_brain.profile import compile_single_user_local, open_existing_single_user_local


def _owner(engine: BrainEngine) -> EffectiveAuthority:
    return EffectiveAuthority(
        engine.profile.owner_actor_id, "lifecycle-test", frozenset(), None, owner=True
    )


def _request(engine: BrainEngine, kind: str, payload: dict[str, object]) -> LifecycleRequest:
    with engine._store.connect() as connection:
        brain, epoch = connection.execute(
            "SELECT brain_id,issuer_epoch FROM brain_identity"
        ).fetchone()
    return LifecycleRequest(str(uuid4()), kind, brain, epoch, payload)


def _group(
    engine: BrainEngine,
    texts: tuple[str, ...],
    *,
    key: str | None = None,
    expected: str | None = None,
) -> tuple[LifecycleRequest, GroupIntent, tuple[LifecycleSegment, ...]]:
    owner = _owner(engine)
    segments = tuple(
        engine.lifecycle.prepare_segment(
            CaptureSubmission.for_local_owner(
                profile=engine.profile,
                payload=TextPayload(text),
                delivery_id="lifecycle." + str(uuid4()),
            ),
            authority=owner,
        )
        for text in texts
    )
    group = GroupIntent(
        key or digest(str(uuid4())),
        digest(list(texts)),
        expected,
        tuple(segment.binding for segment in segments),
    )
    request = _request(engine, "consumer_import", {"groups": [group.value()]})
    engine.lifecycle.begin(request, authority=owner)
    return request, group, segments


def _stage(
    engine: BrainEngine,
    request: LifecycleRequest,
    group: GroupIntent,
    segments: tuple[LifecycleSegment, ...],
) -> None:
    for segment in segments:
        engine.lifecycle.stage_segment(request, group.group_key, segment, authority=_owner(engine))


def _activate(engine: BrainEngine, request: LifecycleRequest, group: GroupIntent) -> str:
    session = "capsess_" + str(uuid4())
    engine.lifecycle.activate_group(
        request, group.group_key, session_id=session, authority=_owner(engine)
    )
    engine.lifecycle.finish(request, authority=_owner(engine))
    return session


def _history(engine: BrainEngine, capture: str) -> str:
    response = engine.history.read_history(
        RecordReadRequest(record_id=capture, expected_revision_id=capture), authority=_owner(engine)
    )
    content = response.to_wire()["content"]
    assert isinstance(content, dict)
    text = content["text"]
    assert isinstance(text, str)
    return text


def test_staging_is_hidden_across_restart_then_whole_group_activates(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    request, group, segments = _group(engine, ("stagequartz first", "stagequartz second"))
    _stage(engine, request, group, segments[:1])
    capture = segments[0].identities.capture_id
    with engine._store.connect() as connection:
        assert (
            connection.execute(
                "SELECT stage FROM captures WHERE capture_id=?", (capture,)
            ).fetchone()[0]
            == 3
        )
        assert (
            connection.execute(
                "SELECT s.historical_only FROM source_revisions r "
                "JOIN logical_sources s USING(source_id) WHERE r.capture_id=?",
                (capture,),
            ).fetchone()[0]
            == 1
        )
    assert engine.capture.get(capture) is None
    assert engine.retrieval.search("stagequartz") == ()
    assert engine.retrieval.fetch(capture) is None
    with pytest.raises(T03Error, match="not_found"):
        _history(engine, capture)
    with pytest.raises(LifecycleError, match="operation_pending"):
        _activate(engine, request, group)
    engine = BrainEngine.open(engine.profile)
    assert engine.retrieval.search("stagequartz") == ()
    _stage(engine, request, group, segments)
    _activate(engine, request, group)
    assert len(engine.retrieval.search("stagequartz")) == 2
    assert "stagequartz first" in _history(engine, capture)
    assert engine.capture.get(capture) is not None
    assert engine.tasks.lifecycle is engine.lifecycle


def test_replacement_keeps_previous_group_until_activation_and_history_after(
    tmp_path: Path,
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    first, group, old = _group(engine, ("oldtopaz one", "oldtopaz two"))
    _stage(engine, first, group, old)
    _activate(engine, first, group)
    second, replacement, new = _group(
        engine, ("newtopaz one",), key=group.group_key, expected=group.revision_sha256
    )
    _stage(engine, second, replacement, new)
    assert len(engine.retrieval.search("oldtopaz")) == 2
    assert engine.retrieval.search("newtopaz") == ()
    _activate(engine, second, replacement)
    assert engine.retrieval.search("oldtopaz") == ()
    assert len(engine.retrieval.search("newtopaz")) == 1
    assert "oldtopaz" in _history(engine, old[0].identities.capture_id)
    retract = _request(engine, "capture_retract", {"capture_id": old[0].identities.capture_id})
    engine.lifecycle.retract(retract, authority=_owner(engine))
    assert len(engine.retrieval.search("newtopaz")) == 1


def test_session_retraction_and_replay_survive_real_portable_roundtrip(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    request, group, segments = _group(engine, ("zircon retained evidence", "zircon sibling"))
    _stage(engine, request, group, segments)
    session = _activate(engine, request, group)
    capture = segments[0].identities.capture_id
    snapshot = engine.lifecycle.session_snapshot(session, authority=_owner(engine))
    retract = _request(engine, "session_retract", snapshot)
    receipt = engine.lifecycle.retract(retract, authority=_owner(engine))
    assert engine.lifecycle.retract(retract, authority=_owner(engine)) == receipt
    assert engine.retrieval.search("zircon") == ()
    assert engine.retrieval.fetch(capture) is None
    assert "zircon" in _history(engine, capture)
    archive, destination = tmp_path / "archive", tmp_path / "restored"
    assert (
        engine.portability.export(archive, export_id="export_" + str(uuid4())).schema_version == 14
    )
    engine.portability.import_clean(archive, destination, import_id="import_" + str(uuid4()))
    restored = BrainEngine.open(open_existing_single_user_local(destination))
    assert restored.lifecycle.retract(retract, authority=_owner(restored)) == receipt
    assert restored.retrieval.search("zircon") == ()
    assert "zircon" in _history(restored, capture)


def test_staged_delivery_cannot_bypass_ingestion_and_halt_never_publishes(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    request, group, segments = _group(engine, ("opal staged",))
    _stage(engine, request, group, segments)
    with pytest.raises(LifecycleError, match="operation_conflict"):
        engine.capture.submit(segments[0].envelope.submission)
    engine.lifecycle.halt(request, authority=_owner(engine))
    engine = BrainEngine.open(engine.profile)
    capture = segments[0].identities.capture_id
    with engine._store.connect() as connection:
        assert not lifecycle_permits_capture(connection, capture, history=True)
    assert engine.retrieval.search("opal") == ()
    with pytest.raises(T03Error, match="not_found"):
        _history(engine, capture)


def test_authority_and_existing_capture_binding_refuse_without_journal_write(
    tmp_path: Path,
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    owner = _owner(engine)
    with pytest.raises(LifecycleError):
        engine.lifecycle.begin(
            _request(engine, "consumer_import", {"groups": []}),
            authority=replace(owner, owner=False),
        )
    ordinary = engine.capture.accept(TextPayload("legacy amber"), delivery_id="ordinary")
    request, group, segments = _group(engine, ("sapphire pending",))
    forged = replace(
        group, segments=(replace(segments[0].binding, capture_id=ordinary.capture_id),)
    )
    with pytest.raises(LifecycleError, match="operation_conflict"):
        engine.lifecycle.begin(
            _request(engine, "consumer_import", {"groups": [forged.value()]}), authority=owner
        )
    assert len(engine.retrieval.search("amber")) == 1
    assert (
        engine.retrieval.search_page(
            SearchPageRequest(query="sapphire"), authority=owner
        ).to_wire()["results"]
        == []
    )


def test_durable_custody_recovers_original_reservation_after_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    request, group, segments = _group(engine, ("peridot recovery",))

    def interrupted(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic interruption")

    with monkeypatch.context() as failure:
        failure.setattr(engine, "_materialize_capture_locked", interrupted)
        with pytest.raises(RuntimeError, match="synthetic interruption"):
            _stage(engine, request, group, segments)
    reopened = BrainEngine.open(engine.profile)
    reopened.lifecycle.recover_pending(request, authority=_owner(reopened))
    with reopened._store.connect() as connection:
        row = connection.execute(
            "SELECT capture_id,accepted_receipt_id,accepted_at FROM captures"
        ).fetchone()
        ids = segments[0].identities
        assert tuple(row) == (ids.capture_id, ids.accepted_receipt_id, ids.accepted_at)
    assert reopened.retrieval.search("peridot") == ()
    _activate(reopened, request, group)
    assert len(reopened.retrieval.search("peridot")) == 1


def test_activation_rolls_back_entire_group_when_index_publication_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    request, group, segments = _group(engine, ("moonstone first", "moonstone second"))
    _stage(engine, request, group, segments)
    original = engine._upsert_source_search
    calls = 0

    def interrupted(connection: sqlite3.Connection, row: sqlite3.Row) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic index interruption")
        original(connection, row)

    session = "capsess_" + str(uuid4())
    with monkeypatch.context() as failure:
        failure.setattr(engine, "_upsert_source_search", interrupted)
        with pytest.raises(RuntimeError, match="synthetic index interruption"):
            engine.lifecycle.activate_group(
                request, group.group_key, session_id=session, authority=_owner(engine)
            )
    assert engine.retrieval.search("moonstone") == ()
    with engine._store.connect() as connection:
        assert connection.execute("SELECT count(*) FROM source_revisions").fetchone()[0] == 2
        assert (
            connection.execute(
                "SELECT count(*) FROM logical_sources WHERE historical_only=0"
            ).fetchone()[0]
            == 0
        )
    engine.lifecycle.activate_group(
        request, group.group_key, session_id=session, authority=_owner(engine)
    )
    engine.lifecycle.finish(request, authority=_owner(engine))
    assert len(engine.retrieval.search("moonstone")) == 2


def test_retracted_canonical_owner_history_uses_retained_evidence_after_restore(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    receipt = engine.capture.accept(
        TextPayload("alexandrite retained canonical"),
        delivery_id="canonical.lifecycle",
        action=CaptureAction.CANONICAL_NOTE,
        space_id=engine.inbox.create_space("Synthetic", delivery_id="lifecycle.space").space_id,
    )
    with engine._store.connect() as connection:
        page, publication = connection.execute(
            "SELECT page_id,publication_id FROM captures WHERE capture_id=?", (receipt.capture_id,)
        ).fetchone()
    request = RecordReadRequest(
        record_id=page, expected_revision_id=canonical_revision_id(publication)
    )
    assert engine.history.read_history(request, authority=_owner(engine))
    tombstone = _request(engine, "capture_retract", {"capture_id": receipt.capture_id})
    engine.lifecycle.retract(tombstone, authority=_owner(engine))
    for _ in range(2):
        assert engine.retrieval.search("alexandrite") == ()
        assert engine.retrieval.fetch(page) is None
        response = engine.history.read_history(request, authority=_owner(engine)).to_wire()
        assert "alexandrite" in str(response["content"])
        entries = engine.history.list_history(
            HistoryListRequest(record_id=page), authority=_owner(engine)
        ).to_wire()
        values = entries["entries"]
        assert isinstance(values, list)
        assert not values[0]["is_current"]
        engine = BrainEngine.open(engine.profile)
        engine._rederive_live_search_projection()
    archive, destination = tmp_path / "archive", tmp_path / "restored"
    engine.portability.export(archive, export_id="export_" + str(uuid4()))
    engine.portability.import_clean(archive, destination, import_id="import_" + str(uuid4()))
    restored = BrainEngine.open(open_existing_single_user_local(destination))
    assert restored.retrieval.search("alexandrite") == ()
    assert restored.history.read_history(request, authority=_owner(restored))


def test_pending_and_retracted_capture_refuse_legacy_proposal_and_enrichment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from open_brain_engine.engine.contracts import EnrichmentRequest, ProposalDraft

    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    request, group, segments = _group(engine, ("tourmaline hidden evidence",))
    _stage(engine, request, group, segments)
    capture = segments[0].identities.capture_id
    calls: list[str] = []

    class SyntheticEnrichment:
        def enrich(self, value: EnrichmentRequest) -> tuple[ProposalDraft, ...]:
            calls.append(value.source_text)
            return (ProposalDraft("Synthetic", "Synthetic proposal"),)

    monkeypatch.setattr(engine, "_enrichment_provider", SyntheticEnrichment())
    for state in ("pending", "retracted"):
        with pytest.raises(T03Error, match="not_found"):
            engine.capture.retry_enrichment(capture, delivery_id="hidden.enrich." + state)
        with pytest.raises(T03Error, match="not_found"):
            engine.review.propose(
                capture,
                (ProposalDraft("Synthetic", "Synthetic proposal"),),
                delivery_id="hidden.propose." + state,
            )
        assert calls == []
        if state == "pending":
            _activate(engine, request, group)
            tombstone = _request(engine, "capture_retract", {"capture_id": capture})
            engine.lifecycle.retract(tombstone, authority=_owner(engine))

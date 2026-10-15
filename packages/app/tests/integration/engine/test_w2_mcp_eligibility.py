"""Synthetic end-to-end eligibility for implicit MCP and derived evidence."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import CaptureAction, DecisionOutcome, ProposalDraft, TextPayload
from open_brain_engine.engine.mcp_eligibility import capture_visible, record_visible
from open_brain_engine.engine.paging import authority_binding, generations
from open_brain_engine.engine.t03_contracts import (
    HistoryListRequest,
    RecordReadRequest,
    SearchPageRequest,
    SessionOrigin,
    SourceRouteRequest,
    T03Error,
)

from packages.app.tests.integration.engine.test_slack_development_evidence import (
    _authority,
    _derived,
    _engine,
    _raw,
)
from packages.app.tests.integration.engine.test_slack_sharing import _single


@pytest.mark.parametrize("owner", [False, True])
def test_implicit_mcp_filters_original_copy_and_canonical_before_search_and_reads(
    tmp_path: Path,
    owner: bool,
) -> None:
    journey = _single(tmp_path)
    raw = journey.sources()[0]["head_capture_id"]
    copy = journey.approve(journey.sources()[0])[3].copy_capture_id
    proposal = journey.tasks.review.propose(
        (copy,),
        (ProposalDraft("Onboarding rationale", "Onboarding derived rationale"),),
        delivery_id="w2.propose",
    )[0]
    decided = journey.tasks.review.decide(
        proposal.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="w2.approve",
        expected_review_digest=proposal.review_digest,
    )
    ordinary = journey.tasks.capture.accept(
        TextPayload("Onboarding independent owner control"),
        delivery_id="w2.control",
        privacy_tier=PrivacyTier.WORK,
    )
    implicit = replace(
        journey.owner,
        owner=owner,
        session_origin=SessionOrigin.MCP_IMPLICIT,
        capabilities=journey.owner.capabilities | {"search", "content-read", "history-read"},
    )
    retrieval = journey.tasks.retrieval.with_authority(implicit)
    assert {item.capture_id for item in retrieval.search("onboarding", limit=1)} == {
        ordinary.capture_id
    }
    assert {
        row["record_id"]
        for row in journey.tasks.retrieval.search_page(
            SearchPageRequest(query="onboarding", limit=1),
            authority=implicit,
        ).to_wire()["results"]
    } == {ordinary.capture_id}
    for identity in (raw, copy):
        assert retrieval.fetch(identity) is None
        assert retrieval.read_page(identity) is None
        with pytest.raises(T03Error, match="not_found"):
            journey.tasks.retrieval.read_record(
                RecordReadRequest(
                    record_id=identity, expected_revision_id="capture_" + str(uuid4())
                ),
                authority=implicit,
            )
        with pytest.raises(T03Error, match="not_found"):
            journey.tasks.history.list_history(
                HistoryListRequest(record_id=identity), authority=implicit
            )
    assert retrieval.fetch(decided.page_id) is None
    explicit = replace(implicit, session_origin=SessionOrigin.MCP_POLICY)
    assert journey.tasks.retrieval.with_authority(explicit).fetch(raw) is not None
    assert journey.tasks.retrieval.with_authority(journey.owner).fetch(raw) is not None
    assert authority_binding(implicit) != authority_binding(explicit)


def test_implicit_auxiliary_guards_proposals_inbox_and_mutation_replay(tmp_path: Path) -> None:
    journey = _single(tmp_path)
    raw = journey.sources()[0]["head_capture_id"]
    ordinary = journey.tasks.capture.accept(
        TextPayload("Independent inbox"), delivery_id="w2.inbox"
    )
    proposal = journey.tasks.review.propose(
        (raw,),
        (ProposalDraft("Protected draft", "Protected source rationale"),),
        delivery_id="w2.protected.proposal",
    )[0]
    implicit = replace(journey.owner, session_origin=SessionOrigin.MCP_IMPLICIT)
    inbox = journey.tasks.inbox.with_authority(implicit)
    assert [item.capture_id for item in inbox.list(limit=1)] == [ordinary.capture_id]
    with pytest.raises(T03Error, match="not_found"):
        inbox.route(raw, journey.space, delivery_id="w2.denied.route")
    review = journey.tasks.review.with_authority(implicit)
    assert review.list(limit=1) == ()
    with pytest.raises(ValueError, match="unknown proposal"):
        review.show(proposal.proposal_id)
    with pytest.raises(ValueError, match="unknown proposal"):
        review.decide(
            proposal.proposal_id, DecisionOutcome.REJECTED, delivery_id="w2.denied.reject"
        )
    source = journey.sources()[0]
    with pytest.raises(T03Error, match="not_found"):
        journey.tasks.sources.route(
            SourceRouteRequest(
                source_id=source["source_id"],
                space_id=journey.space,
                expected_head=raw,
                expected_route_version=source["route_version"],
                operation_id="operation_" + str(uuid4()),
            ),
            authority=implicit,
        )


def test_derived_development_closure_does_not_launder_slack_work(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    raw = _raw(engine, 1)
    first, second = _derived(engine, "first"), _derived(engine, "second")
    engine.developments.bind(first, (raw,), operation_id="w2.bind.first", authority=_authority())
    engine.developments.bind(
        second,
        (first,),
        operation_id="w2.bind.second",
        authority=_authority(),
    )
    authority = replace(_authority(), session_origin=SessionOrigin.MCP_IMPLICIT)
    with engine._store.connect() as connection:
        assert not capture_visible(connection, raw, authority)
        assert not capture_visible(connection, second, authority)
    assert engine.retrieval.with_authority(authority).search("Synthetic") == ()


def test_workspace_snapshot_counts_and_retained_lineage_are_gated(tmp_path: Path) -> None:
    journey = _single(tmp_path)
    copy = journey.approve(journey.sources()[0])[3].copy_capture_id
    proposal = journey.tasks.review.propose(
        (copy,),
        (ProposalDraft("Slack graph", "Protected graph rationale"),),
        delivery_id="w2.graph.propose",
    )[0]
    decided = journey.tasks.review.decide(
        proposal.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="w2.graph.approve",
        expected_review_digest=proposal.review_digest,
    )
    control = journey.tasks.capture.accept(
        TextPayload("Independent graph"),
        action=CaptureAction.CANONICAL_NOTE,
        space_id=journey.space,
        delivery_id="w2.graph.control",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    setup = journey.tasks.managed_workspace.setup(str(workspace), operation_id="w2.graph.setup")
    implicit = replace(journey.owner, session_origin=SessionOrigin.MCP_IMPLICIT)
    bound = journey.tasks.managed_workspace.with_authority(implicit)
    snapshot = bound.graph_snapshot(setup.workspace_id)
    assert len(snapshot.sources) == 1
    assert snapshot.sources[0].note_id != decided.page_id
    assert bound.status().active_notes == 1
    assert not bound.graph_note_visible(decided.page_id)
    assert journey.tasks.managed_workspace.status().active_notes == 2
    before_status = bound.status()
    before_snapshot = bound.graph_snapshot(setup.workspace_id)
    journey.tasks.managed_policy.set_exclusion(
        setup.workspace_id,
        "note",
        decided.page_id,
        excluded=True,
        operation_id="w2.hidden.graph.exclude",
    )
    assert bound.status() == before_status
    assert bound.graph_snapshot(setup.workspace_id) == before_snapshot
    assert (
        journey.tasks.managed_inference.with_authority(implicit).suggestions(setup.workspace_id)
        == ()
    )
    assert control.capture_id
    with journey.tasks.retrieval._engine._store.connect() as connection:
        assert not record_visible(connection, "canonical", decided.page_id, implicit)


def test_hidden_approvals_do_not_change_visible_generation(tmp_path: Path) -> None:
    journey = _single(tmp_path)
    implicit = replace(journey.owner, session_origin=SessionOrigin.MCP_IMPLICIT)
    engine = journey.tasks.retrieval._engine
    with engine._store.connect() as connection:
        before = generations(connection, profile=engine.profile, authority=implicit)
    journey.approve(journey.sources()[0])
    with engine._store.connect() as connection:
        after = generations(connection, profile=engine.profile, authority=implicit)
    assert before == after


def test_retained_slack_revisions_and_mixed_canonical_cannot_escape(tmp_path: Path) -> None:
    from open_brain.services.t03_adapters import owner_authority

    engine = _engine(tmp_path / "brain")
    old = _raw(engine, 1)
    head = _raw(engine, 2, old)
    engine.inbox.route(head, engine.inbox.spaces()[0].space_id, delivery_id="w2.raw.route")
    ordinary = _derived(engine, "mixed-control")
    owner = owner_authority(engine.tasks, session_id="w2.retained.owner")
    proposal = engine.review.propose(
        (ordinary, head),
        (ProposalDraft("Mixed rationale", "Mixed retained evidence"),),
        delivery_id="w2.mixed.propose",
    )[0]
    decided = engine.review.decide(
        proposal.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="w2.mixed.approve",
        expected_review_digest=proposal.review_digest,
    )
    assert decided.page_id is not None
    implicit = replace(owner, session_origin=SessionOrigin.MCP_IMPLICIT)
    assert engine.retrieval.with_authority(implicit).fetch(ordinary) is not None
    assert engine.retrieval.with_authority(implicit).fetch(decided.page_id) is None
    with pytest.raises(T03Error, match="not_found"):
        engine.history.read_history(
            RecordReadRequest(record_id=head, expected_revision_id=old), authority=implicit
        )
    assert engine.history.read_history(
        RecordReadRequest(record_id=head, expected_revision_id=old), authority=owner
    )
    with pytest.raises(T03Error, match="not_found"):
        engine.review.with_authority(implicit).propose(
            (ordinary,),
            (ProposalDraft("Update", "Owner independent update"),),
            target_page_id=decided.page_id,
            delivery_id="w2.denied.update",
        )


def test_facet_assignment_history_and_cursor_exclude_hidden_changes(tmp_path: Path) -> None:
    from open_brain_engine.engine import facet_contracts as fc

    journey = _single(tmp_path)
    facets = journey.tasks.facets
    owner = journey.owner
    implicit = replace(owner, session_origin=SessionOrigin.MCP_IMPLICIT)
    dimension = facets.dimension_create(
        fc.DimensionCreateRequest(operation_id="w2.dim.one", expected_version=0, name="One"),
        authority=owner,
    ).to_wire()["subject_id"]
    facets.dimension_create(
        fc.DimensionCreateRequest(operation_id="w2.dim.two", expected_version=0, name="Two"),
        authority=owner,
    )
    value = facets.value_create(
        fc.ValueCreateRequest(
            operation_id="w2.value",
            expected_version=0,
            expected_dimension_version=1,
            dimension_id=dimension,
            name="Chosen",
        ),
        authority=owner,
    ).to_wire()["subject_id"]
    raw = journey.sources()[0]["head_capture_id"]
    patch = fc.AssignmentsPatchRequest(
        operation_id="w2.assign.raw",
        expected_version=0,
        record_id=raw,
        expected_revision_id=raw,
        add_value_ids=[value],
    )
    facets.assignments_patch(patch, authority=owner)
    first = facets.dimensions_list(fc.DimensionsListRequest(limit=1), authority=implicit).to_wire()
    assert first["next_cursor"]
    # A hidden approval changes global retrieval counters but not the visible facet commitment.
    journey.approve(journey.sources()[0])
    continued = facets.dimensions_list(
        fc.DimensionsListRequest(limit=1, cursor=first["next_cursor"]), authority=implicit
    ).to_wire()
    assert len(continued["items"]) == 1 and continued["complete"]
    with pytest.raises(fc.FacetError, match="not_found"):
        facets.assignments_get(fc.AssignmentsGetRequest(record_id=raw), authority=implicit)
    with pytest.raises(fc.FacetError, match="not_found"):
        facets.assignments_patch(patch, authority=implicit)  # replay must reauthorize
    source_id = journey.sources()[0]["source_id"]
    with pytest.raises(fc.FacetError, match="not_found"):
        facets.history_list(
            fc.HistoryListRequest(subject={"kind": "source", "id": source_id}), authority=implicit
        )
    query = fc.QueryPageRequest(
        selector={
            "all_value_ids": [value],
            "space_ids": [],
            "payload_families": [],
            "record_types": [],
        },
        limit=1,
    )
    assert facets.query_page(query, authority=implicit).to_wire()["items"] == []


def test_selection_descriptor_requires_visible_anchor(tmp_path: Path) -> None:
    from open_brain_engine.engine.sharing_contracts import SlackSelectionCreateRequest

    journey = _single(tmp_path)
    source = journey.sources()[0]
    selection = journey.tasks.sharing.create_selection(
        SlackSelectionCreateRequest(
            operation_id="w2.selection",
            brain_id=journey.brain_id,
            issuer_epoch=journey.epoch,
            destination_space_id=journey.space,
            member_source_ids=(source["source_id"],),
        ),
        authority=journey.owner,
    )
    copy = journey.approve(source, selection["selection_id"])[3].copy_capture_id
    implicit = replace(journey.owner, session_origin=SessionOrigin.MCP_IMPLICIT)
    with pytest.raises(T03Error, match="not_found"):
        journey.tasks.retrieval.read_record(
            RecordReadRequest(record_id=copy, expected_revision_id=copy), authority=implicit
        )
    explicit = replace(implicit, session_origin=SessionOrigin.MCP_POLICY)
    response = journey.tasks.retrieval.read_record(
        RecordReadRequest(record_id=copy, expected_revision_id=copy), authority=explicit
    ).to_wire()
    assert response["selection"]["selection_id"] == selection["selection_id"]


def test_stale_suggestion_endpoint_is_hidden_even_after_note_deactivation(tmp_path: Path) -> None:
    from packages.app.tests.integration.engine.test_managed_inference import _grant, _prepare

    journey = _single(tmp_path)
    copy = journey.approve(journey.sources()[0])[3].copy_capture_id
    proposal = journey.tasks.review.propose(
        (copy,), (ProposalDraft("Protected graph", "Secret graph"),), delivery_id="w2.stale.propose"
    )[0]
    protected = journey.tasks.review.decide(
        proposal.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="w2.stale.approve",
        expected_review_digest=proposal.review_digest,
    ).page_id
    for index in range(2):
        journey.tasks.capture.accept(
            TextPayload(f"Independent graph control {index}"),
            action=CaptureAction.CANONICAL_NOTE,
            space_id=journey.space,
            delivery_id=f"w2.stale.{index}",
        )
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    setup = journey.tasks.managed_workspace.setup(str(workspace), operation_id="w2.stale.setup")
    engine = journey.tasks.retrieval._engine
    ordinary = tuple(
        source.note_id
        for source in engine.managed_workspace.graph_snapshot(setup.workspace_id).sources
        if source.note_id != protected
    )
    _grant(engine, setup.workspace_id)
    request_id = "request_" + str(uuid4())
    _prepare(engine, setup.workspace_id, ordinary, request_id)
    engine.managed_inference.release(request_id)
    suggestion = engine.managed_inference.record_suggestion(
        request_id,
        source_note_id=ordinary[0],
        target_note_id=ordinary[1],
        source_quote="Independent graph",
        target_quote="Independent graph",
        model="synthetic-w2",
    )
    # Synthetic retained suggestion: inactive endpoints still carry their original lineage.
    with engine._store.transaction() as connection:
        revision = connection.execute(
            "SELECT accepted_revision_id FROM managed_notes WHERE note_id=?", (protected,)
        ).fetchone()[0]
        connection.execute(
            "UPDATE managed_suggestions SET target_note_id=?,target_revision_id=?,"
            "target_quote='Secret graph' WHERE suggestion_id=?",
            (protected, revision, suggestion.suggestion_id),
        )
        connection.execute("UPDATE managed_notes SET active=0 WHERE note_id=?", (protected,))
    assert engine.managed_inference.suggestions(setup.workspace_id)
    implicit = replace(journey.owner, session_origin=SessionOrigin.MCP_IMPLICIT)
    assert engine.managed_inference.with_authority(implicit).suggestions(setup.workspace_id) == ()
    assert engine.managed_workspace.with_authority(implicit).status().pending_suggestions == 0


@pytest.mark.parametrize(
    "outcome", [DecisionOutcome.APPROVED, DecisionOutcome.REJECTED, DecisionOutcome.EDITED]
)
def test_review_terminal_replays_recheck_protected_evidence(
    tmp_path: Path,
    outcome: DecisionOutcome,
) -> None:
    journey = _single(tmp_path)
    raw = journey.sources()[0]["head_capture_id"]
    proposal = journey.tasks.review.propose(
        (raw,),
        (ProposalDraft("Protected source", "Protected derived publication"),),
        delivery_id="w2.replay.propose",
    )[0]
    edited = "Protected edited publication" if outcome is DecisionOutcome.EDITED else None
    receipt = journey.tasks.review.decide(
        proposal.proposal_id,
        outcome,
        delivery_id="w2.replay.decide",
        edited_markdown=edited,
        expected_review_digest=proposal.review_digest,
    )
    implicit = replace(journey.owner, session_origin=SessionOrigin.MCP_IMPLICIT)
    with pytest.raises(ValueError, match="unknown proposal"):
        journey.tasks.review.with_authority(implicit).decide(
            proposal.proposal_id,
            outcome,
            delivery_id="w2.replay.decide",
            edited_markdown=edited,
            expected_review_digest=proposal.review_digest,
        )
    assert journey.tasks.review.with_authority(journey.owner).show(proposal.proposal_id)
    assert receipt.proposal_id == proposal.proposal_id


def test_implicit_mcp_reads_ordinary_canonical_current_and_retained_history(tmp_path: Path) -> None:
    from packages.app.tests.integration.services.test_local_mcp import (
        INITIALIZE,
        _call,
        _exchange,
        _start,
    )

    engine = _engine(tmp_path / "brain")
    source = _derived(engine, "ordinary-canonical-source")
    page_id: str | None = None
    for index, body in enumerate(
        ("Retained ordinary canonical body", "Current ordinary canonical body")
    ):
        proposal = engine.review.propose(
            (source,),
            (ProposalDraft("Ordinary canonical", body),),
            target_page_id=page_id,
            delivery_id=f"w2.canonical.propose.{index}",
        )[0]
        decision = engine.review.decide(
            proposal.proposal_id,
            DecisionOutcome.APPROVED,
            delivery_id=f"w2.canonical.approve.{index}",
            expected_review_digest=proposal.review_digest,
        )
        page_id = decision.page_id
    assert page_id is not None
    child = _start(engine.profile.root, "--allow-content-read", "--allow-history-read")
    try:
        assert "result" in _exchange(child, INITIALIZE)
        listed = _exchange(
            child,
            _call(
                "brain_history_list",
                {
                    "dto_version": 1,
                    "record_id": page_id,
                },
            ),
        )["result"]["structuredContent"]
        current, retained = listed["entries"]
        for tool, revision, expected in (
            ("brain_read", current["revision_id"], "Current ordinary canonical body"),
            ("brain_history_show", retained["revision_id"], "Retained ordinary canonical body"),
        ):
            response = _exchange(
                child,
                _call(
                    tool,
                    {
                        "dto_version": 1,
                        "record_id": page_id,
                        "expected_revision_id": revision,
                    },
                ),
            )
            assert "structuredContent" in response["result"], response
            shown = response["result"]["structuredContent"]
            assert shown["record"]["record_type"] == "canonical"
            assert shown["record"]["revision_id"] == revision
            assert expected in shown["content"]["text"]
            assert "selection" not in shown
    finally:
        if child.stdin is not None and not child.stdin.closed:
            child.stdin.close()
        if child.poll() is None:
            child.wait(timeout=10)
        assert child.stderr is not None and child.stderr.read() == ""

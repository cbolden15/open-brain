"""Bound multi-source review/publication engine behavior."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest
from open_brain_engine.engine import (
    BrainEngine,
    CaptureAction,
    CaptureFault,
    DecisionOutcome,
    InjectedFault,
    ProposalDraft,
    ReferencePayload,
    TextPayload,
)
from open_brain_engine.engine import review_bound as review_bound_module
from open_brain_engine.engine.normalization import _new_id

from open_brain.profile import compile_single_user_local


def _engine(root: Path, *, faults: set[CaptureFault] | None = None) -> BrainEngine:
    return BrainEngine.open(
        compile_single_user_local(root, starter_spaces=("Notes",)),
        faults=faults or set(),
    )


def _sources(engine: BrainEngine) -> tuple[str, str]:
    space_id = engine.inbox.spaces()[0].space_id
    first = engine.capture.accept(
        ReferencePayload("https://private.example.test/a", "First synthetic source"),
        delivery_id="delivery.bound.source.first",
        space_id=space_id,
    )
    second = engine.capture.accept(
        TextPayload("Second synthetic source"),
        delivery_id="delivery.bound.source.second",
        space_id=space_id,
    )
    return first.capture_id, second.capture_id


def _text_sources(engine: BrainEngine, count: int, *, prefix: str) -> tuple[str, ...]:
    space_id = engine.inbox.spaces()[0].space_id
    return tuple(
        engine.capture.accept(
            TextPayload(f"Synthetic {prefix} source {index}"),
            delivery_id=f"delivery.{prefix}.source.{index}",
            space_id=space_id,
        ).capture_id
        for index in range(count)
    )


def test_bound_proposal_freezes_multi_source_review_context(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    engine = _engine(root)
    first, second = _sources(engine)

    proposal = engine.review.propose(
        (first, second),
        (ProposalDraft("Combined", "Draft cites https://private.example.test/a"),),
        delivery_id="delivery.bound.proposal",
    )[0]
    shown = engine.review.show(proposal.proposal_id)

    assert proposal.capture_ids == (first, second)
    assert proposal.selected_capture_ids == (first, second)
    assert proposal.review_digest == shown.review_digest
    assert shown.capture_ids == (first, second)
    assert shown.selected_capture_ids == (first, second)
    assert shown.operation == "create"
    assert shown.target_page_id is None
    assert shown.projection_applied is True
    assert "private.example.test" not in shown.markdown
    assert tuple(item.capture_id for item in shown.evidence) == (first, second)
    assert all(
        item.sha256 == sha256(item.excerpt.encode("utf-8")).hexdigest() for item in shown.evidence
    )
    assert len(tuple((root / "history" / "review-bindings").rglob("*.json"))) == 1


def test_bound_decision_requires_exact_review_digest(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    first, second = _sources(engine)
    proposal = engine.review.propose(
        (first, second),
        (ProposalDraft("Combined", "Synthetic combined draft"),),
        delivery_id="delivery.bound.digest.proposal",
    )[0]

    with pytest.raises(ValueError, match="review digest"):
        engine.review.decide(
            proposal.proposal_id,
            DecisionOutcome.APPROVED,
            delivery_id="delivery.bound.digest.missing",
        )
    with pytest.raises(ValueError, match="review digest"):
        engine.review.decide(
            proposal.proposal_id,
            DecisionOutcome.APPROVED,
            delivery_id="delivery.bound.digest.stale",
            expected_review_digest="0" * 64,
        )

    decided = engine.review.decide(
        proposal.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="delivery.bound.digest.approve",
        expected_review_digest=proposal.review_digest,
    )
    assert decided.publication_id is not None


@pytest.mark.parametrize("over_limit", (False, True))
def test_bound_edit_enforces_utf8_byte_limit_before_reservation(
    tmp_path: Path, over_limit: bool
) -> None:
    engine = _engine(tmp_path / "brain")
    first, _ = _sources(engine)
    proposal = engine.review.propose(
        (first,), (ProposalDraft("Edit", "Original"),), delivery_id="edit.proposal"
    )[0]
    body = "🌿" * (16384 + int(over_limit))
    if over_limit:
        with pytest.raises(ValueError, match="review markdown limit"):
            engine.review.decide(
                proposal.proposal_id,
                DecisionOutcome.EDITED,
                delivery_id="edit.decision",
                edited_markdown=body,
                expected_review_digest=proposal.review_digest,
            )
        assert engine.review.show(proposal.proposal_id).status == "pending"
    else:
        decision = engine.review.decide(
            proposal.proposal_id,
            DecisionOutcome.EDITED,
            delivery_id="edit.decision",
            edited_markdown=body,
            expected_review_digest=proposal.review_digest,
        )
        assert decision.publication_id is not None


def test_terminal_bound_decision_does_not_claim_an_unreserved_retry_key(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    first, _ = _sources(engine)
    first_proposal, second_proposal = engine.review.propose(
        (first,),
        (ProposalDraft("One", "First"), ProposalDraft("Two", "Second")),
        delivery_id="retry-key.proposals",
    )
    engine.review.decide(
        first_proposal.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="retry-key.first",
        expected_review_digest=first_proposal.review_digest,
    )
    with pytest.raises(ValueError, match="terminal decision"):
        engine.review.decide(
            first_proposal.proposal_id,
            DecisionOutcome.APPROVED,
            delivery_id="retry-key.second",
            expected_review_digest=first_proposal.review_digest,
        )
    with pytest.raises(ValueError, match="conflicting delivery"):
        engine.review.decide(
            first_proposal.proposal_id,
            DecisionOutcome.APPROVED,
            delivery_id="retry-key.first",
            expected_review_digest="0" * 64,
        )
    decision = engine.review.decide(
        second_proposal.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="retry-key.second",
        expected_review_digest=second_proposal.review_digest,
    )
    assert decision.page_id == second_proposal.page_id


@pytest.mark.parametrize("update", (False, True))
def test_manual_exact_proposed_page_cannot_bypass_predecessor_check(
    tmp_path: Path, update: bool
) -> None:
    root = tmp_path / "brain"
    engine = _engine(root)
    first, _ = _sources(engine)
    proposal = engine.review.propose(
        (first,), (ProposalDraft("Manual target", "First"),), delivery_id="manual.create"
    )[0]
    if update:
        engine.review.decide(
            proposal.proposal_id,
            DecisionOutcome.APPROVED,
            delivery_id="manual.first",
            expected_review_digest=proposal.review_digest,
        )
        proposal = engine.review.propose(
            (first,),
            (ProposalDraft("Manual target", "Second"),),
            delivery_id="manual.update",
            target_page_id=proposal.page_id,
        )[0]
    row = engine._proposal_row(proposal.proposal_id)
    page = root / row["canonical_path"]
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_bytes(row["proposed_bytes"])
    history = {p: p.read_bytes() for p in (root / "history").rglob("*.json")}
    with pytest.raises(ValueError, match="canonical page revision conflict"):
        engine.review.decide(
            proposal.proposal_id,
            DecisionOutcome.APPROVED,
            delivery_id="manual.conflict",
            expected_review_digest=proposal.review_digest,
        )
    assert engine.review.show(proposal.proposal_id).status == "pending"
    assert {p: p.read_bytes() for p in (root / "history").rglob("*.json")} == history


def test_review_list_projects_title_against_all_source_references(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    space = engine.inbox.spaces()[0]
    references = ("https://first.example.test/private", "https://second.example.test/private")
    sources = tuple(
        engine.capture.accept(
            ReferencePayload(reference, "Synthetic evidence"),
            delivery_id=f"title.source.{index}",
            space_id=space.space_id,
        ).capture_id
        for index, reference in enumerate(references)
    )
    title = " ".join((*references, "/Users/private/note", "api_key=synthetic"))
    proposal = engine.review.propose(
        sources, (ProposalDraft(title, "Draft"),), delivery_id="title.proposal"
    )[0]
    listed = engine.review.list()[0]
    assert listed.title == engine.review.show(proposal.proposal_id).title
    assert all(reference not in listed.title for reference in references)
    assert "/Users/private" not in listed.title
    assert "synthetic" not in listed.title


def test_bound_multiple_drafts_freeze_sorted_siblings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _engine(tmp_path / "brain")
    first, _ = _sources(engine)
    ids = iter(
        (
            "proposal_ffffffff-ffff-4fff-8fff-ffffffffffff",
            "proposal_00000000-0000-4000-8000-000000000001",
        )
    )
    original = _new_id
    monkeypatch.setattr(
        review_bound_module,
        "_new_id",
        lambda kind: next(ids) if kind == "proposal" else original(kind),
    )
    proposals = engine.review.propose(
        (first,),
        (ProposalDraft("First", "First draft"), ProposalDraft("Second", "Second draft")),
        delivery_id="sorted.proposals",
    )
    assert [engine.review.show(p.proposal_id).markdown for p in proposals] == [
        "First draft",
        "Second draft",
    ]
    assert all(p.sibling_proposal_ids == tuple(sorted(p.sibling_proposal_ids)) for p in proposals)
    engine.portability.export(
        tmp_path / "export", export_id="export_ffffffff-ffff-4fff-8fff-ffffffffffff"
    )


def test_legacy_update_refuses_symlink_before_decision_reservation(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    engine = _engine(root)
    space = engine.inbox.spaces()[0]
    capture = engine.capture.accept(
        TextPayload("Legacy symlink target"),
        delivery_id="symlink.capture",
        action=CaptureAction.CANONICAL_NOTE,
        space_id=space.space_id,
    )
    page_id = engine.retrieval.search("Legacy symlink target", record_type="canonical")[0].result_id
    proposal = engine.review.propose(
        (capture.capture_id,),
        (ProposalDraft("Updated", "Successor"),),
        delivery_id="symlink.proposal",
        target_page_id=page_id,
    )[0]
    page = next((root / "content/spaces").rglob(f"{page_id}.md"))
    outside = tmp_path / "outside-copy.md"
    outside.write_bytes(page.read_bytes())
    page.unlink()
    page.symlink_to(outside)
    history = {p: p.read_bytes() for p in (root / "history").rglob("*.json")}
    with pytest.raises(ValueError, match="canonical page revision conflict"):
        engine.review.decide(
            proposal.proposal_id,
            DecisionOutcome.APPROVED,
            delivery_id="symlink.decision",
            expected_review_digest=proposal.review_digest,
        )
    assert engine.review.show(proposal.proposal_id).status == "pending"
    assert {p: p.read_bytes() for p in (root / "history").rglob("*.json")} == history


@pytest.mark.parametrize("bound", (False, True))
def test_long_protected_reference_is_projected_before_evidence_truncation(
    tmp_path: Path, bound: bool
) -> None:
    engine = _engine(tmp_path / "brain")
    reference = "https://private.example.test/" + "confidential-segment/" * 50
    source = engine.capture.accept(
        ReferencePayload(reference, "Useful evidence after the protected reference"),
        delivery_id="long.source",
        space_id=engine.inbox.spaces()[0].space_id,
    )
    proposal = engine.review.propose(
        (source.capture_id,) if bound else source.capture_id,
        (ProposalDraft("Safe title", "Safe body"),),
        delivery_id="long.proposal",
    )[0]
    shown = engine.review.show(proposal.proposal_id)
    assert reference[:80] not in shown.evidence[0].excerpt
    assert "confidential-segment" not in shown.evidence[0].excerpt
    assert shown.evidence[0].projection_applied
    assert shown.projection_applied
    assert "Useful evidence" in shown.evidence[0].excerpt


@pytest.mark.parametrize("bound", (False, True))
def test_review_rejects_symlinked_source_or_legacy_history(tmp_path: Path, bound: bool) -> None:
    root = tmp_path / "brain"
    engine = _engine(root)
    first, _ = _sources(engine)
    proposal = engine.review.propose(
        (first,) if bound else first,
        (ProposalDraft("Confined", "Bound content"),),
        delivery_id="confined.proposal",
    )[0]
    name = first if bound else proposal.proposal_id
    directory = root / ("sources/captures" if bound else "history/proposals")
    path = next(directory.rglob(name + ".json"))
    outside = tmp_path / "outside-record.json"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(outside)
    if bound:
        with pytest.raises(ValueError, match="review source is not durable"):
            engine.review.decide(
                proposal.proposal_id,
                DecisionOutcome.APPROVED,
                delivery_id="confined.decision",
                expected_review_digest=proposal.review_digest,
            )
        assert engine.review.show(proposal.proposal_id).status == "pending"
    else:
        with pytest.raises(ValueError, match="invalid frozen review proposal"):
            engine.review.show(proposal.proposal_id)
    assert not tuple((root / "history/decisions").rglob("*.json"))


def test_bound_decision_refuses_changed_source_route(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    engine = _engine(root)
    first, _ = _sources(engine)
    proposal = engine.review.propose(
        (first,),
        (ProposalDraft("Frozen", "Frozen source route"),),
        delivery_id="delivery.bound.route.proposal",
    )[0]
    other = engine.inbox.create_space("Other", delivery_id="delivery.bound.route.space")
    engine.inbox.route(first, other.space_id, delivery_id="delivery.bound.route.change")

    with pytest.raises(ValueError, match="review source state conflict"):
        engine.review.decide(
            proposal.proposal_id,
            DecisionOutcome.APPROVED,
            delivery_id="delivery.bound.route.decision",
            expected_review_digest=proposal.review_digest,
        )
    assert tuple((root / "content" / "spaces").rglob(f"{proposal.page_id}.md")) == ()


def test_bound_proposal_refuses_invalid_source_sets(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    first, second = _sources(engine)
    other = engine.inbox.create_space("Other", delivery_id="delivery.invalid.other.space")
    mixed = engine.capture.accept(
        TextPayload("Mixed space source"),
        delivery_id="delivery.invalid.mixed.source",
        space_id=other.space_id,
    ).capture_id
    unrouted = engine.capture.accept(
        TextPayload("Unrouted source"), delivery_id="delivery.invalid.unrouted.source"
    ).capture_id
    cases = (
        ((), "invalid review sources"),
        ((first, first), "duplicate review source"),
        (("capture_00000000-0000-4000-8000-000000000000",), "unknown capture"),
        ((unrouted,), "routed capture"),
        ((second, mixed), "share a space"),
    )

    for index, (capture_ids, message) in enumerate(cases):
        with pytest.raises(ValueError, match=message):
            engine.review.propose(
                capture_ids,
                (ProposalDraft("Invalid", "Must not reserve"),),
                delivery_id=f"delivery.invalid.proposal.{index}",
            )
    assert engine.review.list() == ()
    foreign = engine.review.propose(
        (mixed,),
        (ProposalDraft("Foreign page", "Other space"),),
        delivery_id="delivery.invalid.foreign.create",
    )[0]
    engine.review.decide(
        foreign.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="delivery.invalid.foreign.approve",
        expected_review_digest=foreign.review_digest,
    )
    before = engine.review.list()
    with pytest.raises(ValueError, match="share a space"):
        engine.review.propose(
            (first,),
            (ProposalDraft("Wrong target", "Must not reserve"),),
            delivery_id="delivery.invalid.foreign.update",
            target_page_id=foreign.page_id,
        )
    assert engine.review.list() == before


def test_cumulative_source_and_markdown_byte_boundaries(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    sources = _text_sources(engine, 33, prefix="limits")
    created = engine.review.propose(
        sources[:30],
        (ProposalDraft("Limit", "Initial"),),
        delivery_id="delivery.limits.create",
    )[0]
    engine.review.decide(
        created.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="delivery.limits.create.approve",
        expected_review_digest=created.review_digest,
    )
    thirty_two = engine.review.propose(
        sources[30:32],
        (ProposalDraft("Limit", "Thirty two"),),
        delivery_id="delivery.limits.update.32",
        target_page_id=created.page_id,
    )[0]
    assert len(thirty_two.capture_ids) == 32
    engine.review.decide(
        thirty_two.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="delivery.limits.update.32.approve",
        expected_review_digest=thirty_two.review_digest,
    )
    with pytest.raises(ValueError, match="source limit"):
        engine.review.propose(
            (sources[32],),
            (ProposalDraft("Limit", "Thirty three"),),
            delivery_id="delivery.limits.update.33",
            target_page_id=created.page_id,
        )

    exact = engine.review.propose(
        (sources[0],),
        (ProposalDraft("Bytes", "é" * (64 * 1024 // 2)),),
        delivery_id="delivery.limits.bytes.exact",
    )[0]
    assert len(engine.review.show(exact.proposal_id).markdown.encode("utf-8")) == 64 * 1024
    with pytest.raises(ValueError, match="markdown limit"):
        engine.review.propose(
            (sources[0],),
            (ProposalDraft("Bytes", "é" * (64 * 1024 // 2 + 1)),),
            delivery_id="delivery.limits.bytes.over",
        )


def test_bound_decision_refuses_corrupted_immutable_source_bytes(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    engine = _engine(root)
    first, _ = _sources(engine)
    proposal = engine.review.propose(
        (first,),
        (ProposalDraft("Frozen", "Immutable source bytes"),),
        delivery_id="delivery.corrupt.proposal",
    )[0]
    source = next((root / "sources" / "captures").rglob(f"{first}.json"))
    source.write_bytes(source.read_bytes() + b" ")

    with pytest.raises(ValueError, match="review source state conflict"):
        engine.review.decide(
            proposal.proposal_id,
            DecisionOutcome.APPROVED,
            delivery_id="delivery.corrupt.decision",
            expected_review_digest=proposal.review_digest,
        )


@pytest.mark.parametrize(
    "fault",
    [CaptureFault.AFTER_DECISION_RESERVATION, CaptureFault.AFTER_REVIEW_PUBLICATION_WRITE],
)
def test_approved_bound_reservation_prevents_rerouting(tmp_path: Path, fault: CaptureFault) -> None:
    root = tmp_path / fault.value
    setup = _engine(root)
    first, _ = _sources(setup)
    proposal = setup.review.propose(
        (first,),
        (ProposalDraft("Frozen", "Reserved publication"),),
        delivery_id=f"delivery.route.refusal.proposal.{fault.value}",
    )[0]
    with pytest.raises(InjectedFault):
        _engine(root, faults={fault}).review.decide(
            proposal.proposal_id,
            DecisionOutcome.APPROVED,
            delivery_id=f"delivery.route.refusal.decision.{fault.value}",
            expected_review_digest=proposal.review_digest,
        )
    reopened = _engine(root)
    other = reopened.inbox.create_space(
        f"Other {fault.value}", delivery_id=f"delivery.route.refusal.space.{fault.value}"
    )
    with pytest.raises(ValueError, match="published capture cannot be rerouted"):
        reopened.inbox.route(
            first, other.space_id, delivery_id=f"delivery.route.refusal.route.{fault.value}"
        )


def test_competing_updates_allow_only_one_successor_and_terminal_outcome(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    first, second = _sources(engine)
    third = _text_sources(engine, 1, prefix="competing")[0]
    created = engine.review.propose(
        (first,),
        (ProposalDraft("Stable", "Root"),),
        delivery_id="delivery.competing.root",
    )[0]
    engine.review.decide(
        created.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="delivery.competing.root.approve",
        expected_review_digest=created.review_digest,
    )
    first_update = engine.review.propose(
        (second,),
        (ProposalDraft("Stable", "Winner"),),
        delivery_id="delivery.competing.first",
        target_page_id=created.page_id,
    )[0]
    second_update = engine.review.propose(
        (third,),
        (ProposalDraft("Stable", "Loser"),),
        delivery_id="delivery.competing.second",
        target_page_id=created.page_id,
    )[0]
    engine.review.decide(
        first_update.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="delivery.competing.first.approve",
        expected_review_digest=first_update.review_digest,
    )
    with pytest.raises(ValueError, match="canonical page revision conflict"):
        engine.review.decide(
            second_update.proposal_id,
            DecisionOutcome.APPROVED,
            delivery_id="delivery.competing.second.approve",
            expected_review_digest=second_update.review_digest,
        )
    with pytest.raises(ValueError, match="terminal decision"):
        engine.review.decide(
            first_update.proposal_id,
            DecisionOutcome.REJECTED,
            delivery_id="delivery.competing.first.reject",
            expected_review_digest=first_update.review_digest,
        )


def test_retry_identity_binds_sources_draft_target_outcome_and_token(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    engine = _engine(root)
    first, second = _sources(engine)
    proposal = engine.review.propose(
        (first,),
        (ProposalDraft("Retry", "Original"),),
        delivery_id="delivery.retry.proposal",
    )[0]
    for capture_ids, draft in (
        ((second,), ProposalDraft("Retry", "Original")),
        ((first,), ProposalDraft("Retry", "Changed")),
    ):
        with pytest.raises(ValueError, match="conflicting delivery"):
            _engine(root).review.propose(
                capture_ids,
                (draft,),
                delivery_id="delivery.retry.proposal",
            )
    with pytest.raises(ValueError, match="conflicting delivery"):
        _engine(root).review.propose(
            (first,),
            (ProposalDraft("Retry", "Original"),),
            delivery_id="delivery.retry.proposal",
            target_page_id=proposal.page_id,
        )
    approved = engine.review.decide(
        proposal.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="delivery.retry.decision",
        expected_review_digest=proposal.review_digest,
    )
    replay = _engine(root).review.decide(
        proposal.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="delivery.retry.decision",
        expected_review_digest=proposal.review_digest,
    )
    assert replay.decision_id == approved.decision_id
    assert replay.duplicate is True
    with pytest.raises(ValueError, match="conflicting delivery"):
        _engine(root).review.decide(
            proposal.proposal_id,
            DecisionOutcome.APPROVED,
            delivery_id="delivery.retry.decision",
            expected_review_digest="f" * 64,
        )
    with pytest.raises(ValueError, match="conflicting delivery|terminal decision"):
        _engine(root).review.decide(
            proposal.proposal_id,
            DecisionOutcome.REJECTED,
            delivery_id="delivery.retry.decision",
            expected_review_digest=proposal.review_digest,
        )


@pytest.mark.parametrize(
    "fault",
    [
        CaptureFault.AFTER_PROPOSAL_RESERVATION,
        CaptureFault.AFTER_PROPOSAL_WRITE,
        CaptureFault.AFTER_DECISION_RESERVATION,
        CaptureFault.AFTER_DECISION_WRITE,
    ],
)
def test_bound_review_replays_early_fault_boundaries(tmp_path: Path, fault: CaptureFault) -> None:
    root = tmp_path / fault.value
    setup = _engine(root)
    first, _ = _sources(setup)
    if fault in {CaptureFault.AFTER_PROPOSAL_RESERVATION, CaptureFault.AFTER_PROPOSAL_WRITE}:
        with pytest.raises(InjectedFault):
            _engine(root, faults={fault}).review.propose(
                (first,),
                (ProposalDraft("Recovery", "Proposal replay"),),
                delivery_id=f"delivery.early.{fault.value}",
            )
        recovered = _engine(root).review.propose(
            (first,),
            (ProposalDraft("Recovery", "Proposal replay"),),
            delivery_id=f"delivery.early.{fault.value}",
        )[0]
        assert recovered.status == "pending"
        return
    proposal = setup.review.propose(
        (first,),
        (ProposalDraft("Recovery", "Decision replay"),),
        delivery_id=f"delivery.early.proposal.{fault.value}",
    )[0]
    with pytest.raises(InjectedFault):
        _engine(root, faults={fault}).review.decide(
            proposal.proposal_id,
            DecisionOutcome.APPROVED,
            delivery_id=f"delivery.early.{fault.value}",
            expected_review_digest=proposal.review_digest,
        )
    recovered_decision = _engine(root).review.decide(
        proposal.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id=f"delivery.early.{fault.value}",
        expected_review_digest=proposal.review_digest,
    )
    assert recovered_decision.duplicate is True
    assert recovered_decision.publication_id is not None


def test_older_update_replay_cannot_overwrite_newer_revision(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    engine = _engine(root)
    sources = _text_sources(engine, 3, prefix="ordered-replay")
    created = engine.review.propose(
        (sources[0],),
        (ProposalDraft("Stable", "Revision zero"),),
        delivery_id="delivery.ordered.create",
    )[0]
    engine.review.decide(
        created.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="delivery.ordered.create.approve",
        expected_review_digest=created.review_digest,
    )
    decisions = []
    for index, source in enumerate(sources[1:], start=1):
        update = engine.review.propose(
            (source,),
            (ProposalDraft("Stable", f"Revision {index}"),),
            delivery_id=f"delivery.ordered.update.{index}",
            target_page_id=created.page_id,
        )[0]
        decision = engine.review.decide(
            update.proposal_id,
            DecisionOutcome.APPROVED,
            delivery_id=f"delivery.ordered.update.{index}.approve",
            expected_review_digest=update.review_digest,
        )
        decisions.append((update, decision))

    replay = _engine(root).review.decide(
        decisions[0][0].proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="delivery.ordered.update.1.approve",
        expected_review_digest=decisions[0][0].review_digest,
    )
    page = next((root / "content" / "spaces").rglob(f"{created.page_id}.md"))
    assert replay.duplicate is True
    assert replay.decision_id == decisions[0][1].decision_id
    assert "Revision 2" in page.read_text(encoding="utf-8")
    assert "Revision 1" not in page.read_text(encoding="utf-8")


def test_show_projects_hostile_secondary_evidence_without_losing_unicode(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    space_id = engine.inbox.spaces()[0].space_id
    first = engine.capture.accept(
        TextPayload("Primary café 東京 source RTL\u202econtrol"),
        delivery_id="delivery.hostile.primary",
        space_id=space_id,
    )
    secondary = engine.capture.accept(
        ReferencePayload(
            "https://secondary.private.example.test/token",
            "Secondary café 東京 api_key=secret /Users/private/note",
        ),
        delivery_id="delivery.hostile.secondary",
        space_id=space_id,
    )
    proposal = engine.review.propose(
        (first.capture_id, secondary.capture_id),
        (
            ProposalDraft(
                "Unicode café 東京",
                "Keep café 東京; hide https://secondary.private.example.test/token api_key=secret",
            ),
        ),
        delivery_id="delivery.hostile.proposal",
    )[0]
    shown = engine.review.show(proposal.proposal_id)

    assert "café 東京" in shown.markdown
    assert "RTL\u202econtrol" in shown.evidence[0].excerpt
    assert "secondary.private.example.test" not in shown.markdown
    assert "secret" not in shown.markdown
    assert "secondary.private.example.test" not in shown.evidence[1].excerpt
    assert "/Users/private" not in shown.evidence[1].excerpt
    assert shown.evidence[1].projection_applied is True


def test_bound_update_preserves_identity_path_and_cumulative_provenance(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    engine = _engine(root)
    first, second = _sources(engine)
    created = engine.review.propose(
        (first,),
        (ProposalDraft("Stable", "Revision one"),),
        delivery_id="delivery.bound.create.proposal",
    )[0]
    first_decision = engine.review.decide(
        created.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="delivery.bound.create.approve",
        expected_review_digest=created.review_digest,
    )
    page = next((root / "content" / "spaces").rglob(f"{created.page_id}.md"))
    original_path = page.relative_to(root).as_posix()

    updated = engine.review.propose(
        (second,),
        (ProposalDraft("Stable", "Revision two"),),
        delivery_id="delivery.bound.update.proposal",
        target_page_id=created.page_id,
    )[0]
    shown = engine.review.show(updated.proposal_id)

    assert updated.page_id == created.page_id
    assert updated.target_page_id == created.page_id
    assert updated.operation == "update"
    assert updated.capture_ids == (first, second)
    assert updated.selected_capture_ids == (second,)
    assert shown.expected_publication_id == first_decision.publication_id

    second_decision = engine.review.decide(
        updated.proposal_id,
        DecisionOutcome.EDITED,
        delivery_id="delivery.bound.update.approve",
        edited_markdown="Edited revision two",
        expected_review_digest=updated.review_digest,
    )
    assert second_decision.page_id == created.page_id
    assert page.relative_to(root).as_posix() == original_path
    assert "Edited revision two" in page.read_text(encoding="utf-8")
    assert tuple(engine.review.show(updated.proposal_id).capture_ids) == (first, second)


def test_bound_update_refuses_manual_target_divergence(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    engine = _engine(root)
    first, second = _sources(engine)
    created = engine.review.propose(
        (first,),
        (ProposalDraft("Stable", "Revision one"),),
        delivery_id="delivery.bound.conflict.create",
    )[0]
    engine.review.decide(
        created.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="delivery.bound.conflict.approve",
        expected_review_digest=created.review_digest,
    )
    updated = engine.review.propose(
        (second,),
        (ProposalDraft("Stable", "Revision two"),),
        delivery_id="delivery.bound.conflict.update",
        target_page_id=created.page_id,
    )[0]
    page = next((root / "content" / "spaces").rglob(f"{created.page_id}.md"))
    page.write_text(page.read_text(encoding="utf-8") + "manual divergence\n", encoding="utf-8")

    with pytest.raises(ValueError, match="canonical page revision conflict"):
        engine.review.decide(
            updated.proposal_id,
            DecisionOutcome.APPROVED,
            delivery_id="delivery.bound.conflict.decision",
            expected_review_digest=updated.review_digest,
        )
    assert "Revision two" not in page.read_text(encoding="utf-8")


def test_first_bound_update_can_anchor_a_legacy_publication(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    engine = _engine(root)
    space_id = engine.inbox.spaces()[0].space_id
    legacy = engine.capture.accept(
        TextPayload("Legacy canonical body"),
        delivery_id="delivery.bound.legacy.page",
        action=CaptureAction.CANONICAL_NOTE,
        space_id=space_id,
    )
    source = engine.capture.accept(
        TextPayload("New source"),
        delivery_id="delivery.bound.legacy.source",
        space_id=space_id,
    )
    legacy_page_id = engine.retrieval.search("Legacy canonical body", record_type="canonical")[
        0
    ].result_id
    update = engine.review.propose(
        (source.capture_id,),
        (ProposalDraft("Legacy", "Bound successor"),),
        delivery_id="delivery.bound.legacy.update",
        target_page_id=legacy_page_id,
    )[0]

    decided = engine.review.decide(
        update.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="delivery.bound.legacy.approve",
        expected_review_digest=update.review_digest,
    )

    assert decided.page_id == legacy_page_id
    assert update.capture_ids == (legacy.capture_id, source.capture_id)
    page = next((root / "content" / "spaces").rglob(f"{legacy_page_id}.md"))
    assert "Bound successor" in page.read_text(encoding="utf-8")


def test_review_list_filters_source_space_and_pages(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    first, second = _sources(engine)
    space_id = engine.inbox.spaces()[0].space_id
    proposals = tuple(
        engine.review.propose(
            (capture_id,),
            (ProposalDraft(title, body),),
            delivery_id=delivery_id,
        )[0]
        for capture_id, title, body, delivery_id in (
            (first, "First", "First draft", "delivery.bound.list.first"),
            (second, "Second", "Second draft", "delivery.bound.list.second"),
        )
    )

    assert engine.review.list(capture_id=second) == (proposals[1],)
    assert engine.review.list(space_id=space_id, limit=1, offset=1) == (proposals[1],)
    with pytest.raises(ValueError, match="page bounds"):
        engine.review.list(limit=101)


@pytest.mark.parametrize(
    "fault",
    [CaptureFault.AFTER_REVIEW_PAGE_WRITE, CaptureFault.AFTER_REVIEW_PUBLICATION_WRITE],
)
def test_new_bound_mutation_drains_interrupted_update_before_reservation(
    tmp_path: Path, fault: CaptureFault
) -> None:
    root = tmp_path / fault.value
    setup = _engine(root)
    first, second = _sources(setup)
    created = setup.review.propose(
        (first,),
        (ProposalDraft("Stable", "Revision one"),),
        delivery_id="delivery.bound.recovery.create",
    )[0]
    setup.review.decide(
        created.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="delivery.bound.recovery.create.approve",
        expected_review_digest=created.review_digest,
    )
    update = setup.review.propose(
        (second,),
        (ProposalDraft("Stable", "Recovered revision"),),
        delivery_id="delivery.bound.recovery.update",
        target_page_id=created.page_id,
    )[0]

    with pytest.raises(InjectedFault):
        _engine(root, faults={fault}).review.decide(
            update.proposal_id,
            DecisionOutcome.APPROVED,
            delivery_id="delivery.bound.recovery.update.approve",
            expected_review_digest=update.review_digest,
        )

    reopened = _engine(root)
    fresh = reopened.review.propose(
        (first,),
        (ProposalDraft("Independent", "Fresh proposal"),),
        delivery_id="delivery.bound.recovery.fresh",
    )[0]
    recovered = reopened.review.show(update.proposal_id)
    page = next((root / "content" / "spaces").rglob(f"{created.page_id}.md"))
    assert fresh.status == "pending"
    assert recovered.status == "approved"
    assert "Recovered revision" in page.read_text(encoding="utf-8")
    assert len(tuple((root / "history" / "publications").rglob("*.json"))) == 2

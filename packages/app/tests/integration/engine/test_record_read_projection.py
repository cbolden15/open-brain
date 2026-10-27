"""Record reads disclose when public projection replaced stored text."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from open_brain_engine.engine import BrainEngine, DecisionOutcome, ProposalDraft, TextPayload
from open_brain_engine.engine.t03_contracts import EffectiveAuthority, RecordReadRequest
from open_brain_engine.portable.v4 import canonical_revision_id

from open_brain.profile import compile_single_user_local

OWNER = EffectiveAuthority(
    "projection-owner", "projection-session", frozenset(), None, owner=True
)


def _engine(root: Path) -> BrainEngine:
    return BrainEngine.open(compile_single_user_local(root, starter_spaces=("Notes",)))


def _wire(response: object) -> dict[str, Any]:
    return cast(dict[str, Any], cast(Any, response).to_wire())


def test_source_read_flags_projected_text(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    space_id = engine.inbox.spaces()[0].space_id
    plain, pathy = (
        engine.capture.accept(
            TextPayload(text), delivery_id=f"projection.{index}", space_id=space_id
        ).capture_id
        for index, text in enumerate(
            ("Plain synthetic note", "Tool at /srv/synthetic/tool.py")
        )
    )

    clean = _wire(
        engine.retrieval.read_record(
            RecordReadRequest(record_id=plain, expected_revision_id=plain), authority=OWNER
        )
    )
    flagged = _wire(
        engine.retrieval.read_record(
            RecordReadRequest(record_id=pathy, expected_revision_id=pathy), authority=OWNER
        )
    )

    assert clean["projection_applied"] is False
    assert flagged["projection_applied"] is True
    assert "[protected]" in flagged["content"]["text"]
    assert "/srv/synthetic" not in flagged["content"]["text"]


def test_canonical_read_and_history_show_flag_projected_text(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    space_id = engine.inbox.spaces()[0].space_id
    source = engine.capture.accept(
        TextPayload("Synthetic canonical source"),
        delivery_id="projection.canonical.source",
        space_id=space_id,
    ).capture_id
    proposal = engine.review.propose(
        (source,),
        (ProposalDraft("Projected page", "Tool at /srv/synthetic/tool.py"),),
        delivery_id="projection.canonical",
    )[0]
    decision = engine.review.decide(
        proposal.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="projection.canonical.approve",
        expected_review_digest=proposal.review_digest,
    )
    assert decision.page_id is not None and decision.publication_id is not None
    revision = canonical_revision_id(decision.publication_id)
    request = RecordReadRequest(record_id=decision.page_id, expected_revision_id=revision)

    assert _wire(engine.retrieval.read_record(request, authority=OWNER))[
        "projection_applied"
    ] is True
    assert _wire(engine.history.read_history(request, authority=OWNER))[
        "projection_applied"
    ] is True

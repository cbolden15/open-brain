from __future__ import annotations

import json
from pathlib import Path

from open_brain_engine.engine import (
    BrainEngine,
    DecisionOutcome,
    ProposalDraft,
    PublicProvenance,
    ReferencePayload,
)

from open_brain.profile import compile_single_user_local


def test_public_provenance_retains_every_source_without_private_metadata() -> None:
    captures = (
        "capture_00000000-0000-4000-8000-000000000001",
        "capture_00000000-0000-4000-8000-000000000002",
    )
    provenance = PublicProvenance(
        capture_id=captures[0], source_origin="mixed", capture_ids=captures
    )
    assert provenance.capture_ids == captures
    assert provenance.as_dict() == {
        "capture_id": captures[0],
        "capture_ids": list(captures),
        "source_origin": "mixed",
        "source_record_id": captures[0],
    }
    assert dict(provenance) == provenance.as_dict()


def test_search_and_reindex_preserve_all_sources_and_redact_secondary_reference(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    engine = BrainEngine.open(compile_single_user_local(root, starter_spaces=("Synthetic",)))
    space = engine.inbox.spaces()[0]
    private_reference = "https://example.test/synthetic-secondary-reference-canary"
    captures = tuple(
        engine.capture.accept(
            ReferencePayload(reference, f"Synthetic evidence {index}"),
            delivery_id=f"search.review.source.{index}",
            space_id=space.space_id,
        ).capture_id
        for index, reference in enumerate(("https://example.test/primary", private_reference))
    )
    proposal = engine.review.propose(
        captures,
        (ProposalDraft("Combined nebula", f"Nebula combines evidence. {private_reference}"),),
        delivery_id="search.review.proposal",
    )[0]
    view = engine.review.show(proposal.proposal_id)
    assert private_reference not in view.markdown
    decision = engine.review.decide(
        proposal.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="search.review.decision",
        expected_review_digest=view.review_digest,
    )
    assert decision.page_id is not None
    for _ in range(2):
        result = engine.retrieval.fetch(decision.page_id)
        assert result is not None
        assert result.provenance.capture_ids == captures
        assert private_reference not in json.dumps(result.provenance.as_dict()) + result.excerpt
        page = engine.retrieval.read_page(decision.page_id)
        assert page is not None and private_reference not in page.markdown
        assert len(engine.retrieval.search("nebula", record_type="canonical")) == 1
        engine._rederive_live_search_projection()

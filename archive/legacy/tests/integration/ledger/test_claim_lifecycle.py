from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.core.policy import classify_privacy

from open_brain_legacy.ledger.age import age_claims
from open_brain_legacy.ledger.embed import embed_claims
from open_brain_legacy.ledger.index import ClaimInput, ClaimStatus, index_claims
from open_brain_legacy.ledger.merge import TrustedCitation
from open_brain_legacy.ledger.reinforce import rank_claims, reinforce_claims
from open_brain_legacy.ledger.render import ClaimViewRenderer, RenderDisposition


def _citation(value: str) -> TrustedCitation:
    return TrustedCitation.create(
        citation_id=value,
        destination=f"captures/{value}.md",
    )


def _input(*, text: str, citation_id: str, observed_at: datetime) -> ClaimInput:
    return ClaimInput.create(
        topic_id="research",
        text=text,
        citations=(_citation(citation_id),),
        observed_at=observed_at,
    )


def test_index_embed_reinforce_rank_age_and_no_delete_views(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    indexed = index_claims(
        (
            _input(
                text="Synthetic duplicate claim",
                citation_id="cite-duplicate-one",
                observed_at=now - timedelta(days=5),
            ),
            _input(
                text="Synthetic duplicate claim.",
                citation_id="cite-duplicate-two",
                observed_at=now - timedelta(days=4),
            ),
            _input(
                text="Synthetic aging claim",
                citation_id="cite-aging",
                observed_at=now - timedelta(days=40),
            ),
            _input(
                text="Synthetic retired claim",
                citation_id="cite-retired",
                observed_at=now - timedelta(days=120),
            ),
        )
    )

    assert len(indexed) == 3
    duplicate = next(claim for claim in indexed if "duplicate" in claim.text)
    assert tuple(citation.citation_id for citation in duplicate.citations) == (
        "cite-duplicate-one",
        "cite-duplicate-two",
    )

    first_embedding = embed_claims(indexed, dimensions=32)
    second_embedding = embed_claims(indexed, dimensions=32)
    assert first_embedding == second_embedding
    assert all(len(claim.embedding or ()) == 32 for claim in first_embedding)
    assert rank_claims(
        query="Synthetic duplicate claim",
        claims=first_embedding,
        limit=1,
    )[0].claim_id == duplicate.claim_id

    embedded_duplicate = next(
        claim for claim in first_embedding if "duplicate" in claim.text
    )
    aging = next(claim for claim in first_embedding if "aging" in claim.text)
    reinforced = reinforce_claims(
        (
            embedded_duplicate,
            replace(
                aging,
                embedding=embedded_duplicate.embedding,
                citations=aging.citations + (_citation("cite-reinforcement"),),
            ),
        ),
        reinforced_at=now,
        similarity_threshold=0.99,
    )
    assert len(reinforced) == 1
    assert reinforced[0].reinforcement_count == 2
    assert {citation.citation_id for citation in reinforced[0].citations} == {
        "cite-aging",
        "cite-duplicate-one",
        "cite-duplicate-two",
        "cite-reinforcement",
    }

    privacy = classify_privacy(PrivacyTier.WORK, policy_version="synthetic-v1")
    renderer = ClaimViewRenderer(root=tmp_path / "views")
    initial_views = renderer.render(claims=first_embedding, privacy=privacy)
    aged = age_claims(
        first_embedding,
        now=now,
        aging_after=timedelta(days=30),
        retire_after=timedelta(days=90),
    )
    aged_views = renderer.render(claims=aged, privacy=privacy)
    rerun = renderer.render(claims=aged, privacy=privacy)

    assert next(claim for claim in aged if "duplicate" in claim.text).status is ClaimStatus.ACTIVE
    assert next(claim for claim in aged if "aging" in claim.text).status is ClaimStatus.AGING
    assert next(claim for claim in aged if "retired" in claim.text).status is ClaimStatus.RETIRED
    assert len(aged) == len(first_embedding)
    rendered = aged_views.current.document.body + aged_views.archive.document.body
    assert all(rendered.count(claim.claim_id) == 1 for claim in aged)
    retired = next(claim for claim in aged if claim.status is ClaimStatus.RETIRED)
    assert retired.claim_id not in aged_views.current.document.body
    assert retired.claim_id in aged_views.archive.document.body
    assert initial_views.current.document.document_id != aged_views.current.document.document_id
    assert len(tuple((tmp_path / "views").rglob("*.md"))) == 4
    assert rerun.current.disposition is RenderDisposition.UNCHANGED
    assert rerun.archive.disposition is RenderDisposition.UNCHANGED

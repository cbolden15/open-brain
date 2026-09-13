from __future__ import annotations

from open_brain_legacy.integrations.relationships import (
    IdentityCluster,
    RelationshipIdentity,
    RelationshipRecord,
    RelationshipReviewKind,
    detect_relationship_identity_reviews,
)


def _identity(source_ref: str, subject_ref: str) -> RelationshipIdentity:
    return RelationshipIdentity.create(source_ref=source_ref, subject_ref=subject_ref)


def test_identity_collision_creates_an_idempotent_review_without_merging() -> None:
    first_identity = _identity("source_alpha", "subject_one")
    second_identity = _identity("source_beta", "subject_two")
    relationships = (
        RelationshipRecord("relationship_alpha", (first_identity,)),
        RelationshipRecord("relationship_beta", (second_identity,)),
    )
    observed = (
        IdentityCluster("cluster_shared", (second_identity, first_identity)),
    )

    result = detect_relationship_identity_reviews(
        relationships=relationships,
        observed_clusters=observed,
    )

    assert result.relationships == relationships
    assert len(result.relationships) == 2
    assert len(result.proposals) == 1
    assert result.proposals[0].kind is RelationshipReviewKind.COLLISION
    assert result.proposals[0].relationship_refs == (
        "relationship_alpha",
        "relationship_beta",
    )
    assert result.proposals[0].cluster_refs == ("cluster_shared",)
    assert (
        detect_relationship_identity_reviews(
            relationships=relationships,
            observed_clusters=observed,
        )
        == result
    )


def test_identity_split_creates_an_idempotent_review_without_reassignment() -> None:
    first_identity = _identity("source_alpha", "subject_one")
    second_identity = _identity("source_beta", "subject_two")
    relationships = (
        RelationshipRecord(
            "relationship_shared",
            (first_identity, second_identity),
        ),
    )
    observed = (
        IdentityCluster("cluster_alpha", (first_identity,)),
        IdentityCluster("cluster_beta", (second_identity,)),
    )

    result = detect_relationship_identity_reviews(
        relationships=relationships,
        observed_clusters=observed,
    )

    assert result.relationships == relationships
    assert result.relationships[0].identities == (first_identity, second_identity)
    assert len(result.proposals) == 1
    assert result.proposals[0].kind is RelationshipReviewKind.SPLIT
    assert result.proposals[0].relationship_refs == ("relationship_shared",)
    assert result.proposals[0].cluster_refs == ("cluster_alpha", "cluster_beta")
    assert (
        detect_relationship_identity_reviews(
            relationships=relationships,
            observed_clusters=tuple(reversed(observed)),
        )
        == result
    )

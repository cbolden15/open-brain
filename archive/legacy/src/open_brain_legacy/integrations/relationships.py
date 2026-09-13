"""Provider-neutral relationship identity review detection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256

from open_brain_engine.core.ids import canonical_json_bytes

_OPAQUE_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


@dataclass(frozen=True, slots=True)
class RelationshipIdentity:
    """Stable identity for one opaque subject within an opaque source."""

    identity_id: str
    source_ref: str
    subject_ref: str

    @classmethod
    def create(cls, *, source_ref: str, subject_ref: str) -> RelationshipIdentity:
        if not _is_ref(source_ref) or not _is_ref(subject_ref):
            raise ValueError("invalid relationship identity")
        return cls(_identity_id(source_ref, subject_ref), source_ref, subject_ref)

    def __post_init__(self) -> None:
        if (
            not _is_ref(self.source_ref)
            or not _is_ref(self.subject_ref)
            or self.identity_id != _identity_id(self.source_ref, self.subject_ref)
        ):
            raise ValueError("invalid relationship identity")


@dataclass(frozen=True, slots=True)
class RelationshipRecord:
    """Current reviewed identity assignments for one relationship."""

    relationship_ref: str
    identities: tuple[RelationshipIdentity, ...]

    def __post_init__(self) -> None:
        if not _is_ref(self.relationship_ref) or not _valid_identities(self.identities):
            raise ValueError("invalid relationship record")
        object.__setattr__(self, "identities", _sorted_identities(self.identities))


@dataclass(frozen=True, slots=True)
class IdentityCluster:
    """One observed group of identities, without an automatic assignment."""

    cluster_ref: str
    identities: tuple[RelationshipIdentity, ...]

    def __post_init__(self) -> None:
        if not _is_ref(self.cluster_ref) or not _valid_identities(self.identities):
            raise ValueError("invalid identity cluster")
        object.__setattr__(self, "identities", _sorted_identities(self.identities))


class RelationshipReviewKind(StrEnum):
    COLLISION = "collision"
    SPLIT = "split"


@dataclass(frozen=True, slots=True)
class RelationshipReviewProposal:
    """Deterministic request for an operator to resolve ambiguous identity state."""

    proposal_id: str
    kind: RelationshipReviewKind
    relationship_refs: tuple[str, ...]
    cluster_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RelationshipIdentityReviewResult:
    """Review proposals plus the original, deliberately unchanged assignments."""

    relationships: tuple[RelationshipRecord, ...]
    proposals: tuple[RelationshipReviewProposal, ...]


def detect_relationship_identity_reviews(
    *,
    relationships: tuple[RelationshipRecord, ...],
    observed_clusters: tuple[IdentityCluster, ...],
) -> RelationshipIdentityReviewResult:
    """Detect collision and split candidates without merging or reassigning identities."""
    if not isinstance(relationships, tuple) or any(
        not isinstance(relationship, RelationshipRecord) for relationship in relationships
    ):
        raise ValueError("invalid relationships")
    if not isinstance(observed_clusters, tuple) or any(
        not isinstance(cluster, IdentityCluster) for cluster in observed_clusters
    ):
        raise ValueError("invalid identity clusters")

    relationship_refs_by_identity: dict[str, set[str]] = {}
    for relationship in relationships:
        for identity in relationship.identities:
            relationship_refs_by_identity.setdefault(identity.identity_id, set()).add(
                relationship.relationship_ref
            )

    proposals: list[RelationshipReviewProposal] = []
    for cluster in observed_clusters:
        relationship_refs = tuple(
            sorted(
                {
                    relationship_ref
                    for identity in cluster.identities
                    for relationship_ref in relationship_refs_by_identity.get(
                        identity.identity_id, set()
                    )
                }
            )
        )
        if len(relationship_refs) > 1:
            proposals.append(
                _proposal(
                    kind=RelationshipReviewKind.COLLISION,
                    relationship_refs=relationship_refs,
                    cluster_refs=(cluster.cluster_ref,),
                )
            )

    for relationship in relationships:
        identity_ids = {identity.identity_id for identity in relationship.identities}
        cluster_refs = tuple(
            sorted(
                cluster.cluster_ref
                for cluster in observed_clusters
                if identity_ids.intersection(
                    identity.identity_id for identity in cluster.identities
                )
            )
        )
        if len(cluster_refs) > 1:
            proposals.append(
                _proposal(
                    kind=RelationshipReviewKind.SPLIT,
                    relationship_refs=(relationship.relationship_ref,),
                    cluster_refs=cluster_refs,
                )
            )

    return RelationshipIdentityReviewResult(
        relationships=relationships,
        proposals=tuple(sorted(proposals, key=lambda proposal: proposal.proposal_id)),
    )


def _proposal(
    *,
    kind: RelationshipReviewKind,
    relationship_refs: tuple[str, ...],
    cluster_refs: tuple[str, ...],
) -> RelationshipReviewProposal:
    proposal_id = "relationship_review_" + sha256(
        canonical_json_bytes(
            {
                "identity_version": 1,
                "kind": kind.value,
                "relationship_refs": relationship_refs,
                "cluster_refs": cluster_refs,
            }
        )
    ).hexdigest()
    return RelationshipReviewProposal(
        proposal_id=proposal_id,
        kind=kind,
        relationship_refs=relationship_refs,
        cluster_refs=cluster_refs,
    )


def _identity_id(source_ref: str, subject_ref: str) -> str:
    return "relationship_identity_" + sha256(
        canonical_json_bytes(
            {
                "identity_version": 1,
                "source_ref": source_ref,
                "subject_ref": subject_ref,
            }
        )
    ).hexdigest()


def _valid_identities(identities: object) -> bool:
    return (
        isinstance(identities, tuple)
        and bool(identities)
        and all(isinstance(identity, RelationshipIdentity) for identity in identities)
        and len({identity.identity_id for identity in identities}) == len(identities)
    )


def _sorted_identities(
    identities: tuple[RelationshipIdentity, ...],
) -> tuple[RelationshipIdentity, ...]:
    return tuple(sorted(identities, key=lambda identity: identity.identity_id))


def _is_ref(value: object) -> bool:
    return isinstance(value, str) and _OPAQUE_REF.fullmatch(value) is not None

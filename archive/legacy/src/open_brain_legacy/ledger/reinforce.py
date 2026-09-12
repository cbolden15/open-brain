"""Local claim ranking and citation-preserving semantic reinforcement."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime

from .embed import embed_text
from .index import ClaimRecord
from .merge import TrustedCitation


def _similarity(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    if len(left) != len(right) or not left:
        raise ValueError("incompatible claim embeddings")
    return sum(a * b for a, b in zip(left, right, strict=True))


def rank_claims(
    *, query: str, claims: tuple[ClaimRecord, ...], limit: int
) -> tuple[ClaimRecord, ...]:
    if (
        not isinstance(claims, tuple)
        or not claims
        or any(not isinstance(claim, ClaimRecord) for claim in claims)
        or not isinstance(limit, int)
        or isinstance(limit, bool)
        or limit < 1
    ):
        raise ValueError("invalid claim ranking input")
    dimensions = len(claims[0].embedding or ())
    if dimensions == 0:
        raise ValueError("claim embedding unavailable")
    query_embedding = embed_text(query, dimensions=dimensions)
    scored: list[tuple[float, str, ClaimRecord]] = []
    for claim in claims:
        claim.validate()
        if claim.embedding is None:
            raise ValueError("claim embedding unavailable")
        scored.append((_similarity(query_embedding, claim.embedding), claim.claim_id, claim))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return tuple(item[2] for item in scored[:limit])


def reinforce_claims(
    claims: tuple[ClaimRecord, ...],
    *,
    reinforced_at: datetime,
    similarity_threshold: float,
) -> tuple[ClaimRecord, ...]:
    if (
        not isinstance(claims, tuple)
        or any(not isinstance(claim, ClaimRecord) for claim in claims)
        or not isinstance(similarity_threshold, float)
        or not math.isfinite(similarity_threshold)
        or not 0.0 <= similarity_threshold <= 1.0
    ):
        raise ValueError("invalid claim reinforcement input")
    pending = list(sorted(claims, key=lambda claim: claim.claim_id))
    reinforced: list[ClaimRecord] = []
    while pending:
        survivor = pending.pop(0)
        survivor.validate()
        if survivor.embedding is None:
            raise ValueError("claim embedding unavailable")
        matches: list[ClaimRecord] = []
        remaining: list[ClaimRecord] = []
        for candidate in pending:
            candidate.validate()
            if candidate.embedding is None:
                raise ValueError("claim embedding unavailable")
            if _similarity(survivor.embedding, candidate.embedding) >= similarity_threshold:
                matches.append(candidate)
            else:
                remaining.append(candidate)
        pending = remaining
        if matches:
            survivor = _merge_claims(survivor, tuple(matches), reinforced_at=reinforced_at)
        reinforced.append(survivor)
    return tuple(sorted(reinforced, key=lambda claim: claim.claim_id))


def _merge_claims(
    survivor: ClaimRecord,
    matches: tuple[ClaimRecord, ...],
    *,
    reinforced_at: datetime,
) -> ClaimRecord:
    citations: dict[str, TrustedCitation] = {}
    for claim in (survivor, *matches):
        for citation in claim.citations:
            existing = citations.get(citation.citation_id)
            if existing is not None and existing != citation:
                raise ValueError("claim citation conflict")
            citations[citation.citation_id] = citation
    value = replace(
        survivor,
        citations=tuple(citations[key] for key in sorted(citations)),
        first_seen_at=min(claim.first_seen_at for claim in (survivor, *matches)),
        last_reinforced_at=reinforced_at,
        reinforcement_count=sum(claim.reinforcement_count for claim in (survivor, *matches)),
    )
    value.validate()
    return value

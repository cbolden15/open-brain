"""Typed, citation-bound claim indexing for local ledger lifecycle operations."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256

from open_brain_engine.core.ids import canonical_json_bytes

from .merge import TrustedCitation
from .sanitize import LedgerSection, sanitize_leaf

_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")


class ClaimStatus(StrEnum):
    ACTIVE = "active"
    AGING = "aging"
    RETIRED = "retired"


def _timestamp(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("invalid claim timestamp")
    return value.astimezone(UTC)


def _citations(values: tuple[TrustedCitation, ...]) -> tuple[TrustedCitation, ...]:
    if not isinstance(values, tuple) or not values or any(
        not isinstance(citation, TrustedCitation) for citation in values
    ):
        raise ValueError("claim requires trusted citations")
    ordered = tuple(sorted(values, key=lambda citation: citation.citation_id))
    for citation in ordered:
        citation.validate()
    if len({citation.citation_id for citation in ordered}) != len(ordered):
        raise ValueError("duplicate claim citation")
    return ordered


@dataclass(frozen=True, slots=True)
class ClaimInput:
    topic_id: str
    text: str
    normalized_key: str
    citations: tuple[TrustedCitation, ...]
    observed_at: datetime

    @classmethod
    def create(
        cls,
        *,
        topic_id: str,
        text: str,
        citations: tuple[TrustedCitation, ...],
        observed_at: datetime,
    ) -> ClaimInput:
        if not isinstance(topic_id, str) or not _IDENTIFIER.fullmatch(topic_id):
            raise ValueError("invalid claim topic")
        sanitized = sanitize_leaf(
            item_id="claim-input",
            section=LedgerSection.SUMMARY,
            text=text,
        )
        if sanitized.leaf is None:
            raise ValueError("invalid claim text")
        return cls(
            topic_id=topic_id,
            text=sanitized.leaf.text,
            normalized_key=sanitized.leaf.normalized_key,
            citations=_citations(citations),
            observed_at=_timestamp(observed_at),
        )

    def validate(self) -> None:
        recreated = ClaimInput.create(
            topic_id=self.topic_id,
            text=self.text,
            citations=self.citations,
            observed_at=self.observed_at,
        )
        if recreated != self:
            raise ValueError("claim input binding mismatch")


@dataclass(frozen=True, slots=True)
class ClaimRecord:
    claim_id: str
    topic_id: str
    text: str
    normalized_key: str
    citations: tuple[TrustedCitation, ...]
    first_seen_at: datetime
    last_reinforced_at: datetime
    reinforcement_count: int
    status: ClaimStatus
    embedding: tuple[float, ...] | None

    @classmethod
    def from_input(cls, value: ClaimInput) -> ClaimRecord:
        value.validate()
        claim_id = "claim_" + sha256(
            canonical_json_bytes(
                {"normalized_key": value.normalized_key, "topic_id": value.topic_id}
            )
        ).hexdigest()
        return cls(
            claim_id=claim_id,
            topic_id=value.topic_id,
            text=value.text,
            normalized_key=value.normalized_key,
            citations=value.citations,
            first_seen_at=value.observed_at,
            last_reinforced_at=value.observed_at,
            reinforcement_count=1,
            status=ClaimStatus.ACTIVE,
            embedding=None,
        )

    def validate(self) -> None:
        expected_id = "claim_" + sha256(
            canonical_json_bytes(
                {"normalized_key": self.normalized_key, "topic_id": self.topic_id}
            )
        ).hexdigest()
        recreated = ClaimInput.create(
            topic_id=self.topic_id,
            text=self.text,
            citations=self.citations,
            observed_at=self.first_seen_at,
        )
        if (
            self.claim_id != expected_id
            or recreated.normalized_key != self.normalized_key
            or _timestamp(self.last_reinforced_at) < recreated.observed_at
            or not isinstance(self.reinforcement_count, int)
            or isinstance(self.reinforcement_count, bool)
            or self.reinforcement_count < 1
            or not isinstance(self.status, ClaimStatus)
        ):
            raise ValueError("invalid claim record")
        if self.embedding is not None and (
            not isinstance(self.embedding, tuple)
            or not self.embedding
            or any(
                not isinstance(component, float)
                or not math.isfinite(component)
                or not -1.0 <= component <= 1.0
                for component in self.embedding
            )
        ):
            raise ValueError("invalid claim embedding")


def index_claims(values: tuple[ClaimInput, ...]) -> tuple[ClaimRecord, ...]:
    """Dedupe normalized claims while preserving every trusted citation."""
    if not isinstance(values, tuple) or any(not isinstance(value, ClaimInput) for value in values):
        raise ValueError("invalid claim inputs")
    indexed: dict[tuple[str, str], ClaimRecord] = {}
    ordered_inputs = sorted(
        values,
        key=lambda value: (value.observed_at, value.topic_id, value.normalized_key),
    )
    for value in ordered_inputs:
        value.validate()
        key = (value.topic_id, value.normalized_key)
        current = indexed.get(key)
        if current is None:
            indexed[key] = ClaimRecord.from_input(value)
            continue
        citations = _citations(
            tuple(
                {
                    citation.citation_id: citation
                    for citation in (*current.citations, *value.citations)
                }
                .values()
            )
        )
        indexed[key] = ClaimRecord(
            claim_id=current.claim_id,
            topic_id=current.topic_id,
            text=current.text,
            normalized_key=current.normalized_key,
            citations=citations,
            first_seen_at=min(current.first_seen_at, value.observed_at),
            last_reinforced_at=max(current.last_reinforced_at, value.observed_at),
            reinforcement_count=current.reinforcement_count,
            status=ClaimStatus.ACTIVE,
            embedding=None,
        )
    result = tuple(sorted(indexed.values(), key=lambda claim: claim.claim_id))
    for claim in result:
        claim.validate()
    return result

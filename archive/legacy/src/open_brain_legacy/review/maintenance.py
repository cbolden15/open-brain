"""Typed legacy-parity contracts for curation target edits and review archival."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from hashlib import sha256
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import cast

from open_brain_engine.core.ids import ReviewId, canonical_json_bytes, validate_identifier
from open_brain_engine.core.models import PrivacyTier, ValidationError
from open_brain_engine.review.models import Actor, ActorKind, ReviewAggregate, ReviewState

_CATEGORY = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_SLUG_PART = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
_MONTH = re.compile(r"\d{4}-\d{2}")
_DOMAIN_ALIASES = MappingProxyType(
    {"projects": PrivacyTier.WORK, "dev": PrivacyTier.WORK, "business": PrivacyTier.WORK}
)
_PREDECESSOR_CATEGORIES = MappingProxyType(
    {
        PrivacyTier.WORK: frozenset(
            {
                "agents",
                "clients",
                "decisions",
                "entity",
                "environments",
                "evaluations",
                "integrations",
                "learning",
                "mcp",
                "models",
                "patterns",
                "projects",
                "skills",
                "strategy",
                "tools",
                "workflows",
            }
        ),
        PrivacyTier.PERSONAL: frozenset(
            {
                "errands",
                "finance",
                "health",
                "home",
                "learning",
                "media",
                "notes",
                "people",
                "relationships",
            }
        ),
    }
)


class CurationClass(StrEnum):
    NOISE = "noise"
    JOURNAL = "journal"
    PAGE_UPDATE = "page_update"
    NEW_PAGE = "new_page"
    PATTERN = "pattern"


@dataclass(frozen=True, slots=True)
class CurationTaxonomy:
    categories: Mapping[PrivacyTier, frozenset[str]]

    def __post_init__(self) -> None:
        normalized: dict[PrivacyTier, frozenset[str]] = {}
        if not isinstance(self.categories, Mapping):
            raise ValidationError("invalid curation taxonomy")
        for raw_tier, raw_categories in self.categories.items():
            try:
                tier = PrivacyTier(raw_tier)
            except ValueError as error:
                raise ValidationError("invalid curation taxonomy") from error
            if tier not in {PrivacyTier.WORK, PrivacyTier.PERSONAL} or not isinstance(
                raw_categories, frozenset
            ):
                raise ValidationError("invalid curation taxonomy")
            categories = frozenset(_category(value) for value in raw_categories)
            if not categories:
                raise ValidationError("invalid curation taxonomy")
            normalized[tier] = categories
        if set(normalized) != {PrivacyTier.WORK, PrivacyTier.PERSONAL}:
            raise ValidationError("invalid curation taxonomy")
        object.__setattr__(self, "categories", MappingProxyType(normalized))

    def require(self, *, tier: PrivacyTier, category: str) -> str:
        normalized = _category(category)
        if normalized not in self.categories.get(tier, frozenset()):
            raise ValidationError("invalid curation category")
        return normalized


def predecessor_curation_taxonomy() -> CurationTaxonomy:
    """Return the immutable category contract extracted from the predecessor."""
    return CurationTaxonomy(categories=_PREDECESSOR_CATEGORIES)


@dataclass(frozen=True, slots=True)
class ReviewTargetEdit:
    tier: PrivacyTier
    category: str
    slug: str
    title: str | None
    classification_class: CurationClass | None
    occurred_at: datetime
    actor: Actor

    @classmethod
    def create(
        cls,
        *,
        tier: PrivacyTier | str,
        category: str,
        slug: str,
        title: str | None,
        classification_class: CurationClass | str | None,
        occurred_at: datetime,
        actor: Actor,
    ) -> ReviewTargetEdit:
        try:
            raw_tier = unicodedata.normalize("NFC", str(tier)).strip().casefold()
            destination = _DOMAIN_ALIASES.get(raw_tier)
            if destination is None:
                destination = PrivacyTier(raw_tier)
            kind = (
                None
                if classification_class is None
                else CurationClass(classification_class)
            )
        except ValueError as error:
            raise ValidationError("invalid curation target") from error
        if destination not in {PrivacyTier.WORK, PrivacyTier.PERSONAL}:
            raise ValidationError("invalid curation target")
        if not isinstance(actor, Actor) or actor.kind is not ActorKind.OWNER:
            raise ValidationError("invalid curation target actor")
        return cls(
            destination,
            _category(category),
            _slug(slug),
            _title(title),
            kind,
            _utc(occurred_at),
            actor,
        )


@dataclass(frozen=True, slots=True)
class CurationTarget:
    review_id: ReviewId
    source_privacy_tier: PrivacyTier
    tier: PrivacyTier
    category: str
    slug: str
    page: PurePosixPath
    title: str | None
    classification_class: CurationClass
    updated_at: datetime

    @classmethod
    def create(
        cls,
        *,
        review: ReviewAggregate,
        tier: PrivacyTier | str,
        category: str,
        slug: str,
        title: str | None,
        classification_class: CurationClass | str,
        occurred_at: datetime,
        taxonomy: CurationTaxonomy,
    ) -> CurationTarget:
        if not isinstance(review, ReviewAggregate) or review.proposal.state is not ReviewState.OPEN:
            raise ValidationError("invalid curation review")
        command = ReviewTargetEdit.create(
            tier=tier,
            category=category,
            slug=slug,
            title=title,
            classification_class=classification_class,
            occurred_at=occurred_at,
            actor=Actor(ActorKind.OWNER, "target-registration"),
        )
        if command.classification_class is None:
            raise ValidationError("invalid curation target")
        return cls._from_edit(
            review_id=review.proposal.review_id,
            source_privacy_tier=review.proposal.privacy_tier,
            command=command,
            taxonomy=taxonomy,
            fallback=None,
        )

    @classmethod
    def _from_edit(
        cls,
        *,
        review_id: ReviewId,
        source_privacy_tier: PrivacyTier,
        command: ReviewTargetEdit,
        taxonomy: CurationTaxonomy,
        fallback: CurationTarget | None,
    ) -> CurationTarget:
        category = taxonomy.require(tier=command.tier, category=command.category)
        _require_privacy(source=source_privacy_tier, destination=command.tier)
        classification = command.classification_class
        if classification is None:
            if fallback is None:
                raise ValidationError("invalid curation target")
            classification = fallback.classification_class
        title = command.title if command.title is not None else (
            None if fallback is None else fallback.title
        )
        page = PurePosixPath(category, command.slug + ".md")
        return cls(
            review_id,
            source_privacy_tier,
            command.tier,
            category,
            command.slug,
            page,
            title,
            classification,
            command.occurred_at,
        )

    def edit(
        self, command: ReviewTargetEdit, *, taxonomy: CurationTaxonomy
    ) -> CurationTarget:
        if not isinstance(command, ReviewTargetEdit):
            raise ValidationError("invalid curation target edit")
        return self._from_edit(
            review_id=self.review_id,
            source_privacy_tier=self.source_privacy_tier,
            command=command,
            taxonomy=taxonomy,
            fallback=self,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "review_id": str(self.review_id),
            "source_privacy_tier": self.source_privacy_tier.value,
            "tier": self.tier.value,
            "category": self.category,
            "slug": self.slug,
            "page": self.page.as_posix(),
            "title": self.title,
            "classification_class": self.classification_class.value,
            "updated_at": _timestamp(self.updated_at),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> CurationTarget:
        if set(value) != {
            "review_id",
            "source_privacy_tier",
            "tier",
            "category",
            "slug",
            "page",
            "title",
            "classification_class",
            "updated_at",
        }:
            raise ValidationError("invalid curation target")
        try:
            review_id = ReviewId(
                validate_identifier(cast(str, value["review_id"]), prefix="review_")
            )
            source_tier = PrivacyTier(cast(str, value["source_privacy_tier"]))
            tier = PrivacyTier(cast(str, value["tier"]))
            classification = CurationClass(cast(str, value["classification_class"]))
            category = _category(cast(str, value["category"]))
            slug = _slug(cast(str, value["slug"]))
            page = PurePosixPath(cast(str, value["page"]))
            title = _title(cast(str | None, value["title"]))
            updated_at = _parse_timestamp(cast(str, value["updated_at"]))
        except (TypeError, ValueError) as error:
            raise ValidationError("invalid curation target") from error
        _require_privacy(source=source_tier, destination=tier)
        if page != PurePosixPath(category, slug + ".md"):
            raise ValidationError("invalid curation target")
        return cls(
            review_id,
            source_tier,
            tier,
            category,
            slug,
            page,
            title,
            classification,
            updated_at,
        )

    def digest_sha256(self) -> str:
        return sha256(canonical_json_bytes(self.to_dict())).hexdigest()


@dataclass(frozen=True, slots=True)
class ReviewMaintenanceEvent:
    event_id: str
    review_id: ReviewId
    action: str
    occurred_at: datetime
    actor: Actor
    evidence_sha256: str

    @classmethod
    def create(
        cls,
        *,
        review_id: ReviewId,
        action: str,
        occurred_at: datetime,
        actor: Actor,
        evidence_sha256: str,
    ) -> ReviewMaintenanceEvent:
        if action not in {"edited", "archived"} or not re.fullmatch(
            r"[0-9a-f]{64}", evidence_sha256
        ):
            raise ValidationError("invalid review maintenance event")
        timestamp = _utc(occurred_at)
        payload = canonical_json_bytes(
            {
                "review_id": str(review_id),
                "action": action,
                "occurred_at": _timestamp(timestamp),
                "actor": actor.to_dict(),
                "evidence_sha256": evidence_sha256,
            }
        )
        return cls(
            "maintenance_" + sha256(payload).hexdigest(),
            review_id,
            action,
            timestamp,
            actor,
            evidence_sha256,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "review_id": str(self.review_id),
            "action": self.action,
            "occurred_at": _timestamp(self.occurred_at),
            "actor": self.actor.to_dict(),
            "evidence_sha256": self.evidence_sha256,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ReviewMaintenanceEvent:
        if set(value) != {
            "event_id",
            "review_id",
            "action",
            "occurred_at",
            "actor",
            "evidence_sha256",
        }:
            raise ValidationError("invalid review maintenance event")
        try:
            review_id = ReviewId(
                validate_identifier(cast(str, value["review_id"]), prefix="review_")
            )
            event = cls.create(
                review_id=review_id,
                action=cast(str, value["action"]),
                occurred_at=_parse_timestamp(cast(str, value["occurred_at"])),
                actor=Actor.from_dict(cast(Mapping[str, object], value["actor"])),
                evidence_sha256=cast(str, value["evidence_sha256"]),
            )
        except (TypeError, ValueError) as error:
            raise ValidationError("invalid review maintenance event") from error
        if value["event_id"] != event.event_id:
            raise ValidationError("invalid review maintenance event")
        return event


@dataclass(frozen=True, slots=True)
class ArchivedReview:
    aggregate: ReviewAggregate
    target: CurationTarget | None
    closed_month: str
    archived_at: datetime


@dataclass(frozen=True, slots=True)
class ArchiveResult:
    archived: int
    months: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.archived, int)
            or isinstance(self.archived, bool)
            or self.archived < 0
            or self.months != tuple(sorted(set(self.months)))
            or any(validate_month(month) != month for month in self.months)
        ):
            raise ValidationError("invalid archive result")


def validate_month(value: str) -> str:
    if not isinstance(value, str) or _MONTH.fullmatch(value) is None:
        raise ValidationError("invalid archive month")
    try:
        date.fromisoformat(value + "-01")
    except ValueError as error:
        raise ValidationError("invalid archive month") from error
    return value


def closed_month(aggregate: ReviewAggregate) -> str | None:
    if aggregate.proposal.state not in {ReviewState.APPLIED, ReviewState.REJECTED}:
        return None
    if len(aggregate.events) != 1:
        raise ValidationError("invalid terminal review")
    return _timestamp(aggregate.events[0].occurred_at)[:7]


def _category(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value).strip().casefold()
    if _CATEGORY.fullmatch(normalized) is None:
        raise ValidationError("invalid curation category")
    return normalized


def _slug(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value).strip().casefold()
    if normalized.endswith(".md"):
        normalized = normalized[:-3]
    path = PurePosixPath(normalized)
    if (
        not normalized
        or path.is_absolute()
        or ".." in path.parts
        or len(path.parts) > 8
        or any(_SLUG_PART.fullmatch(part) is None for part in path.parts)
    ):
        raise ValidationError("invalid curation slug")
    return path.as_posix()


def _title(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = unicodedata.normalize("NFC", value).strip()
    if (
        not normalized
        or len(normalized) > 200
        or any(marker in normalized for marker in ("\r", "\n", "\u0085", "\u2028", "\u2029"))
    ):
        raise ValidationError("invalid curation title")
    return normalized


def _require_privacy(*, source: PrivacyTier, destination: PrivacyTier) -> None:
    if source in {PrivacyTier.SECRET, PrivacyTier.UNKNOWN}:
        raise ValidationError("curation target privacy unavailable")
    if source is PrivacyTier.PERSONAL and destination is not PrivacyTier.PERSONAL:
        raise ValidationError("curation target privacy widening")
    if destination not in {PrivacyTier.WORK, PrivacyTier.PERSONAL}:
        raise ValidationError("curation target privacy unavailable")


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("invalid timestamp")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _utc(value).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_timestamp(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValidationError("invalid timestamp")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError as error:
        raise ValidationError("invalid timestamp") from error

"""Bounded, read-only work context built on the shared retrieval seam."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from open_brain_legacy._compat.open_brain.integrations.ports import (
    RedactedText,
    RetrievalBatch,
    RetrievalRequest,
    TrustLabel,
)

from .repository_identity import StableRepoIdentity
from .retrieval import WorkPageSnapshot

_WORD = re.compile(r"[A-Za-z0-9]+")
_HEADING = re.compile(r"^(?P<marks>#{1,6})\s+(?P<title>.+?)\s*$", re.MULTILINE)
_MAX_QUERY_LENGTH = 4_096
_MAX_CONTEXT_ITEMS = 8
_MIN_CONTEXT_BYTES = 64
_MAX_CONTEXT_BYTES = 65_536


class ContextStatus(StrEnum):
    """Structural outcomes for a read-only work context request."""

    AVAILABLE = "available"
    EMPTY = "empty"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ContextRequest:
    """Bounded context input using topic text and optional path-free repo identity."""

    topic: str | None = None
    repository: StableRepoIdentity | None = None
    limit: int = 4
    max_bytes: int = 2_000

    def __post_init__(self) -> None:
        if (
            (self.topic is not None and not _valid_topic(self.topic))
            or (self.repository is not None and not isinstance(self.repository, StableRepoIdentity))
            or (self.topic is None and self.repository is None)
            or type(self.limit) is not int
            or not 1 <= self.limit <= _MAX_CONTEXT_ITEMS
            or type(self.max_bytes) is not int
            or not _MIN_CONTEXT_BYTES <= self.max_bytes <= _MAX_CONTEXT_BYTES
        ):
            raise ValueError("invalid context request")

    @property
    def query(self) -> str:
        values = []
        if self.topic is not None:
            values.append(self.topic)
        if self.repository is not None:
            values.append(self.repository.slug)
        return " ".join(values)


@dataclass(frozen=True, slots=True)
class ContextItem:
    """One path-free context section with its retrieval trust label intact."""

    result_id: str
    title: RedactedText
    heading: RedactedText
    content: RedactedText
    trust: TrustLabel
    hop: int

    def __post_init__(self) -> None:
        if (
            re.fullmatch(r"result_[0-9a-f]{32}", self.result_id) is None
            or not isinstance(self.title, RedactedText)
            or not isinstance(self.heading, RedactedText)
            or not isinstance(self.content, RedactedText)
            or not isinstance(self.trust, TrustLabel)
            or type(self.hop) is not int
            or self.hop not in {0, 1}
        ):
            raise ValueError("invalid context item")

    @property
    def public_bytes(self) -> int:
        return sum(
            len(value.text.encode("utf-8"))
            for value in (self.title, self.heading, self.content)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "result_id": self.result_id,
            "title": self.title.to_dict(),
            "heading": self.heading.to_dict(),
            "content": self.content.to_dict(),
            "trust": self.trust.value,
            "hop": self.hop,
        }


@dataclass(frozen=True, slots=True)
class ContextBlock:
    """A bounded work-context response with explicit availability state."""

    context_id: str
    status: ContextStatus
    items: tuple[ContextItem, ...]
    bytes_used: int
    max_bytes: int
    truncated: bool

    def __post_init__(self) -> None:
        if (
            re.fullmatch(r"context_[0-9a-f]{32}", self.context_id) is None
            or not isinstance(self.status, ContextStatus)
            or not isinstance(self.items, tuple)
            or len(self.items) > _MAX_CONTEXT_ITEMS
            or any(not isinstance(item, ContextItem) for item in self.items)
            or type(self.bytes_used) is not int
            or self.bytes_used != sum(item.public_bytes for item in self.items)
            or type(self.max_bytes) is not int
            or not _MIN_CONTEXT_BYTES <= self.max_bytes <= _MAX_CONTEXT_BYTES
            or self.bytes_used > self.max_bytes
            or type(self.truncated) is not bool
            or (
                self.status in {ContextStatus.UNAVAILABLE, ContextStatus.FAILED}
                and (self.items or self.bytes_used != 0 or self.truncated)
            )
            or (self.status is ContextStatus.AVAILABLE and not self.items)
            or (self.status is ContextStatus.EMPTY and self.items)
        ):
            raise ValueError("invalid context block")

    def to_dict(self) -> dict[str, object]:
        return {
            "context_id": self.context_id,
            "scope": "work",
            "status": self.status.value,
            "results": [item.to_dict() for item in self.items],
            "bytes_used": self.bytes_used,
            "max_bytes": self.max_bytes,
            "truncated": self.truncated,
        }


class ContextRetriever(Protocol):
    """The concrete retrieval operations required for one-hop context."""

    @property
    def available(self) -> bool: ...

    def search(self, request: RetrievalRequest) -> RetrievalBatch: ...

    def snapshot(self, result_id: str) -> WorkPageSnapshot | None: ...


class WorkContextService:
    """Select bounded sections from work retrieval results and one link hop."""

    def __init__(self, *, retriever: ContextRetriever) -> None:
        self._retriever = retriever

    def retrieve(self, request: ContextRequest) -> ContextBlock:
        if not isinstance(request, ContextRequest):
            raise ValueError("invalid context request")
        if not self._retriever.available:
            return _empty_block(ContextStatus.UNAVAILABLE, request.max_bytes)

        batch = self._retriever.search(
            RetrievalRequest(question=request.query, limit=request.limit)
        )
        if not batch.hits:
            return _empty_block(ContextStatus.EMPTY, request.max_bytes)

        candidates: list[tuple[str, int]] = [(batch.hits[0].result_id, 0)]
        top_page = self._retriever.snapshot(batch.hits[0].result_id)
        if top_page is not None:
            candidates.extend((result_id, 1) for result_id in top_page.linked_result_ids)
        candidates.extend((hit.result_id, 0) for hit in batch.hits[1:])

        terms = _terms(request.query)
        items: list[ContextItem] = []
        seen: set[str] = set()
        used_bytes = 0
        truncated = batch.truncated
        for result_id, hop in candidates:
            if result_id in seen:
                continue
            seen.add(result_id)
            if len(items) >= request.limit:
                truncated = True
                break
            page = self._retriever.snapshot(result_id)
            if page is None:
                continue
            heading, content = _best_section(page.markdown.text, terms)
            remaining = request.max_bytes - used_bytes
            item, was_truncated = _bounded_item(
                page=page,
                heading=heading,
                content=content,
                hop=hop,
                maximum=remaining,
            )
            truncated = truncated or was_truncated
            if item is None:
                truncated = True
                continue
            items.append(item)
            used_bytes += item.public_bytes

        if not items:
            return ContextBlock(
                context_id=_context_id(),
                status=ContextStatus.EMPTY,
                items=(),
                bytes_used=0,
                max_bytes=request.max_bytes,
                truncated=truncated,
            )
        return ContextBlock(
            context_id=_context_id(),
            status=ContextStatus.AVAILABLE,
            items=tuple(items),
            bytes_used=used_bytes,
            max_bytes=request.max_bytes,
            truncated=truncated,
        )

    def retrieve_fail_zero(self, request: ContextRequest) -> ContextBlock:
        """Return a silent structural failure instead of raising at hook boundaries."""
        try:
            return self.retrieve(request)
        except Exception:
            return _empty_block(ContextStatus.FAILED, request.max_bytes)


def _valid_topic(value: str) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= _MAX_QUERY_LENGTH
        and not value.isspace()
        and not any(ord(character) < 32 and character not in {"\n", "\t"} for character in value)
    )


def _terms(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(match.group().casefold() for match in _WORD.finditer(value)))


def _strip_frontmatter(markdown: str) -> str:
    if not markdown.startswith("---\n"):
        return markdown
    end = markdown.find("\n---\n", 4)
    return markdown[end + 5 :] if end >= 0 else markdown


def _best_section(markdown: str, terms: tuple[str, ...]) -> tuple[str, str]:
    body = _strip_frontmatter(markdown)
    matches = tuple(_HEADING.finditer(body))
    if not matches:
        normalized = " ".join(body.split())
        return "Context", normalized or "[no public context]"

    sections: list[tuple[int, str, str]] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        sections.append((len(match.group("marks")), match.group("title"), body[start:end].strip()))
    detailed = [section for section in sections if section[0] >= 2]
    candidates = detailed or sections
    _, heading, content = max(
        candidates,
        key=lambda section: (
            sum(section[1].casefold().count(term) * 3 for term in terms)
            + sum(section[2].casefold().count(term) for term in terms),
            -sections.index(section),
        ),
    )
    normalized = " ".join(content.split())
    return heading.strip(), normalized or "[no public context]"


def _bounded_item(
    *,
    page: WorkPageSnapshot,
    heading: str,
    content: str,
    hop: int,
    maximum: int,
) -> tuple[ContextItem | None, bool]:
    public_heading = RedactedText.redact(heading[:256])
    base_bytes = len(page.title.text.encode("utf-8")) + len(
        public_heading.text.encode("utf-8")
    )
    if maximum <= base_bytes:
        return None, True
    bounded_content, truncated = _truncate_utf8(content, maximum - base_bytes)
    if not bounded_content:
        return None, True
    item = ContextItem(
        result_id=page.result_id,
        title=page.title,
        heading=public_heading,
        content=RedactedText.redact(bounded_content),
        trust=page.trust,
        hop=hop,
    )
    return item, truncated


def _truncate_utf8(value: str, maximum: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum:
        return value, False
    clipped = encoded[:maximum]
    while clipped:
        try:
            return clipped.decode("utf-8").rstrip(), True
        except UnicodeDecodeError:
            clipped = clipped[:-1]
    return "", True


def _context_id() -> str:
    return "context_" + secrets.token_hex(16)


def _empty_block(status: ContextStatus, max_bytes: int) -> ContextBlock:
    return ContextBlock(
        context_id=_context_id(),
        status=status,
        items=(),
        bytes_used=0,
        max_bytes=max_bytes,
        truncated=False,
    )

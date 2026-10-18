"""Typed seam between the engine and a local embedding backend.

The engine owns authorization, chunk identity and scoring; a backend only turns
engine-authorized strings into validated unit vectors. The installed helper
transport lives in the app and satisfies ``EmbeddingBackend`` structurally, so
the engine never imports the app.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

EMBEDDING_ERROR_SCHEMA = "ob.embedding-error.v1"
EMBEDDING_DIMENSIONS = 384
EMBEDDING_ERROR_CATEGORIES = frozenset(
    {
        "consent_required",
        "resource_missing",
        "resource_untrusted",
        "component_mismatch",
        "unsupported_platform",
        "input_limit",
        "protocol_invalid",
        "worker_failed",
        "deadline",
        "cancelled",
        "cleanup_failed",
        "cache_invalid",
        "cache_incomplete",
        "currentness_changed",
    }
)
# Owner-visible hint only: a fresh explicit operation may succeed. Never replayed.
_RETRYABLE = frozenset({"currentness_changed", "cache_incomplete", "deadline"})
_HEX64 = re.compile(r"^[0-9a-f]{64}$")

Purpose = Literal["query", "document"]


class EmbeddingFailure(Exception):
    """One bounded ``ob.embedding-error.v1`` category; never native or text details."""

    def __init__(self, category: str) -> None:
        if category not in EMBEDDING_ERROR_CATEGORIES:
            raise ValueError("invalid embedding failure category")
        self.category = category
        super().__init__(category)

    @property
    def retryable(self) -> bool:
        return self.category in _RETRYABLE


def _count(value: object) -> bool:
    return type(value) is int and value >= 0


@dataclass(frozen=True, slots=True)
class EmbeddingChunk:
    ordinal: int
    token_start: int
    token_end: int
    byte_start: int
    byte_end: int
    vector: tuple[float, ...]

    def __post_init__(self) -> None:
        if (
            not all(
                _count(value)
                for value in (
                    self.ordinal,
                    self.token_start,
                    self.token_end,
                    self.byte_start,
                    self.byte_end,
                )
            )
            or self.token_start >= self.token_end
            or self.byte_start >= self.byte_end
            or type(self.vector) is not tuple
            or len(self.vector) != EMBEDDING_DIMENSIONS
            or not all(type(value) is float and math.isfinite(value) for value in self.vector)
        ):
            raise ValueError("invalid embedding chunk")


@dataclass(frozen=True, slots=True)
class EmbeddedItem:
    slot: int
    text_sha256: str
    token_count: int
    chunks: tuple[EmbeddingChunk, ...]

    def __post_init__(self) -> None:
        if (
            not _count(self.slot)
            or not isinstance(self.text_sha256, str)
            or _HEX64.fullmatch(self.text_sha256) is None
            or not _count(self.token_count)
            or self.token_count == 0
            or type(self.chunks) is not tuple
            or not self.chunks
            or not all(isinstance(item, EmbeddingChunk) for item in self.chunks)
        ):
            raise ValueError("invalid embedded item")


class EmbeddingBackend(Protocol):
    """Embed NFC engine-projected strings; raise ``EmbeddingFailure`` on any failure."""

    component_id: str

    def embed(self, purpose: Purpose, texts: Sequence[str]) -> list[EmbeddedItem]: ...

"""Engine-side embedding backend protocol: value objects and failure categories only."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from open_brain_engine.engine import embedding_backend as backend
from open_brain_engine.engine.embedding_backend import (
    EmbeddedItem,
    EmbeddingBackend,
    EmbeddingChunk,
    EmbeddingFailure,
)

VECTOR = (1.0,) + (0.0,) * 383
SHA = "a" * 64


def chunk(**changes: object) -> EmbeddingChunk:
    values: dict[str, object] = {
        "ordinal": 0,
        "token_start": 0,
        "token_end": 3,
        "byte_start": 0,
        "byte_end": 11,
        "vector": VECTOR,
    }
    values.update(changes)
    return EmbeddingChunk(**values)  # type: ignore[arg-type]


def test_categories_are_the_section_five_set() -> None:
    assert backend.EMBEDDING_ERROR_SCHEMA == "ob.embedding-error.v1"
    expected = frozenset(
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
    assert expected == backend.EMBEDDING_ERROR_CATEGORIES


def test_failure_carries_one_category_and_retry_hint() -> None:
    failure = EmbeddingFailure("deadline")
    assert failure.category == "deadline"
    assert failure.retryable is True
    assert str(failure) == "deadline"
    assert EmbeddingFailure("worker_failed").retryable is False
    with pytest.raises(ValueError):
        EmbeddingFailure("native panic: /secret/path")


def test_chunk_and_item_are_frozen_values() -> None:
    item = EmbeddedItem(slot=0, text_sha256=SHA, token_count=3, chunks=(chunk(),))
    assert item.chunks[0].vector == VECTOR
    with pytest.raises(AttributeError):
        item.slot = 1  # type: ignore[misc]


@pytest.mark.parametrize(
    "changes",
    [
        {"ordinal": -1},
        {"ordinal": True},
        {"token_start": 3},
        {"byte_end": 0},
        {"vector": VECTOR[:-1]},
        {"vector": (float("nan"),) + VECTOR[1:]},
        {"vector": list(VECTOR)},
    ],
)
def test_chunk_rejects_invalid_values(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        chunk(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"slot": -1},
        {"text_sha256": "A" * 64},
        {"token_count": 0},
        {"chunks": ()},
        {"chunks": [chunk()]},
    ],
)
def test_item_rejects_invalid_values(changes: dict[str, object]) -> None:
    values: dict[str, object] = {
        "slot": 0,
        "text_sha256": SHA,
        "token_count": 3,
        "chunks": (chunk(),),
    }
    values.update(changes)
    with pytest.raises(ValueError):
        EmbeddedItem(**values)  # type: ignore[arg-type]


def test_protocol_is_structural() -> None:
    class Fixed:
        component_id = "b" * 64

        def embed(self, purpose: backend.Purpose, texts: object) -> list[EmbeddedItem]:
            return []

    accepted: EmbeddingBackend = Fixed()
    assert accepted.component_id == "b" * 64


def test_engine_module_does_not_import_the_app() -> None:
    tree = ast.parse(Path(backend.__file__).read_text(encoding="utf-8"))
    names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not any(name.startswith("open_brain.") or name == "open_brain" for name in names)

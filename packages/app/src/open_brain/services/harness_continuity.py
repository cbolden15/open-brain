"""Extractive capture batches with stable per-message selection indexes.

No byte/turn cursor certifies custody. An immutable outbox request and its durable
receipt certify each batch, so replay and a failed partial enqueue cannot skip work.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, cast

from .harness_memory import (
    _RECIPE_REQUEST,
    MAX_CANDIDATE_BYTES,
    MAX_CANDIDATES,
    MAX_INPUT_BYTES,
    MAX_MESSAGE_BYTES,
    MAX_TOTAL_BYTES,
    MemoryCandidate,
    MemoryMessage,
    _complete_recipe,
    _user_statements,
)

MAX_EVENT_MESSAGES = 16384
MAX_EVENT_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class IndexedMemory:
    candidate: MemoryCandidate
    index: int

    @property
    def key(self) -> tuple[int, int]:
        return self.candidate.ordinal, self.index


@dataclass(frozen=True, slots=True)
class CaptureBatch:
    messages: tuple[MemoryMessage, ...]
    selection: tuple[tuple[int, int], ...]


def indexed_memories(messages: Sequence[MemoryMessage]) -> tuple[IndexedMemory, ...]:
    """Apply the existing conservative syntax without a first-32/first-256 cutoff.

    Public event input bounds and each source/candidate byte bound still apply.
    Indexes are per original authored message, independent of preceding selection.
    """
    if (
        len(messages) > MAX_EVENT_MESSAGES
        or sum(len(m.text.encode()) for m in messages) > MAX_EVENT_BYTES
    ):
        raise ValueError("event_too_large")
    selected: list[IndexedMemory] = []
    recipe_requested = False
    for message in messages:
        if not message.authored:
            continue
        if len(message.text.encode()) > MAX_MESSAGE_BYTES:
            raise ValueError("message_too_large")
        excerpts: list[tuple[str, str]] = []
        if message.role == "user":
            recipe_requested = bool(_RECIPE_REQUEST.search(" ".join(message.text.split())))
            excerpts = (
                [("material", message.text)]
                if _complete_recipe(message.text)
                else list(_user_statements(message.text))
            )
        elif message.role == "assistant" and recipe_requested:
            excerpts = [("material", message.text)] if _complete_recipe(message.text) else []
            recipe_requested = False
        index = 0
        for kind, excerpt in excerpts:
            text = excerpt.strip() if kind == "material" else " ".join(excerpt.split())
            if max(len(text.encode()), len(excerpt.encode())) > MAX_CANDIDATE_BYTES:
                continue
            # Syntax comes from the same selector; kind is closed by construction.
            candidate = MemoryCandidate(
                cast(Literal["preference", "fact", "decision", "material"], kind),
                text,
                message.ordinal,
                message.role,
                excerpt,
                message.source_id,
            )
            selected.append(IndexedMemory(candidate, index))
            index += 1
    return tuple(selected)


def capture_batches(messages: Sequence[MemoryMessage]) -> tuple[CaptureBatch, ...]:
    """Keep original messages and recipe request context; never synthesize turns."""
    selected = indexed_memories(messages)
    by_ordinal = {message.ordinal: message for message in messages}
    previous_users: dict[int, MemoryMessage] = {}
    previous: MemoryMessage | None = None
    for message in messages:
        if message.authored and message.role == "user":
            previous = message
        elif message.authored and message.role == "assistant" and previous is not None:
            previous_users[message.ordinal] = previous
            previous = None
    batches: list[CaptureBatch] = []
    keys: list[tuple[int, int]] = []
    retained: dict[int, MemoryMessage] = {}
    selected_bytes = 0

    def flush() -> None:
        nonlocal keys, retained, selected_bytes
        if keys:
            batches.append(CaptureBatch(tuple(retained.values()), tuple(keys)))
        keys, retained, selected_bytes = [], {}, 0

    for item in selected:
        source = by_ordinal[item.candidate.ordinal]
        context = previous_users.get(source.ordinal) if source.role == "assistant" else None
        needed = ([context] if context is not None else []) + [source]
        combined = {**retained, **{m.ordinal: m for m in needed}}
        size = len(item.candidate.text.encode())
        if (
            len(keys) >= MAX_CANDIDATES
            or selected_bytes + size > MAX_TOTAL_BYTES
            or sum(len(json.dumps(m.text, ensure_ascii=False).encode()) for m in combined.values())
            > MAX_INPUT_BYTES
        ):
            flush()
        for message in needed:
            retained[message.ordinal] = message
        keys.append(item.key)
        selected_bytes += size
    flush()
    # Empty selection still carries markings and session metadata to custody.
    if not batches:
        batches.append(CaptureBatch((), ()))
    return tuple(batches)

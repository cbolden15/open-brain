"""Inert, source-bound memory material; citations do not establish factual truth."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

SOURCE_PREFIX = "urn:open-brain:harness:v1:"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_MAX_BYTES = 65_536


def _identifier(value: object) -> None:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValueError("invalid memory identifier")


def _text(value: object, maximum: int, *, empty: bool = False) -> None:
    if (
        not isinstance(value, str) or not empty and not value.strip()
        or isinstance(value, str) and (
            len(value.encode("utf-8")) > maximum
            or any(ord(char) < 32 and char not in "\n\r\t" for char in value)
        )
    ):
        raise ValueError("invalid memory text")


@dataclass(frozen=True, slots=True)
class HarnessCitation:
    harness: str
    client: str
    project_id: str
    session_id: str
    message_ordinal: int
    authorship: str
    timestamp: str | None

    def __post_init__(self) -> None:
        for value in (self.harness, self.client, self.project_id, self.session_id):
            _identifier(value)
        if self.harness not in {"codex", "claude-code", "pi"}:
            raise ValueError("invalid memory harness")
        if type(self.message_ordinal) is not int or not 0 <= self.message_ordinal <= 2**53 - 1:
            raise ValueError("invalid memory ordinal")
        if self.authorship not in {"user", "assistant"}:
            raise ValueError("invalid memory authorship")
        if self.timestamp is not None:
            if not isinstance(self.timestamp, str) or len(self.timestamp) > 64:
                raise ValueError("invalid memory timestamp")
            try:
                timestamp = datetime.fromisoformat(self.timestamp)
            except ValueError:
                raise ValueError("invalid memory timestamp") from None
            if timestamp.tzinfo is None:
                raise ValueError("memory timestamp requires timezone")


@dataclass(frozen=True, slots=True)
class HarnessMemory:
    kind: str
    text: str
    source_context: str
    citation: HarnessCitation
    version: int = 1

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != 1:
            raise ValueError("unsupported memory version")
        if self.kind not in {"fact", "decision", "preference", "material"}:
            raise ValueError("invalid memory kind")
        if not isinstance(self.citation, HarnessCitation):
            raise ValueError("invalid memory citation")
        _text(self.text, _MAX_BYTES)
        _text(self.source_context, 8192, empty=True)


def encode_memory(memory: HarnessMemory) -> str:
    if not isinstance(memory, HarnessMemory):
        raise ValueError("invalid memory")
    encoded = json.dumps(asdict(memory), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > _MAX_BYTES:
        raise ValueError("memory envelope exceeds text payload budget")
    return encoded


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate memory field")
        result[key] = value
    return result


def decode_memory(text: str) -> HarnessMemory:
    _text(text, _MAX_BYTES)
    try:
        raw = json.loads(text, object_pairs_hook=_unique)
        if not isinstance(raw, dict) or set(raw) != {
            "version", "kind", "text", "source_context", "citation",
        }:
            raise ValueError("invalid memory envelope")
        citation = raw["citation"]
        if not isinstance(citation, dict) or set(citation) != {
            "harness", "client", "project_id", "session_id", "message_ordinal",
            "authorship", "timestamp",
        }:
            raise ValueError("invalid memory citation")
        raw["citation"] = HarnessCitation(**citation)
        return HarnessMemory(**raw)
    except (TypeError, UnicodeError, ValueError):
        raise ValueError("invalid memory envelope") from None


def is_harness_source_reference(reference: str) -> bool:
    return (
        reference.startswith(SOURCE_PREFIX)
        and _ID.fullmatch(reference[len(SOURCE_PREFIX):]) is not None
    )


def recognized_memory(text: str, *, source_reference: str) -> HarnessMemory | None:
    if not is_harness_source_reference(source_reference):
        return None
    return decode_memory(text)

"""Typed, provider-backed capture distillation with replay-safe output."""

from __future__ import annotations

import json
import os
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Protocol, cast

from open_brain_engine.capture.models import ExtractionState, NormalizedExtraction
from open_brain_engine.core.ids import canonical_json_bytes, validate_identifier
from open_brain_engine.core.models import ContentKind, PrivacyDecision
from open_brain_engine.core.policy import BoundaryErrorCode, BoundaryResult
from open_brain_engine.core.ports import PutDisposition, PutResult, TextModelRequest
from open_brain_engine.providers.base import ProviderService


@dataclass(frozen=True, slots=True)
class DistillationInput:
    capture_id: str
    capture_why: str
    extraction: NormalizedExtraction

    @classmethod
    def create(
        cls,
        *,
        capture_id: str,
        capture_why: str,
        extraction: NormalizedExtraction,
    ) -> DistillationInput:
        try:
            validate_identifier(capture_id, prefix="cap_")
        except ValueError as error:
            raise ValueError("invalid distillation input") from error
        normalized_why = _bounded_text(capture_why, 2_000)
        if (
            not normalized_why.strip()
            or not isinstance(extraction, NormalizedExtraction)
            or extraction.state not in {ExtractionState.COMPLETE, ExtractionState.NO_CONTENT}
        ):
            raise ValueError("invalid distillation input")
        return cls(capture_id, normalized_why, extraction)


@dataclass(frozen=True, slots=True)
class DistilledCapture:
    schema_version: int
    capture_id: str
    capture_why: str
    content_kind: ContentKind
    title: str
    summary: str
    topics: tuple[str, ...]
    provider_name: str

    @classmethod
    def create(
        cls,
        *,
        capture_id: str,
        capture_why: str,
        content_kind: ContentKind | str,
        title: str,
        summary: str,
        topics: tuple[str, ...],
        provider_name: str,
    ) -> DistilledCapture:
        try:
            validate_identifier(capture_id, prefix="cap_")
            normalized_kind = ContentKind(content_kind)
        except (TypeError, ValueError) as error:
            raise ValueError("invalid distilled capture") from error
        normalized_topics = tuple(_bounded_text(topic, 80) for topic in topics)
        if (
            not isinstance(topics, tuple)
            or len(topics) > 20
            or normalized_topics != tuple(sorted(set(normalized_topics)))
        ):
            raise ValueError("invalid distilled capture")
        return cls(
            1,
            capture_id,
            _bounded_text(capture_why, 2_000),
            normalized_kind,
            _bounded_text(title, 512),
            _bounded_text(summary, 20_000),
            normalized_topics,
            _bounded_text(provider_name, 128),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "capture_id": self.capture_id,
            "capture_why": self.capture_why,
            "content_kind": self.content_kind.value,
            "title": self.title,
            "summary": self.summary,
            "topics": list(self.topics),
            "provider_name": self.provider_name,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> DistilledCapture:
        if set(value) != {
            "schema_version",
            "capture_id",
            "capture_why",
            "content_kind",
            "title",
            "summary",
            "topics",
            "provider_name",
        } or value["schema_version"] != 1:
            raise ValueError("invalid distilled capture")
        raw_topics = value["topics"]
        if not isinstance(raw_topics, list):
            raise ValueError("invalid distilled capture")
        return cls.create(
            capture_id=_string(value["capture_id"]),
            capture_why=_string(value["capture_why"]),
            content_kind=_string(value["content_kind"]),
            title=_string(value["title"]),
            summary=_string(value["summary"]),
            topics=tuple(_string(topic) for topic in raw_topics),
            provider_name=_string(value["provider_name"]),
        )


class DistillationStore(Protocol):
    def get(self, capture_id: str) -> DistilledCapture | None: ...

    def put_if_absent(self, result: DistilledCapture) -> PutResult: ...


class FilesystemDistillationStore:
    def __init__(self, root: Path) -> None:
        if not isinstance(root, Path) or not root.is_absolute():
            raise ValueError("distillation root must be absolute")
        self._root = root
        root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def get(self, capture_id: str) -> DistilledCapture | None:
        path = self._path(capture_id)
        if not path.exists():
            return None
        try:
            decoded = json.loads(path.read_text("utf-8"))
            return DistilledCapture.from_dict(_mapping(decoded))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise ValueError("invalid distilled capture") from error

    def put_if_absent(self, result: DistilledCapture) -> PutResult:
        if not isinstance(result, DistilledCapture):
            raise ValueError("invalid distilled capture")
        path = self._path(result.capture_id)
        payload = result.canonical_bytes()
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if self.get(result.capture_id) != result:
                raise ValueError("immutable distillation conflict") from None
            return PutResult(
                PutDisposition.DUPLICATE,
                result.capture_id,
                sha256(payload).hexdigest(),
            )
        try:
            _write_all(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory = os.open(self._root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return PutResult(PutDisposition.CREATED, result.capture_id, sha256(payload).hexdigest())

    def _path(self, capture_id: str) -> Path:
        validate_identifier(capture_id, prefix="cap_")
        return self._root / (capture_id + ".json")


class DistillationService:
    def __init__(
        self,
        *,
        store: DistillationStore,
        provider: ProviderService | None = None,
        timeout_seconds: float = 30.0,
        max_output_bytes: int = 64 * 1024,
    ) -> None:
        self._store = store
        self._provider = provider
        self._timeout_seconds = timeout_seconds
        self._max_output_bytes = max_output_bytes

    def distill(
        self,
        item: DistillationInput,
        *,
        privacy: PrivacyDecision,
    ) -> BoundaryResult[DistilledCapture]:
        if not isinstance(item, DistillationInput) or not isinstance(privacy, PrivacyDecision):
            raise ValueError("invalid distillation request")
        try:
            existing = self._store.get(item.capture_id)
        except Exception:
            return BoundaryResult(None, BoundaryErrorCode.IMPLEMENTATION_FAILURE)
        if existing is not None:
            if (
                existing.capture_why != item.capture_why
                or existing.content_kind is not item.extraction.content_kind
            ):
                return BoundaryResult(None, BoundaryErrorCode.IMPLEMENTATION_FAILURE)
            return BoundaryResult(existing, None)
        if self._provider is None:
            return BoundaryResult(None, BoundaryErrorCode.OPTIONAL_EXTRA_UNAVAILABLE)
        request = TextModelRequest.create(
            request_id="distill-" + item.capture_id,
            purpose="capture-distillation-v1",
            prompt=_prompt(item),
            timeout_seconds=self._timeout_seconds,
            max_output_bytes=self._max_output_bytes,
        )
        completion = self._provider.complete(request, privacy=privacy)
        if completion.error_code is not None or completion.value is None:
            return BoundaryResult(
                None,
                completion.error_code or BoundaryErrorCode.MALFORMED_RESPONSE,
            )
        try:
            result = _parse_result(item, completion.value.text, completion.value.provider_name)
        except (json.JSONDecodeError, TypeError, ValueError):
            return BoundaryResult(None, BoundaryErrorCode.MALFORMED_RESPONSE)
        try:
            self._store.put_if_absent(result)
            stored = self._store.get(item.capture_id)
        except Exception:
            return BoundaryResult(None, BoundaryErrorCode.IMPLEMENTATION_FAILURE)
        if stored != result:
            return BoundaryResult(None, BoundaryErrorCode.IMPLEMENTATION_FAILURE)
        return BoundaryResult(result, None)


def _prompt(item: DistillationInput) -> str:
    payload = {
        "capture_id": item.capture_id,
        "capture_why": item.capture_why,
        "content_kind": item.extraction.content_kind.value,
        "source_type": item.extraction.source_type.value,
        "source_title": item.extraction.metadata.title,
        "text": item.extraction.text,
        "transcript": item.extraction.transcript,
    }
    return (
        "Return one JSON object with exactly title, summary, and topics. "
        "Topics must be a sorted unique JSON string array. Do not propose tasks or actions.\n"
        + canonical_json_bytes(payload).decode("utf-8")
    )


def _parse_result(
    item: DistillationInput, payload: str, provider_name: str
) -> DistilledCapture:
    value = _mapping(json.loads(payload))
    if set(value) != {"title", "summary", "topics"}:
        raise ValueError("invalid provider schema")
    raw_topics = value["topics"]
    if not isinstance(raw_topics, list) or len(raw_topics) > 20:
        raise ValueError("invalid provider schema")
    topics = tuple(
        sorted(set(_bounded_text(_string(topic), 80) for topic in raw_topics))
    )
    return DistilledCapture.create(
        capture_id=item.capture_id,
        capture_why=item.capture_why,
        content_kind=item.extraction.content_kind,
        title=_string(value["title"]),
        summary=_string(value["summary"]),
        topics=topics,
        provider_name=provider_name,
    )


def _bounded_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid text")
    normalized = unicodedata.normalize("NFC", value)
    if not normalized.strip() or len(normalized) > limit or "\x00" in normalized:
        raise ValueError("invalid text")
    return normalized


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError("invalid mapping")
    return cast(Mapping[str, object], value)


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid string")
    return value


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("short distillation write")
        remaining = remaining[written:]


__all__ = [
    "DistillationInput",
    "DistillationService",
    "DistillationStore",
    "DistilledCapture",
    "FilesystemDistillationStore",
]

"""Deterministic redaction for work-tier capture events."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256

from open_brain_engine.capture.models import CaptureRedactionResult, NormalizedExtraction
from open_brain_engine.core.models import CaptureEnvelope
from open_brain_engine.core.ports import (
    EventRecord,
    RedactionFinding,
    RedactionFindingCategory,
    RedactionReceipt,
)

REDACTION_POLICY_VERSION = "open-brain-redaction-v1"

_CREDENTIAL = "[REDACTED_CREDENTIAL]"
_PERSONAL_IDENTIFIER = "[REDACTED_PERSONAL_IDENTIFIER]"

_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|client[_-]?secret|password|passwd|secret|token)"
    r"(\s*[:=]\s*)(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;&]+)"
)
_BEARER_TOKEN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{8,}={0,2}")
_EMAIL = re.compile(r"(?i)(?<![A-Z0-9._%+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![A-Z0-9.-])")
_PHONE = re.compile(r"(?<![\w-])(?:\+?\d{1,3}[ .-])?(?:\(?\d{3}\)?[ .-])\d{3}[ .-]\d{4}(?!\w)")
_SECRET_SHAPED_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{32,}(?![A-Za-z0-9_-])")


def has_redaction_finding(value: str) -> bool:
    """Return whether the approved policy would redact any part of one text value."""
    if not isinstance(value, str):
        raise ValueError("invalid redaction text")
    _, counts = _redact_text(value)
    return any(counts.values())


def redact_text(value: str) -> str:
    """Apply the approved policy to one text value and return the redacted text."""
    if not isinstance(value, str):
        raise ValueError("invalid redaction text")
    return _redact_text(value)[0]


@dataclass(frozen=True, slots=True)
class VersionedCaptureRedactor:
    """The only redaction policy approved for capture-service event emission."""

    def redact(
        self,
        extraction: NormalizedExtraction,
        envelope: CaptureEnvelope,
    ) -> CaptureRedactionResult:
        if not isinstance(extraction, NormalizedExtraction) or not isinstance(
            envelope, CaptureEnvelope
        ):
            raise ValueError("invalid redaction input")
        payload: dict[str, object] = {
            "extractor": extraction.extractor.value,
            "state": extraction.state.value,
            "source_type": extraction.source_type.value,
            "content_kind": extraction.content_kind.value,
            "metadata": extraction.metadata.to_dict(),
            "text": extraction.text,
            "transcript": extraction.transcript,
            "transcript_state": extraction.transcript_state.value,
        }
        redacted_payload, counts = _redact_value(payload)
        if not isinstance(redacted_payload, dict):
            raise AssertionError("redaction payload must remain an object")
        findings = tuple(
            RedactionFinding.create(category=category, count=count)
            for category, count in sorted(counts.items(), key=lambda item: item[0].value)
            if count
        )
        return CaptureRedactionResult.create(
            payload=redacted_payload,
            receipt=RedactionReceipt.create(
                source_digest_sha256=sha256(extraction.canonical_bytes()).hexdigest(),
                output_digest_sha256=EventRecord.output_digest_sha256(redacted_payload),
                policy_version=REDACTION_POLICY_VERSION,
                findings=findings,
            ),
        )


def _redact_value(value: object) -> tuple[object, dict[RedactionFindingCategory, int]]:
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, Mapping):
        redacted: dict[str, object] = {}
        counts: dict[RedactionFindingCategory, int] = {}
        for key, item in value.items():
            redacted_item, item_counts = _redact_value(item)
            redacted[key] = redacted_item
            _merge_counts(counts, item_counts)
        return redacted, counts
    if isinstance(value, list | tuple):
        redacted_items: list[object] = []
        counts = {}
        for item in value:
            redacted_item, item_counts = _redact_value(item)
            redacted_items.append(redacted_item)
            _merge_counts(counts, item_counts)
        return redacted_items, counts
    return value, {}


def _redact_text(value: str) -> tuple[str, dict[RedactionFindingCategory, int]]:
    counts: dict[RedactionFindingCategory, int] = {}
    result, count = _CREDENTIAL_ASSIGNMENT.subn(r"\1\2" + _CREDENTIAL, value)
    counts[RedactionFindingCategory.CREDENTIAL] = count
    result, count = _BEARER_TOKEN.subn("Bearer " + _CREDENTIAL, result)
    counts[RedactionFindingCategory.CREDENTIAL] += count
    result, count = _EMAIL.subn(_PERSONAL_IDENTIFIER, result)
    counts[RedactionFindingCategory.PERSONAL_IDENTIFIER] = count
    result, count = _PHONE.subn(_PERSONAL_IDENTIFIER, result)
    counts[RedactionFindingCategory.PERSONAL_IDENTIFIER] += count
    result, count = _SECRET_SHAPED_TOKEN.subn(_CREDENTIAL, result)
    counts[RedactionFindingCategory.CREDENTIAL] += count
    return result, counts


def _merge_counts(
    destination: dict[RedactionFindingCategory, int],
    source: Mapping[RedactionFindingCategory, int],
) -> None:
    for category, count in source.items():
        destination[category] = destination.get(category, 0) + count

"""Pure sanitization for untrusted ledger-model leaves."""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256

from open_brain_engine.capture.models import (
    ExtractionMetadata,
    ExtractionState,
    ExtractorKind,
    NormalizedExtraction,
    TranscriptState,
)
from open_brain_engine.capture.redaction import VersionedCaptureRedactor
from open_brain_engine.core.models import (
    CaptureEnvelope,
    CaptureSource,
    CaptureWhyOrigin,
    ContentKind,
    ContentOrigin,
    Provenance,
    SourceType,
)
from open_brain_engine.core.policy import classify_privacy


class LedgerSection(StrEnum):
    SUMMARY = "summary"
    KEY_POINTS = "key_points"
    CONTEXT = "context"
    QUESTIONS = "questions"
    REFERENCES = "references"


class QuarantineReason(StrEnum):
    EMPTY = "empty"
    REDACTION = "redaction"
    DIRECTIVE = "directive"
    INVALID_SECTION = "invalid_section"


@dataclass(frozen=True, slots=True)
class SanitizedLeaf:
    text: str
    normalized_key: str

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if (
            not isinstance(self.text, str)
            or not isinstance(self.normalized_key, str)
            or not self.text
            or _normalize_candidate(self.text) != self.text
            or any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in self.text)
            or html.escape(html.unescape(self.text), quote=True) != self.text
            or _has_unescaped_markdown(self.text)
            or self.normalized_key != _normalized_key(self.text)
        ):
            raise ValueError("invalid sanitized leaf")
        unescaped = html.unescape(self.text)
        if (
            _has_redaction_finding(unescaped)
            or _DIRECTIVE.search(unescaped)
            or _REVEAL_DIRECTIVE.search(unescaped)
        ):
            raise ValueError("invalid sanitized leaf")


@dataclass(frozen=True, slots=True)
class QuarantineRecord:
    record_id: str
    item_digest_sha256: str
    section: LedgerSection | None
    reason: QuarantineReason


@dataclass(frozen=True, slots=True)
class SanitizationResult:
    leaf: SanitizedLeaf | None
    quarantine: QuarantineRecord | None


_NETWORK_LOCATION = re.compile(r"(?i)\b(?:https?|ftp)://|\bwww\.")
_PRIVATE_PATH = re.compile(r"(?:^|[\s\"'(])(?:/Users/|/home/|~/|[A-Za-z]:\\\\)")
_DIRECTIVE = re.compile(
    r"(?is)\b(?:ignore|disregard|override|bypass)\b.{0,120}?"
    r"\b(?:previous|prior|above|system|prompt|instructions?)\b"
)
_REVEAL_DIRECTIVE = re.compile(
    r"(?is)\b(?:reveal|show|print|leak)\b.{0,120}?"
    r"\b(?:system|hidden|prompt|instructions?)\b"
)


def sanitize_leaf(*, item_id: object, section: object, text: object) -> SanitizationResult:
    """Return one safe leaf or a code-only quarantine record for untrusted text."""
    normalized_section = section if isinstance(section, LedgerSection) else None
    candidate = _normalize_candidate(text)
    if normalized_section is None:
        return _quarantine(
            item_id=item_id,
            text=candidate,
            section=None,
            reason=QuarantineReason.INVALID_SECTION,
        )
    if not candidate:
        return _quarantine(
            item_id=item_id,
            text=candidate,
            section=normalized_section,
            reason=QuarantineReason.EMPTY,
        )
    if _has_redaction_finding(candidate):
        return _quarantine(
            item_id=item_id,
            text=candidate,
            section=normalized_section,
            reason=QuarantineReason.REDACTION,
        )
    if _DIRECTIVE.search(candidate) or _REVEAL_DIRECTIVE.search(candidate):
        return _quarantine(
            item_id=item_id,
            text=candidate,
            section=normalized_section,
            reason=QuarantineReason.DIRECTIVE,
        )
    escaped = _escape_leaf(candidate)
    return SanitizationResult(
        leaf=SanitizedLeaf(text=escaped, normalized_key=_normalized_key(escaped)), quarantine=None
    )


def _normalize_candidate(value: object) -> str:
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFC", value)
    without_format_controls = "".join(
        character
        for character in normalized
        if unicodedata.category(character) != "Cf"
        and (unicodedata.category(character) != "Cc" or character.isspace())
    )
    return " ".join(without_format_controls.split())


def _has_redaction_finding(candidate: str) -> bool:
    if _NETWORK_LOCATION.search(candidate) or _PRIVATE_PATH.search(candidate):
        return True
    return bool(_redaction_findings(candidate))


def _redaction_findings(candidate: str) -> tuple[object, ...]:
    source_ref = "urn:open-brain:text:sha256:" + sha256(candidate.encode("utf-8")).hexdigest()
    envelope = CaptureEnvelope.create(
        source_type=SourceType.TEXT,
        content_kind=ContentKind.OTHER,
        source_url=None,
        title=None,
        shared_text=candidate,
        captured_at=datetime(2026, 1, 1, tzinfo=UTC),
        capture_why="",
        capture_why_origin=CaptureWhyOrigin.AUTOMATION_ABSENT,
        capture_source=CaptureSource.PLAYLIST,
        provenance=Provenance.create(
            source_ref=source_ref,
            content_origin=ContentOrigin.THIRD_PARTY,
            owner_context=CaptureWhyOrigin.AUTOMATION_ABSENT,
        ),
        raw_assets=(),
        privacy_decision=classify_privacy("public", policy_version="ledger-sanitize-v1"),
    )
    extraction = NormalizedExtraction.create(
        extractor=ExtractorKind.TEXT,
        state=ExtractionState.COMPLETE,
        source_type=SourceType.TEXT,
        content_kind=ContentKind.OTHER,
        metadata=ExtractionMetadata.create(),
        text=candidate,
        transcript=None,
        transcript_state=TranscriptState.NOT_APPLICABLE,
        assets=(),
        failure=None,
    )
    return VersionedCaptureRedactor().redact(extraction, envelope).receipt.findings


def _escape_leaf(candidate: str) -> str:
    escaped = html.escape(candidate, quote=True).replace("\\", "\\\\")
    escaped = escaped.replace("[", "\\[").replace("]", "\\]")
    escaped = escaped.replace("(", "\\(").replace(")", "\\)")
    leading_backslashes = len(escaped) - len(escaped.lstrip("\\"))
    if (
        leading_backslashes < len(escaped)
        and escaped[leading_backslashes] in {"#", ">"}
        and leading_backslashes % 2 == 0
    ):
        return escaped[:leading_backslashes] + "\\" + escaped[leading_backslashes:]
    return escaped


def _has_unescaped_markdown(value: str) -> bool:
    leading_backslashes = len(value) - len(value.lstrip("\\"))
    if (
        leading_backslashes < len(value)
        and value[leading_backslashes] in {"#", ">"}
        and leading_backslashes % 2 == 0
    ):
        return True
    for index, character in enumerate(value):
        if character not in "[]()":
            continue
        preceding_backslashes = 0
        cursor = index - 1
        while cursor >= 0 and value[cursor] == "\\":
            preceding_backslashes += 1
            cursor -= 1
        if preceding_backslashes % 2 == 0:
            return True
    return False


def _normalized_key(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).casefold().split()).rstrip(" .")


def _quarantine(
    *, item_id: object, text: str, section: LedgerSection | None, reason: QuarantineReason
) -> SanitizationResult:
    item_label = item_id if isinstance(item_id, str) else "invalid-item"
    item_digest = sha256(item_label.encode("utf-8")).hexdigest()
    record_digest = sha256((item_digest + "\x00" + text).encode("utf-8")).hexdigest()
    return SanitizationResult(
        leaf=None,
        quarantine=QuarantineRecord(
            record_id="quarantine_" + record_digest,
            item_digest_sha256=item_digest,
            section=section,
            reason=reason,
        ),
    )

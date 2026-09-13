"""Replay-safe Markdown publication for distilled work and saved content."""

from __future__ import annotations

import html
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath

from open_brain_engine.capture.models import NormalizedExtraction
from open_brain_engine.core.models import (
    CaptureEnvelope,
    CaptureWhyOrigin,
    ContentOrigin,
    PrivacyTier,
    SourceType,
)
from open_brain_engine.core.ports import PutDisposition, PutResult
from open_brain_engine.storage.filesystem import WriteState, atomic_write_new, read_confined

from open_brain_legacy.capture.distillation import DistilledCapture

_EXTERNAL_SOURCES = frozenset({SourceType.WEB, SourceType.SOCIAL, SourceType.YOUTUBE})


@dataclass(frozen=True, slots=True)
class CaptureDestinationPublisher:
    """Route trusted owner text to work and third-party sources to saved content."""

    work_root: Path
    saved_content_root: Path

    def __post_init__(self) -> None:
        if (
            not isinstance(self.work_root, Path)
            or not self.work_root.is_absolute()
            or not isinstance(self.saved_content_root, Path)
            or not self.saved_content_root.is_absolute()
            or self.work_root == self.saved_content_root
        ):
            raise ValueError("invalid capture publication roots")

    def publish(
        self,
        *,
        envelope: CaptureEnvelope,
        extraction: NormalizedExtraction,
        distilled: DistilledCapture,
    ) -> PutResult | None:
        _validate_binding(envelope=envelope, extraction=extraction, distilled=distilled)
        destination = _destination(envelope)
        if destination is None:
            return None
        root, relative, payload = (
            (
                self.work_root,
                _relative_path(envelope),
                _work_markdown(envelope, extraction, distilled),
            )
            if destination == "work"
            else (
                self.saved_content_root,
                _relative_path(envelope),
                _saved_content_markdown(envelope, extraction, distilled),
            )
        )
        state = atomic_write_new(root=root, relative=relative, data=payload)
        if read_confined(root=root, relative=relative) != payload:
            raise ValueError("capture publication read-back mismatch")
        return PutResult(
            PutDisposition.CREATED if state is WriteState.CREATED else PutDisposition.DUPLICATE,
            distilled.capture_id,
            sha256(payload).hexdigest(),
        )


def _destination(envelope: CaptureEnvelope) -> str | None:
    if (
        envelope.privacy_decision.tier is PrivacyTier.WORK
        and envelope.source_type is SourceType.TEXT
        and envelope.provenance.content_origin is ContentOrigin.OWNER_AUTHORED
        and envelope.capture_why_origin is CaptureWhyOrigin.OWNER_AUTHORED
    ):
        return "work"
    if (
        envelope.privacy_decision.tier in {PrivacyTier.PUBLIC, PrivacyTier.WORK}
        and envelope.source_type in _EXTERNAL_SOURCES
        and envelope.provenance.content_origin in {ContentOrigin.MIXED, ContentOrigin.THIRD_PARTY}
        and envelope.capture_why_origin is CaptureWhyOrigin.OWNER_AUTHORED
    ):
        return "saved-content"
    return None


def _validate_binding(
    *,
    envelope: CaptureEnvelope,
    extraction: NormalizedExtraction,
    distilled: DistilledCapture,
) -> None:
    if (
        not isinstance(envelope, CaptureEnvelope)
        or not isinstance(extraction, NormalizedExtraction)
        or not isinstance(distilled, DistilledCapture)
        or distilled.capture_id != str(envelope.capture_id)
        or distilled.capture_why != envelope.capture_why
        or distilled.content_kind is not envelope.content_kind
        or extraction.source_type is not envelope.source_type
        or extraction.content_kind is not envelope.content_kind
    ):
        raise ValueError("invalid capture publication binding")


def _relative_path(envelope: CaptureEnvelope) -> PurePosixPath:
    return PurePosixPath("inbox") / "open-brain" / (str(envelope.capture_id) + ".md")


def _work_markdown(
    envelope: CaptureEnvelope,
    extraction: NormalizedExtraction,
    distilled: DistilledCapture,
) -> bytes:
    body = extraction.transcript or extraction.text
    return _markdown(
        marker="work-capture",
        envelope=envelope,
        distilled=distilled,
        source_url=None,
        captured_text=body,
    )


def _saved_content_markdown(
    envelope: CaptureEnvelope,
    extraction: NormalizedExtraction,
    distilled: DistilledCapture,
) -> bytes:
    if envelope.source_url is None or envelope.provenance.source_ref != envelope.source_url:
        raise ValueError("saved-content provenance unavailable")
    source_url = extraction.metadata.canonical_url or envelope.source_url
    body = extraction.transcript or extraction.text
    return _markdown(
        marker="saved-content",
        envelope=envelope,
        distilled=distilled,
        source_url=source_url,
        captured_text=body,
    )


def _markdown(
    *,
    marker: str,
    envelope: CaptureEnvelope,
    distilled: DistilledCapture,
    source_url: str | None,
    captured_text: str,
) -> bytes:
    sections = [
        f"<!-- open-brain-{marker}-v1 {envelope.capture_id} -->",
        "",
        "# " + _escape(distilled.title),
        "",
    ]
    if source_url is not None:
        sections.extend(("Source: " + _escape(source_url), ""))
    sections.extend(
        (
            _escape(distilled.summary),
            "",
            "## Why this was captured",
            "",
            _escape(envelope.capture_why),
            "",
            "## Captured content",
            "",
            _escape(captured_text),
            "",
            "## Topics",
            "",
        )
    )
    sections.extend("- " + _escape(topic) for topic in distilled.topics)
    sections.append("")
    return "\n".join(sections).encode("utf-8")


def _escape(value: str) -> str:
    return html.escape(value, quote=False).replace("\x00", "")


__all__ = ["CaptureDestinationPublisher"]

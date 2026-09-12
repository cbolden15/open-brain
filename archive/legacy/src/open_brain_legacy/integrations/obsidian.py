"""Root-confined, configuration-driven Markdown output for Obsidian vaults."""

from __future__ import annotations

import re
import stat
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from types import MappingProxyType

from open_brain_engine.core.models import ContentKind, Intent, Provenance, SourceType
from open_brain_engine.storage.filesystem import (
    DuplicateConflictError,
    DurabilityError,
    RootConfinementError,
    StorageError,
    WriteState,
    atomic_write_new,
    read_confined,
)
from open_brain_engine.storage.markdown import MarkdownFormatError, parse_markdown, render_markdown

from open_brain_legacy._compat.open_brain.integrations.ports import (
    PageDocument,
    PageReadRequest,
    RedactedText,
    TrustLabel,
    VaultWriteDisposition,
    VaultWriteRequest,
    VaultWriteResult,
)

_OPAQUE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_MAX_TEXT = 2 * 1024 * 1024


class NoteStatus(StrEnum):
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ObsidianTaxonomy:
    """Public logical destinations supplied by configuration, never note content."""

    reviewed: PurePosixPath
    destinations: Mapping[SourceType, PurePosixPath]

    @classmethod
    def create(
        cls,
        *,
        reviewed: str | PurePosixPath,
        destinations: Mapping[SourceType | str, str | PurePosixPath],
    ) -> ObsidianTaxonomy:
        normalized: dict[SourceType, PurePosixPath] = {}
        for raw_source, raw_path in destinations.items():
            try:
                source = SourceType(raw_source)
            except (TypeError, ValueError):
                raise ValueError("invalid Obsidian taxonomy") from None
            if source in normalized:
                raise ValueError("invalid Obsidian taxonomy")
            normalized[source] = _safe_taxonomy_path(raw_path)
        if set(normalized) != set(SourceType):
            raise ValueError("invalid Obsidian taxonomy")
        return cls(
            reviewed=_safe_taxonomy_path(reviewed),
            destinations=MappingProxyType(
                dict(sorted(normalized.items(), key=lambda item: item[0]))
            ),
        )

    def relative_path(
        self,
        *,
        source_type: SourceType,
        page_id: str,
    ) -> PurePosixPath:
        _require_page_id(page_id)
        if not isinstance(source_type, SourceType):
            raise ValueError("invalid source type")
        return self.destinations[source_type] / f"{page_id}.md"

    def reviewed_path(self, *, page_id: str) -> PurePosixPath:
        _require_page_id(page_id)
        return self.reviewed / f"{page_id}.md"

    def search_paths(self, *, page_id: str) -> tuple[PurePosixPath, ...]:
        directories = {self.reviewed, *self.destinations.values()}
        return tuple(directory / f"{page_id}.md" for directory in sorted(directories))


@dataclass(frozen=True, slots=True)
class NormalizedNote:
    page_id: str
    title: str
    source_type: SourceType
    content_kind: ContentKind
    source_url: str | None
    status: NoteStatus
    summary: str | None
    transcript: str | None
    failure_reason: str | None
    capture_why: str
    trust: TrustLabel
    intent: Intent
    provenance: Provenance
    captured_at: datetime
    processed_at: datetime
    review_id: str

    @classmethod
    def create(
        cls,
        *,
        page_id: str,
        title: str,
        source_type: SourceType,
        content_kind: ContentKind,
        source_url: str | None,
        status: NoteStatus,
        summary: str | None,
        transcript: str | None,
        failure_reason: str | None,
        capture_why: str,
        trust: TrustLabel,
        intent: Intent,
        provenance: Provenance,
        captured_at: datetime,
        processed_at: datetime,
        review_id: str,
    ) -> NormalizedNote:
        note = cls(
            page_id=page_id,
            title=title,
            source_type=source_type,
            content_kind=content_kind,
            source_url=source_url,
            status=status,
            summary=summary,
            transcript=transcript,
            failure_reason=failure_reason,
            capture_why=capture_why,
            trust=trust,
            intent=intent,
            provenance=provenance,
            captured_at=captured_at,
            processed_at=processed_at,
            review_id=review_id,
        )
        note._validate()
        return note

    def _validate(self) -> None:
        _require_page_id(self.page_id)
        _require_page_id(self.review_id)
        _require_text(self.title, field="title")
        _require_text(self.capture_why, field="capture reason")
        if (
            not isinstance(self.source_type, SourceType)
            or not isinstance(self.content_kind, ContentKind)
            or not isinstance(self.status, NoteStatus)
            or not isinstance(self.trust, TrustLabel)
            or not isinstance(self.intent, Intent)
            or not isinstance(self.provenance, Provenance)
        ):
            raise ValueError("invalid normalized note")
        if self.source_url is not None:
            _require_text(self.source_url, field="source URL")
            if self.provenance.source_ref != self.source_url:
                raise ValueError("invalid normalized note provenance")
        if self.status is NoteStatus.READY:
            _require_text(self.summary, field="summary")
            if self.failure_reason is not None:
                raise ValueError("invalid normalized note status")
        else:
            _require_text(self.failure_reason, field="failure reason")
            if self.summary is not None or self.transcript is not None:
                raise ValueError("invalid normalized note status")
        if self.transcript is not None:
            _require_text(self.transcript, field="transcript")
        _require_utc(self.captured_at)
        _require_utc(self.processed_at)


class ObsidianVault:
    """Concrete immutable vault adapter with a separate normalized-note seam."""

    def __init__(self, *, root: Path, taxonomy: ObsidianTaxonomy) -> None:
        if not isinstance(root, Path) or not root.is_absolute() or not isinstance(
            taxonomy, ObsidianTaxonomy
        ):
            raise RootConfinementError("unsafe storage root")
        try:
            metadata = root.lstat()
        except OSError:
            raise RootConfinementError("unsafe storage root") from None
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise RootConfinementError("unsafe storage root")
        self._root = root
        self._taxonomy = taxonomy

    def write_note(self, note: NormalizedNote) -> VaultWriteResult:
        if not isinstance(note, NormalizedNote):
            raise StorageError("invalid normalized note")
        try:
            note._validate()
        except ValueError:
            raise StorageError("invalid normalized note") from None
        payload = _render_note(note).encode("utf-8")
        return self._write_new(
            page_id=note.page_id,
            relative=self._taxonomy.relative_path(
                source_type=note.source_type,
                page_id=note.page_id,
            ),
            payload=payload,
        )

    def write(self, request: VaultWriteRequest) -> VaultWriteResult:
        if not isinstance(request, VaultWriteRequest):
            raise StorageError("invalid vault write request")
        payload = render_markdown(
            fields={
                "page_id": request.page_id,
                "review_id": request.review_id,
                "schema_version": 1,
                "status": NoteStatus.READY.value,
                "title": request.title,
                "trust": TrustLabel.VERIFIED_WORK.value,
            },
            body=request.markdown,
        ).encode("utf-8")
        return self._write_new(
            page_id=request.page_id,
            relative=self._taxonomy.reviewed_path(page_id=request.page_id),
            payload=payload,
        )

    def read(self, request: PageReadRequest) -> PageDocument | None:
        if not isinstance(request, PageReadRequest):
            raise StorageError("invalid page read request")
        matches: list[bytes] = []
        for relative in self._taxonomy.search_paths(page_id=request.page_id):
            try:
                payload = read_confined(root=self._root, relative=relative)
            except DurabilityError:
                continue
            if payload is not None:
                matches.append(payload)
        if not matches:
            return None
        if len(matches) != 1:
            raise DuplicateConflictError("vault page collision")
        try:
            document = parse_markdown(matches[0])
            title = document.fields["title"]
            raw_trust = document.fields["trust"]
            if not isinstance(title, str) or not isinstance(raw_trust, str):
                raise ValueError
            trust = TrustLabel(raw_trust)
        except (KeyError, MarkdownFormatError, TypeError, ValueError):
            raise StorageError("invalid stored Markdown document") from None
        return PageDocument(
            page_id=request.page_id,
            title=RedactedText.redact(title),
            markdown=RedactedText.redact(document.body),
            trust=trust,
        )

    def _write_new(
        self,
        *,
        page_id: str,
        relative: PurePosixPath,
        payload: bytes,
    ) -> VaultWriteResult:
        state = atomic_write_new(root=self._root, relative=relative, data=payload)
        return VaultWriteResult(
            page_id=page_id,
            disposition=(
                VaultWriteDisposition.CREATED
                if state is WriteState.CREATED
                else VaultWriteDisposition.DUPLICATE
            ),
            bytes_written=len(payload) if state is WriteState.CREATED else 0,
        )


def _render_note(note: NormalizedNote) -> str:
    fields = {
        "capture_why": note.capture_why,
        "captured_at": _format_timestamp(note.captured_at),
        "content_kind": note.content_kind.value,
        "intent": note.intent.value,
        "page_id": note.page_id,
        "processed_at": _format_timestamp(note.processed_at),
        "provenance": note.provenance.to_dict(),
        "review_id": note.review_id,
        "schema_version": 1,
        "source_type": note.source_type.value,
        "source_url": note.source_url,
        "status": note.status.value,
        "title": note.title,
        "transcript_present": note.transcript is not None,
        "trust": note.trust.value,
    }
    lines = [f"# {note.title}", ""]
    if note.status is NoteStatus.FAILED:
        lines.extend(["> [!warning] Processing failed", f"> {note.failure_reason}", ""])
    else:
        lines.extend(["## Summary", "", str(note.summary), ""])
    lines.extend(["## Why it was captured", "", note.capture_why])
    if note.transcript is not None:
        transcript = [">" if not line else f"> {line}" for line in note.transcript.splitlines()]
        lines.extend(["", "## Transcript", "", "> [!quote]- Transcript", *transcript])
    return render_markdown(fields=fields, body="\n".join(lines))


def _safe_taxonomy_path(raw: str | PurePosixPath) -> PurePosixPath:
    text = str(raw)
    parts = text.split("/")
    if (
        not text
        or text.startswith("/")
        or "\\" in text
        or "\x00" in text
        or any(
            not part
            or part in {".", ".."}
            or part.startswith(".")
            or any(unicodedata.category(character) == "Cc" for character in part)
            for part in parts
        )
    ):
        raise ValueError("unsafe taxonomy path")
    path = PurePosixPath(text)
    if path.is_absolute():
        raise ValueError("unsafe taxonomy path")
    return path


def _require_page_id(value: str) -> None:
    if not isinstance(value, str) or not _OPAQUE_ID.fullmatch(value):
        raise ValueError("invalid page identifier")


def _require_text(value: object, *, field: str) -> None:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > _MAX_TEXT:
        raise ValueError(f"invalid {field}")
    normalized = unicodedata.normalize("NFC", value)
    if any(
        unicodedata.category(character) == "Cc" and character not in {"\t", "\n", "\r"}
        for character in normalized
    ):
        raise ValueError(f"invalid {field}")


def _require_utc(value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("invalid note timestamp")
    if value.astimezone(UTC) != value:
        raise ValueError("invalid note timestamp")


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

"""Verified work-item to ledger scan conversion with trusted path routing."""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath

from open_brain_engine.capture.models import DistillationWorkItem
from open_brain_engine.core.ids import canonical_json_bytes
from open_brain_engine.core.models import (
    CaptureSource,
    ContentKind,
    ContentOrigin,
    Provenance,
    SourceType,
)
from open_brain_engine.core.policy import classify_privacy
from open_brain_engine.core.ports import EventRecord

from .models import LedgerScanRecord, LedgerTaxonomy, LedgerValidationError, validate_source_locator

_MAX_SOURCE_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class LedgerSourceManifestEntry:
    """One immutable root-relative Markdown source identity."""

    key: str
    source_locator: PurePosixPath
    content_digest_sha256: str
    size_bytes: int

    @classmethod
    def create(
        cls,
        *,
        source_locator: PurePosixPath,
        content_digest_sha256: str,
        size_bytes: int,
    ) -> LedgerSourceManifestEntry:
        locator = validate_source_locator(source_locator)
        if (
            len(content_digest_sha256) != 64
            or any(character not in "0123456789abcdef" for character in content_digest_sha256)
            or not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or not 0 <= size_bytes <= _MAX_SOURCE_BYTES
        ):
            raise LedgerValidationError("invalid ledger source manifest entry")
        key = "source_" + sha256(
            canonical_json_bytes(
                {
                    "content_digest_sha256": content_digest_sha256,
                    "source_locator": locator.as_posix(),
                }
            )
        ).hexdigest()
        return cls(
            key=key,
            source_locator=locator,
            content_digest_sha256=content_digest_sha256,
            size_bytes=size_bytes,
        )

    def validate(self) -> None:
        recreated = LedgerSourceManifestEntry.create(
            source_locator=self.source_locator,
            content_digest_sha256=self.content_digest_sha256,
            size_bytes=self.size_bytes,
        )
        if recreated != self:
            raise LedgerValidationError("ledger source manifest entry binding mismatch")

    def to_dict(self) -> dict[str, object]:
        self.validate()
        return {
            "content_digest_sha256": self.content_digest_sha256,
            "key": self.key,
            "size_bytes": self.size_bytes,
            "source_locator": self.source_locator.as_posix(),
        }


@dataclass(frozen=True, slots=True)
class LedgerSourceManifest:
    """Deterministically sorted identity for one confined source-root scan."""

    manifest_id: str
    manifest_digest_sha256: str
    entries: tuple[LedgerSourceManifestEntry, ...]

    @classmethod
    def create(
        cls, *, entries: tuple[LedgerSourceManifestEntry, ...]
    ) -> LedgerSourceManifest:
        if not isinstance(entries, tuple) or any(
            not isinstance(entry, LedgerSourceManifestEntry) for entry in entries
        ):
            raise LedgerValidationError("invalid ledger source manifest")
        ordered = tuple(sorted(entries, key=lambda entry: entry.source_locator.as_posix()))
        for entry in ordered:
            entry.validate()
        if len({entry.key for entry in ordered}) != len(ordered) or len(
            {entry.source_locator for entry in ordered}
        ) != len(ordered):
            raise LedgerValidationError("duplicate ledger source manifest entry")
        digest = sha256(
            canonical_json_bytes([entry.to_dict() for entry in ordered])
        ).hexdigest()
        return cls(
            manifest_id="manifest_" + digest,
            manifest_digest_sha256=digest,
            entries=ordered,
        )

    def validate(self) -> None:
        if LedgerSourceManifest.create(entries=self.entries) != self:
            raise LedgerValidationError("ledger source manifest binding mismatch")

    def entry_for(self, key: str) -> LedgerSourceManifestEntry | None:
        self.validate()
        return next((entry for entry in self.entries if entry.key == key), None)

    def canonical_bytes(self) -> bytes:
        self.validate()
        return canonical_json_bytes(
            {
                "entries": [entry.to_dict() for entry in self.entries],
                "manifest_digest_sha256": self.manifest_digest_sha256,
                "manifest_id": self.manifest_id,
            }
        )


def scan_source_root(*, root: Path) -> LedgerSourceManifest:
    """Scan regular Markdown files without following any filesystem link."""
    entries: list[LedgerSourceManifestEntry] = []
    root_fd = _open_source_root(root)

    def visit(directory_fd: int, prefix: PurePosixPath | None) -> None:
        try:
            directory_entries = sorted(os.scandir(directory_fd), key=lambda entry: entry.name)
        except OSError:
            raise LedgerValidationError("ledger source scan unavailable") from None
        for directory_entry in directory_entries:
            try:
                metadata = directory_entry.stat(follow_symlinks=False)
            except OSError:
                raise LedgerValidationError("ledger source scan unavailable") from None
            if stat.S_ISLNK(metadata.st_mode):
                raise LedgerValidationError("ledger source scan is not confined")
            locator = (
                PurePosixPath(directory_entry.name)
                if prefix is None
                else prefix / directory_entry.name
            )
            if stat.S_ISDIR(metadata.st_mode):
                child_fd = _open_directory_at(directory_fd, directory_entry.name)
                try:
                    visit(child_fd, locator)
                finally:
                    os.close(child_fd)
                continue
            if not directory_entry.name.endswith(".md"):
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise LedgerValidationError("ledger source scan is not confined")
            payload = _read_regular_at(directory_fd, directory_entry.name)
            try:
                payload.decode("utf-8")
            except UnicodeDecodeError:
                raise LedgerValidationError("invalid ledger source file") from None
            entries.append(
                LedgerSourceManifestEntry.create(
                    source_locator=locator,
                    content_digest_sha256=sha256(payload).hexdigest(),
                    size_bytes=len(payload),
                )
            )

    try:
        visit(root_fd, None)
    except LedgerValidationError:
        raise
    except OSError:
        raise LedgerValidationError("ledger source scan unavailable") from None
    finally:
        os.close(root_fd)
    return LedgerSourceManifest.create(entries=tuple(entries))


def _open_source_root(root: Path) -> int:
    if not isinstance(root, Path) or not root.is_absolute():
        raise LedgerValidationError("ledger source scan is not confined")
    try:
        descriptor = os.open(
            root,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError:
        raise LedgerValidationError("ledger source scan unavailable") from None
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise LedgerValidationError("ledger source scan is not confined")
    return descriptor


def _open_directory_at(parent_fd: int, name: str) -> int:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
    except OSError:
        raise LedgerValidationError("ledger source scan is not confined") from None
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise LedgerValidationError("ledger source scan is not confined")
    return descriptor


def _read_regular_at(parent_fd: int, name: str) -> bytes:
    descriptor = -1
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise LedgerValidationError("ledger source scan is not confined")
        if metadata.st_size > _MAX_SOURCE_BYTES:
            raise LedgerValidationError("ledger source file exceeds limit")
        chunks: list[bytes] = []
        remaining = _MAX_SOURCE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if (
            len(payload) != metadata.st_size
            or len(payload) > _MAX_SOURCE_BYTES
            or b"\x00" in payload
        ):
            raise LedgerValidationError("invalid ledger source file")
        return payload
    except LedgerValidationError:
        raise
    except OSError:
        raise LedgerValidationError("ledger source scan unavailable") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def scan_distillation_work_item(
    *,
    item: DistillationWorkItem,
    event: EventRecord,
    taxonomy: LedgerTaxonomy,
    source_locator: PurePosixPath,
) -> LedgerScanRecord:
    """Verify the only ledger entry pair and create its immutable route binding."""
    if not isinstance(item, DistillationWorkItem) or not isinstance(event, EventRecord):
        raise LedgerValidationError("invalid ledger work item")
    try:
        if DistillationWorkItem.from_canonical_bytes(item.canonical_bytes()) != item:
            raise ValueError
    except (TypeError, ValueError) as error:
        raise LedgerValidationError("invalid ledger work item") from error
    if not isinstance(taxonomy, LedgerTaxonomy):
        raise LedgerValidationError("invalid ledger taxonomy")
    locator = validate_source_locator(source_locator)
    event_digest = sha256(event.canonical_bytes()).hexdigest()
    if (
        item.capture_id != event.stream_id
        or item.event_id != event.event_id
        or item.redacted_event_digest_sha256 != event_digest
        or event.event_type != "capture.extracted"
        or event.redaction_receipt.output_digest_sha256
        != EventRecord.output_digest_sha256(event.payload)
    ):
        raise LedgerValidationError("ledger work item event binding mismatch")
    text, capture_why, capture_source, source_type, content_kind, provenance = _verified_payload(
        event.payload
    )
    route = taxonomy.route_for(locator)
    privacy = classify_privacy(
        None if route is None else route.privacy_tier,
        policy_version=taxonomy.version,
    )
    eligible = (
        route is not None
        and route.privacy_tier is not None
        and provenance.content_origin is ContentOrigin.THIRD_PARTY
    )
    return LedgerScanRecord.create(
        capture_id=item.capture_id,
        event_id=item.event_id,
        event_digest_sha256=event_digest,
        event_type=event.event_type,
        source_locator=locator,
        content_digest_sha256=event.redaction_receipt.output_digest_sha256,
        taxonomy_version=taxonomy.version,
        route=route,
        event_privacy_decision=event.privacy_decision,
        privacy_decision=privacy,
        upstream_redaction_receipt=event.redaction_receipt,
        redacted_text=text,
        capture_why=capture_why,
        captured_at=event.occurred_at,
        capture_source=capture_source,
        source_type=source_type,
        content_kind=content_kind,
        provenance=provenance,
        topic_id=route.topic_id if eligible and route is not None else None,
        topic_label=route.topic_label if eligible and route is not None else None,
    )


def _verified_payload(
    payload: Mapping[str, object],
) -> tuple[str, str, CaptureSource, SourceType, ContentKind, Provenance]:
    try:
        text = payload["text"]
        capture_why = payload["capture_why"]
        raw_capture_source = payload["capture_source"]
        raw_source_type = payload["source_type"]
        raw_content_kind = payload["content_kind"]
        provenance_value = payload["provenance"]
        if (
            not isinstance(text, str)
            or not isinstance(capture_why, str)
            or not isinstance(raw_capture_source, str)
            or not isinstance(raw_source_type, str)
            or not isinstance(raw_content_kind, str)
            or not isinstance(provenance_value, Mapping)
        ):
            raise ValueError
        capture_source = CaptureSource(raw_capture_source)
        source_type = SourceType(raw_source_type)
        content_kind = ContentKind(raw_content_kind)
        provenance = Provenance.from_dict(provenance_value)
    except (KeyError, TypeError, ValueError) as error:
        raise LedgerValidationError("verified event lacks ledger provenance") from error
    return text, capture_why, capture_source, source_type, content_kind, provenance

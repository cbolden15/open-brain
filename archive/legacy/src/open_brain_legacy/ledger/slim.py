"""Archive-first slimming for immutable private ledger source views."""

from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Protocol

from open_brain_engine.core.ids import canonical_json_bytes
from open_brain_engine.storage.filesystem import atomic_write_new, read_confined

from .store import LedgerRowIdentity, LedgerStoreError, SqliteLedgerStore

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAX_CONTENT_BYTES = 4 * 1024 * 1024
_MAX_TRANSCRIPT_BYTES = 32 * 1024 * 1024


class SlimError(StrEnum):
    INVALID_SOURCE_VIEW = "invalid_source_view"
    GRACE_NOT_ELAPSED = "grace_not_elapsed"
    CITATIONS_UNVERIFIED = "citations_unverified"
    ARCHIVE_FAILED = "archive_failed"
    ARCHIVE_MISMATCH = "archive_mismatch"
    SUCCESSOR_FAILED = "successor_failed"
    FINALIZE_FAILED = "finalize_failed"


def _identifier(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"invalid {field}")
    return value


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("invalid source-view timestamp")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _bounded_bytes(value: object, *, field: str, limit: int) -> bytes:
    if not isinstance(value, bytes) or not value or len(value) > limit:
        raise ValueError(f"invalid {field}")
    return value


@dataclass(frozen=True, slots=True)
class LedgerSourceView:
    """A private, immutable source projection distinct from ``RawCapture``."""

    source_id: str
    version_id: str
    created_at: datetime
    content: bytes
    transcript: bytes | None

    @classmethod
    def create(
        cls,
        *,
        source_id: str,
        created_at: datetime,
        content: bytes,
        transcript: bytes | None,
    ) -> LedgerSourceView:
        normalized_source_id = _identifier(source_id, field="source-view source ID")
        normalized_content = _bounded_bytes(
            content, field="source-view content", limit=_MAX_CONTENT_BYTES
        )
        normalized_transcript = (
            None
            if transcript is None
            else _bounded_bytes(
                transcript,
                field="source-view transcript",
                limit=_MAX_TRANSCRIPT_BYTES,
            )
        )
        normalized_created_at = created_at.astimezone(UTC)
        identity = {
            "content_base64": base64.b64encode(normalized_content).decode("ascii"),
            "created_at": _timestamp(normalized_created_at),
            "source_id": normalized_source_id,
            "transcript_base64": (
                None
                if normalized_transcript is None
                else base64.b64encode(normalized_transcript).decode("ascii")
            ),
        }
        version_id = "source_view_" + sha256(canonical_json_bytes(identity)).hexdigest()
        return cls(
            source_id=normalized_source_id,
            version_id=version_id,
            created_at=normalized_created_at,
            content=normalized_content,
            transcript=normalized_transcript,
        )

    def validate(self) -> None:
        recreated = LedgerSourceView.create(
            source_id=self.source_id,
            created_at=self.created_at,
            content=self.content,
            transcript=self.transcript,
        )
        if recreated != self:
            raise ValueError("source-view binding mismatch")

    def to_dict(self) -> dict[str, object]:
        self.validate()
        return {
            "content_base64": base64.b64encode(self.content).decode("ascii"),
            "created_at": _timestamp(self.created_at),
            "source_id": self.source_id,
            "transcript_base64": (
                None
                if self.transcript is None
                else base64.b64encode(self.transcript).decode("ascii")
            ),
            "version_id": self.version_id,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    def transcript_free_successor(self, *, created_at: datetime) -> LedgerSourceView:
        self.validate()
        return LedgerSourceView.create(
            source_id=self.source_id,
            created_at=created_at,
            content=self.content,
            transcript=None,
        )


@dataclass(frozen=True, slots=True)
class SourceViewReceipt:
    record_id: str
    digest_sha256: str

    @classmethod
    def create(cls, *, record_id: str, digest_sha256: str) -> SourceViewReceipt:
        if not isinstance(digest_sha256, str) or not _DIGEST.fullmatch(digest_sha256):
            raise ValueError("invalid source-view receipt")
        return cls(
            record_id=_identifier(record_id, field="source-view receipt ID"),
            digest_sha256=digest_sha256,
        )


class GraceVerifier(Protocol):
    def elapsed(self, source_view: LedgerSourceView, *, now: datetime) -> bool: ...


class SourceViewArchive(Protocol):
    def write_if_absent(self, source_view: LedgerSourceView) -> SourceViewReceipt: ...

    def read(self, receipt: SourceViewReceipt) -> bytes | None: ...


class SourceViewSuccessorStore(Protocol):
    def write_if_absent(self, source_view: LedgerSourceView) -> SourceViewReceipt: ...


class SourceViewSuccessorReader(Protocol):
    def read(self, receipt: SourceViewReceipt) -> bytes | None: ...


@dataclass(frozen=True, slots=True)
class PreparedSlim:
    source_id: str
    original_version_id: str
    archive_digest_sha256: str
    successor: LedgerSourceView
    successor_digest_sha256: str


@dataclass(frozen=True, slots=True)
class SlimResult:
    prepared: PreparedSlim | None
    error: SlimError | None


class LedgerSlimService:
    def __init__(
        self,
        *,
        store: SqliteLedgerStore,
        archive: SourceViewArchive,
        successor_store: SourceViewSuccessorStore,
        successor_reader: SourceViewSuccessorReader,
        grace_verifier: GraceVerifier,
    ) -> None:
        if type(successor_reader) is not AtomicSourceViewSuccessorReader:
            raise ValueError("approved root-confined reader required")
        self._store = store
        self._archive = archive
        self._successor_store = successor_store
        self._successor_reader = successor_reader
        self._grace_verifier = grace_verifier

    def prepare(
        self,
        *,
        source_view: object,
        row_identity: object,
        now: datetime,
    ) -> SlimResult:
        if not isinstance(source_view, LedgerSourceView) or not isinstance(
            row_identity, LedgerRowIdentity
        ):
            return SlimResult(None, SlimError.INVALID_SOURCE_VIEW)
        try:
            source_view.validate()
            _timestamp(now)
            current_time = now.astimezone(UTC)
        except (AttributeError, TypeError, ValueError):
            return SlimResult(None, SlimError.INVALID_SOURCE_VIEW)
        if source_view.transcript is None:
            return SlimResult(None, SlimError.INVALID_SOURCE_VIEW)
        try:
            durable = self._store.slim_state(row_identity)
        except LedgerStoreError:
            return SlimResult(None, SlimError.CITATIONS_UNVERIFIED)
        if (
            durable is None
            or durable.source_id != source_view.source_id
            or not durable.citation_ids
        ):
            return SlimResult(None, SlimError.CITATIONS_UNVERIFIED)
        try:
            if not self._grace_verifier.elapsed(source_view, now=current_time):
                return SlimResult(None, SlimError.GRACE_NOT_ELAPSED)
        except Exception:
            return SlimResult(None, SlimError.GRACE_NOT_ELAPSED)

        archived_bytes = source_view.canonical_bytes()
        expected_archive_digest = sha256(archived_bytes).hexdigest()
        successor = source_view.transcript_free_successor(created_at=source_view.created_at)
        successor_bytes = successor.canonical_bytes()
        successor_digest = sha256(successor_bytes).hexdigest()
        prepared = PreparedSlim(
            source_id=source_view.source_id,
            original_version_id=source_view.version_id,
            archive_digest_sha256=expected_archive_digest,
            successor=successor,
            successor_digest_sha256=successor_digest,
        )
        if durable.slimmed:
            if (
                durable.archive_digest_sha256 != expected_archive_digest
                or durable.successor_id != successor.version_id
                or durable.successor_digest_sha256 != successor_digest
            ):
                return SlimResult(None, SlimError.FINALIZE_FAILED)
            return SlimResult(prepared, None)

        try:
            archive_receipt = self._archive.write_if_absent(source_view)
        except Exception:
            return SlimResult(None, SlimError.ARCHIVE_FAILED)
        if (
            archive_receipt.record_id != source_view.version_id
            or archive_receipt.digest_sha256 != expected_archive_digest
        ):
            return SlimResult(None, SlimError.ARCHIVE_MISMATCH)
        try:
            verified_archive = self._archive.read(archive_receipt)
        except Exception:
            return SlimResult(None, SlimError.ARCHIVE_MISMATCH)
        if (
            verified_archive != archived_bytes
            or sha256(verified_archive).hexdigest() != expected_archive_digest
        ):
            return SlimResult(None, SlimError.ARCHIVE_MISMATCH)

        try:
            successor_receipt = self._successor_store.write_if_absent(successor)
        except Exception:
            return SlimResult(None, SlimError.SUCCESSOR_FAILED)
        if (
            successor_receipt.record_id != successor.version_id
            or successor_receipt.digest_sha256 != successor_digest
        ):
            return SlimResult(None, SlimError.SUCCESSOR_FAILED)
        try:
            verified_successor = self._successor_reader.read(successor_receipt)
        except Exception:
            return SlimResult(None, SlimError.SUCCESSOR_FAILED)
        if (
            not isinstance(verified_successor, bytes)
            or verified_successor != successor_bytes
            or sha256(verified_successor).hexdigest() != successor_digest
        ):
            return SlimResult(None, SlimError.SUCCESSOR_FAILED)
        try:
            self._store.finalize_slim(
                row_identity,
                archive_digest_sha256=expected_archive_digest,
                successor_id=successor.version_id,
                successor_digest_sha256=successor_digest,
            )
        except LedgerStoreError:
            return SlimResult(None, SlimError.FINALIZE_FAILED)
        return SlimResult(prepared, None)


class AtomicSourceViewArchive:
    """Root-confined, content-addressed immutable source-view archive."""

    def __init__(self, *, root: Path) -> None:
        _private_root(root)
        self._root = root

    def write_if_absent(self, source_view: LedgerSourceView) -> SourceViewReceipt:
        source_view.validate()
        payload = source_view.canonical_bytes()
        digest = sha256(payload).hexdigest()
        atomic_write_new(root=self._root, relative=_archive_path(digest), data=payload)
        return SourceViewReceipt.create(
            record_id=source_view.version_id,
            digest_sha256=digest,
        )

    def read(self, receipt: SourceViewReceipt) -> bytes | None:
        return read_confined(root=self._root, relative=_archive_path(receipt.digest_sha256))


class AtomicSourceViewSuccessorStore:
    """Immutable versions; writing a successor never mutates the original view."""

    def __init__(self, *, root: Path) -> None:
        _private_root(root)
        self._root = root

    def write_if_absent(self, source_view: LedgerSourceView) -> SourceViewReceipt:
        source_view.validate()
        if source_view.transcript is not None:
            raise ValueError("source-view successor retains transcript")
        payload = source_view.canonical_bytes()
        digest = sha256(payload).hexdigest()
        atomic_write_new(root=self._root, relative=_successor_path(digest), data=payload)
        return SourceViewReceipt.create(
            record_id=source_view.version_id,
            digest_sha256=digest,
        )


class AtomicSourceViewSuccessorReader:
    """Independent root-confined proof that successor bytes reached durable storage."""

    def __init__(self, *, root: Path) -> None:
        _private_root(root)
        self._root = root

    def read(self, receipt: SourceViewReceipt) -> bytes | None:
        return read_confined(root=self._root, relative=_successor_path(receipt.digest_sha256))


def _private_root(root: Path) -> None:
    if not isinstance(root, Path) or not root.is_absolute():
        raise ValueError("invalid source-view root")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)


def _archive_path(digest_sha256: str) -> PurePosixPath:
    if not _DIGEST.fullmatch(digest_sha256):
        raise ValueError("invalid archive digest")
    return PurePosixPath("archive", digest_sha256[:2], digest_sha256 + ".json")


def _successor_path(digest_sha256: str) -> PurePosixPath:
    if not _DIGEST.fullmatch(digest_sha256):
        raise ValueError("invalid successor digest")
    return PurePosixPath("source-views", digest_sha256[:2], digest_sha256 + ".json")

"""Synthetic, work-scoped development workflow integration.

The adapter is intentionally provider-free: fixtures are supplied by callers and session
signals stay local and bounded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from open_brain_legacy._compat.open_brain.integrations.ports import (
    ReviewBoundWriter,
    ReviewDisposition,
    ReviewWriteRequest,
    ReviewWriteResult,
)

_MAX_CAPACITY = 100
_MAX_REPOSITORIES = 64
_MAX_SESSION_SIGNALS = 100


class TimeoutClass(StrEnum):
    """Bounded execution categories for synthetic workflow fixtures."""

    SHORT = "short"
    LONG = "long"


class FixtureJournalDisposition(StrEnum):
    """Safe outcomes for fixture journal recording."""

    RECORDED = "recorded"
    DUPLICATE = "duplicate"
    EXCLUDED = "excluded"
    CAPPED = "capped"


@dataclass(frozen=True, slots=True)
class FixtureJournalEntry:
    """An opaque fixture record; repository paths and command output are excluded."""

    entry_id: str
    repository_id: str
    timeout_class: TimeoutClass

    def __post_init__(self) -> None:
        if (
            not _is_opaque_id(self.entry_id)
            or not _is_opaque_id(self.repository_id)
            or not isinstance(self.timeout_class, TimeoutClass)
        ):
            raise ValueError("invalid fixture journal entry")


@dataclass(frozen=True, slots=True)
class FixtureJournalResult:
    """A structural fixture journal acknowledgement."""

    entry_id: str
    disposition: FixtureJournalDisposition

    def __post_init__(self) -> None:
        if not _is_opaque_id(self.entry_id) or not isinstance(
            self.disposition, FixtureJournalDisposition
        ):
            raise ValueError("invalid fixture journal result")


@dataclass(slots=True)
class DevWorkflowFixtureJournal:
    """A finite, idempotent journal for synthetic development workflow fixtures."""

    capacity: int
    excluded_repository_ids: frozenset[str] = field(default_factory=frozenset)
    _entries: list[FixtureJournalEntry] = field(default_factory=list, init=False, repr=False)
    _entry_ids: set[str] = field(default_factory=set, init=False, repr=False)

    def __post_init__(self) -> None:
        if type(self.capacity) is not int or not 1 <= self.capacity <= _MAX_CAPACITY:
            raise ValueError("invalid fixture journal capacity")
        if (
            not isinstance(self.excluded_repository_ids, frozenset)
            or len(self.excluded_repository_ids) > _MAX_REPOSITORIES
            or not all(_is_opaque_id(value) for value in self.excluded_repository_ids)
        ):
            raise ValueError("invalid excluded repository identifiers")

    @property
    def entries(self) -> tuple[FixtureJournalEntry, ...]:
        return tuple(self._entries)

    def record(self, entry: FixtureJournalEntry) -> FixtureJournalResult:
        if entry.entry_id in self._entry_ids:
            disposition = FixtureJournalDisposition.DUPLICATE
        elif entry.repository_id in self.excluded_repository_ids:
            disposition = FixtureJournalDisposition.EXCLUDED
        elif len(self._entries) >= self.capacity:
            disposition = FixtureJournalDisposition.CAPPED
        else:
            self._entries.append(entry)
            self._entry_ids.add(entry.entry_id)
            disposition = FixtureJournalDisposition.RECORDED
        return FixtureJournalResult(entry_id=entry.entry_id, disposition=disposition)


@dataclass(frozen=True, slots=True)
class SessionSignal:
    """Content-minimal metadata for a work session event."""

    signal_id: str
    repository_id: str
    session_id: str

    def __post_init__(self) -> None:
        if not all(
            _is_opaque_id(value) for value in (self.signal_id, self.repository_id, self.session_id)
        ):
            raise ValueError("invalid session signal")


class SessionSignalDisposition(StrEnum):
    """Non-raising outcomes for local session signal recording."""

    RECORDED = "recorded"
    DUPLICATE = "duplicate"
    CAPPED = "capped"
    FAILED = "failed"


class SessionSignalError(StrEnum):
    """Redacted error classes for session signal failures."""

    INVALID_SIGNAL = "invalid_signal"
    UNCONFINED_REPOSITORY = "unconfined_repository"
    CAPACITY = "capacity"


@dataclass(frozen=True, slots=True)
class SessionSignalResult:
    """A content-free session signal acknowledgement."""

    signal_id: str
    disposition: SessionSignalDisposition
    error: SessionSignalError | None = None

    def __post_init__(self) -> None:
        if (
            not _is_opaque_id(self.signal_id)
            or not isinstance(self.disposition, SessionSignalDisposition)
            or (self.error is not None and not isinstance(self.error, SessionSignalError))
            or (self.disposition is SessionSignalDisposition.FAILED) != (self.error is not None)
        ):
            raise ValueError("invalid session signal result")


@dataclass(frozen=True, slots=True)
class WorkWriteRequest:
    """A typed review write confined by an opaque repository identifier."""

    repository_id: str
    write: ReviewWriteRequest

    def __post_init__(self) -> None:
        if not _is_opaque_id(self.repository_id) or not isinstance(
            self.write, ReviewWriteRequest
        ):
            raise ValueError("invalid work write request")


@dataclass(slots=True)
class DevWorkflowIntegration:
    """Bounded signals and review-routed writes for allowlisted work fixtures."""

    allowed_repository_ids: frozenset[str]
    signal_capacity: int = _MAX_SESSION_SIGNALS
    review_writer: ReviewBoundWriter | None = None
    _session_signals: list[SessionSignal] = field(default_factory=list, init=False, repr=False)
    _signal_ids: set[str] = field(default_factory=set, init=False, repr=False)
    _write_request_ids: set[str] = field(default_factory=set, init=False, repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.allowed_repository_ids, frozenset)
            or not self.allowed_repository_ids
            or len(self.allowed_repository_ids) > _MAX_REPOSITORIES
            or not all(_is_opaque_id(value) for value in self.allowed_repository_ids)
        ):
            raise ValueError("invalid allowed repository identifiers")
        if (
            type(self.signal_capacity) is not int
            or not 1 <= self.signal_capacity <= _MAX_SESSION_SIGNALS
        ):
            raise ValueError("invalid session signal capacity")

    @property
    def session_signals(self) -> tuple[SessionSignal, ...]:
        return tuple(self._session_signals)

    def submit_work_write(self, request: WorkWriteRequest) -> ReviewWriteResult:
        write = request.write
        if (
            request.repository_id not in self.allowed_repository_ids
            or self.review_writer is None
        ):
            return _blocked_write_result(write)
        if write.request_id in self._write_request_ids:
            return ReviewWriteResult(
                request_id=write.request_id,
                disposition=ReviewDisposition.DUPLICATE,
                review_id=write.review_id,
            )
        try:
            result = self.review_writer.submit(write)
        except Exception:
            return _blocked_write_result(write)
        if (
            not isinstance(result, ReviewWriteResult)
            or result.request_id != write.request_id
            or result.review_id != write.review_id
        ):
            return _blocked_write_result(write)
        if result.disposition in {ReviewDisposition.QUEUED, ReviewDisposition.DUPLICATE}:
            self._write_request_ids.add(write.request_id)
        return result

    def record_session_signal(self, signal: object) -> SessionSignalResult:
        if not isinstance(signal, SessionSignal):
            return _failed_signal_result("redacted", SessionSignalError.INVALID_SIGNAL)
        if signal.repository_id not in self.allowed_repository_ids:
            return _failed_signal_result(
                signal.signal_id, SessionSignalError.UNCONFINED_REPOSITORY
            )
        if signal.signal_id in self._signal_ids:
            return SessionSignalResult(
                signal_id=signal.signal_id,
                disposition=SessionSignalDisposition.DUPLICATE,
            )
        if len(self._session_signals) >= self.signal_capacity:
            return _failed_signal_result(signal.signal_id, SessionSignalError.CAPACITY)
        self._session_signals.append(signal)
        self._signal_ids.add(signal.signal_id)
        return SessionSignalResult(
            signal_id=signal.signal_id,
            disposition=SessionSignalDisposition.RECORDED,
        )


def _failed_signal_result(signal_id: str, error: SessionSignalError) -> SessionSignalResult:
    return SessionSignalResult(
        signal_id=signal_id,
        disposition=SessionSignalDisposition.FAILED,
        error=error,
    )


def _blocked_write_result(write: ReviewWriteRequest) -> ReviewWriteResult:
    return ReviewWriteResult(
        request_id=write.request_id,
        disposition=ReviewDisposition.BLOCKED,
        review_id=write.review_id,
    )


def _is_opaque_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 128
        and all(
            character.isascii() and (character.isalnum() or character in "_.-")
            for character in value
        )
    )

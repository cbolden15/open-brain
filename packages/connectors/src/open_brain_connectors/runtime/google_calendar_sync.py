"""Durable preview and import state for bounded Google Calendar sync."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from secrets import token_hex
from typing import Final, TypeGuard, cast

from open_brain_engine.engine import PrivacyDecision

from open_brain_connectors.runtime.calendar import CalendarEventRecord, CalendarSourceAdapter
from open_brain_connectors.runtime.connectors import ConnectorContractError
from open_brain_connectors.runtime.google_calendar_contracts import (
    MAX_EVENTS,
    MAX_PAGES,
    GoogleCalendarCapture,
    GoogleCalendarChange,
    GoogleCalendarError,
    GoogleCalendarProvider,
    GoogleCalendarSelection,
)
from open_brain_connectors.runtime.source_intake import SourceRecordIntake, SourceRecordKey

_MAX_STATE_BYTES: Final = 4 * 1024 * 1024
_MAX_INTAKE_TEXT: Final = 65_536
_PREVIEW_ID: Final = re.compile(r"[0-9a-f]{64}")
_IDENTITY: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/#?=&%@,+~|-]{0,511}")
_URL: Final = re.compile(r"https://[^\s\x00]{1,2048}")


@dataclass(frozen=True, slots=True)
class _KnownEvent:
    event_id: str
    start_time: str
    end_time: str
    timezone: str
    source_reference: str

    @classmethod
    def from_record(
        cls,
        record: CalendarEventRecord,
        source_reference: str,
    ) -> _KnownEvent:
        return cls(
            record.event_id,
            record.start_time,
            record.end_time,
            record.timezone,
            source_reference,
        )

    @classmethod
    def from_dict(cls, value: object) -> _KnownEvent:
        if not isinstance(value, Mapping) or set(value) != {
            "end_time",
            "event_id",
            "source_reference",
            "start_time",
            "timezone",
        }:
            raise GoogleCalendarError("google_calendar_state_invalid")
        parts = tuple(value[field] for field in sorted(value))
        if any(type(part) is not str for part in parts):
            raise GoogleCalendarError("google_calendar_state_invalid")
        return cls(
            event_id=cast(str, value["event_id"]),
            start_time=cast(str, value["start_time"]),
            end_time=cast(str, value["end_time"]),
            timezone=cast(str, value["timezone"]),
            source_reference=cast(str, value["source_reference"]),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "end_time": self.end_time,
            "event_id": self.event_id,
            "source_reference": self.source_reference,
            "start_time": self.start_time,
            "timezone": self.timezone,
        }


@dataclass(frozen=True, slots=True)
class _Checkpoint:
    destination_hash: str
    selection: GoogleCalendarSelection
    sync_token: str | None
    known: dict[str, _KnownEvent]
    applied_preview_id: str | None = None
    submitted_count: int = 0

    @classmethod
    def initial(cls, destination_hash: str, selection: GoogleCalendarSelection) -> _Checkpoint:
        return cls(destination_hash, selection, None, {})

    @classmethod
    def from_dict(cls, value: object) -> _Checkpoint:
        if not isinstance(value, Mapping) or set(value) != {
            "applied_preview_id",
            "destination_hash",
            "known_events",
            "schema_version",
            "selection",
            "sync_token",
            "submitted_count",
        }:
            raise GoogleCalendarError("google_calendar_state_invalid")
        if value["schema_version"] != 1:
            raise GoogleCalendarError("google_calendar_state_invalid")
        selection_value = value["selection"]
        known_value = value["known_events"]
        if not isinstance(selection_value, Mapping) or not isinstance(known_value, list):
            raise GoogleCalendarError("google_calendar_state_invalid")
        try:
            selection = GoogleCalendarSelection(
                connection_id=cast(str, selection_value["connection_id"]),
                calendar_id=cast(str, selection_value["calendar_id"]),
                range_start=cast(str, selection_value["range_start"]),
                range_end=cast(str, selection_value["range_end"]),
                timezone=cast(str, selection_value["timezone"]),
            )
            destination_hash = cast(str, value["destination_hash"])
            sync_token = cast(str | None, value["sync_token"])
            applied_preview_id = value["applied_preview_id"]
            submitted_count = value["submitted_count"]
            known_records = tuple(_KnownEvent.from_dict(item) for item in known_value)
        except (KeyError, TypeError) as error:
            raise GoogleCalendarError("google_calendar_state_invalid") from error
        if (
            type(destination_hash) is not str
            or _PREVIEW_ID.fullmatch(destination_hash) is None
            or (sync_token is not None and not _valid_cursor(sync_token))
            or (applied_preview_id is not None and (
                type(applied_preview_id) is not str
                or _PREVIEW_ID.fullmatch(applied_preview_id) is None))
            or type(submitted_count) is not int or submitted_count < 0
            or len(known_records) != len({item.event_id for item in known_records})
        ):
            raise GoogleCalendarError("google_calendar_state_invalid")
        known = {item.event_id: item for item in known_records}
        try:
            for item in known_records:
                if _URL.fullmatch(item.source_reference) is None:
                    raise GoogleCalendarError("google_calendar_state_invalid")
                CalendarEventRecord(
                    calendar_id=selection.resource_id,
                    event_id=item.event_id,
                    revision_id="state",
                    provider="google_calendar",
                    title="Calendar event unavailable",
                    start_time=item.start_time,
                    end_time=item.end_time,
                    timezone=item.timezone,
                    cancelled=True,
                )
        except ConnectorContractError as error:
            raise GoogleCalendarError("google_calendar_state_invalid") from error
        return cls(destination_hash, selection, sync_token, known,
                   applied_preview_id, submitted_count)

    def to_dict(self) -> dict[str, object]:
        return {
            "applied_preview_id": self.applied_preview_id,
            "destination_hash": self.destination_hash,
            "known_events": [self.known[event_id].to_dict() for event_id in sorted(self.known)],
            "schema_version": 1,
            "selection": self.selection.to_dict(),
            "sync_token": self.sync_token,
            "submitted_count": self.submitted_count,
        }

    def generation(self) -> str:
        return _digest(self.to_dict())

    def receipt(self) -> dict[str, object]:
        return {
            "checkpoint_committed": True,
            "known_count": len(self.known),
            "preview_id": self.applied_preview_id,
            "status": "completed" if self.submitted_count else "unchanged",
            "submitted_count": self.submitted_count,
        }


class GoogleCalendarSyncStore:
    """Private atomic state and pending-batch persistence."""

    def __init__(self, root: Path) -> None:
        if not isinstance(root, Path) or not root.is_absolute():
            raise GoogleCalendarError("google_calendar_state_invalid")
        self._root = root

    @contextmanager
    def locked(self, key: str) -> Iterator[None]:
        _require_store_key(key)
        directory_fd = self._open_root()
        lock_fd = -1
        try:
            lock_fd = self._open_file(directory_fd, f"calendar-{key}.lock", create=True)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno in {errno.EACCES, errno.EAGAIN}:
                    raise GoogleCalendarError("google_calendar_sync_busy") from None
                raise GoogleCalendarError("google_calendar_state_invalid") from error
            yield
        finally:
            if lock_fd >= 0:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(lock_fd)
            os.close(directory_fd)

    def load_checkpoint(
        self,
        key: str,
        *,
        destination_hash: str,
        selection: GoogleCalendarSelection,
    ) -> _Checkpoint:
        _require_store_key(key)
        decoded = self._read_json(f"calendar-{key}.json")
        if decoded is None:
            return _Checkpoint.initial(destination_hash, selection)
        checkpoint = _Checkpoint.from_dict(decoded)
        if checkpoint.destination_hash != destination_hash:
            raise GoogleCalendarError("google_calendar_state_invalid")
        if checkpoint.selection != selection:
            raise GoogleCalendarError("google_calendar_selection_changed")
        return checkpoint

    def save_checkpoint(self, key: str, checkpoint: _Checkpoint) -> None:
        _require_store_key(key)
        self._write_json(f"calendar-{key}.json", checkpoint.to_dict())

    def load_pending(self, key: str) -> dict[str, object] | None:
        _require_store_key(key)
        decoded = self._read_json(f"calendar-{key}.pending.json")
        if decoded is None:
            return None
        if not isinstance(decoded, dict):
            raise GoogleCalendarError("google_calendar_state_invalid")
        return cast(dict[str, object], decoded)

    def save_pending(self, key: str, pending: Mapping[str, object]) -> None:
        _require_store_key(key)
        self._write_json(f"calendar-{key}.pending.json", pending)

    def delete_pending(self, key: str) -> None:
        _require_store_key(key)
        directory_fd = self._open_root()
        try:
            try:
                os.unlink(f"calendar-{key}.pending.json", dir_fd=directory_fd)
            except FileNotFoundError:
                return
            os.fsync(directory_fd)
        except OSError as error:
            raise GoogleCalendarError("google_calendar_state_invalid") from error
        finally:
            os.close(directory_fd)

    def _open_root(self) -> int:
        try:
            self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory_fd = os.open(
                self._root,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
        except OSError as error:
            raise GoogleCalendarError("google_calendar_state_invalid") from error
        metadata = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or metadata.st_uid != os.getuid()
        ):
            os.close(directory_fd)
            raise GoogleCalendarError("google_calendar_state_invalid")
        return directory_fd

    def _open_file(self, directory_fd: int, name: str, *, create: bool = False) -> int:
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            if create:
                try:
                    descriptor = os.open(
                        name,
                        flags | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=directory_fd,
                    )
                except FileExistsError:
                    descriptor = os.open(name, flags, dir_fd=directory_fd)
            else:
                descriptor = os.open(name, flags, dir_fd=directory_fd)
        except OSError as error:
            raise GoogleCalendarError("google_calendar_state_invalid") from error
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
            or metadata.st_uid != os.getuid()
        ):
            os.close(descriptor)
            raise GoogleCalendarError("google_calendar_state_invalid")
        return descriptor

    def _read_json(self, name: str) -> object | None:
        directory_fd = self._open_root()
        descriptor = -1
        try:
            try:
                descriptor = self._open_file(directory_fd, name)
            except GoogleCalendarError as error:
                try:
                    os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                except FileNotFoundError:
                    return None
                raise error
            metadata = os.fstat(descriptor)
            if metadata.st_size > _MAX_STATE_BYTES:
                raise GoogleCalendarError("google_calendar_state_invalid")
            body = bytearray()
            while chunk := os.read(descriptor, min(65_536, _MAX_STATE_BYTES + 1 - len(body))):
                body.extend(chunk)
                if len(body) > _MAX_STATE_BYTES:
                    raise GoogleCalendarError("google_calendar_state_invalid")
            return cast(object, json.loads(body.decode("utf-8")))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GoogleCalendarError("google_calendar_state_invalid") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            os.close(directory_fd)

    def _write_json(self, name: str, value: Mapping[str, object]) -> None:
        payload = _canonical_json(value) + b"\n"
        if len(payload) > _MAX_STATE_BYTES:
            raise GoogleCalendarError("google_calendar_state_invalid")
        directory_fd = self._open_root()
        temporary = f".{name}.{token_hex(16)}.tmp"
        descriptor = -1
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=directory_fd,
            )
            _write_all(descriptor, payload)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            os.fsync(directory_fd)
        except OSError as error:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=directory_fd)
            raise GoogleCalendarError("google_calendar_state_invalid") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            os.close(directory_fd)


class GoogleCalendarSync:
    """Prepare, apply, and report one durable selected-calendar sync."""

    def __init__(
        self,
        provider: GoogleCalendarProvider,
        store: GoogleCalendarSyncStore,
        destination_id: str,
    ) -> None:
        if (
            not isinstance(store, GoogleCalendarSyncStore)
            or type(destination_id) is not str
            or not destination_id
            or len(destination_id) > 1024
            or any(ord(character) < 32 for character in destination_id)
        ):
            raise GoogleCalendarError("google_calendar_state_invalid")
        self._provider = provider
        self._store = store
        self._destination_hash = hashlib.sha256(destination_id.encode()).hexdigest()
        self._adapter = CalendarSourceAdapter()

    def prepare(self, selection: GoogleCalendarSelection) -> dict[str, object]:
        key = self._key(selection)
        with self._store.locked(key):
            checkpoint = self._store.load_checkpoint(
                key,
                destination_hash=self._destination_hash,
                selection=selection,
            )
            existing_pending = self._store.load_pending(key)
            if existing_pending is not None:
                self._require_pending(existing_pending, selection, None)
            changes, sync_token, full_sync = self._fetch(selection, checkpoint.sync_token)
            intakes, known_after = self._build_batch(selection, checkpoint, changes, full_sync)
            pending: dict[str, object] = {
                "base_generation": checkpoint.generation(),
                "discovered_count": len(changes),
                "full_sync": full_sync,
                "intakes": [_intake_to_dict(intake) for intake in intakes],
                "known_events": [
                    known_after[event_id].to_dict() for event_id in sorted(known_after)
                ],
                "next_sync_token": sync_token,
                "schema_version": 1,
                "selection_identity": selection.identity(),
            }
            preview_id = _digest(pending)
            pending["preview_id"] = preview_id
            self._store.save_pending(key, pending)
            return {
                "full_sync": full_sync,
                "preview_id": preview_id,
                "record_count": len(intakes),
                "records": [_intake_summary(intake) for intake in intakes],
                "status": "ready",
            }

    def apply(
        self,
        selection: GoogleCalendarSelection,
        preview_id: str,
        capture: GoogleCalendarCapture,
    ) -> dict[str, object]:
        if type(preview_id) is not str or _PREVIEW_ID.fullmatch(preview_id) is None:
            raise GoogleCalendarError("google_calendar_preview_invalid")
        key = self._key(selection)
        with self._store.locked(key):
            checkpoint = self._store.load_checkpoint(
                key,
                destination_hash=self._destination_hash,
                selection=selection,
            )
            raw_pending = self._store.load_pending(key)
            if checkpoint.applied_preview_id == preview_id:
                # A lost receipt or crash after commit must be retryable without
                # another fetch/capture. Never remove a newer prepared batch.
                if raw_pending is not None and raw_pending.get("preview_id") == preview_id:
                    self._store.delete_pending(key)
                return checkpoint.receipt()
            pending = self._require_pending(raw_pending, selection, preview_id)
            if pending["base_generation"] != checkpoint.generation():
                raise GoogleCalendarError("google_calendar_preview_stale")
            intakes = _intakes_from_pending(pending)
            known = _known_from_pending(pending)
            next_sync_token = pending["next_sync_token"]
            if not _valid_cursor(next_sync_token):
                raise GoogleCalendarError("google_calendar_state_invalid")
            _validate_pending_intakes(selection, intakes, known)
            for intake in intakes:
                capture(intake)
            advanced = _Checkpoint(
                self._destination_hash,
                selection,
                next_sync_token,
                known,
                preview_id,
                len(intakes),
            )
            self._store.save_checkpoint(key, advanced)
            self._store.delete_pending(key)
            return advanced.receipt()

    def status(self, selection: GoogleCalendarSelection) -> dict[str, object]:
        key = self._key(selection)
        with self._store.locked(key):
            checkpoint = self._store.load_checkpoint(
                key,
                destination_hash=self._destination_hash,
                selection=selection,
            )
            pending = self._store.load_pending(key)
            if (pending is not None
                    and pending.get("preview_id") == checkpoint.applied_preview_id):
                self._store.delete_pending(key)
                pending = None
            pending_id: str | None = None
            if pending is not None:
                required = self._require_pending(pending, selection, None)
                pending_id = cast(str, required["preview_id"])
            return {
                "configured": checkpoint.sync_token is not None,
                "known_count": len(checkpoint.known),
                "pending": pending is not None,
                "pending_preview_id": pending_id,
                "status": "ready",
            }

    def _key(self, selection: GoogleCalendarSelection) -> str:
        if type(selection) is not GoogleCalendarSelection:
            raise GoogleCalendarError("google_calendar_invalid_selection")
        if self._provider.connection_id != selection.connection_id:
            raise GoogleCalendarError("google_calendar_invalid_selection")
        return hashlib.sha256(
            (
                f"{self._destination_hash}\x1f{selection.connection_id}\x1f{selection.calendar_id}"
            ).encode()
        ).hexdigest()

    def _fetch(
        self,
        selection: GoogleCalendarSelection,
        sync_token: str | None,
    ) -> tuple[tuple[GoogleCalendarChange, ...], str, bool]:
        try:
            changes, token = self._fetch_complete(selection, sync_token)
            return changes, token, sync_token is None
        except GoogleCalendarError as error:
            if str(error) != "google_calendar_sync_expired":
                raise
        changes, token = self._fetch_complete(selection, None)
        return changes, token, True

    def _fetch_complete(
        self,
        selection: GoogleCalendarSelection,
        sync_token: str | None,
    ) -> tuple[tuple[GoogleCalendarChange, ...], str]:
        changes: list[GoogleCalendarChange] = []
        observed_events: set[str] = set()
        observed_pages: set[str] = set()
        page_token: str | None = None
        for _ in range(MAX_PAGES):
            page = self._provider.fetch_page(
                selection,
                page_token=page_token,
                sync_token=sync_token,
            )
            if page.next_page_token is not None and page.next_sync_token is not None:
                raise GoogleCalendarError("google_calendar_sync_incomplete")
            for change in page.changes:
                _validate_change(change)
                if change.event_id in observed_events:
                    raise GoogleCalendarError("google_calendar_sync_incomplete")
                observed_events.add(change.event_id)
                changes.append(change)
                if len(changes) > MAX_EVENTS:
                    raise GoogleCalendarError("google_calendar_sync_incomplete")
            if page.next_page_token is None:
                if not _valid_cursor(page.next_sync_token):
                    raise GoogleCalendarError("google_calendar_sync_incomplete")
                return tuple(changes), page.next_sync_token
            if not _valid_cursor(page.next_page_token) or page.next_page_token in observed_pages:
                raise GoogleCalendarError("google_calendar_sync_incomplete")
            observed_pages.add(page.next_page_token)
            page_token = page.next_page_token
        raise GoogleCalendarError("google_calendar_sync_incomplete")

    def _build_batch(
        self,
        selection: GoogleCalendarSelection,
        checkpoint: _Checkpoint,
        changes: tuple[GoogleCalendarChange, ...],
        full_sync: bool,
    ) -> tuple[tuple[SourceRecordIntake, ...], dict[str, _KnownEvent]]:
        known_after = dict(checkpoint.known)
        intakes: list[SourceRecordIntake] = []
        observed = {change.event_id for change in changes}
        source_selection = self._adapter.calendar_selection(
            connection_id=selection.connection_id,
            calendar_id=selection.resource_id,
            provider="google_calendar",
        )
        privacy = _calendar_privacy()
        for change in changes:
            if change.record is not None:
                prior = checkpoint.known.get(change.event_id)
                intake = self._adapter.intake(source_selection, change.record, privacy=privacy)
                accepted_reference = (
                    prior.source_reference
                    if prior is not None
                    else change.source_reference or intake.source_reference
                )
                intake = _finalize_intake(
                    intake,
                    source_reference=accepted_reference,
                    revision_id=change.revision_id,
                )
                known_after[change.event_id] = _KnownEvent.from_record(
                    change.record,
                    intake.source_reference,
                )
                intakes.append(intake)
                continue
            known = checkpoint.known.get(change.event_id)
            if known is None:
                continue
            intakes.append(
                _finalize_intake(
                    self._adapter.intake(
                        source_selection,
                        _unavailable_record(selection, known, change.revision_id, change.reason),
                        privacy=privacy,
                    ),
                    source_reference=known.source_reference,
                    revision_id=change.revision_id,
                )
            )
        if full_sync:
            for event_id in sorted(checkpoint.known.keys() - observed):
                known = checkpoint.known[event_id]
                revision_id = "missing:" + hashlib.sha256(event_id.encode()).hexdigest()
                intakes.append(
                    _finalize_intake(
                        self._adapter.intake(
                            source_selection,
                            _unavailable_record(selection, known, revision_id, "missing"),
                            privacy=privacy,
                        ),
                        source_reference=known.source_reference,
                        revision_id=revision_id,
                    )
                )
        return tuple(intakes), known_after

    def _require_pending(
        self,
        value: object,
        selection: GoogleCalendarSelection,
        preview_id: str | None,
    ) -> dict[str, object]:
        required_fields = {
            "base_generation",
            "discovered_count",
            "full_sync",
            "intakes",
            "known_events",
            "next_sync_token",
            "preview_id",
            "schema_version",
            "selection_identity",
        }
        if not isinstance(value, dict) or set(value) != required_fields:
            raise GoogleCalendarError("google_calendar_preview_missing")
        stored_preview = value["preview_id"]
        if (
            value["schema_version"] != 1
            or type(stored_preview) is not str
            or _PREVIEW_ID.fullmatch(stored_preview) is None
        ):
            raise GoogleCalendarError("google_calendar_state_invalid")
        if value["selection_identity"] != selection.identity():
            raise GoogleCalendarError("google_calendar_selection_changed")
        payload = dict(value)
        del payload["preview_id"]
        if _digest(payload) != stored_preview:
            raise GoogleCalendarError("google_calendar_state_invalid")
        if preview_id is not None and stored_preview != preview_id:
            raise GoogleCalendarError("google_calendar_preview_stale")
        return cast(dict[str, object], value)


def _validate_change(change: GoogleCalendarChange) -> None:
    if type(change) is not GoogleCalendarChange:
        raise GoogleCalendarError("google_calendar_sync_incomplete")
    if (
        _IDENTITY.fullmatch(change.event_id) is None
        or _IDENTITY.fullmatch(change.revision_id) is None
        or (change.reason == "updated") != (change.record is not None)
        or (
            change.record is not None
            and (
                change.record.event_id != change.event_id
                or change.record.revision_id != change.revision_id
                or change.record.provider != "google_calendar"
            )
        )
    ):
        raise GoogleCalendarError("google_calendar_sync_incomplete")


def _unavailable_record(
    selection: GoogleCalendarSelection,
    known: _KnownEvent,
    revision_id: str,
    reason: str,
) -> CalendarEventRecord:
    title = "Calendar event cancelled" if reason == "cancelled" else "Calendar event unavailable"
    return CalendarEventRecord(
        calendar_id=selection.resource_id,
        event_id=known.event_id,
        revision_id=revision_id,
        provider="google_calendar",
        title=title,
        start_time=known.start_time,
        end_time=known.end_time,
        timezone=known.timezone,
        cancelled=True,
    )


def _calendar_privacy() -> PrivacyDecision:
    return PrivacyDecision.from_dict(
        {
            "authority": {"cloud": False, "external_egress": False},
            "confirmation_ref": None,
            "policy_version": "privacy-v1",
            "reason": "personal_local_only",
            "tier": "personal",
        }
    )


def _finalize_intake(
    intake: SourceRecordIntake,
    *,
    source_reference: str,
    revision_id: str,
) -> SourceRecordIntake:
    text = f"Source revision: {revision_id}\n{intake.text}"
    if len(text) > _MAX_INTAKE_TEXT:
        raise GoogleCalendarError("google_calendar_event_too_large")
    try:
        return SourceRecordIntake(
            key=intake.key,
            url=source_reference,
            text=text,
            privacy=intake.privacy,
            title=intake.title,
        )
    except ConnectorContractError as error:
        raise GoogleCalendarError("google_calendar_sync_incomplete") from error


def _intake_summary(intake: SourceRecordIntake) -> dict[str, object]:
    return {
        "content_type": "calendar_event",
        "delivery_id": intake.key.delivery_id(),
        "selected": True,
        "source_reference": intake.source_reference,
        "title": intake.title,
    }


def _intake_to_dict(intake: SourceRecordIntake) -> dict[str, object]:
    return {
        "key": {
            "connection_id": intake.key.connection_id,
            "connector_name": intake.key.connector_name,
            "external_id": intake.key.external_id,
            "resource_id": intake.key.resource_id,
            "revision_id": intake.key.revision_id,
        },
        "privacy": intake.privacy.to_dict(),
        "text": intake.text,
        "title": intake.title,
        "url": intake.url,
    }


def _intakes_from_pending(value: Mapping[str, object]) -> tuple[SourceRecordIntake, ...]:
    raw_intakes = value["intakes"]
    if not isinstance(raw_intakes, list):
        raise GoogleCalendarError("google_calendar_state_invalid")
    intakes: list[SourceRecordIntake] = []
    try:
        for raw in raw_intakes:
            if not isinstance(raw, Mapping) or set(raw) != {
                "key",
                "privacy",
                "text",
                "title",
                "url",
            }:
                raise GoogleCalendarError("google_calendar_state_invalid")
            key = raw["key"]
            privacy = raw["privacy"]
            if not isinstance(key, Mapping) or not isinstance(privacy, dict):
                raise GoogleCalendarError("google_calendar_state_invalid")
            intakes.append(
                SourceRecordIntake(
                    key=SourceRecordKey(
                        connector_name=cast(str, key["connector_name"]),
                        connection_id=cast(str, key["connection_id"]),
                        resource_id=cast(str, key["resource_id"]),
                        external_id=cast(str, key["external_id"]),
                        revision_id=cast(str, key["revision_id"]),
                    ),
                    url=cast(str, raw["url"]),
                    text=cast(str, raw["text"]),
                    privacy=PrivacyDecision.from_dict(privacy),
                    title=cast(str | None, raw["title"]),
                )
            )
    except (KeyError, TypeError, ValueError) as error:
        raise GoogleCalendarError("google_calendar_state_invalid") from error
    return tuple(intakes)


def _known_from_pending(value: Mapping[str, object]) -> dict[str, _KnownEvent]:
    raw_known = value["known_events"]
    if not isinstance(raw_known, list):
        raise GoogleCalendarError("google_calendar_state_invalid")
    known_records = tuple(_KnownEvent.from_dict(item) for item in raw_known)
    if len(known_records) != len({item.event_id for item in known_records}):
        raise GoogleCalendarError("google_calendar_state_invalid")
    return {item.event_id: item for item in known_records}


def _validate_pending_intakes(
    selection: GoogleCalendarSelection,
    intakes: tuple[SourceRecordIntake, ...],
    known: Mapping[str, _KnownEvent],
) -> None:
    deliveries: set[str] = set()
    expected_privacy = _calendar_privacy()
    for intake in intakes:
        external_prefix = "google_calendar:"
        event_id = intake.key.external_id.removeprefix(external_prefix)
        known_event = known.get(event_id)
        delivery_id = intake.key.delivery_id()
        if (
            not intake.key.external_id.startswith(external_prefix)
            or known_event is None
            or intake.key.connector_name != "calendar"
            or intake.key.connection_id != selection.connection_id
            or intake.key.resource_id != selection.resource_id
            or intake.source_reference != known_event.source_reference
            or intake.privacy != expected_privacy
            or not intake.text.startswith(f"Source revision: {intake.key.revision_id}\n")
            or delivery_id in deliveries
        ):
            raise GoogleCalendarError("google_calendar_state_invalid")
        deliveries.add(delivery_id)


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    except (TypeError, ValueError) as error:
        raise GoogleCalendarError("google_calendar_state_invalid") from error


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _valid_cursor(value: object) -> TypeGuard[str]:
    return (
        type(value) is str
        and bool(value)
        and len(value.encode("utf-8")) <= 8_192
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


def _require_store_key(value: object) -> None:
    if type(value) is not str or _PREVIEW_ID.fullmatch(value) is None:
        raise GoogleCalendarError("google_calendar_state_invalid")


def _write_all(descriptor: int, payload: bytes) -> None:
    written = 0
    while written < len(payload):
        count = os.write(descriptor, payload[written:])
        if count <= 0:
            raise OSError("short write")
        written += count

"""Thin owner-local retry spool for harness captures that could not be delivered.

One append-only JSONL file, mode 0600, in a 0700 directory. Every change rewrites
a temporary sibling, fsyncs it, renames it over the spool and fsyncs the directory,
so a crash leaves either the previous or the next complete file. Delivered lines are
removed. Delivery IDs make a replayed line a downstream no-op, so no receipts exist.
This protects against accidental paths/modes; it is not hostile same-user isolation.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from hashlib import sha256
from pathlib import Path
from typing import NotRequired, TypedDict, cast
from uuid import uuid4

from open_brain_engine.core.access_contracts import derive_brain_id
from open_brain_engine.core.ids import portable_canonical_json_bytes

from .harness_batching import capture_batches
from .harness_events import HarnessCaptureEvent, HarnessEventError, _screen
from .harness_memory import MemoryMessage
from .harness_workflow import HarnessWorkflow

_VERSION = "open-brain-harness-spool.v1"
_SPOOL = "spool.jsonl"
_LOCK = "spool.lock"
_MAX_LINE_BYTES = 256 * 1024
_MAX_SPOOL_BYTES = 64 * 1024 * 1024
_DRAIN_LIMIT = 16
_BRAIN = re.compile(r"brn_[a-z2-7]{26}\Z")
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_DELIVERED = frozenset({"acknowledged", "success_empty"})


class _CaptureArguments(TypedDict):
    timestamps: Mapping[int, str | None]
    local_only: NotRequired[bool]
    selection: NotRequired[tuple[tuple[int, int], ...]]


class HarnessSpoolError(ValueError):
    """Closed spool refusal metadata; never a path or source text."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class HarnessSpool:
    """Foreground retry spool. Credential screening precedes every write.

    The parent directory must exist. Filesystem calls have no wall-clock bound;
    hosts enforce process deadlines. A deadline during drain leaves every line.
    """

    def __init__(self, root: Path, brain_id: str) -> None:
        if (
            not root.is_absolute()
            or ".." in root.parts
            or "\0" in str(root)
            or _BRAIN.fullmatch(brain_id) is None
        ):
            raise ValueError("invalid_spool")
        self.root = root
        self.brain_id = brain_id

    def requests(self, event: HarnessCaptureEvent) -> tuple[dict[str, object], ...]:
        """Screen the whole event, then split it into bounded delivery requests."""
        for message in event.messages:
            # Also screen retained injected context; it must not smuggle credentials.
            _screen(message.text)
        if type(event.local_only) is not bool:
            raise HarnessSpoolError("invalid_event")
        try:
            batches = capture_batches(event.messages)
        except ValueError:
            raise HarnessSpoolError("request_too_large") from None
        documents: list[dict[str, object]] = []
        for batch in batches:
            messages = event.messages if len(batches) == 1 else batch.messages
            ordinals = {message.ordinal for message in messages}
            document: dict[str, object] = {
                "version": _VERSION,
                "brain_id": self.brain_id,
                "client": event.client,
                "session_id": event.session_id,
                "project": str(event.project),
                "messages": [
                    {
                        "role": item.role,
                        "text": item.text,
                        "ordinal": item.ordinal,
                        "authored": item.authored,
                        "source_id": item.source_id,
                    }
                    for item in messages
                ],
                "timestamps": [
                    [key, value] for key, value in sorted(event.timestamps.items())
                    if key in ordinals
                ],
            }
            if event.local_only:
                document["local_only"] = True
            if len(batches) > 1:
                document["selection"] = [list(key) for key in batch.selection]
            if len(_line(document, 0, None)) > _MAX_LINE_BYTES:
                raise HarnessSpoolError("request_too_large")
            documents.append(document)
        return tuple(documents)

    def append(self, event: HarnessCaptureEvent) -> dict[str, object]:
        """A queued result means every request of the event is durably spooled."""
        try:
            return self.store(self.requests(event))
        except HarnessEventError:
            return self._metadata("refused", "credential_signal")
        except HarnessSpoolError as error:
            return self._metadata("refused", str(error))
        except ValueError, TypeError, AttributeError:
            return self._metadata("refused", "invalid_event")

    def store(self, documents: tuple[dict[str, object], ...]) -> dict[str, object]:
        """Append already screened requests; an identical pending line is kept once."""
        try:
            identifiers = [request_id(document) for document in documents]
            with self._locked(create=True) as directory:
                entries = self._load(directory)
                known = {cast(str, entry["id"]) for entry in entries}
                for identifier, document in zip(identifiers, documents, strict=True):
                    if identifier not in known:
                        entries.append(
                            {"id": identifier, "request": document, "attempts": 0,
                             "last_status": None, "last_error": None}
                        )
                        known.add(identifier)
                self._write(directory, entries)
        except HarnessSpoolError as error:
            return self._metadata("refused", str(error))
        except OSError as error:
            return self._io_failure(error)
        result = self._metadata("queued")
        result["request_id"] = identifiers[-1]
        if len(identifiers) > 1:
            result.update({"request_ids": identifiers[:16], "batch_count": len(identifiers)})
        return result

    def deliver(self, workflow: HarnessWorkflow, document: dict[str, object]) -> dict[str, object]:
        """Deliver one request through the workflow; failures are metadata only."""
        try:
            client, session, project, messages, arguments = _decode(document, self.brain_id)
            raw = workflow.capture(client, session, project, messages, **arguments)
        except Exception:
            # Callers must never log raw provider/storage exception contents.
            return {"status": "unavailable", "error_code": "capture_unavailable"}
        return _acknowledgement(raw)

    def drain(self, workflow: HarnessWorkflow, *, limit: int = _DRAIN_LIMIT) -> dict[str, object]:
        """Replay the oldest lines, at most 16, and remove the delivered ones.

        A failed line moves behind every other line, so a failing prefix cannot
        starve later captures. Nothing is discarded until delivery succeeds.
        """
        if type(limit) is not int or not 1 <= limit <= _DRAIN_LIMIT:
            return self._metadata("refused", "invalid_limit")
        try:
            if derive_brain_id(workflow.tasks.profile.tenant_id) != self.brain_id:
                return self._metadata("refused", "destination_mismatch")
        except ValueError, AttributeError:
            return self._metadata("refused", "destination_mismatch")
        try:
            # Hold the lock only while reading and rewriting, never across delivery.
            with self._locked(create=False) as directory:
                batch = self._load(directory)[:limit]
        except FileNotFoundError:
            if self.root.exists() or self.root.is_symlink():
                return self._metadata("refused", "invalid_spool")
            return {**self._metadata("success_empty"), "results": []}
        except HarnessSpoolError as error:
            return self._metadata("refused", str(error))
        except OSError as error:
            return self._io_failure(error)
        results: list[dict[str, object]] = []
        delivered: set[str] = set()
        failed: dict[str, tuple[str, str | None]] = {}
        for entry in batch:
            identifier = cast(str, entry["id"])
            acknowledgement = self.deliver(workflow, cast(dict[str, object], entry["request"]))
            status = cast(str, acknowledgement["status"])
            if status in _DELIVERED:
                delivered.add(identifier)
                results.append(
                    {"status": "delivered", "request_id": identifier,
                     "acknowledgement": acknowledgement}
                )
                continue
            outcome = "pending" if status == "queued" else status
            code = acknowledgement.get("error_code")
            failed[identifier] = (outcome, code if isinstance(code, str) else None)
            results.append({"status": outcome, "request_id": identifier,
                            **({"error_code": code} if isinstance(code, str) else {})})
        try:
            with self._locked(create=False) as directory:
                entries = self._load(directory)
                kept = [e for e in entries if e["id"] not in delivered and e["id"] not in failed]
                retried = [e for e in entries if e["id"] in failed]
                for entry in retried:
                    entry["attempts"] = cast(int, entry["attempts"]) + 1
                    entry["last_status"], entry["last_error"] = failed[cast(str, entry["id"])]
                self._write(directory, kept + retried)
        except HarnessSpoolError as error:
            return self._metadata("refused", str(error))
        except OSError as error:
            return self._io_failure(error)
        statuses = {result["status"] for result in results}
        status = next(
            (
                name for name in ("refused", "unavailable", "pending", "delivered")
                if name in statuses
            ),
            "success_empty",
        )
        return {**self._metadata(status), "results": results, "remaining": len(kept + retried)}

    def mark_unavailable(self) -> dict[str, object]:
        """Record a backend-startup failure on the oldest 16 lines without a workflow."""
        try:
            with self._locked(create=False) as directory:
                entries = self._load(directory)
                for entry in entries[:_DRAIN_LIMIT]:
                    entry["attempts"] = cast(int, entry["attempts"]) + 1
                    entry["last_status"], entry["last_error"] = "unavailable", "capture_unavailable"
                self._write(directory, entries)
        except FileNotFoundError:
            return self._metadata("success_empty")
        except HarnessSpoolError as error:
            return self._metadata("refused", str(error))
        except OSError as error:
            return self._io_failure(error)
        return self._metadata("unavailable", "capture_unavailable")

    def status(self) -> dict[str, object]:
        """Count spooled lines and their latest failures; never returns source text."""
        counts = {"pending": 0, "refused": 0, "unavailable": 0}
        failure: dict[str, object] | None = None
        try:
            with self._locked(create=False) as directory:
                for entry in self._load(directory):
                    counts["pending"] += 1
                    if entry["last_status"] in {"refused", "unavailable"}:
                        counts[entry["last_status"]] += 1
        except FileNotFoundError:
            if self.root.exists() or self.root.is_symlink():
                failure = self._metadata("refused", "invalid_spool")
        except HarnessSpoolError as error:
            failure = self._metadata("refused", str(error))
        except OSError as error:
            failure = self._io_failure(error)
        status = next(
            (name for name in ("refused", "unavailable", "pending") if counts[name]),
            "success_empty",
        )
        return {
            **(failure if failure is not None else self._metadata(status)),
            "counts": counts,
            "error_count": counts["refused"] + counts["unavailable"] + int(failure is not None),
            "complete": failure is None,
        }

    @contextmanager
    def _locked(self, *, create: bool) -> Iterator[int]:
        directory = _open_root(self.root, create=create)
        try:
            lock = os.open(
                _LOCK,
                os.O_RDWR | (os.O_CREAT if create else 0) | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            try:
                _check(os.fstat(lock))
                fcntl.flock(lock, fcntl.LOCK_EX)
                yield directory
            finally:
                os.close(lock)
        finally:
            os.close(directory)

    def _load(self, directory: int) -> list[dict[str, object]]:
        try:
            descriptor = os.open(_SPOOL, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        except FileNotFoundError:
            return []
        with os.fdopen(descriptor, "rb") as stream:
            _check(os.fstat(stream.fileno()))
            body = stream.read(_MAX_SPOOL_BYTES + 1)
        if len(body) > _MAX_SPOOL_BYTES:
            raise HarnessSpoolError("spool_full")
        entries: list[dict[str, object]] = []
        try:
            for raw in body.splitlines():
                entry = json.loads(raw)
                if (
                    not isinstance(entry, dict)
                    or set(entry) != {"id", "request", "attempts", "last_status", "last_error"}
                    or type(entry["attempts"]) is not int
                    or entry["last_status"] not in {None, "pending", "refused", "unavailable"}
                    or not isinstance(entry["request"], dict)
                    or entry["id"] != request_id(entry["request"])
                ):
                    raise ValueError
                entries.append(entry)
        except ValueError, TypeError, UnicodeError:
            raise HarnessSpoolError("invalid_spool") from None
        return entries

    def _write(self, directory: int, entries: list[dict[str, object]]) -> None:
        body = b"".join(
            _line(
                cast(dict[str, object], entry["request"]),
                cast(int, entry["attempts"]),
                cast(str | None, entry["last_status"]),
                cast(str | None, entry["last_error"]),
            )
            for entry in entries
        )
        if len(body) > _MAX_SPOOL_BYTES:
            raise HarnessSpoolError("spool_full")
        temporary = _SPOOL + ".tmp-" + uuid4().hex
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
            dir_fd=directory,
        )
        try:
            try:
                view = memoryview(body)
                while view:
                    view = view[os.write(descriptor, view):]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, _SPOOL, src_dir_fd=directory, dst_dir_fd=directory)
        except BaseException:
            with suppress(OSError):
                os.unlink(temporary, dir_fd=directory)
            raise
        os.fsync(directory)

    def _metadata(self, status: str, error_code: str | None = None) -> dict[str, object]:
        result: dict[str, object] = {"status": status, "brain_id": self.brain_id}
        if error_code is not None:
            result["error_code"] = error_code
        return result

    def _io_failure(self, error: OSError) -> dict[str, object]:
        unsafe = error.errno in {errno.ELOOP, errno.ENOTDIR, errno.EACCES, errno.EPERM}
        return self._metadata(
            "refused" if unsafe else "unavailable",
            "unsafe_spool" if unsafe else "spool_unavailable",
        )


def request_id(document: dict[str, object]) -> str:
    """Stable content identity of one spooled request."""
    return sha256(portable_canonical_json_bytes(document)).hexdigest()


def _line(
    document: dict[str, object], attempts: int, status: str | None, error: str | None = None
) -> bytes:
    entry = {
        "id": request_id(document),
        "request": document,
        "attempts": attempts,
        "last_status": status,
        "last_error": error,
    }
    return portable_canonical_json_bytes(entry) + b"\n"


def _check(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise HarnessSpoolError("unsafe_spool")


def _open_root(path: Path, *, create: bool) -> int:
    parent = os.open(path.parent, _DIRECTORY_FLAGS)
    try:
        if create:
            with suppress(FileExistsError):
                os.mkdir(path.name, 0o700, dir_fd=parent)
                os.fsync(parent)
        directory = os.open(path.name, _DIRECTORY_FLAGS, dir_fd=parent)
    finally:
        os.close(parent)
    metadata = os.fstat(directory)
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        os.close(directory)
        raise HarnessSpoolError("unsafe_spool")
    return directory


def _decode(
    document: dict[str, object], brain_id: str
) -> tuple[str, str, Path, tuple[MemoryMessage, ...], _CaptureArguments]:
    if document.get("version") != _VERSION or document.get("brain_id") != brain_id:
        raise ValueError("destination_mismatch")
    messages = tuple(
        MemoryMessage(
            cast(str, item["role"]),
            cast(str, item["text"]),
            cast(int, item["ordinal"]),
            cast(bool, item["authored"]),
            cast(str | None, item["source_id"]),
        )
        for item in cast(list[dict[str, object]], document["messages"])
    )
    for message in messages:
        _screen(message.text)
    arguments: _CaptureArguments = {
        "timestamps": {
            cast(int, key): cast(str | None, value)
            for key, value in cast(list[list[object]], document["timestamps"])
        }
    }
    if document.get("local_only") is True:
        arguments["local_only"] = True
    if "selection" in document:
        arguments["selection"] = tuple(
            (cast(int, key[0]), cast(int, key[1]))
            for key in cast(list[list[object]], document["selection"])
        )
    return (
        cast(str, document["client"]),
        cast(str, document["session_id"]),
        Path(cast(str, document["project"])),
        messages,
        arguments,
    )


def _acknowledgement(value: object) -> dict[str, object]:
    """Keep only closed metadata from a workflow result; never source text."""
    if not isinstance(value, dict) or not isinstance(value.get("status"), str):
        return {"status": "unavailable", "error_code": "capture_unavailable"}
    result: dict[str, object] = {"status": value["status"]}
    for key in ("capture_ids", "delivery_ids"):
        identifiers = value.get(key, [])
        if isinstance(identifiers, list) and all(isinstance(i, str) for i in identifiers):
            result[key] = identifiers[:32]
    code = value.get("error_code")
    if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,64}", code):
        result["error_code"] = code
    return result

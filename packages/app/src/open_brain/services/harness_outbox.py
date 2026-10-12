"""POSIX owner-local capture custody; no scheduler, listener, or live-store startup.

Immutable canonical requests retain normalized source events. Files are written to
0600 temporary siblings, fsynced, linked atomically without replacing prior data,
then the containing directory is fsynced before returning custody metadata. Root
and child directories are 0700, opened through no-follow directory descriptors.
No source request is removed automatically. Durable receipts record completion.
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
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import NotRequired, TypedDict, cast
from uuid import uuid4

from open_brain_engine.core.access_contracts import derive_brain_id
from open_brain_engine.core.ids import portable_canonical_json_bytes

from .harness_continuity import (
    MAX_EVENT_BYTES,
    MAX_EVENT_MESSAGES,
    capture_batches,
)
from .harness_events import HarnessCaptureEvent, HarnessEventError, _screen
from .harness_memory import MAX_MESSAGES, MemoryMessage
from .harness_policy import _LOCAL_MARKING, HarnessPolicy, HarnessPolicyError
from .harness_workflow import HarnessWorkflow, _cloud_rule

_MAX_REQUEST_BYTES = 256 * 1024
_MAX_STATUS_FILES = 256
_MAX_GUARD_FILES = 256
_VERSION = "open-brain-harness-outbox.v1"
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_BRAIN = re.compile(r"brn_[a-z2-7]{26}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_REQUEST_NAME = re.compile(r"[a-f0-9]{64}\.json\Z")
_ERROR_CODES = frozenset(
    {
        "invalid_policy",
        "unsafe_policy",
        "source_excluded",
        "disabled",
        "policy_changed",
        "credential_signal",
        "capture_unavailable",
        "source_denied",
    }
)
_OUTBOX_CODES = frozenset(
    {
        "unsafe_outbox",
        "request_too_large",
        "outbox_changed",
        "outbox_conflict",
        "invalid_event",
        "invalid_request",
        "request_identity_mismatch",
        "destination_mismatch",
        "invalid_receipt",
        "invalid_acknowledgement",
        "outbox_unavailable",
        "capture_refused",
        "invalid_attempt",
        "lineage_unavailable",
        "lineage_conflict",
        "continuity_scan_limit",
    }
)


class _OutboxFailure(ValueError):
    pass


class _CaptureArguments(TypedDict):
    timestamps: Mapping[int, str | None]
    local_only: NotRequired[bool]
    selection: NotRequired[tuple[tuple[int, int], ...]]
    origin_sessions: NotRequired[Mapping[str, str]]
    origin_ordinals: NotRequired[Mapping[str, int]]


class HarnessOutbox:
    """Foreground custody API. Creation and credential screening precede persistence.

    The parent directory must exist. Every path component must be a real directory,
    not a symlink. A root is bound to one destination and cannot be retargeted.
    Storage and workflow failures return closed metadata, never exception bodies.
    POSIX filesystem calls/fsync have no wall-clock bound: this primitive cannot
    guarantee the two-second hook budget. Hosts must enforce process deadlines;
    an interrupted invocation must not be reported as a queued acknowledgement.
    """

    def __init__(self, root: Path, brain_id: str) -> None:
        if (
            not root.is_absolute()
            or ".." in root.parts
            or "\0" in str(root)
            or _BRAIN.fullmatch(brain_id) is None
        ):
            raise ValueError("invalid_outbox")
        self.root = root
        self.brain_id = brain_id

    def capture_guard(self, event: HarnessCaptureEvent) -> dict[str, object]:
        """Screen unrestricted external custody without writes or SDK startup.

        The embedding caller checks current source policy separately and invokes
        this before retaining source bytes. A refusal is not a durable restriction
        acknowledgement; use the protected metadata-only restriction operation to
        latch a new denial. Existing latches and lineage are read under continuity.
        """
        try:
            _decode_event(self._document(event), large_event=True)
            if event.origin_sessions or event.origin_ordinals:
                raise _OutboxFailure("invalid_event")
            if event.local_only or _LOCAL_MARKING.search(
                "\n".join(message.text for message in event.messages if message.authored)
            ):
                return self._metadata("refused", error_code="source_denied")
            try:
                with self._continuity(create=False) as continuity:
                    resolved, _ = self._resolve_origins(
                        event, continuity, persist_restrictions=False,
                    )
                if resolved.local_only:
                    return self._metadata("refused", error_code="source_denied")
            except FileNotFoundError:
                if self.root.exists() or self.root.is_symlink():
                    raise _OutboxFailure("invalid_outbox") from None
                if event.parent_session_id is not None:
                    raise _OutboxFailure("lineage_unavailable") from None
            return self._metadata("allowed")
        except HarnessEventError as error:
            return self._metadata("refused", error_code=error.code)
        except _OutboxFailure as error:
            return self._metadata("refused", error_code=str(error))
        except OSError as error:
            return self._io_failure(error, None)
        except ValueError, TypeError, AttributeError:
            return self._metadata("refused", error_code="invalid_event")

    def enqueue(self, event: HarnessCaptureEvent) -> dict[str, object]:
        """A queued result means the exact bounded event has durable local custody."""
        request_id: str | None = None
        try:
            # Screen the entire current event before dividing it into batches.
            # No cursor or tail can hide an earlier marking or credential.
            full = self._document(event)
            _decode_event(full, large_event=True)
            try:
                batches = capture_batches(event.messages)
            except ValueError:
                raise _OutboxFailure("request_too_large") from None
            with (
                self._directories(create=True) as (_, requests, _receipts, _attempts),
                self._continuity(create=True) as continuity,
            ):
                event, states = self._resolve_origins(event, continuity)
                # Mark the publication transaction pending before publishing
                # any source bytes. Readers fail closed across interrupted fsync.
                for state in states:
                    state["pending_custody"] = True
                    self._store_state(continuity, state)
                # Hermes restrictions are metadata-only, including restrictions
                # inherited from durable session or origin state. Refuse while
                # holding continuity and before publishing any body or replay.
                if event.client == "hermes" and event.local_only:
                    return self._metadata("refused", error_code="source_denied")
                documents: list[dict[str, object]] = []
                if event.local_only:
                    documents.extend(self._restricted_replays(requests, event))
                preserve_v1 = (
                    len(batches) == 1
                    and len(event.messages) <= MAX_MESSAGES
                    and sum(len(m.text.encode()) for m in event.messages) <= 128 * 1024
                )
                if preserve_v1:
                    documents.append(self._document(event))
                else:
                    for batch in batches:
                        ordinals = {m.ordinal for m in batch.messages}
                        documents.append(
                            self._document(
                                replace(
                                    event,
                                    messages=batch.messages,
                                    selection=batch.selection,
                                    timestamps={
                                        k: v for k, v in event.timestamps.items() if k in ordinals
                                    },
                                    origin_sessions={
                                        key: value
                                        for key, value in event.origin_sessions.items()
                                        if key
                                        in {m.source_id or str(m.ordinal) for m in batch.messages}
                                    },
                                    origin_ordinals={
                                        key: value
                                        for key, value in event.origin_ordinals.items()
                                        if key in {m.source_id for m in batch.messages}
                                    },
                                )
                            )
                        )
                request_ids: list[str] = []
                for document in documents:
                    _decode_event(document)
                    body = portable_canonical_json_bytes(document)
                    if len(body) > _MAX_REQUEST_BYTES:
                        raise _OutboxFailure("request_too_large")
                    request_id = sha256(body).hexdigest()
                    _publish(requests, request_id + ".json", body)
                    request_ids.append(request_id)
                self._store_origins(continuity, event)
                for state in states:
                    state["pending_custody"] = False
                    self._store_state(continuity, state)
            result = self._metadata("queued", request_ids[-1])
            if len(request_ids) > 1:
                result.update({"request_ids": request_ids[:16], "batch_count": len(request_ids)})
            return result
        except HarnessEventError as error:
            if error.code == "credential_signal":
                self.restrict_session(
                    event.client, event.session_id, event.project, reason="credential_signal"
                )
            return self._metadata("refused", request_id, "credential_signal")
        except _OutboxFailure as error:
            return self._metadata("refused", request_id, str(error))
        except OSError as error:
            return self._io_failure(error, request_id)
        except ValueError, TypeError, AttributeError:
            return self._metadata("refused", request_id, "invalid_event")

    def _document(self, event: HarnessCaptureEvent) -> dict[str, object]:
        for message in event.messages:
            # Also screen retained injected context; it must not smuggle credentials.
            _screen(message.text)
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
                for item in event.messages
            ],
            "timestamps": [[key, value] for key, value in sorted(event.timestamps.items())],
        }
        if event.local_only:
            document["local_only"] = event.local_only
        elif type(event.local_only) is not bool:
            raise _OutboxFailure("invalid_event")
        for key, value in (
            ("parent_session_id", event.parent_session_id),
            ("session_timestamp", event.session_timestamp),
            ("selection", None if event.selection is None else [list(k) for k in event.selection]),
            ("origin_sessions", dict(event.origin_sessions) or None),
            ("origin_ordinals", dict(event.origin_ordinals) or None),
        ):
            if value is not None:
                document[key] = value
        return document

    @contextmanager
    def _continuity(self, *, create: bool) -> Iterator[int]:
        with self._directories(create=create) as (root, _, _, _):
            directory = _open_child(root, "continuity", create=create)
            try:
                lock = os.open(
                    "lock",
                    os.O_RDWR | (os.O_CREAT if create else 0) | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory,
                )
                try:
                    _check(os.fstat(lock), directory=False)
                    # Hook invocations never block behind another lifecycle event.
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    yield directory
                finally:
                    os.close(lock)
            finally:
                os.close(directory)

    def _state_name(self, client: str, project: Path, session: str) -> str:
        return (
            "session-"
            + sha256(portable_canonical_json_bytes([client, str(project), session])).hexdigest()
            + ".json"
        )

    def _state(self, directory: int, client: str, project: Path, session: str) -> dict[str, object]:
        try:
            raw = _load(_read(directory, self._state_name(client, project, session)))
        except FileNotFoundError:
            return {
                "version": _VERSION,
                "brain_id": self.brain_id,
                "client": client,
                "project": str(project),
                "session_id": session,
                "local_only": False,
                "credential_blocked": False,
                "pending_custody": False,
            }
        if (
            not isinstance(raw, dict)
            or set(raw)
            != {
                "version",
                "brain_id",
                "client",
                "project",
                "session_id",
                "local_only",
                "credential_blocked",
                "pending_custody",
            }
            or any(
                raw.get(key) != value
                for key, value in (
                    ("version", _VERSION),
                    ("brain_id", self.brain_id),
                    ("client", client),
                    ("project", str(project)),
                    ("session_id", session),
                )
            )
            or any(
                type(raw[key]) is not bool
                for key in ("local_only", "credential_blocked", "pending_custody")
            )
        ):
            raise _OutboxFailure("invalid_request")
        return cast(dict[str, object], raw)

    def _store_state(self, directory: int, state: dict[str, object]) -> None:
        _publish(
            directory,
            self._state_name(
                cast(str, state["client"]),
                Path(cast(str, state["project"])),
                cast(str, state["session_id"]),
            ),
            portable_canonical_json_bytes(state),
            replace=True,
        )

    def _origin_name(self, event: HarnessCaptureEvent, session: str, key: str) -> str:
        return (
            "message-"
            + sha256(
                portable_canonical_json_bytes([event.client, str(event.project), session, key])
            ).hexdigest()
            + ".json"
        )

    def _origin(
        self, directory: int, event: HarnessCaptureEvent, session: str, message: MemoryMessage
    ) -> tuple[str, int] | None:
        if message.source_id is None:
            return None  # V1 ordinals remain valid local revision locators, not lineage evidence.
        key = message.source_id
        try:
            raw = _load(_read(directory, self._origin_name(event, session, key)))
        except FileNotFoundError:
            return None
        if (
            not isinstance(raw, dict)
            or set(raw) != {"source_id", "origin_session", "origin_ordinal", "sha256"}
            or raw["source_id"] != key
            or not isinstance(raw["origin_session"], str)
            or _IDENTIFIER.fullmatch(raw["origin_session"]) is None
            or type(raw["origin_ordinal"]) is not int
            or not 0 <= raw["origin_ordinal"] <= 2**53 - 1
        ):
            raise _OutboxFailure("invalid_request")
        digest = sha256((message.role + "\0" + message.text).encode()).hexdigest()
        if raw["sha256"] != digest:
            raise _OutboxFailure("lineage_conflict")
        return raw["origin_session"], raw["origin_ordinal"]

    def _resolve_origins(
        self, event: HarnessCaptureEvent, directory: int, *, persist_restrictions: bool = True,
    ) -> tuple[HarnessCaptureEvent, list[dict[str, object]]]:
        if event.origin_sessions or event.origin_ordinals:
            # Origin mappings are only produced by the local custody boundary.
            raise _OutboxFailure("invalid_event")
        current = self._state(directory, event.client, event.project, event.session_id)
        states = [current]
        marked = event.local_only or bool(
            _LOCAL_MARKING.search("\n".join(m.text for m in event.messages if m.authored))
        )
        # An unavailable parent cannot undo a current session's newly declared
        # restriction. Retain only its denial metadata before checking lineage.
        if marked and persist_restrictions:
            current["local_only"] = True
            current["pending_custody"] = True
            self._store_state(directory, current)
        parent = event.parent_session_id
        if parent is not None:
            if parent == event.session_id:
                raise _OutboxFailure("lineage_unavailable")
            try:
                _read(directory, self._state_name(event.client, event.project, parent))
            except FileNotFoundError:
                raise _OutboxFailure("lineage_unavailable") from None
            states.append(self._state(directory, event.client, event.project, parent))
        # Latch a marking before origin resolution can fail. Missing lineage
        # cannot leave previously released knowledge eligible for cloud reads.
        if marked and persist_restrictions:
            for state in states:
                state["local_only"] = True
                state["pending_custody"] = True
                self._store_state(directory, state)
        origins: dict[str, str] = {}
        origin_ordinals: dict[str, int] = {}
        needed = {m.ordinal for batch in capture_batches(event.messages) for m in batch.messages}
        for message in event.messages:
            if event.client != "hermes" and message.ordinal not in needed:
                continue
            origin = self._origin(directory, event, event.session_id, message)
            if origin is None and parent is not None:
                if message.source_id is None:
                    raise _OutboxFailure("lineage_unavailable")
                origin = self._origin(directory, event, parent, message)
                if origin is None:
                    timestamp = event.timestamps.get(message.ordinal)
                    try:
                        if timestamp is None or event.session_timestamp is None:
                            raise ValueError
                        created = datetime.fromisoformat(event.session_timestamp)
                        authored = datetime.fromisoformat(timestamp)
                        if created.tzinfo is None or authored.tzinfo is None or authored < created:
                            raise ValueError
                    except ValueError, TypeError:
                        raise _OutboxFailure("lineage_unavailable") from None
            if origin is not None:
                origin_session, origin_ordinal = origin
                key = message.source_id or str(message.ordinal)
                if origin_session != event.session_id:
                    origins[key] = origin_session
                    if origin_session not in {state["session_id"] for state in states}:
                        states.append(
                            self._state(directory, event.client, event.project, origin_session)
                        )
                if origin_session != event.session_id or origin_ordinal != message.ordinal:
                    origin_ordinals[key] = origin_ordinal
        authored_text = "\n".join(m.text for m in event.messages if m.authored)
        restricted = (
            event.local_only
            or bool(_LOCAL_MARKING.search(authored_text))
            or any(state["local_only"] or state["credential_blocked"] for state in states)
        )
        if restricted:
            for state in states:
                state["local_only"] = True
        return replace(
            event, local_only=restricted, origin_sessions=origins, origin_ordinals=origin_ordinals
        ), states

    def _store_origins(self, directory: int, event: HarnessCaptureEvent) -> None:
        needed = {m.ordinal for batch in capture_batches(event.messages) for m in batch.messages}
        for message in event.messages:
            if event.client != "hermes" and message.ordinal not in needed:
                continue
            if message.source_id is None:
                continue
            key = message.source_id
            document = {
                "source_id": key,
                "origin_session": event.origin_sessions.get(key, event.session_id),
                "origin_ordinal": event.origin_ordinals.get(key, message.ordinal),
                "sha256": sha256((message.role + "\0" + message.text).encode()).hexdigest(),
            }
            _publish(
                directory,
                self._origin_name(event, event.session_id, key),
                portable_canonical_json_bytes(document),
            )

    def _restricted_replays(
        self, requests: int, event: HarnessCaptureEvent
    ) -> list[dict[str, object]]:
        names = os.listdir(requests)
        if len(names) > 4096:
            raise _OutboxFailure("continuity_scan_limit")
        sessions = {event.session_id, *event.origin_sessions.values()}
        if event.parent_session_id is not None:
            sessions.add(event.parent_session_id)
        documents: list[dict[str, object]] = []
        for name in names:
            if _REQUEST_NAME.fullmatch(name) is None:
                raise _OutboxFailure("invalid_request")
            body = _read(requests, name)
            if sha256(body).hexdigest() != name[:-5]:
                raise _OutboxFailure("request_identity_mismatch")
            document = _load(body)
            previous = _decode_event(document)
            if (
                previous.client == event.client
                and previous.project == event.project
                and previous.session_id in sessions
                and not previous.local_only
            ):
                documents.append(self._document(replace(previous, local_only=True)))
        return documents

    def restrict_session(
        self, client: str, session_id: str, project: Path, *, reason: str
    ) -> dict[str, object]:
        """Latch a source denial using validated metadata, never source bytes."""
        if (
            not isinstance(client, str)
            or client not in {"codex", "claude-code", "pi", "hermes"}
            or not isinstance(session_id, str)
            or _IDENTIFIER.fullmatch(session_id) is None
            or not isinstance(project, Path)
            or not project.is_absolute()
            or reason not in {"credential_signal", "source_denied"}
        ):
            return self._metadata("refused", error_code="invalid_event")
        try:
            with self._continuity(create=True) as directory:
                state = self._state(directory, client, project, session_id)
                if reason == "credential_signal":
                    state["credential_blocked"] = True
                else:
                    state["pending_custody"] = True
                state["local_only"] = True
                self._store_state(directory, state)
            return self._metadata("refused", error_code=reason)
        except OSError as error:
            return self._io_failure(error, None)
        except _OutboxFailure as error:
            return self._metadata("refused", error_code=str(error))

    def drain(self, workflow: HarnessWorkflow, *, limit: int = 16) -> dict[str, object]:
        """Replay at most 16 pending requests under the workflow's current policy.

        Delivered source requests remain retained. Directory enumeration is not a
        wall-clock guarantee; hosts must bound foreground invocation separately.
        Downstream workflow delivery IDs supply at-least-once deduplication. A crash
        after remote/local commit but before receipt publication replays the request.
        Refused/unavailable requests are retained and may be retried after repair.
        """
        if type(limit) is not int or not 1 <= limit <= 16:
            return self._metadata("refused", error_code="invalid_limit")
        try:
            if derive_brain_id(workflow.tasks.profile.tenant_id) != self.brain_id:
                return self._metadata("refused", error_code="destination_mismatch")
        except ValueError, AttributeError:
            return self._metadata("refused", error_code="destination_mismatch")
        results: list[dict[str, object]] = []
        already_delivered = 0
        try:
            with self._directories(create=False) as (_, requests, receipts, attempts):
                names = set(os.listdir(requests))
                if any(
                    _REQUEST_NAME.fullmatch(name) is None or name not in names
                    for directory in (receipts, attempts)
                    for name in os.listdir(directory)
                ):
                    raise _OutboxFailure("orphan_receipt")
                # Completed requests do not consume the delivery cap. Give new
                # requests a turn before retries, then retry the oldest attempt.
                # Fixed hash order lets a refused prefix starve every later item.
                ordered: list[tuple[int, str, str]] = []
                for name in names:
                    if _REQUEST_NAME.fullmatch(name) is None:
                        raise _OutboxFailure("invalid_request")
                    try:
                        _read(receipts, name)
                    except FileNotFoundError:
                        try:
                            attempt = _validate_attempt(
                                _load(_read(attempts, name)), name[:-5], self.brain_id
                            )
                        except FileNotFoundError:
                            ordered.append((1, "", name))
                        else:
                            when = datetime.fromisoformat(cast(str, attempt["attempted_at"]))
                            ordered.append((2, when.isoformat(timespec="microseconds"), name))
                    else:
                        ordered.append((0, "", name))
                for _, _, name in sorted(ordered):
                    if len(results) >= limit:
                        break
                    request_id = name[:-5] if _REQUEST_NAME.fullmatch(name) else None
                    try:
                        if request_id is None:
                            raise _OutboxFailure("invalid_request")
                        body = _read(requests, name)
                        if sha256(body).hexdigest() != request_id:
                            raise _OutboxFailure("request_identity_mismatch")
                        document = _load(body)
                        event = _decode_event(document)
                        if cast(dict[str, object], document)["brain_id"] != self.brain_id:
                            raise _OutboxFailure("destination_mismatch")
                        with suppress(FileNotFoundError):
                            _validate_attempt(
                                _load(_read(attempts, name)), request_id, self.brain_id
                            )
                        try:
                            receipt_body = _read(receipts, name)
                        except FileNotFoundError:
                            receipt_body = None
                        if receipt_body is not None:
                            _validate_receipt(_load(receipt_body), request_id, self.brain_id)
                            # Reassert durability following an earlier failed fsync.
                            _publish(receipts, name, receipt_body)
                            already_delivered += 1
                            continue
                        result = self._deliver(workflow, event, request_id, requests, receipts)
                    except HarnessEventError:
                        result = self._metadata("refused", request_id, "credential_signal")
                    except _OutboxFailure as error:
                        result = self._metadata("refused", request_id, str(error))
                    except OSError as error:
                        result = self._io_failure(error, request_id)
                    except ValueError, TypeError, UnicodeError:
                        result = self._metadata("refused", request_id, "invalid_request")
                    if request_id is not None and result["status"] != "delivered":
                        try:
                            self._record_attempt(attempts, request_id, result)
                        except _OutboxFailure as error:
                            result = self._metadata("refused", request_id, str(error))
                        except OSError as error:
                            result = self._io_failure(error, request_id)
                    results.append(result)
        except _OutboxFailure as error:
            return self._metadata("refused", error_code=str(error))
        except FileNotFoundError:
            # An absent outbox is distinct from an existing, incomplete structure.
            if not self.root.exists():
                return {**self._metadata("success_empty"), "results": []}
            return self._metadata("refused", error_code="invalid_outbox")
        except OSError as error:
            return self._io_failure(error, None)
        except ValueError, TypeError, UnicodeError:
            return self._metadata("refused", error_code="invalid_request")
        statuses = {result["status"] for result in results}
        status = next(
            (
                value
                for value in ("refused", "unavailable", "pending", "delivered")
                if value in statuses
            ),
            "delivered" if already_delivered else "success_empty",
        )
        return {
            **self._metadata(status),
            "results": results,
            "already_delivered": already_delivered,
        }

    def deliver_request(
        self, workflow: HarnessWorkflow, request_id: str,
    ) -> dict[str, object]:
        """Deliver one retained immutable request; never sweep unrelated pending work."""
        try:
            if (
                not isinstance(request_id, str)
                or _REQUEST_NAME.fullmatch(request_id + ".json") is None
            ):
                raise _OutboxFailure("invalid_request")
            if derive_brain_id(workflow.tasks.profile.tenant_id) != self.brain_id:
                raise _OutboxFailure("destination_mismatch")
            name = request_id + ".json"
            with self._directories(create=False) as (_, requests, receipts, attempts):
                body = _read(requests, name)
                if sha256(body).hexdigest() != request_id:
                    raise _OutboxFailure("request_identity_mismatch")
                document = _load(body)
                event = _decode_event(document)
                if cast(dict[str, object], document)["brain_id"] != self.brain_id:
                    raise _OutboxFailure("destination_mismatch")
                with suppress(FileNotFoundError):
                    _validate_attempt(_load(_read(attempts, name)), request_id, self.brain_id)
                try:
                    receipt_body = _read(receipts, name)
                except FileNotFoundError:
                    result = self._deliver(workflow, event, request_id, requests, receipts)
                    if result["status"] != "delivered":
                        self._record_attempt(attempts, request_id, result)
                    return result
                receipt = _load(receipt_body)
                _validate_receipt(receipt, request_id, self.brain_id)
                _publish(receipts, name, receipt_body)
                return {
                    **self._metadata("delivered", request_id),
                    "acknowledgement": cast(dict[str, object], receipt)["acknowledgement"],
                }
        except _OutboxFailure as error:
            return self._metadata("refused", error_code=str(error))
        except HarnessEventError:
            return self._metadata("refused", error_code="credential_signal")
        except OSError as error:
            return self._io_failure(error, request_id)
        except ValueError, TypeError, UnicodeError:
            return self._metadata("refused", error_code="invalid_request")

    def status(self) -> dict[str, object]:
        """Inspect up to 256 source/receipt/attempt entries without a Brain/workflow.

        Counts are complete only when complete=True. Source text is read locally
        for integrity/credential checks but is never returned. Existing delivered
        receipts have directory durability reasserted without rewriting content.
        Pending counts include all undelivered requests; refused/unavailable counts
        also identify retained requests with a latest failed delivery attempt.
        """
        counts = {"pending": 0, "delivered": 0, "refused": 0, "unavailable": 0}
        source_names: set[str] = set()
        checked_attempts: set[str] = set()
        inspected = 0
        failure: dict[str, object] | None = None
        last_failure: dict[str, object] | None = None
        try:
            with self._directories(create=False) as (_, requests, receipts, attempts):
                with os.scandir(requests) as entries:
                    for entry in entries:
                        if inspected >= _MAX_STATUS_FILES:
                            failure = self._metadata("unavailable", error_code="status_scan_limit")
                            break
                        inspected += 1
                        source_names.add(entry.name)
                        try:
                            if _REQUEST_NAME.fullmatch(entry.name) is None:
                                raise _OutboxFailure("invalid_request")
                            body = _read(requests, entry.name)
                            request_id = entry.name[:-5]
                            if sha256(body).hexdigest() != request_id:
                                raise _OutboxFailure("request_identity_mismatch")
                            document = _load(body)
                            _decode_event(document)
                            if cast(dict[str, object], document)["brain_id"] != self.brain_id:
                                raise _OutboxFailure("destination_mismatch")
                            try:
                                receipt_body = _read(receipts, entry.name)
                            except FileNotFoundError:
                                counts["pending"] += 1
                                checked_attempts.add(entry.name)
                                try:
                                    attempt = _validate_attempt(
                                        _load(_read(attempts, entry.name)),
                                        request_id,
                                        self.brain_id,
                                    )
                                except FileNotFoundError:
                                    continue
                                attempt_status = cast(str, attempt["status"])
                                if attempt_status in {"refused", "unavailable"}:
                                    counts[attempt_status] += 1
                                    if last_failure is None or cast(
                                        str, attempt["attempted_at"]
                                    ) > cast(str, last_failure["attempted_at"]):
                                        last_failure = attempt
                                continue
                            _validate_receipt(_load(receipt_body), request_id, self.brain_id)
                            os.fsync(receipts)
                            counts["delivered"] += 1
                        except (
                            _OutboxFailure,
                            HarnessEventError,
                            ValueError,
                            TypeError,
                            UnicodeError,
                        ):
                            counts["refused"] += 1
                        except OSError as error:
                            counts[cast(str, self._io_failure(error, None)["status"])] += 1
                for directory in (receipts, attempts):
                    if failure is not None:
                        break
                    with os.scandir(directory) as entries:
                        for entry in entries:
                            if inspected >= _MAX_STATUS_FILES:
                                failure = self._metadata(
                                    "unavailable", error_code="status_scan_limit"
                                )
                                break
                            inspected += 1
                            if entry.name not in source_names:
                                counts["refused"] += 1
                            elif directory == attempts and entry.name not in checked_attempts:
                                try:
                                    _validate_attempt(
                                        _load(_read(attempts, entry.name)),
                                        entry.name[:-5],
                                        self.brain_id,
                                    )
                                except _OutboxFailure, ValueError, TypeError, UnicodeError:
                                    counts["refused"] += 1
                                except OSError as error:
                                    counts[cast(str, self._io_failure(error, None)["status"])] += 1
        except _OutboxFailure as error:
            failure = self._metadata("refused", error_code=str(error))
        except FileNotFoundError:
            if self.root.exists():
                failure = self._metadata("refused", error_code="invalid_outbox")
        except OSError as error:
            failure = self._io_failure(error, None)
        status = next(
            (name for name in ("refused", "unavailable", "pending", "delivered") if counts[name]),
            "success_empty",
        )
        return {
            **(failure if failure is not None else self._metadata(status)),
            "counts": counts,
            "error_count": counts["refused"] + counts["unavailable"] + int(failure is not None),
            "inspected": inspected,
            "complete": failure is None,
            "last_failure": last_failure,
        }

    def _record_attempt(self, directory: int, request_id: str, result: dict[str, object]) -> None:
        acknowledgement = result.get("acknowledgement")
        metadata = acknowledgement if isinstance(acknowledgement, dict) else result
        code = metadata.get("error_code")
        status = result["status"]
        attempt = {
            "version": _VERSION,
            "brain_id": self.brain_id,
            "request_id": request_id,
            "status": status,
            "attempted_at": datetime.now(UTC).isoformat(),
            "error_code": code
            if isinstance(code, str) and code in _ERROR_CODES | _OUTBOX_CODES
            else (None if status == "pending" else "capture_unavailable"),
            "capture_ids": metadata.get("capture_ids", []),
            "delivery_ids": metadata.get("delivery_ids", []),
        }
        _validate_attempt(attempt, request_id, self.brain_id)
        _publish(
            directory, request_id + ".json", portable_canonical_json_bytes(attempt), replace=True
        )

    def mark_unavailable(self) -> dict[str, object]:
        """Persist backend-startup failure for at most 16 pending requests.

        No workflow/Brain is opened. Source records remain immutable; prior
        partial acknowledgement IDs remain available in latest-attempt metadata.
        """
        results: list[dict[str, object]] = []
        try:
            with self._directories(create=False) as (_, requests, receipts, attempts):
                names = set(os.listdir(requests))
                if any(
                    _REQUEST_NAME.fullmatch(name) is None or name not in names
                    for directory in (receipts, attempts)
                    for name in os.listdir(directory)
                ):
                    raise _OutboxFailure("orphan_receipt")
                for name in sorted(names):
                    if len(results) >= 16:
                        break
                    if _REQUEST_NAME.fullmatch(name) is None:
                        raise _OutboxFailure("invalid_request")
                    request_id = name[:-5]
                    try:
                        receipt_body = _read(receipts, name)
                    except FileNotFoundError:
                        receipt_body = None
                    if receipt_body is not None:
                        _validate_receipt(_load(receipt_body), request_id, self.brain_id)
                        continue
                    body = _read(requests, name)
                    if sha256(body).hexdigest() != request_id:
                        raise _OutboxFailure("request_identity_mismatch")
                    document = _load(body)
                    _decode_event(document)
                    if cast(dict[str, object], document)["brain_id"] != self.brain_id:
                        raise _OutboxFailure("destination_mismatch")
                    previous: dict[str, object] = {}
                    with suppress(FileNotFoundError):
                        previous = _validate_attempt(
                            _load(_read(attempts, name)), request_id, self.brain_id
                        )
                    result = self._metadata("unavailable", request_id, "capture_unavailable")
                    result.update(
                        {key: previous.get(key, []) for key in ("capture_ids", "delivery_ids")}
                    )
                    self._record_attempt(attempts, request_id, result)
                    results.append(result)
        except _OutboxFailure as error:
            return self._metadata("refused", error_code=str(error))
        except FileNotFoundError:
            if self.root.exists():
                return self._metadata("refused", error_code="invalid_outbox")
        except HarnessEventError:
            return self._metadata("refused", error_code="credential_signal")
        except OSError as error:
            return self._io_failure(error, None)
        except ValueError, TypeError, UnicodeError:
            return self._metadata("refused", error_code="invalid_request")
        return {
            **self._metadata("unavailable", error_code="capture_unavailable"),
            "results": results,
            "updated": len(results),
        }

    def cloud_read_guard(self, policy: HarnessPolicy) -> dict[str, object]:
        """Deny cloud reads while an eligible source's local marking is pending.

        This is a conservative whole-cloud gate, not a new privacy grant. Current
        unknown/excluded/local-only source rules already deny their own content.
        Completed receipts are authoritative and skip source/attempt body reads.
        At most 256 pending events are examined; overflow or unsafe/incomplete
        state fails closed. Historical directory/receipt enumeration still scales
        with retained history. Call before search
        and again before releasing results. A delivered receipt only clears this
        pending gate; the engine must still deny the durably restricted head.
        """
        if not policy.enabled:
            return {"status": "refused", "error_code": "disabled"}
        try:
            with self._directories(create=False) as (_, requests, receipts, attempts):
                try:
                    with self._continuity(create=False) as continuity:
                        states = os.listdir(continuity)
                        if len(states) > 32768:
                            raise _OutboxFailure("continuity_scan_limit")
                        for name in states:
                            if not name.startswith("session-"):
                                continue
                            raw_state = _load(_read(continuity, name))
                            if (
                                not isinstance(raw_state, dict)
                                or not all(
                                    isinstance(raw_state.get(key), str)
                                    for key in ("client", "project", "session_id")
                                )
                                or self._state_name(
                                    cast(str, raw_state["client"]),
                                    Path(cast(str, raw_state["project"])),
                                    cast(str, raw_state["session_id"]),
                                )
                                != name
                            ):
                                raise _OutboxFailure("invalid_request")
                            state = self._state(
                                continuity,
                                cast(str, raw_state["client"]),
                                Path(cast(str, raw_state["project"])),
                                cast(str, raw_state["session_id"]),
                            )
                            if not state["credential_blocked"] and not state["pending_custody"]:
                                continue
                            project = Path(cast(str, state["project"]))
                            try:
                                decision = policy.classify(project)
                            except HarnessPolicyError as error:
                                if error.code == "source_excluded":
                                    continue
                                raise
                            rule = next(
                                (r for r in policy.sources if r.source_id == decision.source_id),
                                None,
                            )
                            if rule is not None and any(
                                _cloud_rule(policy, rule, provider)
                                for provider, _ in policy.providers
                            ):
                                return {
                                    "status": "refused",
                                    "error_code": (
                                        "credential_signal"
                                        if state["credential_blocked"]
                                        else "pending_source_restriction"
                                    ),
                                }
                except FileNotFoundError:
                    if (self.root / "continuity").exists():
                        raise _OutboxFailure("invalid_outbox") from None
                    # V1 custody remains readable without a continuity directory.
                names: dict[int, set[str]] = {requests: set(), receipts: set(), attempts: set()}
                for directory in names:
                    with os.scandir(directory) as entries:
                        for entry in entries:
                            if _REQUEST_NAME.fullmatch(entry.name) is None:
                                raise _OutboxFailure("invalid_request")
                            names[directory].add(entry.name)
                if not names[receipts].issubset(names[requests]) or not names[attempts].issubset(
                    names[requests]
                ):
                    raise _OutboxFailure("orphan_receipt")
                for name in names[receipts]:
                    _validate_receipt(_load(_read(receipts, name)), name[:-5], self.brain_id)
                if names[receipts]:
                    os.fsync(receipts)
                pending = names[requests] - names[receipts]
                if len(pending) > _MAX_GUARD_FILES:
                    return {"status": "unavailable", "error_code": "guard_scan_limit"}
                for name in pending:
                    request_id = name[:-5]
                    body = _read(requests, name)
                    if sha256(body).hexdigest() != request_id:
                        raise _OutboxFailure("request_identity_mismatch")
                    document = _load(body)
                    event = _decode_event(document)
                    if cast(dict[str, object], document)["brain_id"] != self.brain_id:
                        raise _OutboxFailure("destination_mismatch")
                    if name in names[attempts]:
                        _validate_attempt(_load(_read(attempts, name)), request_id, self.brain_id)
                    try:
                        unmarked = policy.classify(event.project)
                    except HarnessPolicyError as error:
                        if error.code == "source_excluded":
                            continue
                        raise
                    rule = next(
                        (rule for rule in policy.sources if rule.source_id == unmarked.source_id),
                        None,
                    )
                    if rule is None or not any(
                        _cloud_rule(policy, rule, provider) for provider, _ in policy.providers
                    ):
                        continue
                    authored = "\n".join(
                        message.text for message in event.messages if message.authored
                    )
                    if (
                        event.local_only
                        or policy.classify(event.project, authored_text=authored).local_only
                    ):
                        return {"status": "refused", "error_code": "pending_source_restriction"}
            return {"status": "allowed"}
        except FileNotFoundError:
            return (
                {"status": "allowed"}
                if not self.root.exists()
                else {"status": "refused", "error_code": "invalid_outbox"}
            )
        except HarnessPolicyError as error:
            return {"status": "refused", "error_code": error.code}
        except _OutboxFailure as error:
            return {"status": "refused", "error_code": str(error)}
        except HarnessEventError:
            return {"status": "refused", "error_code": "credential_signal"}
        except OSError as error:
            failure = self._io_failure(error, None)
            return {"status": failure["status"], "error_code": failure["error_code"]}
        except ValueError, TypeError, UnicodeError:
            return {"status": "refused", "error_code": "invalid_request"}

    def _deliver(
        self,
        workflow: HarnessWorkflow,
        event: HarnessCaptureEvent,
        request_id: str,
        requests: int,
        receipts: int,
    ) -> dict[str, object]:
        try:
            # Apply retained session restrictions even to older queued envelopes.
            try:
                with self._continuity(create=False) as continuity:
                    state = self._state(continuity, event.client, event.project, event.session_id)
                    if state["local_only"]:
                        event = replace(event, local_only=True)
            except FileNotFoundError:
                if (self.root / "continuity").exists():
                    raise _OutboxFailure("invalid_outbox") from None
                # Legacy V1 outboxes have no continuity directory.
            # Pending V1 envelopes may contain more than one selector page.
            # Publish bounded child custody first; the original completes only
            # after every child has a valid durable receipt. A crash only replays.
            batches = capture_batches(event.messages) if event.selection is None else ()
            if len(batches) > 1:
                acknowledgements: list[dict[str, object]] = []
                pending = False
                for batch in batches:
                    child = replace(event, messages=batch.messages, selection=batch.selection)
                    body = portable_canonical_json_bytes(self._document(child))
                    child_id = sha256(body).hexdigest()
                    _publish(requests, child_id + ".json", body)
                    try:
                        receipt = _load(_read(receipts, child_id + ".json"))
                        _validate_receipt(receipt, child_id, self.brain_id)
                    except FileNotFoundError:
                        pending = True
                    else:
                        acknowledgements.append(
                            cast(
                                dict[str, object],
                                cast(dict[str, object], receipt)["acknowledgement"],
                            )
                        )
                if pending:
                    return {
                        **self._metadata("pending", request_id),
                        "acknowledgement": {
                            "status": "queued",
                            "capture_ids": [],
                            "delivery_ids": [],
                        },
                    }
                raw: object = {
                    "status": "acknowledged",
                    "capture_ids": [
                        identifier
                        for ack in acknowledgements
                        for identifier in cast(list[str], ack["capture_ids"])
                    ][:32],
                    "delivery_ids": [
                        identifier
                        for ack in acknowledgements
                        for identifier in cast(list[str], ack["delivery_ids"])
                    ][:32],
                }
                return self._receipt(raw, request_id, receipts)
            arguments: _CaptureArguments = {"timestamps": event.timestamps}
            if event.local_only:
                arguments["local_only"] = True
            if event.selection is not None:
                arguments["selection"] = event.selection
            if event.origin_sessions:
                arguments["origin_sessions"] = event.origin_sessions
            if event.origin_ordinals:
                arguments["origin_ordinals"] = event.origin_ordinals
            raw = workflow.capture(
                event.client,
                event.session_id,
                event.project,
                event.messages,
                **arguments,
            )
        except Exception:
            # Callers must never log raw provider/storage exception contents.
            return self._metadata("unavailable", request_id, "capture_unavailable")
        return self._receipt(raw, request_id, receipts)

    def _receipt(self, raw: object, request_id: str, receipts: int) -> dict[str, object]:
        acknowledgement = _acknowledgement(raw)
        status = acknowledgement["status"]
        if status in {"acknowledged", "success_empty"}:
            receipt = {
                "version": _VERSION,
                "request_id": request_id,
                "brain_id": self.brain_id,
                "status": "delivered",
                "acknowledgement": acknowledgement,
            }
            _publish(receipts, request_id + ".json", portable_canonical_json_bytes(receipt))
            return {**self._metadata("delivered", request_id), "acknowledgement": acknowledgement}
        return {
            **self._metadata("pending" if status == "queued" else cast(str, status), request_id),
            "acknowledgement": acknowledgement,
        }

    def _metadata(
        self,
        status: str,
        request_id: str | None = None,
        error_code: str | None = None,
    ) -> dict[str, object]:
        result: dict[str, object] = {"status": status, "brain_id": self.brain_id}
        if request_id is not None:
            result["request_id"] = request_id
        if error_code is not None:
            result["error_code"] = error_code
        return result

    def _io_failure(self, error: OSError, request_id: str | None) -> dict[str, object]:
        unsafe = error.errno in {errno.ELOOP, errno.ENOTDIR, errno.EACCES, errno.EPERM}
        return self._metadata(
            "refused" if unsafe else "unavailable",
            request_id,
            "unsafe_outbox" if unsafe else "outbox_unavailable",
        )

    @contextmanager
    def _directories(self, *, create: bool) -> Iterator[tuple[int, int, int, int]]:
        opened: list[int] = []
        try:
            root = _open_root(self.root, create=create)
            opened.append(root)
            binding = portable_canonical_json_bytes(
                {"version": _VERSION, "brain_id": self.brain_id}
            )
            try:
                existing = _read(root, "destination.json")
            except FileNotFoundError:
                if not create:
                    raise
                _publish(root, "destination.json", binding)
                existing = binding
            if existing != binding:
                raise _OutboxFailure("destination_mismatch")
            requests = _open_child(root, "requests", create=create)
            opened.append(requests)
            receipts = _open_child(root, "receipts", create=create)
            opened.append(receipts)
            attempts = _open_child(root, "attempts", create=create)
            opened.append(attempts)
            yield root, requests, receipts, attempts
        finally:
            for descriptor in reversed(opened):
                os.close(descriptor)


def _check(metadata: os.stat_result, *, directory: bool) -> None:
    expected_mode = 0o700 if directory else 0o600
    if (
        not (stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode))
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != expected_mode
        or (not directory and metadata.st_nlink != 1)
    ):
        raise _OutboxFailure("unsafe_outbox")


def _open_root(path: Path, *, create: bool) -> int:
    descriptor = os.open(path.anchor, _DIRECTORY_FLAGS)
    try:
        for index, part in enumerate(path.parts[1:]):
            final = index == len(path.parts) - 2
            if final and create:
                with suppress(FileExistsError):
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                # Reassert directory-entry durability after an earlier failed fsync.
                os.fsync(descriptor)
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        _check(os.fstat(descriptor), directory=True)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_child(parent: int, name: str, *, create: bool) -> int:
    if create:
        with suppress(FileExistsError):
            os.mkdir(name, 0o700, dir_fd=parent)
        os.fsync(parent)
    descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    try:
        _check(os.fstat(descriptor), directory=True)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read(directory: int, name: str) -> bytes:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        _check(before, directory=False)
        if before.st_size > _MAX_REQUEST_BYTES:
            raise _OutboxFailure("request_too_large")
        body = stream.read(_MAX_REQUEST_BYTES + 1)
        after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise _OutboxFailure("outbox_changed")
    if len(body) > _MAX_REQUEST_BYTES:
        raise _OutboxFailure("request_too_large")
    return body


def _publish(directory: int, name: str, body: bytes, *, replace: bool = False) -> None:
    temporary = ".tmp-" + uuid4().hex
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            with suppress(FileNotFoundError):
                _read(directory, name)  # Refuse unsafe metadata before replacing it.
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        else:
            try:
                os.link(
                    temporary,
                    name,
                    src_dir_fd=directory,
                    dst_dir_fd=directory,
                    follow_symlinks=False,
                )
            except FileExistsError:
                if _read(directory, name) != body:
                    raise _OutboxFailure("outbox_conflict") from None
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory)
    os.fsync(directory)


def _decode_event(document: object, *, large_event: bool = False) -> HarnessCaptureEvent:
    required = {
        "version",
        "brain_id",
        "client",
        "session_id",
        "project",
        "messages",
        "timestamps",
    }
    optional = {
        "local_only",
        "parent_session_id",
        "session_timestamp",
        "origin_sessions",
        "origin_ordinals",
        "selection",
    }
    if (
        not isinstance(document, dict)
        or not required.issubset(document)
        or not set(document).issubset(required | optional)
    ):
        raise _OutboxFailure("invalid_request")
    local_only = document.get("local_only", False)
    if type(local_only) is not bool:
        raise _OutboxFailure("invalid_request")
    if (
        document["version"] != _VERSION
        or not isinstance(document["brain_id"], str)
        or _BRAIN.fullmatch(document["brain_id"]) is None
        or document["client"] not in {"codex", "claude-code", "pi", "hermes"}
        or not isinstance(document["session_id"], str)
        or _IDENTIFIER.fullmatch(document["session_id"]) is None
        or not isinstance(document["project"], str)
        or "\0" in document["project"]
        or not Path(document["project"]).is_absolute()
    ):
        raise _OutboxFailure("invalid_request")
    messages, timestamps = document["messages"], document["timestamps"]
    max_messages = MAX_EVENT_MESSAGES if large_event else MAX_MESSAGES
    if (
        not isinstance(messages, list)
        or len(messages) > max_messages
        or not isinstance(timestamps, list)
        or len(timestamps) > max_messages
    ):
        raise _OutboxFailure("invalid_request")
    parsed: list[MemoryMessage] = []
    seen: set[int] = set()
    for item in messages:
        source_id = item.get("source_id") if isinstance(item, dict) else None
        if (
            not isinstance(item, dict)
            or set(item)
            not in (
                {"role", "text", "ordinal", "authored"},
                {"role", "text", "ordinal", "authored", "source_id"},
            )
            or item["role"] not in {"user", "assistant"}
            or not isinstance(item["text"], str)
            or "\0" in item["text"]
            or type(item["ordinal"]) is not int
            or item["ordinal"] < 0
            or item["ordinal"] in seen
            or type(item["authored"]) is not bool
            or source_id is not None
            and (not isinstance(source_id, str) or _IDENTIFIER.fullmatch(source_id) is None)
        ):
            raise _OutboxFailure("invalid_request")
        if document["client"] == "hermes" and (
            source_id is None or not item["authored"]
            or source_id in {message.source_id for message in parsed}
            or item["ordinal"] > 2**53 - 1
        ):
            raise _OutboxFailure("invalid_request")
        seen.add(item["ordinal"])
        _screen(item["text"])
        parsed.append(
            MemoryMessage(
                item["role"],
                item["text"],
                item["ordinal"],
                item["authored"],
                source_id,
            )
        )
    parsed_times: dict[int, str | None] = {}
    for item in timestamps:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or type(item[0]) is not int
            or item[0] < 0
            or item[0] in parsed_times
            or (item[1] is not None and not isinstance(item[1], str))
        ):
            raise _OutboxFailure("invalid_request")
        parsed_times[item[0]] = item[1]
    if sum(len(m.text.encode()) for m in parsed) > (MAX_EVENT_BYTES if large_event else 128 * 1024):
        raise _OutboxFailure("request_too_large")
    parent = document.get("parent_session_id")
    session_time = document.get("session_timestamp")
    if parent is not None and (
        not isinstance(parent, str) or _IDENTIFIER.fullmatch(parent) is None
    ):
        raise _OutboxFailure("invalid_request")
    if session_time is not None:
        try:
            if (
                not isinstance(session_time, str)
                or datetime.fromisoformat(session_time).tzinfo is None
            ):
                raise ValueError
        except ValueError, TypeError:
            raise _OutboxFailure("invalid_request") from None
    origins = document.get("origin_sessions", {})
    if (
        not isinstance(origins, dict)
        or len(origins) > max_messages
        or any(
            not isinstance(key, str)
            or _IDENTIFIER.fullmatch(key) is None
            or not isinstance(value, str)
            or _IDENTIFIER.fullmatch(value) is None
            for key, value in origins.items()
        )
    ):
        raise _OutboxFailure("invalid_request")
    origin_ordinals = document.get("origin_ordinals", {})
    if (
        not isinstance(origin_ordinals, dict)
        or not set(origin_ordinals).issubset({m.source_id for m in parsed})
        or any(type(value) is not int or not 0 <= value <= 2**53 - 1
               for value in origin_ordinals.values())
    ):
        raise _OutboxFailure("invalid_request")
    selection_raw = document.get("selection")
    selection: tuple[tuple[int, int], ...] | None = None
    if selection_raw is not None:
        if (
            not isinstance(selection_raw, list)
            or len(selection_raw) > 32
            or any(
                not isinstance(k, list)
                or len(k) != 2
                or any(type(index) is not int or index < 0 for index in k)
                for k in selection_raw
            )
        ):
            raise _OutboxFailure("invalid_request")
        selection = tuple((key[0], key[1]) for key in selection_raw)
        if len(set(selection)) != len(selection) or any(key[0] not in seen for key in selection):
            raise _OutboxFailure("invalid_request")
    return HarnessCaptureEvent(
        cast(str, document["client"]),
        document["session_id"],
        Path(document["project"]),
        tuple(parsed),
        parsed_times,
        local_only,
        parent,
        session_time,
        origins,
        selection,
        origin_ordinals,
    )


def _load(body: bytes) -> object:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise _OutboxFailure("invalid_request")
            value[key] = item
        return value

    return json.loads(body, object_pairs_hook=unique)


def _acknowledgement(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or value.get("status") not in {
        "acknowledged",
        "success_empty",
        "queued",
        "refused",
        "unavailable",
    }:
        raise _OutboxFailure("invalid_acknowledgement")
    result: dict[str, object] = {"status": value["status"]}
    for key in ("capture_ids", "delivery_ids"):
        raw = value.get(key, [])
        if (
            not isinstance(raw, list)
            or len(raw) > 32
            or any(not isinstance(item, str) or _IDENTIFIER.fullmatch(item) is None for item in raw)
        ):
            raise _OutboxFailure("invalid_acknowledgement")
        result[key] = raw
    if value["status"] == "acknowledged" and (
        not result["capture_ids"] or not result["delivery_ids"]
    ):
        raise _OutboxFailure("invalid_acknowledgement")
    if value["status"] == "success_empty" and (result["capture_ids"] or result["delivery_ids"]):
        raise _OutboxFailure("invalid_acknowledgement")
    if value["status"] in {"refused", "unavailable"}:
        code = value.get("error_code")
        result["error_code"] = (
            code
            if isinstance(code, str) and code in _ERROR_CODES
            else ("capture_refused" if value["status"] == "refused" else "capture_unavailable")
        )
    return result


def _validate_receipt(value: object, request_id: str, brain_id: str) -> None:
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "version",
            "request_id",
            "brain_id",
            "status",
            "acknowledgement",
        }
        or value["version"] != _VERSION
        or value["request_id"] != request_id
        or value["brain_id"] != brain_id
        or value["status"] != "delivered"
    ):
        raise _OutboxFailure("invalid_receipt")
    acknowledgement = _acknowledgement(value["acknowledgement"])
    if acknowledgement["status"] not in {"acknowledged", "success_empty"}:
        raise _OutboxFailure("invalid_receipt")


def _validate_attempt(value: object, request_id: str, brain_id: str) -> dict[str, object]:
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "version",
            "brain_id",
            "request_id",
            "status",
            "attempted_at",
            "error_code",
            "capture_ids",
            "delivery_ids",
        }
        or value["version"] != _VERSION
        or value["request_id"] != request_id
        or value["brain_id"] != brain_id
        or value["status"] not in {"pending", "refused", "unavailable"}
        or not isinstance(value["attempted_at"], str)
    ):
        raise _OutboxFailure("invalid_attempt")
    try:
        if datetime.fromisoformat(value["attempted_at"]).tzinfo != UTC:
            raise ValueError
    except ValueError:
        raise _OutboxFailure("invalid_attempt") from None
    code = value["error_code"]
    if (
        code is not None
        and (not isinstance(code, str) or code not in _ERROR_CODES | _OUTBOX_CODES)
        or value["status"] != "pending"
        and code is None
    ):
        raise _OutboxFailure("invalid_attempt")
    _acknowledgement(
        {**value, "status": "queued" if value["status"] == "pending" else value["status"]}
    )
    return cast(dict[str, object], value)

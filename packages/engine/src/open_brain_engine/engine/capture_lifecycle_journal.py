"""Immutable lifecycle evidence with isolated and schema18 engine adapters.

Callers own the transaction and source admission. The isolated installer remains
closed to existing databases; production uses the explicitly fenced adapter.
"""

from __future__ import annotations

import base64
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from hashlib import sha256
from typing import cast

from .capture_lifecycle_contracts import (
    GroupIntent,
    LifecycleError,
    LifecycleRequest,
    canonical_bytes,
    decode_canonical,
    digest,
    validate_capture_id,
    validate_digest,
    validate_session_id,
)

JOURNAL_CONTRACT = "ob.capture-lifecycle-journal.v1"
MAX_SESSION_MEMBERS = 20_000
_ZERO = "0" * 64
_TERMINAL = frozenset({"committed", "halted"})

_TABLES = (
    """CREATE TABLE lifecycle_journal_identity (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 contract TEXT NOT NULL, brain_id TEXT NOT NULL)""",
    """CREATE TABLE lifecycle_journal_requests (
 operation_id TEXT PRIMARY KEY, request_bytes BLOB NOT NULL,
 request_sha256 TEXT NOT NULL UNIQUE)""",
    """CREATE TABLE lifecycle_journal_payloads (
 sha256 TEXT PRIMARY KEY, payload_bytes BLOB NOT NULL)""",
    """CREATE TABLE lifecycle_journal_events (
 sequence INTEGER PRIMARY KEY, operation_id TEXT NOT NULL
 REFERENCES lifecycle_journal_requests(operation_id),
 kind TEXT NOT NULL, payload_bytes BLOB NOT NULL,
 previous_sha256 TEXT NOT NULL, event_sha256 TEXT NOT NULL UNIQUE)""",
)
JOURNAL_SCHEMA = _TABLES + tuple(
    f"CREATE TRIGGER {table}_{verb.lower()} BEFORE {verb} ON {table} "
    "BEGIN SELECT RAISE(ABORT, 'lifecycle evidence is immutable'); END"
    for table in (
        "lifecycle_journal_identity",
        "lifecycle_journal_requests",
        "lifecycle_journal_payloads",
        "lifecycle_journal_events",
    )
    for verb in ("UPDATE", "DELETE")
)
JOURNAL_SCHEMA_SHA256 = digest(list(JOURNAL_SCHEMA))


def _shape(connection: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    return tuple(
        tuple(row)
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        )
    )


@lru_cache(maxsize=1)
def _expected_shape() -> tuple[tuple[object, ...], ...]:
    connection = sqlite3.connect(":memory:")
    try:
        for statement in JOURNAL_SCHEMA:
            connection.execute(statement)
        return _shape(connection)
    finally:
        connection.close()


@contextmanager
def _atomic(connection: sqlite3.Connection) -> Iterator[None]:
    if not connection.in_transaction:
        raise LifecycleError("local_operation_failed")
    connection.execute("SAVEPOINT capture_lifecycle_write")
    try:
        yield
    except BaseException:
        connection.execute("ROLLBACK TO capture_lifecycle_write")
        connection.execute("RELEASE capture_lifecycle_write")
        raise
    else:
        connection.execute("RELEASE capture_lifecycle_write")


def install_lifecycle_journal(connection: sqlite3.Connection, *, brain_id: str) -> None:
    """Install only into an empty, caller-owned transactional test/staging store."""
    # Reuse request identity validation; this does not create an accepted request.
    LifecycleRequest("00000000-0000-4000-8000-000000000001", "consumer_import", brain_id, 1, {})
    with _atomic(connection):
        if (
            connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone() is not None
            or connection.execute("PRAGMA user_version").fetchone()[0] != 0
            or connection.execute("PRAGMA application_id").fetchone()[0] != 0
        ):
            raise LifecycleError("local_operation_failed")
        for statement in JOURNAL_SCHEMA:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO lifecycle_journal_identity VALUES(1,?,?)", (JOURNAL_CONTRACT, brain_id)
        )


def _object(value: object, keys: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise LifecycleError("invalid_evidence")
    return cast(dict[str, object], value)


@dataclass
class _State:
    requests: dict[str, LifecycleRequest]
    blobs: dict[str, bytes]
    status: dict[str, str] = field(default_factory=dict)
    staged: dict[tuple[str, str], GroupIntent] = field(default_factory=dict)
    capture_bindings: dict[str, tuple[str, str]] = field(default_factory=dict)
    custodied: set[tuple[str, str, str]] = field(default_factory=set)
    revisions: dict[tuple[str, str], GroupIntent] = field(default_factory=dict)
    current: dict[str, str] = field(default_factory=dict)
    completed: set[tuple[str, str]] = field(default_factory=set)
    sessions: dict[str, str] = field(default_factory=dict)
    members: dict[str, list[str]] = field(default_factory=dict)
    session_versions: dict[str, int] = field(default_factory=dict)
    retracted_sessions: set[str] = field(default_factory=set)
    retracted_captures: set[str] = field(default_factory=set)
    barriers: set[str] = field(default_factory=set)
    last_event: dict[str, str] = field(default_factory=dict)
    generation: int = 0

    def request(self, operation_id: str) -> LifecycleRequest:
        try:
            return self.requests[operation_id]
        except KeyError:
            raise LifecycleError("operation_not_found") from None

    def intended_groups(self, operation_id: str) -> dict[str, GroupIntent]:
        request = self.request(operation_id)
        if request.kind != "consumer_import":
            raise LifecycleError("invalid_arguments")
        groups = request.value()["payload"]
        assert isinstance(groups, dict)
        values = groups.get("groups")
        if not isinstance(values, list) or not values:
            raise LifecycleError("invalid_arguments")
        parsed = [GroupIntent.from_value(value) for value in values]
        result = {item.group_key: item for item in parsed}
        if len(result) != len(parsed):
            raise LifecycleError("invalid_arguments")
        return result

    def admitted(self, capture_id: str) -> bool:
        binding = self.capture_bindings.get(capture_id)
        if binding is None:
            return True  # Existing source admission remains the caller's responsibility.
        revision = self.revisions.get(binding)
        return revision is not None and any(
            item.capture_id == capture_id for item in revision.segments
        )

    def permits(self, capture_id: str, *, history: bool) -> bool:
        binding = self.capture_bindings.get(capture_id)
        if not self.admitted(capture_id):
            return False
        if history:
            return True
        if capture_id in self.retracted_captures or any(
            capture_id in self.members[session] for session in self.retracted_sessions
        ):
            return False
        if binding is None:
            return True  # Existing source admission/authority is still required.
        group_key, revision = binding
        if self.current.get(group_key) != revision:
            return False
        return all(
            member.capture_id not in self.retracted_captures
            and not any(
                member.capture_id in self.members[session] for session in self.retracted_sessions
            )
            for member in self.revisions[binding].segments
        )

    def apply(self, operation_id: str, kind: str, payload: object) -> None:
        request = self.request(operation_id)
        if self.status.get(operation_id) in _TERMINAL:
            raise LifecycleError("invalid_evidence")
        if kind == "group_staged":
            data = _object(payload, {"manifest"})
            manifest = GroupIntent.from_value(data["manifest"])
            intended = self.intended_groups(operation_id).get(manifest.group_key)
            if intended != manifest or (operation_id, manifest.group_key) in self.staged:
                raise LifecycleError("invalid_evidence")
            if manifest.group_key in self.barriers:
                raise LifecycleError("retracted")
            if self.current.get(manifest.group_key) != manifest.expected_revision_sha256:
                raise LifecycleError("revision_changed")
            if (manifest.group_key, manifest.revision_sha256) in self.revisions or any(
                old.group_key == manifest.group_key
                and old.revision_sha256 == manifest.revision_sha256
                and self.status.get(old_operation) != "halted"
                for (old_operation, _), old in self.staged.items()
            ):
                raise LifecycleError("operation_conflict")
            for segment in manifest.segments:
                if segment.capture_id in self.capture_bindings:
                    raise LifecycleError("invalid_evidence")
            self.staged[operation_id, manifest.group_key] = manifest
            for segment in manifest.segments:
                self.capture_bindings[segment.capture_id] = (
                    manifest.group_key,
                    manifest.revision_sha256,
                )
            self.status[operation_id] = "materializing"
        elif kind == "segment_custodied":
            data = _object(payload, {"group_key", "capture_id"})
            group = validate_digest(data["group_key"])
            capture = validate_capture_id(data["capture_id"])
            custody_manifest = self.staged.get((operation_id, group))
            custody_segment = (
                next(
                    (item for item in custody_manifest.segments if item.capture_id == capture), None
                )
                if custody_manifest is not None
                else None
            )
            key = (operation_id, group, capture)
            if (
                custody_segment is None
                or key in self.custodied
                or (operation_id, group) in self.completed
            ):
                raise LifecycleError("invalid_evidence")
            body = self.blobs.get(custody_segment.body_sha256)
            envelope = self.blobs.get(custody_segment.request_sha256)
            if body is None or envelope is None:
                raise LifecycleError("invalid_evidence")
            decode_canonical(envelope)
            self.custodied.add(key)
        elif kind == "group_activated":
            data = _object(payload, {"group_key", "session_id"})
            group = validate_digest(data["group_key"])
            session = validate_session_id(data["session_id"])
            activation_manifest = self.staged.get((operation_id, group))
            if activation_manifest is None or (operation_id, group) in self.completed:
                raise LifecycleError("invalid_evidence")
            if group in self.barriers or session in self.retracted_sessions:
                raise LifecycleError("retracted")
            if self.current.get(group) != activation_manifest.expected_revision_sha256:
                raise LifecycleError("revision_changed")
            if not all(
                (operation_id, group, item.capture_id) in self.custodied
                for item in activation_manifest.segments
            ):
                raise LifecycleError("operation_pending")
            if operation_id in self.sessions and self.sessions[operation_id] != session:
                raise LifecycleError("invalid_evidence")
            if session in self.members and self.sessions.get(operation_id) != session:
                raise LifecycleError("invalid_evidence")
            members = self.members.setdefault(session, [])
            if len(members) + len(activation_manifest.segments) > MAX_SESSION_MEMBERS:
                raise LifecycleError("invalid_arguments")
            if any(
                item.capture_id in self.retracted_captures for item in activation_manifest.segments
            ):
                raise LifecycleError("retracted")
            self.sessions[operation_id] = session
            members.extend(item.capture_id for item in activation_manifest.segments)
            self.session_versions[session] = self.session_versions.get(session, 0) + 1
            self.revisions[group, activation_manifest.revision_sha256] = activation_manifest
            self.current[group] = activation_manifest.revision_sha256
            self.completed.add((operation_id, group))
            self.generation += 1
        elif kind == "operation_finished":
            _object(payload, set())
            if not all(
                (operation_id, group) in self.completed
                for group in self.intended_groups(operation_id)
            ):
                raise LifecycleError("operation_pending")
            self.status[operation_id] = "committed"
        elif kind == "operation_halted":
            _object(payload, set())
            if request.kind != "consumer_import":
                raise LifecycleError("invalid_evidence")
            self.status[operation_id] = "halted"
        elif kind == "capture_retracted":
            data = _object(payload, {"capture_id"})
            if request.kind != "capture_retract" or request.value()["payload"] != data:
                raise LifecycleError("invalid_evidence")
            capture = validate_capture_id(data["capture_id"])
            binding = self.capture_bindings.get(capture)
            if not self.admitted(capture):
                raise LifecycleError("operation_pending")
            self.retracted_captures.add(capture)
            if binding is not None:
                self.barriers.add(binding[0])
            self.generation += 1
            self.status[operation_id] = "committed"
        elif kind == "session_retracted":
            data = _object(payload, {"session_id", "membership_sha256", "membership_version"})
            if request.kind != "session_retract" or request.value()["payload"] != data:
                raise LifecycleError("invalid_evidence")
            session = validate_session_id(data["session_id"])
            if (
                session not in self.members
                or digest(self.members[session]) != data["membership_sha256"]
                or type(data["membership_version"]) is not int
                or self.session_versions[session] != data["membership_version"]
            ):
                raise LifecycleError("revision_changed")
            self.retracted_sessions.add(session)
            for capture in self.members[session]:
                self.barriers.add(self.capture_bindings[capture][0])
            self.generation += 1
            self.status[operation_id] = "committed"
        else:
            raise LifecycleError("invalid_evidence")


def _event_value(
    sequence: int, operation_id: str, kind: str, payload: object, previous: str
) -> dict[str, object]:
    return {
        "sequence": sequence,
        "operation_id": operation_id,
        "kind": kind,
        "payload": payload,
        "previous_sha256": previous,
    }


class LifecycleJournal:
    """Engine-internal evidence; a true predicate never substitutes for authorization."""

    def __init__(self, connection: sqlite3.Connection, *, brain_id: str, issuer_epoch: int) -> None:
        if not connection.in_transaction:
            raise LifecycleError("local_operation_failed")
        LifecycleRequest(
            "00000000-0000-4000-8000-000000000001", "consumer_import", brain_id, issuer_epoch, {}
        )
        identity = connection.execute("SELECT * FROM lifecycle_journal_identity").fetchall()
        if [tuple(row) for row in identity] != [(1, JOURNAL_CONTRACT, brain_id)] or _shape(
            connection
        ) != _expected_shape():
            raise LifecycleError("invalid_evidence")
        self.connection = connection
        self.brain_id = brain_id
        self.issuer_epoch = issuer_epoch
        self._embedded = False

    @classmethod
    def for_engine(
        cls, connection: sqlite3.Connection, *, brain_id: str, issuer_epoch: int
    ) -> LifecycleJournal:
        """Open the unchanged journal at exact supported engine coordinates."""
        if not connection.in_transaction:
            raise LifecycleError("local_operation_failed")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        floor = connection.execute("SELECT * FROM runtime_compatibility").fetchall()
        identity = connection.execute(
            "SELECT brain_id,issuer_epoch FROM brain_identity WHERE singleton=1"
        ).fetchone()
        journal_identity = connection.execute("SELECT * FROM lifecycle_journal_identity").fetchall()
        if (
            version not in {18, 19}
            or [tuple(row) for row in floor] != [(1, {18: 13, 19: 14}.get(version), version)]
            or identity is None
            or tuple(identity) != (brain_id, issuer_epoch)
            or [tuple(row) for row in journal_identity] != [(1, JOURNAL_CONTRACT, brain_id)]
        ):
            raise LifecycleError("invalid_evidence")
        journal = cls.__new__(cls)
        journal.connection = connection
        journal.brain_id = brain_id
        journal.issuer_epoch = issuer_epoch
        journal._embedded = True
        return journal

    def _shape_matches(self) -> bool:
        if not self._embedded:
            return _shape(self.connection) == _expected_shape()
        shape = tuple(
            tuple(row)
            for row in self.connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master "
                "WHERE tbl_name GLOB 'lifecycle_journal_*' ORDER BY type,name"
            )
        )
        return shape == _expected_shape()

    def _state(self) -> _State:
        if not self.connection.in_transaction:
            raise LifecycleError("local_operation_failed")
        if not self._shape_matches():
            raise LifecycleError("invalid_evidence")
        requests: dict[str, LifecycleRequest] = {}
        for operation, raw, request_hash in self.connection.execute(
            "SELECT operation_id,request_bytes,request_sha256 FROM lifecycle_journal_requests"
        ):
            request = LifecycleRequest.from_bytes(raw)
            if (
                request.operation_id != operation
                or request.request_sha256 != request_hash
                or request.brain_id != self.brain_id
            ):
                raise LifecycleError("invalid_evidence")
            requests[operation] = request
        blobs = dict(
            self.connection.execute("SELECT sha256,payload_bytes FROM lifecycle_journal_payloads")
        )
        if any(sha256(raw).hexdigest() != key for key, raw in blobs.items()):
            raise LifecycleError("invalid_evidence")
        state = _State(requests, blobs)
        previous = _ZERO
        for expected, row in enumerate(
            self.connection.execute(
                "SELECT sequence,operation_id,kind,payload_bytes,previous_sha256,event_sha256 "
                "FROM lifecycle_journal_events ORDER BY sequence"
            ),
            1,
        ):
            sequence, operation, kind, raw, linked, event_hash = row
            payload = decode_canonical(raw)
            value = _event_value(sequence, operation, kind, payload, linked)
            if sequence != expected or linked != previous or digest(value) != event_hash:
                raise LifecycleError("invalid_evidence")
            state.apply(operation, kind, payload)
            state.last_event[operation] = event_hash
            previous = event_hash
        referenced: set[str] = set()
        for operation, group, capture in state.custodied:
            segment = next(
                item
                for item in state.staged[operation, group].segments
                if item.capture_id == capture
            )
            referenced.update((segment.body_sha256, segment.request_sha256))
        if referenced != set(blobs):
            raise LifecycleError("invalid_evidence")
        return state

    def _write_request(self, state: _State, operation_id: str) -> LifecycleRequest:
        request = state.request(operation_id)
        if request.issuer_epoch != self.issuer_epoch:
            raise LifecycleError("revision_changed")
        if state.status.get(operation_id) in _TERMINAL:
            raise LifecycleError("operation_conflict")
        return request

    def _append(self, state: _State, operation_id: str, kind: str, payload: object) -> None:
        # Validate against complete retained history before recording a new transition.
        state.apply(operation_id, kind, payload)
        last = self.connection.execute(
            "SELECT sequence,event_sha256 FROM lifecycle_journal_events "
            "ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        sequence, previous = (1, _ZERO) if last is None else (last[0] + 1, last[1])
        value = _event_value(sequence, operation_id, kind, payload, previous)
        self.connection.execute(
            "INSERT INTO lifecycle_journal_events VALUES(?,?,?,?,?,?)",
            (sequence, operation_id, kind, canonical_bytes(payload), previous, digest(value)),
        )

    def reserve(self, request: LifecycleRequest) -> dict[str, object]:
        with _atomic(self.connection):
            state = self._state()
            if request.brain_id != self.brain_id:
                raise LifecycleError("revision_changed")
            previous = state.requests.get(request.operation_id)
            if previous is not None:
                if previous.canonical_bytes != request.canonical_bytes:
                    raise LifecycleError("operation_conflict")
                if state.status.get(request.operation_id) not in _TERMINAL:
                    self._write_request(state, request.operation_id)
                return self.status(request.operation_id)
            if request.issuer_epoch != self.issuer_epoch:
                raise LifecycleError("revision_changed")
            self.connection.execute(
                "INSERT INTO lifecycle_journal_requests VALUES(?,?,?)",
                (request.operation_id, request.canonical_bytes, request.request_sha256),
            )
            return self.status(request.operation_id)

    def stage_group(self, operation_id: str, manifest: GroupIntent) -> None:
        with _atomic(self.connection):
            state = self._state()
            self._write_request(state, operation_id)
            previous = state.staged.get((operation_id, manifest.group_key))
            if previous is not None:
                if previous != manifest:
                    raise LifecycleError("operation_conflict")
                return
            self._append(state, operation_id, "group_staged", {"manifest": manifest.value()})

    def custody_segment(
        self, operation_id: str, group_key: str, capture_id: str, *, body: bytes, envelope: bytes
    ) -> None:
        with _atomic(self.connection):
            state = self._state()
            self._write_request(state, operation_id)
            manifest = state.staged.get((operation_id, group_key))
            segment = (
                next((item for item in manifest.segments if item.capture_id == capture_id), None)
                if manifest is not None
                else None
            )
            if (
                segment is None
                or not isinstance(body, bytes)
                or not isinstance(envelope, bytes)
                or sha256(body).hexdigest() != segment.body_sha256
                or sha256(envelope).hexdigest() != segment.request_sha256
            ):
                raise LifecycleError("invalid_arguments")
            decode_canonical(envelope)
            if (operation_id, group_key, capture_id) in state.custodied:
                return
            for key, raw in ((segment.body_sha256, body), (segment.request_sha256, envelope)):
                self.connection.execute(
                    "INSERT OR IGNORE INTO lifecycle_journal_payloads VALUES(?,?)", (key, raw)
                )
                state.blobs[key] = raw
            self._append(
                state,
                operation_id,
                "segment_custodied",
                {
                    "group_key": group_key,
                    "capture_id": capture_id,
                },
            )

    def activate_group(self, operation_id: str, group_key: str, *, session_id: str) -> None:
        with _atomic(self.connection):
            state = self._state()
            self._write_request(state, operation_id)
            if (operation_id, group_key) in state.completed:
                if state.sessions.get(operation_id) != session_id:
                    raise LifecycleError("operation_conflict")
                return
            self._append(
                state,
                operation_id,
                "group_activated",
                {
                    "group_key": group_key,
                    "session_id": session_id,
                },
            )

    def finish(self, operation_id: str) -> dict[str, object]:
        with _atomic(self.connection):
            state = self._state()
            if state.status.get(operation_id) in _TERMINAL:
                return self.status(operation_id)
            self._write_request(state, operation_id)
            self._append(state, operation_id, "operation_finished", {})
            return self.status(operation_id)

    def halt(self, operation_id: str) -> dict[str, object]:
        with _atomic(self.connection):
            state = self._state()
            request = state.request(operation_id)
            if state.status.get(operation_id) in _TERMINAL:
                return self.status(operation_id)
            if request.kind != "consumer_import":
                raise LifecycleError("invalid_arguments")
            # Current admitted owner may halt unfinished predecessor-issuer work.
            self._append(state, operation_id, "operation_halted", {})
            return self.status(operation_id)

    def retract(self, request: LifecycleRequest) -> dict[str, object]:
        with _atomic(self.connection):
            result = self.reserve(request)
            if result["status"] in _TERMINAL:
                return result
            state = self._state()
            self._write_request(state, request.operation_id)
            kinds = {"capture_retract": "capture_retracted", "session_retract": "session_retracted"}
            if request.kind not in kinds:
                raise LifecycleError("invalid_arguments")
            self._append(
                state, request.operation_id, kinds[request.kind], request.value()["payload"]
            )
            return self.status(request.operation_id)

    def status(self, operation_id: str) -> dict[str, object]:
        state = self._state()
        request = state.request(operation_id)
        status = state.status.get(operation_id, "reserved")
        receipt = None
        if status in _TERMINAL:
            receipt = digest(
                {
                    "operation_id": operation_id,
                    "request_sha256": request.request_sha256,
                    "status": status,
                    "event_sha256": state.last_event[operation_id],
                }
            )
        return {
            "operation_id": operation_id,
            "status": status,
            "session_id": state.sessions.get(operation_id),
            "receipt_sha256": receipt,
        }

    def permits_capture(self, capture_id: str, *, history: bool = False) -> bool:
        """Additional staging/retraction predicate, never source existence/authority."""
        validate_capture_id(capture_id)
        return self._state().permits(capture_id, history=history)

    def session_snapshot(self, session_id: str) -> dict[str, object]:
        state = self._state()
        validate_session_id(session_id)
        if session_id not in state.members:
            raise LifecycleError("operation_not_found")
        return {
            "session_id": session_id,
            "membership_version": state.session_versions[session_id],
            "membership_sha256": digest(state.members[session_id]),
        }

    @property
    def eligibility_generation(self) -> int:
        return self._state().generation

    def export_evidence(self) -> bytes:
        """Preserve the bounded isolated component snapshot contract."""
        return canonical_bytes(self.export_evidence_value())

    def export_evidence_value(self) -> dict[str, object]:
        """Validated terminal records; Portable stores payloads as separate files."""
        state = self._state()
        if any(state.status.get(operation) not in _TERMINAL for operation in state.requests):
            raise LifecycleError("operation_pending")
        requests = [state.requests[key].value() for key in sorted(state.requests)]
        events = []
        for sequence, operation, kind, raw, previous, event_hash in self.connection.execute(
            "SELECT * FROM lifecycle_journal_events ORDER BY sequence"
        ):
            events.append(
                {
                    **_event_value(sequence, operation, kind, decode_canonical(raw), previous),
                    "event_sha256": event_hash,
                }
            )
        return {
            "contract": JOURNAL_CONTRACT,
            "schema_sha256": JOURNAL_SCHEMA_SHA256,
            "brain_id": self.brain_id,
            "requests": requests,
            "events": events,
            "payloads": {
                key: base64.b64encode(raw).decode("ascii")
                for key, raw in sorted(state.blobs.items())
            },
        }


def restore_lifecycle_journal(
    connection: sqlite3.Connection, evidence: bytes, *, brain_id: str, issuer_epoch: int
) -> LifecycleJournal:
    """Validate in a disposable database before touching the empty destination."""
    try:
        value = decode_canonical(evidence)
    except (ValueError, TypeError, RecursionError):
        raise LifecycleError("invalid_evidence") from None
    return restore_lifecycle_journal_value(
        connection, value, brain_id=brain_id, issuer_epoch=issuer_epoch
    )


def restore_lifecycle_journal_value(
    connection: sqlite3.Connection, evidence: object, *, brain_id: str, issuer_epoch: int
) -> LifecycleJournal:
    """Validate per-record contracts and replay, without a global payload-byte ceiling."""
    try:
        value = _object(
            evidence,
            {
                "contract",
                "schema_sha256",
                "brain_id",
                "requests",
                "events",
                "payloads",
            },
        )
        if (
            value["contract"] != JOURNAL_CONTRACT
            or value["schema_sha256"] != JOURNAL_SCHEMA_SHA256
            or value["brain_id"] != brain_id
        ):
            raise LifecycleError("invalid_evidence")
        requests, events, payloads = value["requests"], value["events"], value["payloads"]
        if (
            not isinstance(requests, list)
            or not isinstance(events, list)
            or not isinstance(payloads, dict)
        ):
            raise LifecycleError("invalid_evidence")
        staging = sqlite3.connect(":memory:")
        try:
            staging.execute("PRAGMA foreign_keys=ON")
            staging.execute("BEGIN IMMEDIATE")
            install_lifecycle_journal(staging, brain_id=brain_id)
            for item in requests:
                request = LifecycleRequest.from_bytes(canonical_bytes(item))
                if request.brain_id != brain_id:
                    raise LifecycleError("invalid_evidence")
                staging.execute(
                    "INSERT INTO lifecycle_journal_requests VALUES(?,?,?)",
                    (
                        request.operation_id,
                        request.canonical_bytes,
                        request.request_sha256,
                    ),
                )
            for key, encoded in payloads.items():
                validate_digest(key)
                if not isinstance(encoded, str):
                    raise LifecycleError("invalid_evidence")
                raw = base64.b64decode(encoded, validate=True)
                if (
                    base64.b64encode(raw).decode("ascii") != encoded
                    or sha256(raw).hexdigest() != key
                ):
                    raise LifecycleError("invalid_evidence")
                staging.execute("INSERT INTO lifecycle_journal_payloads VALUES(?,?)", (key, raw))
            for item in events:
                event = _object(
                    item,
                    {
                        "sequence",
                        "operation_id",
                        "kind",
                        "payload",
                        "previous_sha256",
                        "event_sha256",
                    },
                )
                if type(event["sequence"]) is not int or not isinstance(event["operation_id"], str):
                    raise LifecycleError("invalid_evidence")
                staging.execute(
                    "INSERT INTO lifecycle_journal_events VALUES(?,?,?,?,?,?)",
                    (
                        event["sequence"],
                        event["operation_id"],
                        event["kind"],
                        canonical_bytes(event["payload"]),
                        event["previous_sha256"],
                        event["event_sha256"],
                    ),
                )
            verified = LifecycleJournal(staging, brain_id=brain_id, issuer_epoch=issuer_epoch)
            if verified.export_evidence_value() != evidence:
                raise LifecycleError("invalid_evidence")
            # No destination write occurs until canonical and semantic validation pass.
            with _atomic(connection):
                install_lifecycle_journal(connection, brain_id=brain_id)
                for table in (
                    "lifecycle_journal_requests",
                    "lifecycle_journal_payloads",
                    "lifecycle_journal_events",
                ):
                    rows = staging.execute(f"SELECT * FROM {table}").fetchall()
                    if rows:
                        placeholders = ",".join("?" for _ in rows[0])
                        connection.executemany(f"INSERT INTO {table} VALUES({placeholders})", rows)
            return LifecycleJournal(connection, brain_id=brain_id, issuer_epoch=issuer_epoch)
        finally:
            staging.close()
    except ValueError, TypeError, KeyError, sqlite3.Error, RecursionError:
        raise LifecycleError("invalid_evidence") from None


def restore_engine_lifecycle_journal(
    connection: sqlite3.Connection,
    evidence: bytes | dict[str, object],
    *,
    brain_id: str,
    issuer_epoch: int,
) -> LifecycleJournal:
    """Replay validated evidence into an empty schema18 journal atomically."""
    staging = sqlite3.connect(":memory:")
    try:
        staging.execute("BEGIN IMMEDIATE")
        value = decode_canonical(evidence) if isinstance(evidence, bytes) else evidence
        restore_lifecycle_journal_value(
            staging, value, brain_id=brain_id, issuer_epoch=issuer_epoch
        )
        with _atomic(connection):
            journal = LifecycleJournal.for_engine(
                connection, brain_id=brain_id, issuer_epoch=issuer_epoch
            )
            for table in (
                "lifecycle_journal_requests",
                "lifecycle_journal_payloads",
                "lifecycle_journal_events",
            ):
                if connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None:
                    raise LifecycleError("invalid_evidence")
                rows = staging.execute(f"SELECT * FROM {table}").fetchall()
                if rows:
                    placeholders = ",".join("?" for _ in rows[0])
                    connection.executemany(f"INSERT INTO {table} VALUES({placeholders})", rows)
            if journal.export_evidence_value() != value:
                raise LifecycleError("invalid_evidence")
        return journal
    finally:
        staging.close()

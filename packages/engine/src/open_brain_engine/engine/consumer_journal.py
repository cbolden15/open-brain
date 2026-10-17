"""Successor shared replay for frozen owner events and typed consumer outcomes.

The v1 schema and bytes remain unchanged. Only the explicitly migrated identity
and exported contract advance. Consumer intent-object closure and admission are
validated by the engine/Portable caller; an intent digest is an external binding,
never a new journal payload or an authorization token.
"""

from __future__ import annotations

import base64
import sqlite3
from dataclasses import dataclass, field
from hashlib import sha256
from typing import cast

from .capture_lifecycle_contracts import (
    GroupIntent,
    LifecycleError,
    LifecycleRequest,
    canonical_bytes,
    decode_canonical,
    digest,
    validate_digest,
)
from .capture_lifecycle_journal import (
    JOURNAL_CONTRACT as PREDECESSOR_CONTRACT,
)
from .capture_lifecycle_journal import (
    JOURNAL_SCHEMA,
    JOURNAL_SCHEMA_SHA256,
    LifecycleJournal,
    _atomic,
    _event_value,
    _expected_shape,
    _object,
    _shape,
    _State,
    install_lifecycle_journal,
)

JOURNAL_CONTRACT_V2 = "ob.capture-lifecycle-journal.v2"
CONSUMER_INTAKE_CONTRACT = "ob.consumer-intake.v1"
_TERMINAL = frozenset({"committed", "halted"})
_PLANNED = frozenset({"new", "replace_current", "unchanged", "conflict", "retracted", "no_text"})
_ADMISSIONS = frozenset({"new", "replace_current"})
_REFUSALS = frozenset({"conflict", "retracted"})
_TABLE_NAMES = (
    "lifecycle_journal_requests",
    "lifecycle_journal_payloads",
    "lifecycle_journal_events",
)


@dataclass(frozen=True, slots=True)
class ConsumerItem:
    group_key: str
    revision_sha256: str | None
    planned_outcome: str
    reference_receipt_sha256: str | None

    def __post_init__(self) -> None:
        validate_digest(self.group_key)
        if self.revision_sha256 is not None:
            validate_digest(self.revision_sha256)
        if type(self.planned_outcome) is not str or self.planned_outcome not in _PLANNED:
            raise LifecycleError("invalid_evidence")
        if self.reference_receipt_sha256 is not None:
            validate_digest(self.reference_receipt_sha256)
        if (
            (self.planned_outcome == "no_text" and self.revision_sha256 is not None)
            or (
                self.planned_outcome in _ADMISSIONS | {"unchanged"} and self.revision_sha256 is None
            )
            or (
                (self.planned_outcome == "unchanged") != (self.reference_receipt_sha256 is not None)
            )
        ):
            raise LifecycleError("invalid_evidence")

    def value(self) -> dict[str, object]:
        return {
            "group_key": self.group_key,
            "revision_sha256": self.revision_sha256,
            "planned_outcome": self.planned_outcome,
            "reference_receipt_sha256": self.reference_receipt_sha256,
        }

    @classmethod
    def from_value(cls, value: object) -> ConsumerItem:
        data = _object(
            value,
            {
                "group_key",
                "revision_sha256",
                "planned_outcome",
                "reference_receipt_sha256",
            },
        )
        return cls(
            validate_digest(data["group_key"]),
            cast(str | None, data["revision_sha256"]),
            cast(str, data["planned_outcome"]),
            cast(str | None, data["reference_receipt_sha256"]),
        )


def consumer_items(request: LifecycleRequest) -> tuple[ConsumerItem, ...] | None:
    """Validate typed consumer requests; None selects unchanged predecessor replay."""
    payload = request.value()["payload"]
    assert isinstance(payload, dict)
    if "consumer" not in payload:
        return None
    if request.kind != "consumer_import":
        raise LifecycleError("invalid_evidence")
    _object(payload, {"groups", "consumer"})
    consumer = _object(payload["consumer"], {"contract", "intent_sha256", "items"})
    if consumer["contract"] != CONSUMER_INTAKE_CONTRACT:
        raise LifecycleError("invalid_evidence")
    validate_digest(consumer["intent_sha256"])
    values, groups = consumer["items"], payload["groups"]
    if (
        not isinstance(values, list)
        or not 1 <= len(values) <= 10_000
        or not isinstance(groups, list)
    ):
        raise LifecycleError("invalid_evidence")
    items = tuple(ConsumerItem.from_value(value) for value in values)
    keys = [item.group_key for item in items]
    if keys != sorted(set(keys)):
        raise LifecycleError("invalid_evidence")
    manifests = [GroupIntent.from_value(group) for group in groups]
    by_key = {manifest.group_key: manifest for manifest in manifests}
    admissions = {item.group_key: item for item in items if item.planned_outcome in _ADMISSIONS}
    if len(by_key) != len(manifests) or set(by_key) != set(admissions):
        raise LifecycleError("invalid_evidence")
    for key, manifest in by_key.items():
        item = admissions[key]
        if manifest.revision_sha256 != item.revision_sha256 or (
            (item.planned_outcome == "new") != (manifest.expected_revision_sha256 is None)
        ):
            raise LifecycleError("invalid_evidence")
    return items


def _activation_receipt(request: LifecycleRequest, manifest: GroupIntent, session: str) -> str:
    return digest(
        {
            "contract": "ob.consumer-conversation-activation.v1",
            "operation_id": request.operation_id,
            "request_sha256": request.request_sha256,
            "manifest": manifest.value(),
            "session_id": session,
        }
    )


def _outcome_receipt(request: LifecycleRequest, item: ConsumerItem, outcome: str) -> str:
    return digest(
        {
            "contract": "ob.consumer-conversation-outcome.v1",
            "operation_id": request.operation_id,
            "request_sha256": request.request_sha256,
            "item": item.value(),
            "outcome": outcome,
            "reference_receipt_sha256": item.reference_receipt_sha256,
        }
    )


@dataclass
class _ConsumerState(_State):
    outcomes: dict[tuple[str, str], tuple[str, str]] = field(default_factory=dict)
    activation_receipts: dict[tuple[str, str], str] = field(default_factory=dict)

    def intended_groups(self, operation_id: str) -> dict[str, GroupIntent]:
        request = self.request(operation_id)
        if consumer_items(request) is None:
            return super().intended_groups(operation_id)
        payload = request.value()["payload"]
        assert isinstance(payload, dict) and isinstance(payload["groups"], list)
        return {
            manifest.group_key: manifest
            for manifest in (GroupIntent.from_value(value) for value in payload["groups"])
        }

    def _item(self, operation_id: str, group_key: str) -> ConsumerItem:
        items = consumer_items(self.request(operation_id))
        item = next((item for item in items or () if item.group_key == group_key), None)
        if item is None:
            raise LifecycleError("invalid_evidence")
        return item

    def apply(self, operation_id: str, kind: str, payload: object) -> None:
        request = self.request(operation_id)
        items = consumer_items(request)
        if self.status.get(operation_id) in _TERMINAL:
            raise LifecycleError("invalid_evidence")
        if items is not None and kind in {"group_staged", "segment_custodied", "group_activated"}:
            if kind == "group_staged":
                data = _object(payload, {"manifest"})
                group = GroupIntent.from_value(data["manifest"]).group_key
            else:
                fields = (
                    {"group_key", "session_id"}
                    if kind == "group_activated"
                    else {
                        "group_key",
                        "capture_id",
                    }
                )
                group = validate_digest(_object(payload, fields)["group_key"])
            if (operation_id, group) in self.outcomes:
                raise LifecycleError("invalid_evidence")
        if kind == "consumer_outcome":
            data = _object(payload, {"group_key", "outcome", "receipt_sha256"})
            group = validate_digest(data["group_key"])
            item = self._item(operation_id, group)
            outcome = data["outcome"]
            if (
                type(outcome) is not str
                or (
                    outcome != item.planned_outcome
                    and not (item.planned_outcome in _ADMISSIONS and outcome in _REFUSALS)
                )
                or outcome not in {"unchanged", "conflict", "retracted", "no_text"}
            ):
                raise LifecycleError("invalid_evidence")
            if (operation_id, group) in self.outcomes or (operation_id, group) in self.completed:
                raise LifecycleError("invalid_evidence")
            if outcome == "unchanged":
                manifest = self.revisions.get((group, cast(str, item.revision_sha256)))
                if (
                    self.current.get(group) != item.revision_sha256
                    or manifest is None
                    or self.activation_receipts.get((group, manifest.revision_sha256))
                    != item.reference_receipt_sha256
                    or group in self.barriers
                    or not all(
                        self.permits(segment.capture_id, history=False)
                        for segment in manifest.segments
                    )
                ):
                    raise LifecycleError("revision_changed")
            if outcome == "retracted" and group not in self.barriers:
                raise LifecycleError("invalid_evidence")
            receipt = _outcome_receipt(request, item, outcome)
            if data["receipt_sha256"] != receipt:
                raise LifecycleError("invalid_evidence")
            self.outcomes[operation_id, group] = (outcome, receipt)
            return
        if kind == "operation_finished" and items is not None:
            _object(payload, set())
            if any((operation_id, item.group_key) not in self.outcomes for item in items):
                raise LifecycleError("operation_pending")
            if any(
                (operation_id, group) not in self.completed
                and self.outcomes[operation_id, group][0] not in _REFUSALS
                for group in self.intended_groups(operation_id)
            ):
                raise LifecycleError("operation_pending")
            self.status[operation_id] = "committed"
            return
        super().apply(operation_id, kind, payload)
        if kind == "group_activated":
            data = _object(payload, {"group_key", "session_id"})
            group, session = cast(str, data["group_key"]), cast(str, data["session_id"])
            manifest = self.staged[operation_id, group]
            receipt = _activation_receipt(request, manifest, session)
            self.activation_receipts[group, manifest.revision_sha256] = receipt
            if items is not None:
                self.outcomes[operation_id, group] = ("committed", receipt)
        elif kind == "operation_halted" and items is not None:
            for item in items:
                if (operation_id, item.group_key) not in self.outcomes:
                    self.outcomes[operation_id, item.group_key] = (
                        "halted",
                        _outcome_receipt(request, item, "halted"),
                    )


def migrate_lifecycle_journal_v2(connection: sqlite3.Connection, *, brain_id: str) -> None:
    """Promote identity under the caller's transaction, retaining all evidence bytes."""
    with _atomic(connection):
        LifecycleRequest(
            "00000000-0000-4000-8000-000000000001",
            "consumer_import",
            brain_id,
            1,
            {},
        )
        if [
            tuple(row) for row in connection.execute("SELECT * FROM lifecycle_journal_identity")
        ] != [(1, PREDECESSOR_CONTRACT, brain_id)]:
            raise LifecycleError("invalid_evidence")
        predecessor = LifecycleJournal.__new__(LifecycleJournal)
        predecessor.connection, predecessor.brain_id = connection, brain_id
        predecessor.issuer_epoch, predecessor._embedded = 1, True
        predecessor._state()
        trigger = next(
            statement
            for statement in JOURNAL_SCHEMA
            if statement.startswith("CREATE TRIGGER lifecycle_journal_identity_update ")
        )
        connection.execute("DROP TRIGGER lifecycle_journal_identity_update")
        connection.execute(
            "UPDATE lifecycle_journal_identity SET contract=? WHERE singleton=1",
            (JOURNAL_CONTRACT_V2,),
        )
        connection.execute(trigger)
        successor = LifecycleJournalV2.__new__(LifecycleJournalV2)
        successor.connection, successor.brain_id = connection, brain_id
        successor.issuer_epoch, successor._embedded = 1, True
        successor._state()


def install_lifecycle_journal_v2(connection: sqlite3.Connection, *, brain_id: str) -> None:
    """Install only into an empty transactional isolated store."""
    with _atomic(connection):
        install_lifecycle_journal(connection, brain_id=brain_id)
        migrate_lifecycle_journal_v2(connection, brain_id=brain_id)


class LifecycleJournalV2(LifecycleJournal):
    """Shared owner/consumer journal; callers still own admission and transactions."""

    def __init__(self, connection: sqlite3.Connection, *, brain_id: str, issuer_epoch: int) -> None:
        if not connection.in_transaction:
            raise LifecycleError("local_operation_failed")
        LifecycleRequest(
            "00000000-0000-4000-8000-000000000001",
            "consumer_import",
            brain_id,
            issuer_epoch,
            {},
        )
        if [
            tuple(row) for row in connection.execute("SELECT * FROM lifecycle_journal_identity")
        ] != [(1, JOURNAL_CONTRACT_V2, brain_id)] or _shape(connection) != _expected_shape():
            raise LifecycleError("invalid_evidence")
        self.connection, self.brain_id, self.issuer_epoch = connection, brain_id, issuer_epoch
        self._embedded = False

    @classmethod
    def for_engine(
        cls,
        connection: sqlite3.Connection,
        *,
        brain_id: str,
        issuer_epoch: int,
    ) -> LifecycleJournalV2:
        if not connection.in_transaction:
            raise LifecycleError("local_operation_failed")
        LifecycleRequest(
            "00000000-0000-4000-8000-000000000001",
            "consumer_import",
            brain_id,
            issuer_epoch,
            {},
        )
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        floor = [tuple(row) for row in connection.execute("SELECT * FROM runtime_compatibility")]
        identity = connection.execute(
            "SELECT brain_id,issuer_epoch FROM brain_identity WHERE singleton=1"
        ).fetchone()
        journal_identity = [
            tuple(row) for row in connection.execute("SELECT * FROM lifecycle_journal_identity")
        ]
        if (
            (version, floor) not in ((20, [(1, 15, 20)]), (21, [(1, 16, 21)]))
            or identity is None
            or tuple(identity) != (brain_id, issuer_epoch)
            or journal_identity != [(1, JOURNAL_CONTRACT_V2, brain_id)]
        ):
            raise LifecycleError("invalid_evidence")
        journal = cls.__new__(cls)
        journal.connection, journal.brain_id, journal.issuer_epoch = (
            connection,
            brain_id,
            issuer_epoch,
        )
        journal._embedded = True
        if not journal._shape_matches():
            raise LifecycleError("invalid_evidence")
        return journal

    def _state(self) -> _ConsumerState:
        if not self.connection.in_transaction:
            raise LifecycleError("local_operation_failed")
        if not self._shape_matches() or [
            tuple(row)
            for row in self.connection.execute("SELECT * FROM lifecycle_journal_identity")
        ] != [(1, JOURNAL_CONTRACT_V2, self.brain_id)]:
            raise LifecycleError("invalid_evidence")
        requests: dict[str, LifecycleRequest] = {}
        for operation, raw, request_hash in self.connection.execute(
            "SELECT operation_id,request_bytes,request_sha256 FROM lifecycle_journal_requests"
        ):
            request = LifecycleRequest.from_bytes(raw)
            if (
                request.operation_id != operation
                or request.request_sha256 != request_hash
                or (request.brain_id != self.brain_id)
            ):
                raise LifecycleError("invalid_evidence")
            consumer_items(request)
            requests[operation] = request
        blobs = dict(
            self.connection.execute("SELECT sha256,payload_bytes FROM lifecycle_journal_payloads")
        )
        if any(sha256(raw).hexdigest() != key for key, raw in blobs.items()):
            raise LifecycleError("invalid_evidence")
        state = _ConsumerState(requests, blobs)
        previous = "0" * 64
        for expected, row in enumerate(
            self.connection.execute(
                "SELECT sequence,operation_id,kind,payload_bytes,previous_sha256,event_sha256 "
                "FROM lifecycle_journal_events ORDER BY sequence"
            ),
            1,
        ):
            sequence, operation, kind, raw, linked, event_hash = row
            payload = decode_canonical(raw)
            if (
                sequence != expected
                or linked != previous
                or digest(_event_value(sequence, operation, kind, payload, linked)) != event_hash
            ):
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

    def reserve(self, request: LifecycleRequest) -> dict[str, object]:
        consumer_items(request)
        return super().reserve(request)

    def _assert_item_open(self, state: _ConsumerState, operation_id: str, group_key: str) -> None:
        outcome = state.outcomes.get((operation_id, group_key))
        if outcome is not None and outcome[0] != "committed":
            raise LifecycleError("operation_conflict")

    def stage_group(self, operation_id: str, manifest: GroupIntent) -> None:
        with _atomic(self.connection):
            state = self._state()
            if consumer_items(state.request(operation_id)) is not None:
                self._assert_item_open(state, operation_id, manifest.group_key)
                previous = state.staged.get((operation_id, manifest.group_key))
                if previous is not None:
                    if previous != manifest:
                        raise LifecycleError("operation_conflict")
                    return  # Reconciliation does not resume or reactivate retained evidence.
            super().stage_group(operation_id, manifest)

    def custody_segment(
        self,
        operation_id: str,
        group_key: str,
        capture_id: str,
        *,
        body: bytes,
        envelope: bytes,
    ) -> None:
        with _atomic(self.connection):
            state = self._state()
            if consumer_items(state.request(operation_id)) is not None:
                self._assert_item_open(state, operation_id, group_key)
                if (operation_id, group_key, capture_id) in state.custodied:
                    manifest = state.staged[operation_id, group_key]
                    segment = next(
                        item for item in manifest.segments if item.capture_id == capture_id
                    )
                    if (
                        not isinstance(body, bytes)
                        or not isinstance(envelope, bytes)
                        or state.blobs[segment.body_sha256] != body
                        or state.blobs[segment.request_sha256] != envelope
                    ):
                        raise LifecycleError("invalid_arguments")
                    return
            super().custody_segment(
                operation_id, group_key, capture_id, body=body, envelope=envelope
            )

    def activate_group(self, operation_id: str, group_key: str, *, session_id: str) -> None:
        with _atomic(self.connection):
            state = self._state()
            if consumer_items(state.request(operation_id)) is not None:
                self._assert_item_open(state, operation_id, group_key)
                if (operation_id, group_key) in state.completed:
                    if state.sessions.get(operation_id) != session_id:
                        raise LifecycleError("operation_conflict")
                    return
            super().activate_group(operation_id, group_key, session_id=session_id)

    def record_outcome(self, operation_id: str, group_key: str, outcome: str) -> dict[str, object]:
        """Retain an idempotent terminal result; engine admission proves source CAS conflicts."""
        if type(outcome) is not str or outcome not in {
            "unchanged",
            "conflict",
            "retracted",
            "no_text",
        }:
            raise LifecycleError("invalid_arguments")
        with _atomic(self.connection):
            state = self._state()
            item = state._item(operation_id, validate_digest(group_key))
            previous = state.outcomes.get((operation_id, group_key))
            if previous is not None:
                if previous[0] != outcome:
                    raise LifecycleError("operation_conflict")
                return self._outcome_value(state, operation_id, item)
            request = self._write_request(state, operation_id)
            payload = {
                "group_key": group_key,
                "outcome": outcome,
                "receipt_sha256": _outcome_receipt(request, item, outcome),
            }
            self._append(state, operation_id, "consumer_outcome", payload)
            return self._outcome_value(state, operation_id, item)

    @staticmethod
    def _outcome_value(
        state: _ConsumerState, operation_id: str, item: ConsumerItem
    ) -> dict[str, object]:
        outcome, receipt = state.outcomes[operation_id, item.group_key]
        return {
            "group_key": item.group_key,
            "revision_sha256": item.revision_sha256,
            "outcome": outcome,
            "receipt_sha256": receipt,
        }

    def conversation_outcomes(self, operation_id: str) -> list[dict[str, object]]:
        state = self._state()
        items = consumer_items(state.request(operation_id))
        if items is None:
            return []
        return [
            self._outcome_value(state, operation_id, item)
            for item in items
            if (operation_id, item.group_key) in state.outcomes
        ]

    def activation_receipt(self, group_key: str, revision_sha256: str) -> str | None:
        return self._state().activation_receipts.get(
            (
                validate_digest(group_key),
                validate_digest(revision_sha256),
            )
        )

    def export_evidence_value(self) -> dict[str, object]:
        value = super().export_evidence_value()
        value["contract"] = JOURNAL_CONTRACT_V2
        return value


def restore_lifecycle_journal_v2(
    connection: sqlite3.Connection,
    evidence: bytes,
    *,
    brain_id: str,
    issuer_epoch: int,
) -> LifecycleJournalV2:
    try:
        value = decode_canonical(evidence)
    except ValueError, TypeError, RecursionError:
        raise LifecycleError("invalid_evidence") from None
    return restore_lifecycle_journal_v2_value(
        connection,
        value,
        brain_id=brain_id,
        issuer_epoch=issuer_epoch,
    )


def _copy_evidence(staging: sqlite3.Connection, destination: sqlite3.Connection) -> None:
    for table in _TABLE_NAMES:
        rows = staging.execute(f"SELECT * FROM {table}").fetchall()
        if rows:
            placeholders = ",".join("?" for _ in rows[0])
            destination.executemany(f"INSERT INTO {table} VALUES({placeholders})", rows)


def restore_lifecycle_journal_v2_value(
    connection: sqlite3.Connection,
    evidence: object,
    *,
    brain_id: str,
    issuer_epoch: int,
) -> LifecycleJournalV2:
    """Replay and validate canonical reachability in isolation before destination writes."""
    try:
        value = _object(
            evidence, {"contract", "schema_sha256", "brain_id", "requests", "events", "payloads"}
        )
        if (
            value["contract"] != JOURNAL_CONTRACT_V2
            or value["schema_sha256"] != JOURNAL_SCHEMA_SHA256
            or (value["brain_id"] != brain_id)
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
            install_lifecycle_journal_v2(staging, brain_id=brain_id)
            for item in requests:
                request = LifecycleRequest.from_bytes(canonical_bytes(item))
                if request.brain_id != brain_id:
                    raise LifecycleError("invalid_evidence")
                consumer_items(request)
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
            verified = LifecycleJournalV2(staging, brain_id=brain_id, issuer_epoch=issuer_epoch)
            if verified.export_evidence_value() != evidence:
                raise LifecycleError("invalid_evidence")
            with _atomic(connection):
                install_lifecycle_journal_v2(connection, brain_id=brain_id)
                _copy_evidence(staging, connection)
            return LifecycleJournalV2(connection, brain_id=brain_id, issuer_epoch=issuer_epoch)
        finally:
            staging.close()
    except ValueError, TypeError, KeyError, sqlite3.Error, RecursionError:
        raise LifecycleError("invalid_evidence") from None


def restore_engine_lifecycle_journal_v2(
    connection: sqlite3.Connection,
    evidence: bytes | dict[str, object],
    *,
    brain_id: str,
    issuer_epoch: int,
) -> LifecycleJournalV2:
    staging = sqlite3.connect(":memory:")
    try:
        staging.execute("BEGIN IMMEDIATE")
        value = decode_canonical(evidence) if isinstance(evidence, bytes) else evidence
        restore_lifecycle_journal_v2_value(
            staging, value, brain_id=brain_id, issuer_epoch=issuer_epoch
        )
        with _atomic(connection):
            journal = LifecycleJournalV2.for_engine(
                connection, brain_id=brain_id, issuer_epoch=issuer_epoch
            )
            if any(
                connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None
                for table in _TABLE_NAMES
            ):
                raise LifecycleError("invalid_evidence")
            _copy_evidence(staging, connection)
            if journal.export_evidence_value() != value:
                raise LifecycleError("invalid_evidence")
        return journal
    finally:
        staging.close()

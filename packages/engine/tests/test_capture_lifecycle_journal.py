"""Independent behavioral evidence for the isolated journal, using synthetic data."""

from __future__ import annotations

import base64
import sqlite3
from collections.abc import Callable, Iterator
from hashlib import sha256
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.engine.capture_lifecycle_contracts import (
    GroupIntent,
    LifecycleError,
    LifecycleRequest,
    SegmentBinding,
    canonical_bytes,
    decode_canonical,
    digest,
)
from open_brain_engine.engine.capture_lifecycle_journal import (
    JOURNAL_SCHEMA_SHA256,
    LifecycleJournal,
    install_lifecycle_journal,
    restore_lifecycle_journal,
)

BRAIN = "brn_aaaaaaaaaaaaaaaaaaaaaaaaaa"
OTHER_BRAIN = "brn_aaaaaaaaaaaaaaaaaaaaaaaaba"
TABLES = (
    "lifecycle_journal_identity",
    "lifecycle_journal_requests",
    "lifecycle_journal_payloads",
    "lifecycle_journal_events",
)


def _uuid(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012x}"


def _capture(number: int) -> str:
    return "capture_" + _uuid(number)


def _session(number: int) -> str:
    return "capsess_" + _uuid(number)


def _body(number: int) -> bytes:
    return f"Synthetic capture {number}.".encode()


def _envelope(number: int) -> bytes:
    return canonical_bytes({"capture_id": _capture(number), "text": f"Synthetic capture {number}."})


def _group(
    number: int,
    captures: tuple[int, ...],
    *,
    revision: int = 1,
    expected: str | None = None,
) -> GroupIntent:
    return GroupIntent(
        digest({"group": number}),
        digest({"group": number, "revision": revision}),
        expected,
        tuple(
            SegmentBinding(
                _capture(n),
                sha256(_body(n)).hexdigest(),
                digest(
                    decode_canonical(
                        _envelope(n),
                    )
                ),
            )
            for n in captures
        ),
    )


def _import(number: int, *groups: GroupIntent, issuer: int = 1) -> LifecycleRequest:
    return LifecycleRequest(
        _uuid(number),
        "consumer_import",
        BRAIN,
        issuer,
        {"groups": [group.value() for group in groups]},
    )


def _snapshot(connection: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    return tuple(tuple(connection.execute(f"SELECT * FROM {table} ORDER BY 1")) for table in TABLES)


def _new_connection(path: str | Path = ":memory:") -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("BEGIN IMMEDIATE")
    return connection


@pytest.fixture
def db() -> Iterator[tuple[sqlite3.Connection, LifecycleJournal]]:
    connection = _new_connection()
    install_lifecycle_journal(connection, brain_id=BRAIN)
    yield connection, LifecycleJournal(connection, brain_id=BRAIN, issuer_epoch=1)
    connection.close()


def _custody(journal: LifecycleJournal, operation: int, group: GroupIntent) -> None:
    for segment in group.segments:
        number = int(segment.capture_id.rsplit("-", 1)[1], 16)
        journal.custody_segment(
            _uuid(operation),
            group.group_key,
            segment.capture_id,
            body=_body(number),
            envelope=_envelope(number),
        )


def _activate(
    journal: LifecycleJournal,
    operation: int,
    group: GroupIntent,
    *,
    session: int,
) -> None:
    journal.stage_group(_uuid(operation), group)
    _custody(journal, operation, group)
    journal.activate_group(_uuid(operation), group.group_key, session_id=_session(session))


def _complete(
    journal: LifecycleJournal,
    operation: int,
    group: GroupIntent,
    *,
    session: int,
) -> dict[str, object]:
    journal.reserve(_import(operation, group))
    _activate(journal, operation, group, session=session)
    return journal.finish(_uuid(operation))


def _capture_retraction(operation: int, capture: int, *, issuer: int = 1) -> LifecycleRequest:
    return LifecycleRequest(
        _uuid(operation),
        "capture_retract",
        BRAIN,
        issuer,
        {"capture_id": _capture(capture)},
    )


def _session_retraction(
    operation: int,
    snapshot: dict[str, object],
    *,
    issuer: int = 1,
) -> LifecycleRequest:
    return LifecycleRequest(_uuid(operation), "session_retract", BRAIN, issuer, snapshot)


def test_exact_replay_lost_ack_and_changed_intent_are_atomic(
    db: tuple[sqlite3.Connection, LifecycleJournal],
) -> None:
    connection, journal = db
    group = _group(1, (1, 2))
    request = _import(1, group)
    reserved = journal.reserve(request)
    assert reserved["status"] == "reserved"
    assert journal.reserve(request) == reserved
    journal.stage_group(request.operation_id, group)
    _custody(journal, 1, group)
    journal.activate_group(request.operation_id, group.group_key, session_id=_session(1))
    before_replay = _snapshot(connection)
    journal.stage_group(request.operation_id, group)
    _custody(journal, 1, group)
    journal.activate_group(request.operation_id, group.group_key, session_id=_session(1))
    assert _snapshot(connection) == before_replay
    finished = journal.finish(request.operation_id)
    assert finished["status"] == "committed"
    assert isinstance(finished["receipt_sha256"], str)
    committed = _snapshot(connection)
    assert journal.reserve(request) == finished
    assert journal.finish(request.operation_id) == finished
    assert journal.halt(request.operation_id) == finished
    with pytest.raises(LifecycleError, match="^operation_conflict$"):
        journal.reserve(_import(1, _group(1, (1, 2), revision=2)))
    with pytest.raises(LifecycleError, match="^operation_conflict$"):
        journal.reserve(_capture_retraction(1, 1))
    assert _snapshot(connection) == committed
    assert journal.session_snapshot(_session(1))["membership_version"] == 1
    assert journal.eligibility_generation == 1


def test_two_conversations_resume_after_commit_and_reopen(tmp_path: Path) -> None:
    path = tmp_path / "synthetic.sqlite"
    first, second = _group(1, (1, 2)), _group(2, (3,))
    connection = _new_connection(path)
    install_lifecycle_journal(connection, brain_id=BRAIN)
    journal = LifecycleJournal(connection, brain_id=BRAIN, issuer_epoch=1)
    journal.reserve(_import(1, first, second))
    _activate(journal, 1, first, session=1)
    journal.stage_group(_uuid(1), second)
    connection.commit()
    connection.close()
    connection = _new_connection(path)
    try:
        journal = LifecycleJournal(connection, brain_id=BRAIN, issuer_epoch=1)
        assert journal.permits_capture(_capture(1))
        assert not journal.permits_capture(_capture(3))
        assert not journal.permits_capture(_capture(3), history=True)
        _custody(journal, 1, second)
        journal.activate_group(_uuid(1), second.group_key, session_id=_session(1))
        assert journal.finish(_uuid(1))["status"] == "committed"
        assert journal.session_snapshot(_session(1)) == {
            "session_id": _session(1),
            "membership_version": 2,
            "membership_sha256": digest([_capture(1), _capture(2), _capture(3)]),
        }
        assert journal.eligibility_generation == 2
    finally:
        connection.close()


def test_pending_and_partial_custody_refuse_activation_without_sql_changes(
    db: tuple[sqlite3.Connection, LifecycleJournal],
) -> None:
    connection, journal = db
    group = _group(1, (1, 2))
    journal.reserve(_import(1, group))
    journal.stage_group(_uuid(1), group)
    assert not journal.permits_capture(_capture(1))
    assert not journal.permits_capture(_capture(1), history=True)
    journal.custody_segment(
        _uuid(1), group.group_key, _capture(1), body=_body(1), envelope=_envelope(1)
    )
    partial = _snapshot(connection)
    with pytest.raises(LifecycleError, match="^operation_pending$"):
        journal.activate_group(_uuid(1), group.group_key, session_id=_session(1))
    with pytest.raises(LifecycleError, match="^invalid_arguments$"):
        journal.custody_segment(
            _uuid(1), group.group_key, _capture(2), body=b"wrong", envelope=_envelope(2)
        )
    assert _snapshot(connection) == partial
    assert journal.eligibility_generation == 0
    assert not journal.permits_capture(_capture(1), history=True)


def test_competing_revision_cas_and_shorter_replacement(
    db: tuple[sqlite3.Connection, LifecycleJournal],
) -> None:
    connection, journal = db
    old = _group(1, (1, 2, 3))
    _complete(journal, 1, old, session=1)
    chosen = _group(1, (4,), revision=2, expected=old.revision_sha256)
    competing = _group(1, (5,), revision=3, expected=old.revision_sha256)
    journal.reserve(_import(2, chosen))
    journal.reserve(_import(3, competing))
    journal.stage_group(_uuid(2), chosen)
    journal.stage_group(_uuid(3), competing)
    _custody(journal, 2, chosen)
    _custody(journal, 3, competing)
    assert all(journal.permits_capture(_capture(n)) for n in (1, 2, 3))
    journal.activate_group(_uuid(2), chosen.group_key, session_id=_session(2))
    before = _snapshot(connection)
    with pytest.raises(LifecycleError, match="^revision_changed$"):
        journal.activate_group(_uuid(3), competing.group_key, session_id=_session(3))
    assert _snapshot(connection) == before
    assert journal.permits_capture(_capture(4))
    for n in (1, 2, 3):
        assert not journal.permits_capture(_capture(n))
        assert journal.permits_capture(_capture(n), history=True)
    assert not journal.permits_capture(_capture(5), history=True)


def test_capture_retraction_excludes_entire_group_but_retains_history_and_replay(
    db: tuple[sqlite3.Connection, LifecycleJournal],
) -> None:
    connection, journal = db
    group = _group(1, (1, 2))
    old_receipt = _complete(journal, 1, group, session=1)
    request = _capture_retraction(2, 1)
    receipt = journal.retract(request)
    before = _snapshot(connection)
    assert journal.retract(request) == receipt
    assert journal.reserve(_import(1, group)) == old_receipt
    assert _snapshot(connection) == before
    for n in (1, 2):
        assert not journal.permits_capture(_capture(n))
        assert journal.permits_capture(_capture(n), history=True)
    replacement = _group(1, (3,), revision=2, expected=group.revision_sha256)
    journal.reserve(_import(3, replacement))
    before = _snapshot(connection)
    with pytest.raises(LifecycleError, match="^retracted$"):
        journal.stage_group(_uuid(3), replacement)
    assert _snapshot(connection) == before
    assert journal.eligibility_generation == 2


def test_stale_session_snapshot_after_append_is_atomic_and_current_snapshot_retracts(
    db: tuple[sqlite3.Connection, LifecycleJournal],
) -> None:
    connection, journal = db
    first, second = _group(1, (1,)), _group(2, (2,))
    journal.reserve(_import(1, first, second))
    _activate(journal, 1, first, session=1)
    stale = journal.session_snapshot(_session(1))
    _activate(journal, 1, second, session=1)
    before = _snapshot(connection)
    with pytest.raises(LifecycleError, match="^revision_changed$"):
        journal.retract(_session_retraction(2, stale))
    assert _snapshot(connection) == before
    journal.retract(_session_retraction(2, journal.session_snapshot(_session(1))))
    assert all(not journal.permits_capture(_capture(n)) for n in (1, 2))
    assert all(journal.permits_capture(_capture(n), history=True) for n in (1, 2))


@pytest.mark.parametrize("target", ["capture", "session"])
def test_historical_member_retraction_preserves_newer_group_head(
    db: tuple[sqlite3.Connection, LifecycleJournal],
    target: str,
) -> None:
    _, journal = db
    old = _group(1, (1, 2))
    _complete(journal, 1, old, session=1)
    newer = _group(1, (3,), revision=2, expected=old.revision_sha256)
    _complete(journal, 2, newer, session=2)
    request = (
        _capture_retraction(3, 1)
        if target == "capture"
        else _session_retraction(3, journal.session_snapshot(_session(1)))
    )
    journal.retract(request)
    assert journal.permits_capture(_capture(3))
    assert not journal.permits_capture(_capture(1))
    assert journal.permits_capture(_capture(1), history=True)
    blocked = _group(1, (4,), revision=3, expected=newer.revision_sha256)
    journal.reserve(_import(4, blocked))
    with pytest.raises(LifecycleError, match="^retracted$"):
        journal.stage_group(_uuid(4), blocked)


def test_halt_is_terminal_idempotent_and_preserves_partial_success(
    db: tuple[sqlite3.Connection, LifecycleJournal],
) -> None:
    connection, journal = db
    first, second = _group(1, (1,)), _group(2, (2,))
    journal.reserve(_import(1, first, second))
    _activate(journal, 1, first, session=1)
    journal.stage_group(_uuid(1), second)
    _custody(journal, 1, second)
    with pytest.raises(LifecycleError, match="^operation_pending$"):
        journal.export_evidence()
    result = journal.halt(_uuid(1))
    before = _snapshot(connection)
    assert result["status"] == "halted"
    assert journal.halt(_uuid(1)) == result
    assert journal.finish(_uuid(1)) == result
    assert journal.reserve(_import(1, first, second)) == result
    with pytest.raises(LifecycleError, match="^operation_conflict$"):
        journal.activate_group(_uuid(1), second.group_key, session_id=_session(1))
    assert _snapshot(connection) == before
    assert journal.permits_capture(_capture(1))
    assert not journal.permits_capture(_capture(2), history=True)
    assert journal.export_evidence()


def test_new_issuer_cannot_resume_but_can_halt_and_read_old_receipts(
    db: tuple[sqlite3.Connection, LifecycleJournal],
) -> None:
    connection, original = db
    group = _group(1, (1,))
    original.reserve(_import(1, group))
    current = LifecycleJournal(connection, brain_id=BRAIN, issuer_epoch=2)
    before = _snapshot(connection)
    with pytest.raises(LifecycleError, match="^revision_changed$"):
        current.reserve(_import(1, group))
    with pytest.raises(LifecycleError, match="^revision_changed$"):
        current.stage_group(_uuid(1), group)
    assert _snapshot(connection) == before
    result = current.halt(_uuid(1))
    assert current.reserve(_import(1, group)) == result
    assert original.status(_uuid(1)) == result


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("verb", ["UPDATE", "DELETE"])
def test_immutable_evidence_sql_triggers(
    db: tuple[sqlite3.Connection, LifecycleJournal],
    table: str,
    verb: str,
) -> None:
    connection, journal = db
    _complete(journal, 1, _group(1, (1,)), session=1)
    before = _snapshot(connection)
    first_column = connection.execute(f"PRAGMA table_info({table})").fetchone()[1]
    sql = (
        f"UPDATE {table} SET {first_column}={first_column}"
        if verb == "UPDATE"
        else f"DELETE FROM {table}"
    )
    with pytest.raises(sqlite3.IntegrityError, match="lifecycle evidence is immutable"):
        connection.execute(sql)
    assert _snapshot(connection) == before


def test_process_close_rolls_back_uncommitted_custody_but_keeps_reservation(tmp_path: Path) -> None:
    path = tmp_path / "synthetic.sqlite"
    group = _group(1, (1,))
    connection = _new_connection(path)
    install_lifecycle_journal(connection, brain_id=BRAIN)
    journal = LifecycleJournal(connection, brain_id=BRAIN, issuer_epoch=1)
    journal.reserve(_import(1, group))
    connection.commit()
    connection.execute("BEGIN IMMEDIATE")
    journal.stage_group(_uuid(1), group)
    _custody(journal, 1, group)
    connection.close()  # Models process loss before the caller's commit.
    connection = _new_connection(path)
    try:
        journal = LifecycleJournal(connection, brain_id=BRAIN, issuer_epoch=1)
        assert journal.status(_uuid(1))["status"] == "reserved"
        assert connection.execute("SELECT count(*) FROM lifecycle_journal_payloads").fetchone() == (
            0,
        )
        _activate(journal, 1, group, session=1)
        assert journal.finish(_uuid(1))["status"] == "committed"
    finally:
        connection.close()


def test_restore_exact_evidence_and_tombstones(
    db: tuple[sqlite3.Connection, LifecycleJournal],
) -> None:
    _, journal = db
    group = _group(1, (1, 2))
    old_result = _complete(journal, 1, group, session=1)
    journal.retract(_session_retraction(2, journal.session_snapshot(_session(1))))
    evidence = journal.export_evidence()
    destination = _new_connection()
    try:
        restored = restore_lifecycle_journal(destination, evidence, brain_id=BRAIN, issuer_epoch=2)
        assert restored.export_evidence() == evidence
        assert restored.reserve(_import(1, group)) == old_result
        assert restored.eligibility_generation == journal.eligibility_generation
        assert not restored.permits_capture(_capture(1))
        assert not restored.permits_capture(_capture(2))
        assert restored.permits_capture(_capture(1), history=True)
        assert restored.session_snapshot(_session(1)) == journal.session_snapshot(_session(1))
    finally:
        destination.close()


def _rehash_events(value: dict[str, object]) -> None:
    previous = "0" * 64
    for number, item in enumerate(cast(list[dict[str, object]], value["events"]), 1):
        item["sequence"] = number
        item["previous_sha256"] = previous
        body = {key: val for key, val in item.items() if key != "event_sha256"}
        previous = digest(body)
        item["event_sha256"] = previous


@pytest.mark.parametrize(
    "corruption",
    [
        "schema",
        "brain",
        "event_hash",
        "payload_hash",
        "payload_encoding",
        "missing_custody",
        "request_order",
        "event_order",
        "impossible_activation",
        "wrong_kind",
        "duplicate_terminal",
        "unreferenced_payload",
        "unfinished",
    ],
)
def test_restore_refuses_corrupt_or_rehashed_impossible_evidence_before_destination_write(
    db: tuple[sqlite3.Connection, LifecycleJournal],
    corruption: str,
) -> None:
    _, journal = db
    group = _group(1, (1,))
    _complete(journal, 1, group, session=1)
    journal.retract(_capture_retraction(2, 1))
    value = cast(dict[str, object], decode_canonical(journal.export_evidence()))
    events = cast(list[dict[str, object]], value["events"])
    payloads = cast(dict[str, object], value["payloads"])
    if corruption == "schema":
        value["schema_sha256"] = "f" * 64
    elif corruption == "brain":
        value["brain_id"] = OTHER_BRAIN
    elif corruption == "event_hash":
        events[0]["event_sha256"] = "f" * 64
    elif corruption == "payload_hash":
        payloads[next(iter(payloads))] = base64.b64encode(b"wrong body").decode()
    elif corruption == "payload_encoding":
        payloads[next(iter(payloads))] = "***"
    elif corruption == "missing_custody":
        del payloads[group.segments[0].body_sha256]
    elif corruption == "request_order":
        cast(list[object], value["requests"]).reverse()
    elif corruption == "event_order":
        events.reverse()
    elif corruption == "impossible_activation":
        del events[1]  # Remove custody while preserving a perfectly valid hash chain.
        _rehash_events(value)
    elif corruption == "wrong_kind":
        events[0]["kind"] = "imaginary_transition"
        _rehash_events(value)
    elif corruption == "duplicate_terminal":
        events.append(dict(events[-1]))
        _rehash_events(value)
    elif corruption == "unreferenced_payload":
        payloads[sha256(b"extra").hexdigest()] = base64.b64encode(b"extra").decode()
    else:
        events[:] = [event for event in events if event["kind"] != "operation_finished"]
        _rehash_events(value)
    destination = _new_connection()
    try:
        with pytest.raises(LifecycleError, match="^invalid_evidence$"):
            restore_lifecycle_journal(
                destination, canonical_bytes(value), brain_id=BRAIN, issuer_epoch=2
            )
        assert destination.execute("SELECT name FROM sqlite_master").fetchall() == []
        assert destination.in_transaction
    finally:
        destination.close()


@pytest.mark.parametrize("existing", ["table", "view", "journal"])
def test_install_refuses_every_nonempty_database_without_changes(existing: str) -> None:
    connection = _new_connection()
    try:
        if existing == "table":
            connection.execute("CREATE TABLE production17(marker TEXT)")
            connection.execute("PRAGMA user_version=17")
        elif existing == "view":
            connection.execute("CREATE VIEW unrelated AS SELECT 1")
        else:
            install_lifecycle_journal(connection, brain_id=BRAIN)
        before = tuple(connection.iterdump())
        with pytest.raises(LifecycleError, match="^local_operation_failed$"):
            install_lifecycle_journal(connection, brain_id=BRAIN)
        assert tuple(connection.iterdump()) == before
    finally:
        connection.close()


def test_public_journal_operations_require_caller_transaction(
    db: tuple[sqlite3.Connection, LifecycleJournal],
) -> None:
    connection, journal = db
    group = _group(1, (1,))
    journal.reserve(_import(1, group))
    connection.commit()
    calls: tuple[Callable[[], object], ...] = (
        lambda: journal.status(_uuid(1)),
        lambda: journal.permits_capture(_capture(1)),
        lambda: journal.reserve(_import(1, group)),
        lambda: journal.eligibility_generation,
        lambda: journal.export_evidence(),
    )
    for call in calls:
        with pytest.raises(LifecycleError, match="^local_operation_failed$"):
            call()


def test_journal_opener_requires_caller_transaction(
    db: tuple[sqlite3.Connection, LifecycleJournal],
) -> None:
    connection, _ = db
    connection.commit()
    with pytest.raises(LifecycleError, match="^local_operation_failed$"):
        LifecycleJournal(connection, brain_id=BRAIN, issuer_epoch=1)


@pytest.mark.parametrize("metadata", ["user_version", "application_id"])
def test_install_refuses_existing_version_metadata_without_schema_objects(metadata: str) -> None:
    connection = _new_connection()
    try:
        connection.execute(f"PRAGMA {metadata}=17")
        with pytest.raises(LifecycleError, match="^local_operation_failed$"):
            install_lifecycle_journal(connection, brain_id=BRAIN)
        assert connection.execute(f"PRAGMA {metadata}").fetchone() == (17,)
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []
    finally:
        connection.close()


def test_halted_revision_can_be_reapproved_without_admitting_old_pending_capture(
    db: tuple[sqlite3.Connection, LifecycleJournal],
) -> None:
    connection, journal = db
    abandoned = _group(1, (1,))
    journal.reserve(_import(1, abandoned))
    journal.stage_group(_uuid(1), abandoned)
    _custody(journal, 1, abandoned)
    halted = journal.halt(_uuid(1))

    retried = _group(1, (2,))  # Same content revision, fresh engine capture identity.
    assert retried.revision_sha256 == abandoned.revision_sha256
    _complete(journal, 2, retried, session=2)
    assert journal.permits_capture(_capture(2))
    assert not journal.permits_capture(_capture(1))
    assert not journal.permits_capture(_capture(1), history=True)
    before = _snapshot(connection)
    with pytest.raises(LifecycleError, match="^operation_pending$"):
        journal.retract(_capture_retraction(3, 1))
    assert _snapshot(connection) == before
    assert journal.reserve(_import(1, abandoned)) == halted

    evidence = journal.export_evidence()
    restored_connection = _new_connection()
    try:
        restored = restore_lifecycle_journal(
            restored_connection, evidence, brain_id=BRAIN, issuer_epoch=2
        )
        assert restored.export_evidence() == evidence
        assert restored.permits_capture(_capture(2))
        assert not restored.permits_capture(_capture(1), history=True)
    finally:
        restored_connection.close()


def test_frozen_schema_and_restore_fixture_preserve_retractions_and_halted_custody() -> None:
    assert (
        JOURNAL_SCHEMA_SHA256 == "90ca8c4fe7205bc679e2cf1422bf523a6bb5565da242ad8505ad3b48f581657c"
    )
    evidence = (
        Path(__file__).parent / "fixtures" / "capture-lifecycle-journal-v1.json"
    ).read_bytes()
    assert sha256(evidence).hexdigest() == (
        "ac517ea3d556aeeb2bab4392a2463062a250e6e96c5f28d2c391427cb63e19b9"
    )
    connection = _new_connection()
    try:
        restored = restore_lifecycle_journal(connection, evidence, brain_id=BRAIN, issuer_epoch=2)
        assert restored.export_evidence() == evidence
        for capture in (1, 2):
            assert not restored.permits_capture(_capture(capture))
            assert restored.permits_capture(_capture(capture), history=True)
        assert not restored.permits_capture(_capture(3), history=True)
        assert restored.status(_uuid(3))["status"] == "halted"
        before = _snapshot(connection)
        assert restored.reserve(_import(1, _group(1, (1, 2))))["status"] == "committed"
        assert _snapshot(connection) == before
    finally:
        connection.close()

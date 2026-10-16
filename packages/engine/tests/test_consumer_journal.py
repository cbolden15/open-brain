"""Synthetic successor replay alongside the frozen predecessor journal corpus."""

from __future__ import annotations

import base64
import copy
import sqlite3
from collections.abc import Iterator
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
    digest,
)
from open_brain_engine.engine.capture_lifecycle_journal import (
    JOURNAL_SCHEMA,
    LifecycleJournal,
    install_lifecycle_journal,
    restore_lifecycle_journal,
)
from open_brain_engine.engine.consumer_journal import (
    CONSUMER_INTAKE_CONTRACT,
    JOURNAL_CONTRACT_V2,
    ConsumerItem,
    LifecycleJournalV2,
    consumer_items,
    install_lifecycle_journal_v2,
    migrate_lifecycle_journal_v2,
    restore_engine_lifecycle_journal_v2,
    restore_lifecycle_journal_v2,
    restore_lifecycle_journal_v2_value,
)

BRAIN = "brn_aaaaaaaaaaaaaaaaaaaaaaaaaa"
_TABLES = (
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
    return f"Synthetic consumer text {number}.".encode()


def _envelope(number: int) -> bytes:
    return canonical_bytes({"synthetic_capture": _capture(number), "text": _body(number).decode()})


def _group(
    number: int, capture: int, *, revision: int = 1, expected: str | None = None
) -> GroupIntent:
    return GroupIntent(
        digest(["synthetic-group", number]),
        digest(["synthetic-revision", number, revision]),
        expected,
        (
            SegmentBinding(
                _capture(capture),
                sha256(_body(capture)).hexdigest(),
                sha256(_envelope(capture)).hexdigest(),
            ),
        ),
    )


def _item(group: GroupIntent, outcome: str = "new", reference: str | None = None) -> ConsumerItem:
    return ConsumerItem(group.group_key, group.revision_sha256, outcome, reference)


def _request(
    number: int,
    groups: tuple[GroupIntent, ...],
    items: tuple[ConsumerItem, ...],
    *,
    issuer: int = 1,
) -> LifecycleRequest:
    return LifecycleRequest(
        _uuid(number),
        "consumer_import",
        BRAIN,
        issuer,
        {
            "groups": [group.value() for group in groups],
            "consumer": {
                "contract": CONSUMER_INTAKE_CONTRACT,
                "intent_sha256": digest(["intent", number]),
                "items": [item.value() for item in sorted(items, key=lambda item: item.group_key)],
            },
        },
    )


def _connection(path: str | Path = ":memory:") -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("BEGIN IMMEDIATE")
    return connection


def _snapshot(connection: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    return tuple(
        tuple(tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1"))
        for table in _TABLES
    )


@pytest.fixture
def db() -> Iterator[tuple[sqlite3.Connection, LifecycleJournalV2]]:
    connection = _connection()
    install_lifecycle_journal_v2(connection, brain_id=BRAIN)
    yield connection, LifecycleJournalV2(connection, brain_id=BRAIN, issuer_epoch=1)
    connection.close()


def _activate(journal: LifecycleJournal, op: int, group: GroupIntent) -> None:
    journal.stage_group(_uuid(op), group)
    for segment in group.segments:
        number = int(segment.capture_id.rsplit("-", 1)[1], 16)
        journal.custody_segment(
            _uuid(op),
            group.group_key,
            segment.capture_id,
            body=_body(number),
            envelope=_envelope(number),
        )
    journal.activate_group(_uuid(op), group.group_key, session_id=_session(op))


def _admit(journal: LifecycleJournalV2, op: int, group: GroupIntent) -> None:
    journal.reserve(_request(op, (group,), (_item(group),)))
    _activate(journal, op, group)
    journal.finish(_uuid(op))


def _retract(journal: LifecycleJournal, op: int, capture: int) -> None:
    journal.retract(
        LifecycleRequest(_uuid(op), "capture_retract", BRAIN, 1, {"capture_id": _capture(capture)})
    )


def _rehash_events(value: dict[str, object]) -> None:
    previous = "0" * 64
    for event in cast(list[dict[str, object]], value["events"]):
        event["previous_sha256"] = previous
        event["event_sha256"] = digest(
            {key: item for key, item in event.items() if key != "event_sha256"}
        )
        previous = cast(str, event["event_sha256"])


def test_legacy_golden_promotion_retains_every_evidence_byte() -> None:
    golden = (Path(__file__).parent / "fixtures" / "capture-lifecycle-journal-v1.json").read_bytes()
    connection = _connection()
    owner = restore_lifecycle_journal(connection, golden, brain_id=BRAIN, issuer_epoch=1)
    before = _snapshot(connection)
    before_value = owner.export_evidence_value()
    migrate_lifecycle_journal_v2(connection, brain_id=BRAIN)
    journal = LifecycleJournalV2(connection, brain_id=BRAIN, issuer_epoch=1)
    assert _snapshot(connection) == before
    assert journal.export_evidence_value() == {**before_value, "contract": JOURNAL_CONTRACT_V2}
    for statement in JOURNAL_SCHEMA:
        if statement.startswith("CREATE TRIGGER"):
            name = statement.split()[2]
            assert (
                connection.execute(
                    "SELECT sql FROM sqlite_master WHERE name=?", (name,)
                ).fetchone()[0]
                == statement
            )
    with pytest.raises(LifecycleError):
        LifecycleJournal(connection, brain_id=BRAIN, issuer_epoch=1)
    destination = _connection()
    with pytest.raises(LifecycleError):
        restore_lifecycle_journal(
            destination, journal.export_evidence(), brain_id=BRAIN, issuer_epoch=1
        )
    assert destination.execute("SELECT name FROM sqlite_master").fetchall() == []
    connection.close()
    destination.close()


def test_mixed_owner_consumer_unchanged_and_no_text_round_trip(
    db: tuple[sqlite3.Connection, LifecycleJournalV2],
) -> None:
    connection, journal = db
    owner_group, consumer_group = _group(1, 1), _group(2, 2)
    owner_request = LifecycleRequest(
        _uuid(1), "consumer_import", BRAIN, 1, {"groups": [owner_group.value()]}
    )
    assert consumer_items(owner_request) is None
    journal.reserve(owner_request)
    _activate(journal, 1, owner_group)
    journal.finish(_uuid(1))
    _admit(journal, 2, consumer_group)
    original_receipt = journal.activation_receipt(
        owner_group.group_key, owner_group.revision_sha256
    )
    assert original_receipt is not None
    no_text = ConsumerItem(digest(["no-text"]), None, "no_text", None)
    unchanged = _item(owner_group, "unchanged", original_receipt)
    request = _request(3, (), (unchanged, no_text))
    journal.reserve(request)
    before = _snapshot(connection)
    with pytest.raises(LifecycleError, match="operation_pending"):
        journal.finish(request.operation_id)
    assert _snapshot(connection) == before
    no_text_receipt = journal.record_outcome(request.operation_id, no_text.group_key, "no_text")
    unchanged_receipt = journal.record_outcome(
        request.operation_id, owner_group.group_key, "unchanged"
    )
    assert unchanged_receipt["receipt_sha256"] != original_receipt
    assert journal.finish(request.operation_id)["session_id"] is None
    assert (
        journal.record_outcome(request.operation_id, no_text.group_key, "no_text")
        == no_text_receipt
    )
    assert journal.reserve(request)["status"] == "committed"
    assert journal.session_snapshot(_session(1))["membership_version"] == 1
    evidence = journal.export_evidence()
    restored_connection = _connection()
    restored = restore_lifecycle_journal_v2(
        restored_connection, evidence, brain_id=BRAIN, issuer_epoch=2
    )
    assert restored.export_evidence() == evidence
    assert restored.conversation_outcomes(_uuid(3)) == journal.conversation_outcomes(_uuid(3))
    assert (
        restored.activation_receipt(owner_group.group_key, owner_group.revision_sha256)
        == original_receipt
    )
    assert restored.permits_capture(_capture(1))
    restored_connection.close()


def test_restart_and_halt_derive_remaining_outcomes_without_membership(tmp_path: Path) -> None:
    path = tmp_path / "consumer.sqlite"
    connection = _connection(path)
    install_lifecycle_journal_v2(connection, brain_id=BRAIN)
    journal = LifecycleJournalV2(connection, brain_id=BRAIN, issuer_epoch=1)
    first, second = _group(1, 1), _group(2, 2)
    request = _request(1, (first, second), (_item(first), _item(second)))
    journal.reserve(request)
    _activate(journal, 1, first)
    journal.stage_group(request.operation_id, second)
    journal.custody_segment(
        request.operation_id, second.group_key, _capture(2), body=_body(2), envelope=_envelope(2)
    )
    connection.commit()
    connection.close()
    connection = _connection(path)
    journal = LifecycleJournalV2(connection, brain_id=BRAIN, issuer_epoch=2)
    before = _snapshot(connection)
    with pytest.raises(LifecycleError, match="revision_changed"):
        journal.reserve(request)
    with pytest.raises(LifecycleError, match="revision_changed"):
        journal.activate_group(request.operation_id, second.group_key, session_id=_session(1))
    assert _snapshot(connection) == before
    halted = journal.halt(request.operation_id)
    assert halted["status"] == "halted"
    assert journal.halt(request.operation_id) == halted
    assert {row["outcome"] for row in journal.conversation_outcomes(request.operation_id)} == {
        "committed",
        "halted",
    }
    assert journal.session_snapshot(_session(1))["membership_sha256"] == digest([_capture(1)])
    assert not journal.permits_capture(_capture(2), history=True)
    restored_connection = _connection()
    restored = restore_lifecycle_journal_v2(
        restored_connection, journal.export_evidence(), brain_id=BRAIN, issuer_epoch=2
    )
    assert restored.conversation_outcomes(request.operation_id) == journal.conversation_outcomes(
        request.operation_id
    )
    connection.close()
    restored_connection.close()


@pytest.mark.parametrize("retraction", ["capture", "session"])
def test_retraction_barriers_refuse_unchanged_and_allow_recorded_refusal(
    db: tuple[sqlite3.Connection, LifecycleJournalV2],
    retraction: str,
) -> None:
    _, journal = db
    group = _group(1, 1)
    _admit(journal, 1, group)
    reference = journal.activation_receipt(group.group_key, group.revision_sha256)
    journal.reserve(_request(2, (), (_item(group, "unchanged", reference),)))
    if retraction == "capture":
        _retract(journal, 3, 1)
    else:
        journal.retract(
            LifecycleRequest(
                _uuid(3), "session_retract", BRAIN, 1, journal.session_snapshot(_session(1))
            )
        )
    with pytest.raises(LifecycleError, match="revision_changed"):
        journal.record_outcome(_uuid(2), group.group_key, "unchanged")
    journal.halt(_uuid(2))
    replacement = _group(1, 2, revision=2, expected=group.revision_sha256)
    request = _request(4, (replacement,), (_item(replacement, "replace_current"),))
    journal.reserve(request)
    with pytest.raises(LifecycleError, match="retracted"):
        journal.stage_group(request.operation_id, replacement)
    journal.record_outcome(request.operation_id, replacement.group_key, "retracted")
    assert journal.finish(request.operation_id)["status"] == "committed"
    assert not journal.permits_capture(_capture(1))
    assert journal.permits_capture(_capture(1), history=True)


def test_wrong_unchanged_receipt_and_unproved_retraction_are_rejected(
    db: tuple[sqlite3.Connection, LifecycleJournalV2],
) -> None:
    connection, journal = db
    group = _group(1, 1)
    _admit(journal, 1, group)
    journal.reserve(_request(2, (), (_item(group, "unchanged", "0" * 64),)))
    before = _snapshot(connection)
    with pytest.raises(LifecycleError, match="revision_changed"):
        journal.record_outcome(_uuid(2), group.group_key, "unchanged")
    assert _snapshot(connection) == before
    journal.halt(_uuid(2))
    journal.reserve(_request(3, (), (_item(group, "retracted"),)))
    with pytest.raises(LifecycleError):
        journal.record_outcome(_uuid(3), group.group_key, "retracted")
    journal.halt(_uuid(3))


def test_all_unchanged_uses_retained_activations_without_new_session(
    db: tuple[sqlite3.Connection, LifecycleJournalV2],
) -> None:
    _, journal = db
    first, second = _group(1, 1), _group(2, 2)
    _admit(journal, 1, first)
    _admit(journal, 2, second)
    items = tuple(
        _item(
            group, "unchanged", journal.activation_receipt(group.group_key, group.revision_sha256)
        )
        for group in (first, second)
    )
    journal.reserve(_request(3, (), items))
    generation = journal.eligibility_generation
    for item in items:
        journal.record_outcome(_uuid(3), item.group_key, "unchanged")
    assert journal.finish(_uuid(3))["session_id"] is None
    assert journal.eligibility_generation == generation
    assert {item["outcome"] for item in journal.conversation_outcomes(_uuid(3))} == {"unchanged"}


def test_consumer_replacement_preserves_historical_receipt_and_refuses_old_unchanged(
    db: tuple[sqlite3.Connection, LifecycleJournalV2],
) -> None:
    _, journal = db
    old = _group(1, 1)
    _admit(journal, 1, old)
    old_receipt = journal.activation_receipt(old.group_key, old.revision_sha256)
    replacement = _group(1, 2, revision=2, expected=old.revision_sha256)
    journal.reserve(_request(2, (replacement,), (_item(replacement, "replace_current"),)))
    _activate(journal, 2, replacement)
    journal.finish(_uuid(2))
    assert not journal.permits_capture(_capture(1))
    assert journal.permits_capture(_capture(1), history=True)
    assert journal.permits_capture(_capture(2))
    assert journal.activation_receipt(old.group_key, old.revision_sha256) == old_receipt
    assert (
        journal.activation_receipt(replacement.group_key, replacement.revision_sha256)
        != old_receipt
    )
    journal.reserve(_request(3, (), (_item(old, "unchanged", old_receipt),)))
    with pytest.raises(LifecycleError, match="revision_changed"):
        journal.record_outcome(_uuid(3), old.group_key, "unchanged")
    journal.halt(_uuid(3))


def test_mixed_admitted_conflict_and_no_text_finish_with_only_new_membership(
    db: tuple[sqlite3.Connection, LifecycleJournalV2],
) -> None:
    _, journal = db
    admitted, conflicted = _group(1, 1), _group(2, 2)
    no_text = ConsumerItem(digest(["empty"]), None, "no_text", None)
    request = _request(1, (admitted, conflicted), (_item(admitted), _item(conflicted), no_text))
    journal.reserve(request)
    _activate(journal, 1, admitted)
    journal.record_outcome(request.operation_id, conflicted.group_key, "conflict")
    journal.record_outcome(request.operation_id, no_text.group_key, "no_text")
    assert journal.finish(request.operation_id)["status"] == "committed"
    assert journal.session_snapshot(_session(1))["membership_sha256"] == digest([_capture(1)])
    assert {item["outcome"] for item in journal.conversation_outcomes(request.operation_id)} == {
        "committed",
        "conflict",
        "no_text",
    }


def test_terminal_refusal_blocks_staging_custody_activation(
    db: tuple[sqlite3.Connection, LifecycleJournalV2],
) -> None:
    connection, journal = db
    group = _group(1, 1)
    request = _request(1, (group,), (_item(group),))
    journal.reserve(request)
    journal.stage_group(request.operation_id, group)
    journal.record_outcome(request.operation_id, group.group_key, "conflict")
    before = _snapshot(connection)
    for action in (
        lambda: journal.stage_group(request.operation_id, group),
        lambda: journal.custody_segment(
            request.operation_id, group.group_key, _capture(1), body=_body(1), envelope=_envelope(1)
        ),
        lambda: journal.activate_group(
            request.operation_id, group.group_key, session_id=_session(1)
        ),
        lambda: journal.record_outcome(request.operation_id, group.group_key, "retracted"),
    ):
        with pytest.raises(LifecycleError, match="operation_conflict"):
            action()
    assert _snapshot(connection) == before
    assert journal.finish(request.operation_id)["session_id"] is None
    assert not journal.permits_capture(_capture(1), history=True)


@pytest.mark.parametrize("terminal", [None, "committed", "halted"])
def test_completed_consumer_group_exact_retries_do_not_append_or_reactivate(
    db: tuple[sqlite3.Connection, LifecycleJournalV2],
    terminal: str | None,
) -> None:
    connection, journal = db
    first, second = _group(1, 1), _group(2, 2)
    request = _request(1, (first, second), (_item(first), _item(second)))
    journal.reserve(request)
    _activate(journal, 1, first)
    if terminal == "committed":
        _activate(journal, 1, second)
        journal.finish(request.operation_id)
    elif terminal == "halted":
        journal.halt(request.operation_id)
    before = _snapshot(connection)
    status = journal.status(request.operation_id)
    outcomes = journal.conversation_outcomes(request.operation_id)
    generation = journal.eligibility_generation
    _activate(journal, 1, first)
    assert _snapshot(connection) == before
    assert journal.status(request.operation_id) == status
    assert journal.conversation_outcomes(request.operation_id) == outcomes
    assert journal.eligibility_generation == generation
    with pytest.raises(LifecycleError, match="operation_conflict"):
        journal.stage_group(request.operation_id, _group(1, 1, revision=2))
    with pytest.raises(LifecycleError, match="invalid_arguments"):
        journal.custody_segment(
            request.operation_id,
            first.group_key,
            _capture(1),
            body=b"changed",
            envelope=_envelope(1),
        )
    with pytest.raises(LifecycleError, match="invalid_arguments"):
        journal.custody_segment(
            request.operation_id,
            first.group_key,
            _capture(1),
            body=_body(1),
            envelope=canonical_bytes({"changed": True}),
        )
    with pytest.raises(LifecycleError, match="operation_conflict"):
        journal.activate_group(request.operation_id, first.group_key, session_id=_session(99))
    assert _snapshot(connection) == before
    if terminal is None:
        _activate(journal, 1, second)
        assert journal.finish(request.operation_id)["status"] == "committed"


@pytest.mark.parametrize("terminal", ["conflict", "halted"])
def test_incomplete_refused_or_halted_item_cannot_resume_even_retained_steps(
    db: tuple[sqlite3.Connection, LifecycleJournalV2],
    terminal: str,
) -> None:
    connection, journal = db
    # Two segments distinguish exact custody replay from an unperformed custody step.
    first = _group(1, 1)
    tail = _group(2, 2).segments[0]
    group = GroupIntent(first.group_key, first.revision_sha256, None, first.segments + (tail,))
    unstaged = _group(3, 3)
    request = _request(1, (group, unstaged), (_item(group), _item(unstaged)))
    journal.reserve(request)
    journal.stage_group(request.operation_id, group)
    journal.custody_segment(
        request.operation_id, group.group_key, _capture(1), body=_body(1), envelope=_envelope(1)
    )
    if terminal == "conflict":
        journal.record_outcome(request.operation_id, group.group_key, "conflict")
        journal.record_outcome(request.operation_id, unstaged.group_key, "conflict")
    else:
        journal.halt(request.operation_id)
    before = _snapshot(connection)
    with pytest.raises(LifecycleError, match="operation_conflict"):
        journal.stage_group(request.operation_id, group)
    with pytest.raises(LifecycleError, match="operation_conflict"):
        journal.custody_segment(
            request.operation_id, group.group_key, _capture(1), body=_body(1), envelope=_envelope(1)
        )
    with pytest.raises(LifecycleError, match="operation_conflict"):
        journal.custody_segment(
            request.operation_id, group.group_key, _capture(2), body=_body(2), envelope=_envelope(2)
        )
    with pytest.raises(LifecycleError, match="operation_conflict"):
        journal.activate_group(request.operation_id, group.group_key, session_id=_session(1))
    with pytest.raises(LifecycleError, match="operation_conflict"):
        journal.stage_group(request.operation_id, unstaged)
    assert _snapshot(connection) == before
    assert not journal.permits_capture(_capture(1), history=True)
    assert not journal.permits_capture(_capture(2), history=True)


def test_retracted_completed_group_exact_replay_never_restores_eligibility(
    db: tuple[sqlite3.Connection, LifecycleJournalV2],
) -> None:
    connection, journal = db
    group = _group(1, 1)
    _admit(journal, 1, group)
    _retract(journal, 2, 1)
    before = _snapshot(connection)
    _activate(journal, 1, group)
    assert _snapshot(connection) == before
    assert not journal.permits_capture(_capture(1))
    assert journal.permits_capture(_capture(1), history=True)


@pytest.mark.parametrize(
    "mutation",
    [
        "extra",
        "duplicate",
        "unsorted",
        "missing_manifest",
        "bad_new_expected",
        "bad_revision",
        "bad_contract",
        "empty",
        "many",
    ],
)
def test_typed_request_rejects_bad_contracts_without_reservation(
    db: tuple[sqlite3.Connection, LifecycleJournalV2], mutation: str
) -> None:
    connection, journal = db
    first, second = _group(1, 1), _group(2, 2)
    payload = cast(
        dict[str, object],
        _request(1, (first, second), (_item(first), _item(second))).value()["payload"],
    )
    consumer = cast(dict[str, object], payload["consumer"])
    items = cast(list[dict[str, object]], consumer["items"])
    groups = cast(list[dict[str, object]], payload["groups"])
    if mutation == "extra":
        consumer["extra"] = True
    elif mutation == "duplicate":
        items.append(copy.deepcopy(items[0]))
    elif mutation == "unsorted":
        items.reverse()
    elif mutation == "missing_manifest":
        groups.pop()
    elif mutation == "bad_new_expected":
        groups[0]["expected_revision_sha256"] = "0" * 64
    elif mutation == "bad_revision":
        items[0]["revision_sha256"] = "0" * 64
    elif mutation == "bad_contract":
        consumer["contract"] = "unknown"
    elif mutation == "empty":
        consumer["items"] = []
    elif mutation == "many":
        consumer["items"] = [items[0]] * 10_001
    before = _snapshot(connection)
    with pytest.raises(LifecycleError):
        journal.reserve(LifecycleRequest(_uuid(1), "consumer_import", BRAIN, 1, payload))
    assert _snapshot(connection) == before


def test_item_reference_nullability_and_types_are_strict() -> None:
    for data in (
        ConsumerItem("0" * 64, None, "no_text", None).value() | {"group_key": True},
        {
            "group_key": "0" * 64,
            "revision_sha256": None,
            "planned_outcome": "new",
            "reference_receipt_sha256": None,
        },
        {
            "group_key": "0" * 64,
            "revision_sha256": "1" * 64,
            "planned_outcome": "no_text",
            "reference_receipt_sha256": None,
        },
        {
            "group_key": "0" * 64,
            "revision_sha256": "1" * 64,
            "planned_outcome": "unchanged",
            "reference_receipt_sha256": None,
        },
        {
            "group_key": "0" * 64,
            "revision_sha256": "1" * 64,
            "planned_outcome": "conflict",
            "reference_receipt_sha256": "2" * 64,
        },
    ):
        with pytest.raises(LifecycleError):
            ConsumerItem.from_value(data)


@pytest.mark.parametrize(
    "mutation",
    [
        "receipt",
        "extra",
        "event_order",
        "missing_payload",
        "orphan_payload",
        "request_order",
        "request_hash",
        "outcome",
    ],
)
def test_corrupt_mixed_restore_never_touches_destination(
    db: tuple[sqlite3.Connection, LifecycleJournalV2], mutation: str
) -> None:
    _, journal = db
    group = _group(1, 1)
    _admit(journal, 1, group)
    no_text = ConsumerItem(digest(["empty"]), None, "no_text", None)
    journal.reserve(_request(2, (), (no_text,)))
    journal.record_outcome(_uuid(2), no_text.group_key, "no_text")
    journal.finish(_uuid(2))
    evidence = journal.export_evidence_value()
    events = cast(list[dict[str, object]], evidence["events"])
    outcome = next(event for event in events if event["kind"] == "consumer_outcome")
    payload = cast(dict[str, object], outcome["payload"])
    if mutation == "receipt":
        payload["receipt_sha256"] = "0" * 64
    elif mutation == "extra":
        payload["extra"] = True
    elif mutation == "outcome":
        payload["outcome"] = "unchanged"
    elif mutation == "event_order":
        events.reverse()
    elif mutation == "missing_payload":
        cast(dict[str, object], evidence["payloads"]).popitem()
    elif mutation == "orphan_payload":
        cast(dict[str, object], evidence["payloads"])[sha256(b"orphan").hexdigest()] = (
            base64.b64encode(b"orphan").decode()
        )
    elif mutation == "request_order":
        cast(list[object], evidence["requests"]).reverse()
    elif mutation == "request_hash":
        cast(dict[str, object], cast(list[dict[str, object]], evidence["requests"])[0]["payload"])[
            "extra"
        ] = True
    if mutation in {"receipt", "extra", "outcome"}:
        _rehash_events(evidence)
    destination = _connection()
    before = destination.total_changes
    with pytest.raises(LifecycleError):
        restore_lifecycle_journal_v2_value(destination, evidence, brain_id=BRAIN, issuer_epoch=1)
    assert destination.total_changes == before
    assert destination.execute("SELECT name FROM sqlite_master").fetchall() == []
    destination.close()


def test_migrate_nonterminal_legacy_request_and_rollback_promotion() -> None:
    connection = _connection()
    install_lifecycle_journal(connection, brain_id=BRAIN)
    journal = LifecycleJournal(connection, brain_id=BRAIN, issuer_epoch=1)
    group = _group(1, 1)
    request = LifecycleRequest(_uuid(1), "consumer_import", BRAIN, 1, {"groups": [group.value()]})
    journal.reserve(request)
    journal.stage_group(request.operation_id, group)
    before = _snapshot(connection)
    connection.execute("SAVEPOINT promotion_rehearsal")
    migrate_lifecycle_journal_v2(connection, brain_id=BRAIN)
    connection.execute("ROLLBACK TO promotion_rehearsal")
    connection.execute("RELEASE promotion_rehearsal")
    assert _snapshot(connection) == before
    assert (
        LifecycleJournal(connection, brain_id=BRAIN, issuer_epoch=1).status(request.operation_id)[
            "status"
        ]
        == "materializing"
    )
    migrate_lifecycle_journal_v2(connection, brain_id=BRAIN)
    successor = LifecycleJournalV2(connection, brain_id=BRAIN, issuer_epoch=1)
    _activate(successor, 1, group)
    assert successor.finish(request.operation_id)["status"] == "committed"
    assert _snapshot(connection)[0] == before[0]
    connection.close()


def test_failed_identity_update_restores_immutable_trigger_and_v1_identity() -> None:
    connection = _connection()
    install_lifecycle_journal(connection, brain_id=BRAIN)
    before = connection.execute("SELECT name,sql FROM sqlite_master ORDER BY name").fetchall()

    def deny_update(
        action: int,
        table: str | None,
        column: str | None,
        database: str | None,
        trigger: str | None,
    ) -> int:
        if action == sqlite3.SQLITE_UPDATE and table == "lifecycle_journal_identity":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(deny_update)
    with pytest.raises(sqlite3.DatabaseError):
        migrate_lifecycle_journal_v2(connection, brain_id=BRAIN)
    connection.set_authorizer(None)
    assert (
        connection.execute("SELECT name,sql FROM sqlite_master ORDER BY name").fetchall() == before
    )
    assert LifecycleJournal(connection, brain_id=BRAIN, issuer_epoch=1).eligibility_generation == 0
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE lifecycle_journal_identity SET contract='altered'")
    connection.close()


def _engine_tables(connection: sqlite3.Connection) -> None:
    connection.execute("CREATE TABLE runtime_compatibility(singleton,min_runtime,schema_version)")
    connection.execute("INSERT INTO runtime_compatibility VALUES(1,15,20)")
    connection.execute("CREATE TABLE brain_identity(singleton,brain_id,issuer_epoch)")
    connection.execute("INSERT INTO brain_identity VALUES(1,?,1)", (BRAIN,))
    connection.execute("PRAGMA user_version=20")


def test_exact_engine_coordinates_and_atomic_engine_restore(
    db: tuple[sqlite3.Connection, LifecycleJournalV2],
) -> None:
    _, journal = db
    _admit(journal, 1, _group(1, 1))
    evidence = journal.export_evidence_value()
    destination = _connection()
    install_lifecycle_journal_v2(destination, brain_id=BRAIN)
    _engine_tables(destination)
    restored = restore_engine_lifecycle_journal_v2(
        destination, evidence, brain_id=BRAIN, issuer_epoch=1
    )
    assert restored.export_evidence_value() == evidence
    with pytest.raises(LifecycleError):
        restore_engine_lifecycle_journal_v2(destination, evidence, brain_id=BRAIN, issuer_epoch=1)
    before = _snapshot(destination)
    destination.execute("UPDATE runtime_compatibility SET min_runtime=14")
    with pytest.raises(LifecycleError):
        LifecycleJournalV2.for_engine(destination, brain_id=BRAIN, issuer_epoch=1)
    destination.execute("UPDATE runtime_compatibility SET min_runtime=15")
    destination.execute("PRAGMA user_version=19")
    with pytest.raises(LifecycleError):
        LifecycleJournalV2.for_engine(destination, brain_id=BRAIN, issuer_epoch=1)
    assert _snapshot(destination) == before
    destination.close()


def test_successor_golden_is_exact_terminal_internal_evidence(
    db: tuple[sqlite3.Connection, LifecycleJournalV2],
) -> None:
    _, journal = db
    group = _group(1, 1)
    _admit(journal, 1, group)
    no_text = ConsumerItem(digest(["empty"]), None, "no_text", None)
    journal.reserve(_request(2, (), (no_text,)))
    journal.record_outcome(_uuid(2), no_text.group_key, "no_text")
    journal.finish(_uuid(2))
    fixture = Path(__file__).parent / "fixtures" / "consumer-lifecycle-journal-v2.json"
    assert fixture.read_bytes() == journal.export_evidence()

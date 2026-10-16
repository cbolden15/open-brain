"""Explicit admitted journal and typed-operation dispatch preserves owner v1."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from open_brain_engine.engine.capture_lifecycle import (
    LifecycleSegment,
    decode_lifecycle_segment,
    open_lifecycle_journal,
    require_unreserved_lifecycle_delivery,
)
from open_brain_engine.engine.capture_lifecycle_contracts import (
    GroupIntent,
    LifecycleError,
    LifecycleRequest,
    digest,
)
from open_brain_engine.engine.capture_lifecycle_journal import (
    LifecycleJournal,
    install_lifecycle_journal,
)
from open_brain_engine.engine.capture_recovery import CaptureReservationIdentity
from open_brain_engine.engine.consumer_journal import (
    LifecycleJournalV2,
    migrate_lifecycle_journal_v2,
)
from open_brain_engine.engine.contracts import CaptureSubmission, JournalEnvelope, TextPayload

from open_brain.profile import compile_single_user_local
from packages.engine.tests.test_consumer_sources import (
    BRAIN,
    _custody,
    _segment,
)
from packages.engine.tests.test_consumer_sources import (
    db as db,
)


def _owner_segment(tmp_path: Path) -> LifecycleSegment:
    profile = compile_single_user_local(tmp_path / "brain")
    submission = CaptureSubmission.for_local_owner(
        profile=profile,
        payload=TextPayload("Synthetic owner evidence"),
        delivery_id="owner.synthetic." + str(uuid4()),
    )
    return LifecycleSegment(
        JournalEnvelope(submission, submission.privacy),
        CaptureReservationIdentity.allocate(canonical=False, accepted_at="2026-10-16T12:00:00Z"),
    )


def _owner_request(journal: LifecycleJournal, segment: LifecycleSegment) -> LifecycleRequest:
    manifest = GroupIntent(
        digest(["owner-group"]), digest(["owner-revision"]), None, (segment.binding,)
    )
    request = LifecycleRequest(
        str(uuid4()), "consumer_import", BRAIN, 1, {"groups": [manifest.value()]}
    )
    journal.reserve(request)
    journal.stage_group(request.operation_id, manifest)
    journal.custody_segment(
        request.operation_id,
        manifest.group_key,
        segment.identities.capture_id,
        body=segment.body,
        envelope=segment.to_bytes(),
    )
    return request


@pytest.mark.parametrize("version", [18, 19])
def test_legacy_owner_pending_bytes_survive_explicit_successor_dispatch(
    tmp_path: Path,
    version: int,
) -> None:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("BEGIN IMMEDIATE")
        install_lifecycle_journal(connection, brain_id=BRAIN)
        connection.execute("CREATE TABLE brain_identity(singleton,brain_id,issuer_epoch)")
        connection.execute("INSERT INTO brain_identity VALUES(1,?,1)", (BRAIN,))
        connection.execute(
            "CREATE TABLE runtime_compatibility(singleton,"
            "minimum_runtime_session_version,state_schema_version)"
        )
        connection.execute(
            "INSERT INTO runtime_compatibility VALUES(1,?,?)", (version - 5, version)
        )
        connection.execute(f"PRAGMA user_version={version}")
        segment = _owner_segment(tmp_path)
        predecessor = open_lifecycle_journal(connection)
        assert type(predecessor) is LifecycleJournal
        request = _owner_request(predecessor, segment)
        before = tuple(connection.execute("SELECT * FROM lifecycle_journal_events"))
        assert (
            decode_lifecycle_segment(predecessor, request.operation_id, segment.to_bytes())
            == segment
        )
        migrate_lifecycle_journal_v2(connection, brain_id=BRAIN)
        connection.execute(
            "UPDATE runtime_compatibility SET "
            "minimum_runtime_session_version=15,state_schema_version=20"
        )
        connection.execute("PRAGMA user_version=20")
        successor = open_lifecycle_journal(connection)
        assert isinstance(successor, LifecycleJournalV2)
        assert (
            decode_lifecycle_segment(successor, request.operation_id, segment.to_bytes()) == segment
        )
        assert tuple(connection.execute("SELECT * FROM lifecycle_journal_events")) == before
        assert not successor.permits_capture(segment.identities.capture_id, history=True)
    finally:
        connection.close()


def test_typed_consumer_decoder_and_reserved_delivery_cannot_fall_back(
    db: sqlite3.Connection,
) -> None:
    segment = _segment(db)
    request, _manifest = _custody(db, (segment,))
    journal = open_lifecycle_journal(db)
    assert decode_lifecycle_segment(journal, request.operation_id, segment.to_bytes()) == segment
    with pytest.raises(LifecycleError, match="operation_conflict"):
        require_unreserved_lifecycle_delivery(db, segment.envelope.submission.delivery_id)
    require_unreserved_lifecycle_delivery(db, "ordinary.synthetic.delivery")
    with pytest.raises(LifecycleError, match="invalid_evidence"):
        decode_lifecycle_segment(
            journal,
            request.operation_id,
            replace(segment, conversation_revision_sha256="f" * 64).to_bytes(),
        )


def test_typed_request_prevents_consumer_owner_envelope_substitution(
    db: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    consumer = _segment(db)
    consumer_request, _manifest = _custody(db, (consumer,))
    journal = open_lifecycle_journal(db)
    owner = _owner_segment(tmp_path)
    owner_request = _owner_request(journal, owner)
    with pytest.raises(LifecycleError, match="invalid_evidence"):
        decode_lifecycle_segment(journal, consumer_request.operation_id, owner.to_bytes())
    with pytest.raises(LifecycleError, match="invalid_evidence"):
        decode_lifecycle_segment(journal, owner_request.operation_id, consumer.to_bytes())


@pytest.mark.parametrize("change", ["schema", "floor", "issuer"])
def test_dispatch_refuses_mismatched_admission_coordinates(
    db: sqlite3.Connection, change: str
) -> None:
    if change == "schema":
        db.execute("PRAGMA user_version=19")
    elif change == "floor":
        db.execute("UPDATE runtime_compatibility SET minimum_runtime_session_version=14")
    else:
        db.execute("UPDATE brain_identity SET issuer_epoch=NULL")
    with pytest.raises(LifecycleError):
        open_lifecycle_journal(db)

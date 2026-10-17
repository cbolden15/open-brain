"""Synthetic stable-source staging, CAS, activation and group closure."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import asdict, replace
from hashlib import sha256
from typing import Any, cast
from uuid import uuid4

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.engine.capture_lifecycle import open_lifecycle_journal
from open_brain_engine.engine.capture_lifecycle_contracts import (
    GroupIntent,
    LifecycleError,
    LifecycleRequest,
    canonical_bytes,
    digest,
)
from open_brain_engine.engine.capture_lifecycle_policy import lifecycle_permits_capture
from open_brain_engine.engine.consumer_contracts import segment_key
from open_brain_engine.engine.consumer_journal import (
    CONSUMER_INTAKE_CONTRACT,
    ConsumerItem,
    LifecycleJournalV2,
    install_lifecycle_journal_v2,
)
from open_brain_engine.engine.consumer_schema import CONSUMER_STORAGE_SCHEMA
from open_brain_engine.engine.consumer_segments import ConsumerSegmentEnvelope
from open_brain_engine.engine.consumer_sources import (
    activate_consumer_sources,
    consumer_namespace_bytes,
    read_consumer_source_binding,
    register_consumer_capture,
    require_consumer_source_preimages,
)
from open_brain_engine.engine.privacy_store import write_source_revision_privacy
from open_brain_engine.engine.source_intake import require_unreserved_consumer_namespace
from open_brain_engine.engine.source_schema import SOURCE_HISTORY_SCHEMA
from open_brain_engine.engine.t03_contracts import T03Error

from packages.engine.tests.test_consumer_segments import _envelope

BRAIN = "brn_" + "a" * 26


def _capture_values(segment: ConsumerSegmentEnvelope) -> dict[str, Any]:
    submission = segment.envelope.submission
    role = dict(submission.role_claim)
    role["capabilities"] = list(cast(tuple[str, ...], submission.role_claim["capabilities"]))
    return {
        **{key: value for key, value in asdict(segment.identities).items() if key != "canonical"},
        "delivery_id": submission.delivery_id,
        "request_sha256": submission.request_sha256(),
        "payload_family": submission.payload.family,
        "payload_json": canonical(submission.payload.to_dict()),
        "search_text": submission.payload.search_text(),
        "file_bytes": None,
        "source_origin": submission.durable_source_origin(),
        "source_reference": submission.source_reference,
        "space_id": submission.space_id,
        "intent": None if submission.intent is None else submission.intent.value,
        "capture_why": submission.capture_why,
        "action": submission.action.value,
        "title": submission.title,
        "actor_id": submission.actor_id,
        "submission_path": submission.submission_path.value,
        "role_claim_json": canonical(role).decode(),
        "privacy_json": canonical(segment.envelope.admitted_privacy.to_dict()).decode(),
        "provenance_json": canonical(submission.provenance.to_dict()).decode(),
        "stage": 3,
        "source_path": f"sources/captures/{segment.identities.capture_id}.json",
    }


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("BEGIN IMMEDIATE")
    install_lifecycle_journal_v2(connection, brain_id=BRAIN)
    connection.execute("CREATE TABLE spaces(space_id TEXT PRIMARY KEY)")
    for statement in SOURCE_HISTORY_SCHEMA[:8]:
        connection.execute(statement)
    connection.execute("INSERT INTO engine_generations VALUES(1,'synthetic',0,0,1,0,0,0)")
    connection.execute("CREATE TABLE brain_identity(singleton,brain_id,issuer_epoch)")
    connection.execute("INSERT INTO brain_identity VALUES(1,?,1)", (BRAIN,))
    connection.execute(
        "CREATE TABLE runtime_compatibility(singleton,"
        "minimum_runtime_session_version,state_schema_version)"
    )
    connection.execute("INSERT INTO runtime_compatibility VALUES(1,15,20)")
    connection.execute(
        "CREATE TABLE source_lifecycle_state(source_id PRIMARY KEY,lifecycle_version)"
    )
    connection.execute(
        "CREATE TRIGGER source_lifecycle_state_seed AFTER INSERT ON logical_sources "
        "BEGIN INSERT INTO source_lifecycle_state VALUES(NEW.source_id,0); END"
    )
    connection.execute(
        "CREATE TABLE source_revision_privacy(capture_id PRIMARY KEY,effective_privacy_json)"
    )
    connection.execute("CREATE TABLE privacy_invalid_evidence(target_kind,target_id,reason,digest)")
    fields = _capture_values(_envelope())
    connection.execute(
        "CREATE TABLE captures("
        + ",".join(field + (" PRIMARY KEY" if field == "capture_id" else "") for field in fields)
        + ")"
    )
    for statement in CONSUMER_STORAGE_SCHEMA:
        connection.execute(statement)
    connection.execute("PRAGMA user_version=20")
    try:
        yield connection
    finally:
        connection.close()


def _segment(
    connection: sqlite3.Connection,
    *,
    index: int = 0,
    revision: str = "c" * 64,
) -> ConsumerSegmentEnvelope:
    original = _envelope()
    key = segment_key(original.conversation_key, index)
    source = replace(original.source, segment_key=key, source_id="source_" + str(uuid4()))
    current = read_consumer_source_binding(
        connection,
        namespace_id=source.namespace_id,
        conversation_key=source.conversation_key,
        segment_key=source.segment_key,
    )
    source = source if current is None else current
    reference = f"urn:open-brain:consumer-chat:chatgpt:{original.conversation_key}:{index}"
    submission = replace(
        original.envelope.submission,
        delivery_id="consumer." + str(uuid4()),
        source_reference=reference,
        provenance=replace(original.envelope.submission.provenance, source_ref=reference),
    )
    return replace(
        original,
        conversation_revision_sha256=revision,
        normalized_sha256=sha256(b"{}").hexdigest(),
        segment_index=index,
        segment_key=key,
        envelope=replace(original.envelope, submission=submission),
        source=source,
    )


def _custody(
    connection: sqlite3.Connection,
    segments: tuple[ConsumerSegmentEnvelope, ...],
    *,
    expected: str | None = None,
) -> tuple[LifecycleRequest, GroupIntent]:
    journal = open_lifecycle_journal(connection)
    assert isinstance(journal, LifecycleJournalV2)
    first = segments[0]
    manifest = GroupIntent(
        first.conversation_key,
        first.conversation_revision_sha256,
        expected,
        tuple(segment.binding for segment in segments),
    )
    item = ConsumerItem(
        manifest.group_key,
        manifest.revision_sha256,
        "new" if expected is None else "replace_current",
        None,
    )
    request = LifecycleRequest(
        str(uuid4()),
        "consumer_import",
        BRAIN,
        1,
        {
            "groups": [manifest.value()],
            "consumer": {
                "contract": CONSUMER_INTAKE_CONTRACT,
                "intent_sha256": digest(["synthetic-root"]),
                "items": [item.value()],
            },
        },
    )
    journal.reserve(request)
    journal.stage_group(request.operation_id, manifest)
    connection.execute(
        "INSERT OR IGNORE INTO consumer_intent_objects VALUES(?,?,?)",
        (sha256(b"{}").hexdigest(), "normalized", b"{}"),
    )
    for segment in segments:
        raw = segment.to_bytes()
        key = sha256(raw).hexdigest()
        connection.execute(
            "INSERT INTO consumer_intent_objects VALUES(?,?,?)", (key, "segment", raw)
        )
        connection.execute(
            "INSERT INTO consumer_segment_bindings VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                segment.identities.capture_id,
                request.operation_id,
                segment.conversation_key,
                segment.conversation_revision_sha256,
                segment.segment_index,
                segment.source.source_id,
                consumer_namespace_bytes(segment.source),
                canonical_bytes(segment.source.value()),
                segment.normalized_sha256,
                key,
            ),
        )
        journal.custody_segment(
            request.operation_id,
            manifest.group_key,
            segment.identities.capture_id,
            body=segment.body,
            envelope=raw,
        )
    return request, manifest


def _register(connection: sqlite3.Connection, segment: ConsumerSegmentEnvelope) -> bytes:
    values = _capture_values(segment)
    connection.execute(
        "INSERT INTO captures VALUES(" + ",".join("?" for _ in values) + ")", tuple(values.values())
    )
    row = connection.execute(
        "SELECT * FROM captures WHERE capture_id=?", (segment.identities.capture_id,)
    ).fetchone()
    submission = segment.envelope.submission
    role = dict(submission.role_claim)
    role["capabilities"] = list(cast(tuple[str, ...], submission.role_claim["capabilities"]))
    payload = canonical(
        {
            "capture_id": segment.identities.capture_id,
            "actor_id": submission.actor_id,
            "tenant_id": submission.tenant_id,
            "role_claim": role,
            "payload": submission.payload.to_dict(),
            "privacy": segment.envelope.admitted_privacy.to_dict(),
            "source": {"origin": "third_party", "reference": submission.source_reference},
            "provenance": {
                "content_origin": "third_party",
                "owner_context": "automation_absent",
                "source_ref": submission.source_reference,
                "transformation_receipts": [],
            },
        }
    )
    assert register_consumer_capture(connection, row, payload)
    write_source_revision_privacy(
        connection, capture_id=segment.identities.capture_id, privacy_json=values["privacy_json"]
    )
    return payload


def _activate(
    connection: sqlite3.Connection,
    request: LifecycleRequest,
    manifest: GroupIntent,
    segments: tuple[ConsumerSegmentEnvelope, ...],
    *,
    previous: tuple[str, ...] = (),
) -> None:
    activate_consumer_sources(connection, segments, previous_capture_ids=previous)
    journal = open_lifecycle_journal(connection)
    journal.activate_group(
        request.operation_id, manifest.group_key, session_id="capsess_" + str(uuid4())
    )
    journal.finish(request.operation_id)


def test_absent_preimage_stages_hidden_then_activates_stable_namespace(
    db: sqlite3.Connection,
) -> None:
    segment = _segment(db)
    request, manifest = _custody(db, (segment,))
    require_consumer_source_preimages(db, (segment,))
    payload = _register(db, segment)
    require_consumer_source_preimages(db, (segment,))
    assert not lifecycle_permits_capture(db, segment.identities.capture_id, history=True)
    _activate(db, request, manifest, (segment,))
    assert lifecycle_permits_capture(db, segment.identities.capture_id)
    current = read_consumer_source_binding(
        db,
        namespace_id=segment.source.namespace_id,
        conversation_key=segment.conversation_key,
        segment_key=segment.segment_key,
    )
    assert current is not None and current.source_id == segment.source.source_id
    assert current.expected_head == segment.identities.capture_id and current.head_version == 1
    assert bytes(db.execute("SELECT source_bytes FROM source_revisions").fetchone()[0]) == payload


def test_replacement_keeps_heads_then_retires_tail_and_reuses_it(db: sqlite3.Connection) -> None:
    original = (_segment(db), _segment(db, index=1))
    request, manifest = _custody(db, original)
    for segment in original:
        _register(db, segment)
    _activate(db, request, manifest, original)
    replacement = (_segment(db, revision="e" * 64),)
    replacement_request, replacement_manifest = _custody(
        db, replacement, expected=manifest.revision_sha256
    )
    _register(db, replacement[0])
    assert lifecycle_permits_capture(db, original[0].identities.capture_id)
    assert (
        db.execute(
            "SELECT head_capture_id FROM logical_sources WHERE source_id=?",
            (original[0].source.source_id,),
        ).fetchone()[0]
        == original[0].identities.capture_id
    )
    _activate(
        db,
        replacement_request,
        replacement_manifest,
        replacement,
        previous=tuple(segment.identities.capture_id for segment in original),
    )
    assert not lifecycle_permits_capture(db, original[1].identities.capture_id)
    assert lifecycle_permits_capture(db, original[1].identities.capture_id, history=True)
    readded = _segment(db, index=1, revision="f" * 64)
    assert readded.source.source_id == original[1].source.source_id
    assert readded.source.expected_head == original[1].identities.capture_id
    assert replacement[0].source.source_id == original[0].source.source_id
    expanded = (_segment(db, revision="f" * 64), readded)
    expanded_request, expanded_manifest = _custody(
        db, expanded, expected=replacement_manifest.revision_sha256
    )
    for segment in expanded:
        _register(db, segment)
    _activate(
        db,
        expanded_request,
        expanded_manifest,
        expanded,
        previous=(replacement[0].identities.capture_id,),
    )
    assert lifecycle_permits_capture(db, readded.identities.capture_id)
    assert db.execute("SELECT count(*) FROM logical_sources").fetchone()[0] == 2


@pytest.mark.parametrize(
    "change", ["head", "route", "lifecycle", "availability", "privacy", "epoch"]
)
def test_selected_source_change_refuses_activation(db: sqlite3.Connection, change: str) -> None:
    segment = _segment(db)
    _custody(db, (segment,))
    _register(db, segment)
    if change == "head":
        db.execute("UPDATE logical_sources SET head_version=head_version+1")
    elif change == "route":
        db.execute("UPDATE logical_sources SET route_version=route_version+1")
    elif change == "lifecycle":
        db.execute("UPDATE logical_sources SET lifecycle='retired'")
        db.execute("UPDATE source_lifecycle_state SET lifecycle_version=1")
    elif change == "availability":
        db.execute("UPDATE logical_sources SET availability='missing'")
    elif change == "privacy":
        raw = db.execute("SELECT effective_privacy_json FROM source_revision_privacy").fetchone()[0]
        db.execute("UPDATE source_revision_privacy SET effective_privacy_json=?", (raw + " ",))
    else:
        db.execute("UPDATE engine_generations SET control_epoch=1")
    with pytest.raises(LifecycleError):
        activate_consumer_sources(db, (segment,))
    assert db.execute("SELECT historical_only FROM logical_sources").fetchone()[0] == 1


@pytest.mark.parametrize("change", ["availability", "lifecycle", "head", "namespace"])
def test_one_current_member_source_hides_entire_group_but_history_survives(
    db: sqlite3.Connection,
    change: str,
) -> None:
    segments = (_segment(db), _segment(db, index=1))
    request, manifest = _custody(db, segments)
    for segment in segments:
        _register(db, segment)
    _activate(db, request, manifest, segments)
    if change == "head":
        db.execute(
            "UPDATE logical_sources SET head_capture_id=? WHERE source_id=?",
            ("capture_" + str(uuid4()), segments[1].source.source_id),
        )
    elif change == "namespace":
        db.execute(
            "DELETE FROM source_namespaces WHERE source_id=?", (segments[1].source.source_id,)
        )
    else:
        db.execute(
            f"UPDATE logical_sources SET {change}=? WHERE source_id=?",
            ("missing" if change == "availability" else "retired", segments[1].source.source_id),
        )
    assert not lifecycle_permits_capture(db, segments[0].identities.capture_id)
    assert lifecycle_permits_capture(db, segments[0].identities.capture_id, history=True)


def test_activation_failure_rolls_back_all_head_changes(db: sqlite3.Connection) -> None:
    segments = (_segment(db), _segment(db, index=1))
    _custody(db, segments)
    for segment in segments:
        _register(db, segment)
    blocked = segments[1].source.source_id
    db.execute(
        "CREATE TRIGGER fail_activation BEFORE UPDATE ON logical_sources "
        f"WHEN NEW.source_id='{blocked}' BEGIN SELECT RAISE(ABORT,'synthetic fault'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="synthetic fault"):
        activate_consumer_sources(db, segments)
    assert [row[0] for row in db.execute("SELECT historical_only FROM logical_sources")] == [1, 1]


def test_foreign_source_id_collision_and_inert_reference_gives_no_membership(
    db: sqlite3.Connection,
) -> None:
    segment = _segment(db)
    _custody(db, (segment,))
    _register(db, segment)
    impostor = replace(segment, source=replace(segment.source, source_id="source_" + str(uuid4())))
    with pytest.raises(LifecycleError, match="revision_changed"):
        require_consumer_source_preimages(db, (impostor,))
    values = _capture_values(_segment(db, index=1))
    db.execute(
        "INSERT INTO captures VALUES(" + ",".join("?" for _ in values) + ")", tuple(values.values())
    )
    row = db.execute(
        "SELECT * FROM captures WHERE capture_id=?", (values["capture_id"],)
    ).fetchone()
    assert not register_consumer_capture(db, row, b"{}")


def test_consumer_namespace_guard_preserves_predecessor_dto_behavior(
    db: sqlite3.Connection,
) -> None:
    namespace = {"connector_name": "consumer_chat"}
    with pytest.raises(T03Error, match="invalid_arguments"):
        require_unreserved_consumer_namespace(db, namespace)
    require_unreserved_consumer_namespace(db, {"connector_name": "other"})
    db.execute("PRAGMA user_version=19")
    require_unreserved_consumer_namespace(db, namespace)


def test_registered_custody_replay_is_idempotent_and_record_actor_is_immutable(
    db: sqlite3.Connection,
) -> None:
    segment = _segment(db)
    _custody(db, (segment,))
    payload = _register(db, segment)
    row = db.execute("SELECT * FROM captures").fetchone()
    assert register_consumer_capture(db, row, payload)
    assert db.execute("SELECT count(*) FROM source_revisions").fetchone()[0] == 1
    damaged = json.loads(payload)
    damaged["actor_id"] = "actor_" + str(uuid4())
    with pytest.raises(LifecycleError, match="invalid_evidence"):
        register_consumer_capture(db, row, canonical(damaged))


def test_existing_owner_route_is_retained_without_rewriting_source_evidence(
    db: sqlite3.Connection,
) -> None:
    original = _segment(db)
    request, manifest = _custody(db, (original,))
    _register(db, original)
    _activate(db, request, manifest, (original,))
    space = "space_" + str(uuid4())
    db.execute("INSERT INTO spaces VALUES(?)", (space,))
    db.execute("UPDATE logical_sources SET space_id=?,route_version=1", (space,))
    db.execute("UPDATE captures SET space_id=?", (space,))
    replacement = _segment(db, revision="e" * 64)
    replacement_request, replacement_manifest = _custody(
        db, (replacement,), expected=manifest.revision_sha256
    )
    payload = _register(db, replacement)
    _activate(
        db,
        replacement_request,
        replacement_manifest,
        (replacement,),
        previous=(original.identities.capture_id,),
    )
    assert (
        db.execute(
            "SELECT space_id FROM captures WHERE capture_id=?", (replacement.identities.capture_id,)
        ).fetchone()[0]
        == space
    )
    assert (
        bytes(
            db.execute(
                "SELECT source_bytes FROM source_revisions WHERE capture_id=?",
                (replacement.identities.capture_id,),
            ).fetchone()[0]
        )
        == payload
    )


def test_mutated_binding_projection_refused_against_shared_custody(db: sqlite3.Connection) -> None:
    segment = _segment(db)
    _custody(db, (segment,))
    db.execute("DROP TRIGGER consumer_segment_bindings_update_immutable")
    db.execute("UPDATE consumer_segment_bindings SET expected_source_bytes=?", (b"{}",))
    with pytest.raises(LifecycleError, match="invalid_evidence"):
        _register(db, segment)
    assert db.execute("SELECT count(*) FROM logical_sources").fetchone()[0] == 0


def test_unrelated_eligibility_and_retrieval_generations_revalidate_selected_sources(
    db: sqlite3.Connection,
) -> None:
    segment = _segment(db)
    request, manifest = _custody(db, (segment,))
    _register(db, segment)
    journal = open_lifecycle_journal(db)
    generation = journal.eligibility_generation
    journal.retract(
        LifecycleRequest(
            str(uuid4()), "capture_retract", BRAIN, 1, {"capture_id": "capture_" + str(uuid4())}
        )
    )
    assert journal.eligibility_generation == generation + 1
    db.execute("UPDATE engine_generations SET retrieval_generation=91,vector_generation=92")
    require_consumer_source_preimages(db, (segment,))
    _activate(db, request, manifest, (segment,))
    assert lifecycle_permits_capture(db, segment.identities.capture_id)


def test_selected_retraction_barrier_refuses_staging_preimage_without_mutation(
    db: sqlite3.Connection,
) -> None:
    original = _segment(db)
    request, manifest = _custody(db, (original,))
    _register(db, original)
    _activate(db, request, manifest, (original,))
    replacement = _segment(db, revision="e" * 64)
    _custody(db, (replacement,), expected=manifest.revision_sha256)
    journal = open_lifecycle_journal(db)
    journal.retract(
        LifecycleRequest(
            str(uuid4()),
            "capture_retract",
            BRAIN,
            1,
            {"capture_id": original.identities.capture_id},
        )
    )
    before = tuple(db.execute("SELECT * FROM logical_sources"))
    with pytest.raises(LifecycleError, match="retracted"):
        require_consumer_source_preimages(db, (replacement,))
    assert tuple(db.execute("SELECT * FROM logical_sources")) == before

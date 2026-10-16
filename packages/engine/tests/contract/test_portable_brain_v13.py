"""Portable13 proves real-engine custody closure, replay and tombstone restore."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.engine.capture_lifecycle_contracts import (
    GroupIntent,
    LifecycleError,
    LifecycleRequest,
    SegmentBinding,
    digest,
)
from open_brain_engine.engine.capture_lifecycle_journal import (
    JOURNAL_SCHEMA_SHA256,
    LifecycleJournal,
)
from open_brain_engine.engine.capture_recovery import CaptureReservationIdentity
from open_brain_engine.engine.contracts import (
    CaptureReceipt,
    CaptureSubmission,
    JournalEnvelope,
    TextPayload,
)
from open_brain_engine.engine.local import BrainEngine
from open_brain_engine.portable.lifecycle_v13 import (
    LIFECYCLE_PATH,
    LIFECYCLE_PAYLOAD_PREFIX,
    is_lifecycle_path,
    lifecycle_evidence_sidecars,
)
from open_brain_engine.portable.v1 import PortableValidationError
from open_brain_engine.portable.v12 import validate_portable_file_set_v12
from open_brain_engine.portable.v12_catalog import PORTABLE_V12_SCHEMA_CATALOG_DIGEST
from open_brain_engine.portable.v13_catalog import PORTABLE_V13_SCHEMA_CATALOG
from open_brain_engine.portable.versioned import validated_portable_snapshot

from open_brain.profile import compile_single_user_local, open_existing_single_user_local


def _engine(root: Path) -> BrainEngine:
    root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.parent.chmod(0o700)
    return BrainEngine.open(compile_single_user_local(root))


def _journal(connection: sqlite3.Connection) -> LifecycleJournal:
    identity = connection.execute("SELECT brain_id,issuer_epoch FROM brain_identity").fetchone()
    return LifecycleJournal.for_engine(connection, brain_id=identity[0], issuer_epoch=identity[1])


def _segment(engine: BrainEngine, *, materialize: bool = True) -> tuple[str, bytes, bytes]:
    body = TextPayload("Synthetic lifecycle archive body.")
    submission = CaptureSubmission.for_local_owner(
        profile=engine.profile, payload=body,
        delivery_id="synthetic.lifecycle." + str(uuid4()),
    )
    envelope = JournalEnvelope(submission, submission.privacy)
    if materialize:
        receipt = engine.capture.submit(submission)
        assert isinstance(receipt, CaptureReceipt)
        with engine._store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM captures WHERE capture_id=?", (receipt.capture_id,)
            ).fetchone()
            identities = CaptureReservationIdentity(
                capture_id=row["capture_id"], accepted_receipt_id=row["accepted_receipt_id"],
                accepted_at=row["accepted_at"], canonical=False,
            )
    else:
        identities = CaptureReservationIdentity(
            capture_id="capture_" + str(uuid4()), accepted_receipt_id="receipt_" + str(uuid4()),
            accepted_at="2026-10-15T12:00:00Z", canonical=False,
        )
    wrapper = canonical({
        "contract": "engine.lifecycle-segment.v1",
        "capture": envelope.to_dict(), "reservation": asdict(identities),
    })
    return identities.capture_id, body.text.encode(), wrapper


def _record(
    engine: BrainEngine, *, materialize: bool = True, terminal: str = "commit"
) -> tuple[LifecycleRequest, str, dict[str, object]]:
    capture_id, body, wrapper = _segment(engine, materialize=materialize)
    group = GroupIntent(
        digest({"group": str(uuid4())}), digest({"revision": 1}), None,
        (SegmentBinding(capture_id, sha256(body).hexdigest(), sha256(wrapper).hexdigest()),),
    )
    with engine._store.transaction() as connection:
        journal = _journal(connection)
        request = LifecycleRequest(
            str(uuid4()), "consumer_import", journal.brain_id, journal.issuer_epoch,
            {"groups": [group.value()]},
        )
        journal.reserve(request)
        if terminal == "pending":
            return request, capture_id, journal.status(request.operation_id)
        journal.stage_group(request.operation_id, group)
        journal.custody_segment(
            request.operation_id, group.group_key, capture_id, body=body, envelope=wrapper
        )
        if terminal == "halt":
            return request, capture_id, journal.halt(request.operation_id)
        journal.activate_group(
            request.operation_id, group.group_key, session_id="capsess_" + str(uuid4())
        )
        return request, capture_id, journal.finish(request.operation_id)


def _export(engine: BrainEngine, destination: Path) -> None:
    assert engine.tasks.portability.export(
        destination, export_id="export_" + str(uuid4())
    ).schema_version == 13


def _payloads(root: Path) -> dict[str, bytes]:
    return {
        path: raw for path, raw in validated_portable_snapshot(root).files.items()
        if path != "portable-manifest.json"
    }


def _rehash(root: Path) -> None:
    path = root / "portable-manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest["files"] = [
        {"path": item.relative_to(root).as_posix(), "sha256": sha256(item.read_bytes()).hexdigest()}
        for item in sorted(root.rglob("*")) if item.is_file() and item != path
    ]
    path.write_bytes(canonical(manifest))


def _replace_lifecycle(root: Path, evidence: dict[str, object]) -> None:
    for path in root.rglob("*"):
        if path.is_file() and is_lifecycle_path(path.relative_to(root).as_posix()):
            path.unlink()
    for relative, raw in lifecycle_evidence_sidecars(evidence).items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)


def test_empty_schema18_export_restore_and_frozen_v12_composition(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    archive, restored, again = tmp_path / "archive", tmp_path / "restored", tmp_path / "again"
    _export(engine, archive)
    files = _payloads(archive)
    assert json.loads(files[LIFECYCLE_PATH])["requests"] == []
    assert PORTABLE_V13_SCHEMA_CATALOG["base_catalog_sha256"] == PORTABLE_V12_SCHEMA_CATALOG_DIGEST
    assert PORTABLE_V13_SCHEMA_CATALOG["capture_lifecycle_schema_sha256"] == JOURNAL_SCHEMA_SHA256
    validate_portable_file_set_v12(
        {path: raw for path, raw in files.items() if not is_lifecycle_path(path)},
        tenant_id=engine.profile.tenant_id,
    )
    with pytest.raises(PortableValidationError):
        validate_portable_file_set_v12(files, tenant_id=engine.profile.tenant_id)
    engine.tasks.portability.import_clean(archive, restored, import_id="import_" + str(uuid4()))
    reopened = BrainEngine.open(open_existing_single_user_local(restored))
    _export(reopened, again)
    assert _payloads(again) == files


def test_replay_retractions_and_halted_custody_survive_real_restore(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    request, capture_id, receipt = _record(engine)
    halted, staged_id, halted_receipt = _record(engine, materialize=False, terminal="halt")
    _, retained_staged_id, _ = _record(engine, terminal="halt")
    with engine._store.transaction() as connection:
        journal = _journal(connection)
        retract = LifecycleRequest(
            str(uuid4()), "capture_retract", journal.brain_id, journal.issuer_epoch,
            {"capture_id": capture_id},
        )
        tombstone = journal.retract(retract)
        session_id = receipt["session_id"]
        assert isinstance(session_id, str)
        retract_session = LifecycleRequest(
            str(uuid4()), "session_retract", journal.brain_id, journal.issuer_epoch,
            journal.session_snapshot(session_id),
        )
        session_tombstone = journal.retract(retract_session)
    from open_brain_engine.engine.reconciliation import rederive_live_search_projection

    rederive_live_search_projection(engine)
    archive, destination, again = tmp_path / "archive", tmp_path / "restored", tmp_path / "again"
    _export(engine, archive)
    import_id = "import_" + str(uuid4())
    engine.tasks.portability.import_clean(archive, destination, import_id=import_id)
    assert engine.tasks.portability.import_clean(
        archive, destination, import_id=import_id
    ).duplicate
    reopened = BrainEngine.open(open_existing_single_user_local(destination))
    with reopened._store.transaction() as connection:
        journal = _journal(connection)
        assert journal.reserve(request) == receipt
        assert journal.reserve(halted) == halted_receipt
        assert journal.retract(retract) == tombstone
        assert journal.retract(retract_session) == session_tombstone
        assert not journal.permits_capture(capture_id)
        assert journal.permits_capture(capture_id, history=True)
        assert not journal.permits_capture(staged_id, history=True)
        assert not journal.permits_capture(retained_staged_id, history=True)
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 2
    _export(reopened, again)
    assert _payloads(again) == _payloads(archive)


def test_pending_operation_blocks_export_before_destination_exists(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    _record(engine, materialize=False, terminal="pending")
    with pytest.raises(LifecycleError, match="operation_pending"):
        _export(engine, tmp_path / "archive")
    assert not (tmp_path / "archive").exists()


@pytest.mark.parametrize("tamper", [
    "remove", "schema", "activated-source", "retracted-source", "orphan-payload",
    "indexed-orphan-payload", "missing-payload", "changed-payload",
])
def test_rehashed_lifecycle_tamper_refused_before_destination_write(
    tmp_path: Path, tamper: str
) -> None:
    engine = _engine(tmp_path / "brain")
    _record(engine)
    archive, destination = tmp_path / "archive", tmp_path / "destination"
    _export(engine, archive)
    path = archive / LIFECYCLE_PATH
    if tamper == "remove":
        path.unlink()
    elif tamper in {"orphan-payload", "indexed-orphan-payload"}:
        raw = b"Synthetic orphan custody payload."
        extra = archive / (LIFECYCLE_PAYLOAD_PREFIX + sha256(raw).hexdigest())
        extra.write_bytes(raw)
        if tamper == "indexed-orphan-payload":
            metadata = json.loads(path.read_bytes())
            metadata["payloads"] = sorted([*metadata["payloads"], sha256(raw).hexdigest()])
            path.write_bytes(canonical(metadata))
    elif tamper in {"missing-payload", "changed-payload"}:
        key = json.loads(path.read_bytes())["payloads"][0]
        payload_path = archive / (LIFECYCLE_PAYLOAD_PREFIX + key)
        if tamper == "missing-payload":
            payload_path.unlink()
        else:
            payload_path.write_bytes(b"Synthetic replaced custody payload.")
    elif tamper == "schema":
        value = json.loads(path.read_bytes())
        value["schema_sha256"] = "a" * 64
        path.write_bytes(canonical(value))
    else:
        # Semantically valid standalone evidence is insufficient without actual
        # admitted source captures in this same Brain archive.
        from open_brain_engine.engine.capture_lifecycle_journal import install_lifecycle_journal

        with sqlite3.connect(":memory:") as connection:
            connection.execute("BEGIN IMMEDIATE")
            with engine._store.connect() as source:
                identity = source.execute(
                    "SELECT brain_id,issuer_epoch FROM brain_identity"
                ).fetchone()
            install_lifecycle_journal(connection, brain_id=identity[0])
            journal = LifecycleJournal(connection, brain_id=identity[0], issuer_epoch=identity[1])
            fake_id, body, wrapper = _segment(engine, materialize=False)
            if tamper == "retracted-source":
                journal.retract(LifecycleRequest(
                    str(uuid4()), "capture_retract", identity[0], identity[1],
                    {"capture_id": fake_id},
                ))
                _replace_lifecycle(archive, journal.export_evidence_value())
                _rehash(archive)
                with pytest.raises(PortableValidationError, match="lifecycle"):
                    engine.tasks.portability.import_clean(
                        archive, destination, import_id="import_" + str(uuid4())
                    )
                assert not destination.exists()
                return
            group = GroupIntent(digest({"fake": 1}), digest({"revision": 1}), None, (
                SegmentBinding(fake_id, sha256(body).hexdigest(), sha256(wrapper).hexdigest()),
            ))
            request = LifecycleRequest(str(uuid4()), "consumer_import", identity[0], identity[1], {
                "groups": [group.value()],
            })
            journal.reserve(request)
            journal.stage_group(request.operation_id, group)
            journal.custody_segment(
                request.operation_id, group.group_key, fake_id, body=body, envelope=wrapper
            )
            journal.activate_group(
                request.operation_id, group.group_key, session_id="capsess_" + str(uuid4())
            )
            journal.finish(request.operation_id)
            _replace_lifecycle(archive, journal.export_evidence_value())
    _rehash(archive)
    with pytest.raises(PortableValidationError, match="lifecycle"):
        engine.tasks.portability.import_clean(
            archive, destination, import_id="import_" + str(uuid4())
        )
    assert not destination.exists()


def test_frozen_v12_restores_into_schema18_with_empty_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine import local_schema

    with monkeypatch.context() as predecessor:
        predecessor.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 17)
        engine = _engine(tmp_path / "old")
        archive = tmp_path / "v12"
        assert engine.tasks.portability.export(
            archive, export_id="export_" + str(uuid4())
        ).schema_version == 12
    destination = tmp_path / "restored"
    engine.tasks.portability.import_clean(
        archive, destination, import_id="import_" + str(uuid4())
    )
    reopened = BrainEngine.open(open_existing_single_user_local(destination))
    with reopened._store.connect() as connection:
        if not connection.in_transaction:
            connection.execute("BEGIN")
        assert json.loads(_journal(connection).export_evidence())["requests"] == []
    _export(reopened, tmp_path / "v13")


def test_real_halted_staged_segment_is_retained_and_portable(tmp_path: Path) -> None:
    from open_brain_engine.engine.t03_contracts import EffectiveAuthority

    engine = _engine(tmp_path / "brain")
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "synthetic-lifecycle-owner", frozenset(), None, owner=True
    )
    segment = engine.lifecycle.prepare_segment(
        CaptureSubmission.for_local_owner(
            profile=engine.profile, payload=TextPayload("Synthetic staged lifecycle body."),
            delivery_id="synthetic.staged.lifecycle",
        ), authority=owner,
    )
    group = GroupIntent(digest({"actual": 1}), digest({"revision": 1}), None, (segment.binding,))
    with engine._store.connect() as connection:
        journal = _journal(connection)
        request = LifecycleRequest(str(uuid4()), "consumer_import", journal.brain_id,
                                   journal.issuer_epoch, {"groups": [group.value()]})
    engine.lifecycle.begin(request, authority=owner)
    engine.lifecycle.stage_segment(request, group.group_key, segment, authority=owner)
    engine.lifecycle.halt(request, authority=owner)
    with engine._store.connect() as connection:
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 1
        assert not _journal(connection).permits_capture(segment.identities.capture_id, history=True)
    archive, restored = tmp_path / "archive", tmp_path / "restored"
    _export(engine, archive)
    engine.tasks.portability.import_clean(archive, restored, import_id="import_" + str(uuid4()))
    reopened = BrainEngine.open(open_existing_single_user_local(restored))
    with reopened._store.connect() as connection:
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 1
        assert not _journal(connection).permits_capture(segment.identities.capture_id, history=True)
    _export(reopened, tmp_path / "again")
    assert _payloads(tmp_path / "again") == _payloads(archive)


def test_large_real_lifecycle_export_restore_exceeds_isolated_snapshot_limit(
    tmp_path: Path,
) -> None:
    from open_brain_engine.engine.capture_lifecycle_contracts import MAX_CANONICAL_BYTES
    from open_brain_engine.engine.t03_contracts import EffectiveAuthority

    engine = _engine(tmp_path / "brain")
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "synthetic-large-lifecycle", frozenset(), None, owner=True
    )
    segments = [
        engine.lifecycle.prepare_segment(
            CaptureSubmission.for_local_owner(
                profile=engine.profile,
                payload=TextPayload("😀" * 65_496 + f"{number:04d}"),
                delivery_id=f"synthetic.large.lifecycle.{number}",
            ), authority=owner,
        )
        for number in range(25)
    ]
    group = GroupIntent(digest({"large": 1}), digest({"revision": 1}), None,
                        tuple(segment.binding for segment in segments))
    with engine._store.connect() as connection:
        journal = _journal(connection)
        request = LifecycleRequest(str(uuid4()), "consumer_import", journal.brain_id,
                                   journal.issuer_epoch, {"groups": [group.value()]})
    engine.lifecycle.begin(request, authority=owner)
    for segment in segments:
        engine.lifecycle.stage_segment(request, group.group_key, segment, authority=owner)
    engine.lifecycle.activate_group(
        request, group.group_key, session_id="capsess_" + str(uuid4()), authority=owner
    )
    receipt = engine.lifecycle.finish(request, authority=owner)
    with engine._store.connect() as connection:
        journal = _journal(connection)
        evidence = journal.export_evidence_value()
        assert len(canonical(evidence)) > MAX_CANONICAL_BYTES
        with pytest.raises(LifecycleError):
            journal.export_evidence()
    archive, restored = tmp_path / "archive", tmp_path / "restored"
    _export(engine, archive)
    metadata = json.loads((archive / LIFECYCLE_PATH).read_bytes())
    assert len(metadata["payloads"]) == 50
    assert all((archive / (LIFECYCLE_PAYLOAD_PREFIX + key)).is_file()
               for key in metadata["payloads"])
    import_id = "import_" + str(uuid4())
    engine.tasks.portability.import_clean(archive, restored, import_id=import_id)
    reopened = BrainEngine.open(open_existing_single_user_local(restored))
    with reopened._store.connect() as connection:
        journal = _journal(connection)
        assert journal.export_evidence_value() == evidence
        assert journal.reserve(request) == receipt
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 25

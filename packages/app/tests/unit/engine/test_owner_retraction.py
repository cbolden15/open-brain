"""Durable owner approval, stale previews and exact terminal replay."""

from __future__ import annotations

import json
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from open_brain_engine.engine import BrainEngine, CaptureSubmission, TextPayload
from open_brain_engine.engine import owner_retraction as module
from open_brain_engine.engine.capture_lifecycle import _journal
from open_brain_engine.engine.capture_lifecycle_contracts import (
    GroupIntent,
    decode_canonical,
    digest,
)
from open_brain_engine.engine.owner_retraction_contracts import (
    RetractionError,
    approval_record,
    impact_value,
    validate_approval_records,
)

from open_brain.profile import compile_single_user_local
from packages.app.tests.unit.engine.test_capture_lifecycle_engine import (
    _activate,
    _group,
    _owner,
    _request,
    _stage,
)
from packages.app.tests.unit.engine.test_sharing_recovery import _decision
from packages.app.tests.unit.engine.test_sharing_tasks import _managed_source


def _capture(engine: BrainEngine) -> str:
    return engine.capture.accept(
        TextPayload("PRIVATE_SYNTHETIC_RETRACTION_BODY"), delivery_id="capture." + str(uuid4())
    ).capture_id


def test_preview_page_is_read_only_and_commit_receipt_replays_after_expiry_and_restart(
    tmp_path: Path,
) -> None:
    now = [datetime(2026, 10, 16, tzinfo=UTC)]
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"), clock=lambda: now[0])
    capture = _capture(engine)
    preview = engine.lifecycle.preview_retraction(capture_id=capture, authority=_owner(engine))
    identity = str(preview["preview_id"])
    assert len(identity) == 51  # retprev_ is eight characters; base64url body is43.
    assert "PRIVATE_SYNTHETIC" not in json.dumps(preview)
    with closing(engine._store.connect()) as connection:
        before = tuple(connection.iterdump())
    assert engine.lifecycle.preview_retraction_page(identity, authority=_owner(engine)) == preview
    with closing(engine._store.connect()) as connection:
        assert tuple(connection.iterdump()) == before
    operation = str(uuid4())
    receipt = engine.lifecycle.commit_retraction(identity, operation, authority=_owner(engine))
    assert set(receipt) == {
        "dto_version",
        "status",
        "operation_id",
        "receipt_id",
        "counts",
        "history_retained",
    }
    assert receipt["status"] == "committed"
    assert engine.retrieval.search("PRIVATE_SYNTHETIC") == ()
    now[0] += timedelta(hours=2)
    engine = BrainEngine.open(engine.profile, clock=lambda: now[0])
    assert engine.lifecycle.retraction_status(operation, authority=_owner(engine)) == receipt
    with closing(engine._store.connect()) as connection:
        before = tuple(connection.iterdump())
        raw = connection.execute("SELECT request_bytes FROM retraction_approvals").fetchone()[0]
        assert identity.encode() not in bytes(raw)
    assert (
        engine.lifecycle.commit_retraction(identity, operation, authority=_owner(engine)) == receipt
    )
    with closing(engine._store.connect()) as connection:
        assert tuple(connection.iterdump()) == before
    with pytest.raises(RetractionError, match="preview_not_found"):
        engine.lifecycle.preview_retraction_page(identity, authority=_owner(engine))


def test_atomic_receipt_failure_rolls_back_tombstone_and_preserves_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    capture = _capture(engine)
    preview = engine.lifecycle.preview_retraction(capture_id=capture, authority=_owner(engine))
    identity, operation = str(preview["preview_id"]), str(uuid4())
    original = module._insert_approval

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("synthetic receipt interruption")

    monkeypatch.setattr(module, "_insert_approval", fail)
    with pytest.raises(RuntimeError, match="synthetic receipt interruption"):
        engine.lifecycle.commit_retraction(identity, operation, authority=_owner(engine))
    assert engine.retrieval.search("PRIVATE_SYNTHETIC")
    with pytest.raises(RetractionError, match="operation_not_found"):
        engine.lifecycle.retraction_status(operation, authority=_owner(engine))
    assert engine.lifecycle.preview_retraction_page(identity, authority=_owner(engine)) == preview
    monkeypatch.setattr(module, "_insert_approval", original)
    assert (
        engine.lifecycle.commit_retraction(identity, operation, authority=_owner(engine))["status"]
        == "committed"
    )


@pytest.mark.parametrize("change", ["expiry", "route", "privacy", "head"])
def test_changed_preview_refuses_without_lifecycle_decision(tmp_path: Path, change: str) -> None:
    now = [datetime(2026, 10, 16, tzinfo=UTC)]
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"), clock=lambda: now[0])
    capture = _capture(engine)
    identity = str(
        engine.lifecycle.preview_retraction(capture_id=capture, authority=_owner(engine))[
            "preview_id"
        ]
    )
    if change == "expiry":
        now[0] += timedelta(hours=1)
    else:
        with engine._store.transaction() as connection:
            if change == "route":
                connection.execute(
                    "UPDATE logical_sources SET route_version=route_version+1 "
                    "WHERE head_capture_id=?",
                    (capture,),
                )
            elif change == "head":
                connection.execute(
                    "UPDATE logical_sources SET head_version=head_version+1 "
                    "WHERE head_capture_id=?",
                    (capture,),
                )
            else:
                connection.execute(
                    "UPDATE source_revision_privacy SET effective_privacy_json="
                    "json_set(effective_privacy_json,'$.tier','public') WHERE capture_id=?",
                    (capture,),
                )
    expected = "preview_expired" if change == "expiry" else "revision_changed"
    with pytest.raises(RetractionError, match=expected):
        engine.lifecycle.preview_retraction_page(identity, authority=_owner(engine))
    with pytest.raises(RetractionError, match=expected):
        engine.lifecycle.commit_retraction(identity, str(uuid4()), authority=_owner(engine))
    with closing(engine._store.connect()) as connection:
        assert connection.execute("SELECT count(*) FROM retraction_approvals").fetchone()[0] == 0
        assert (
            connection.execute("SELECT count(*) FROM lifecycle_journal_requests").fetchone()[0] == 0
        )


def test_preview_limit_expiry_cleanup_and_owner_only_read(tmp_path: Path) -> None:
    now = [datetime(2026, 10, 16, tzinfo=UTC)]
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"), clock=lambda: now[0])
    capture = _capture(engine)
    previews = [
        engine.lifecycle.preview_retraction(capture_id=capture, authority=_owner(engine))
        for _ in range(16)
    ]
    with pytest.raises(RetractionError, match="operation_pending"):
        engine.lifecycle.preview_retraction(capture_id=capture, authority=_owner(engine))
    for authority in (
        replace(_owner(engine), owner=False),
        replace(_owner(engine), principal_id="other"),
        replace(_owner(engine), space_ids=frozenset()),
    ):
        with pytest.raises(RetractionError, match="unsupported_capability"):
            engine.lifecycle.preview_retraction_page(
                str(previews[0]["preview_id"]), authority=authority
            )
    now[0] += timedelta(hours=1)
    engine.lifecycle.preview_retraction(capture_id=capture, authority=_owner(engine))
    with closing(engine._store.connect()) as connection:
        assert connection.execute("SELECT count(*) FROM retraction_previews").fetchone()[0] == 1


def test_same_operation_with_different_preview_conflicts_and_evidence_is_validated(
    tmp_path: Path,
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    capture = _capture(engine)
    first = str(
        engine.lifecycle.preview_retraction(capture_id=capture, authority=_owner(engine))[
            "preview_id"
        ]
    )
    second = str(
        engine.lifecycle.preview_retraction(capture_id=capture, authority=_owner(engine))[
            "preview_id"
        ]
    )
    operation = str(uuid4())
    engine.lifecycle.commit_retraction(first, operation, authority=_owner(engine))
    with pytest.raises(RetractionError, match="operation_conflict"):
        engine.lifecycle.commit_retraction(second, operation, authority=_owner(engine))
    with closing(engine._store.connect()) as connection:
        request_raw, receipt_raw = connection.execute(
            "SELECT request_bytes,receipt_bytes FROM retraction_approvals"
        ).fetchone()
        request, receipt = (
            decode_canonical(bytes(request_raw)),
            decode_canonical(bytes(receipt_raw)),
        )
        if not connection.in_transaction:
            connection.execute("BEGIN")
        evidence = _journal(connection).export_evidence_value()
    record = approval_record(request, receipt)
    assert validate_approval_records([record], evidence) == [record]
    assert isinstance(receipt, dict)
    receipt["counts"] = {}
    with pytest.raises(RetractionError, match="invalid_evidence"):
        approval_record(request, receipt)


def test_session_public_paging_binds_all_members_and_retracts_group_atomically(
    tmp_path: Path,
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    request, group, segments = _group(
        engine, tuple(f"synthetic member {index}" for index in range(100))
    )
    _stage(engine, request, group, segments)
    session = _activate(engine, request, group)
    preview = engine.lifecycle.preview_retraction(session_id=session, authority=_owner(engine))
    identity = str(preview["preview_id"])
    assert preview["item_pages"] == 2
    page = engine.lifecycle.preview_retraction_page(identity, item_page=1, authority=_owner(engine))
    assert len(page["items"]) == 1  # type: ignore[arg-type]
    for invalid in (-1, 2, True):
        with pytest.raises(RetractionError, match="invalid_arguments"):
            engine.lifecycle.preview_retraction_page(
                identity, item_page=invalid, authority=_owner(engine)
            )
    operation = str(uuid4())
    with closing(engine._store.connect()) as connection:
        projected = connection.execute("SELECT count(*) FROM search_documents").fetchone()[0]
    receipt = engine.lifecycle.commit_retraction(identity, operation, authority=_owner(engine))
    assert receipt["counts"] == preview["counts"]
    with closing(engine._store.connect()) as connection:
        assert (
            connection.execute("SELECT count(*) FROM search_documents").fetchone()[0] == projected
        )
    assert engine.retrieval.search("synthetic member") == ()
    assert engine.lifecycle.retraction_status(operation, authority=_owner(engine)) == receipt


def test_public_session_preview_refuses_a_later_membership_append(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    owner = _owner(engine)
    segments = tuple(
        engine.lifecycle.prepare_segment(
            CaptureSubmission.for_local_owner(
                profile=engine.profile,
                payload=TextPayload(f"member {index}"),
                delivery_id=f"members.{index}",
            ),
            authority=owner,
        )
        for index in range(2)
    )
    groups = tuple(
        GroupIntent(digest(f"group{index}"), digest(f"revision{index}"), None, (segment.binding,))
        for index, segment in enumerate(segments)
    )
    request = _request(engine, "consumer_import", {"groups": [group.value() for group in groups]})
    engine.lifecycle.begin(request, authority=owner)
    session = "capsess_" + str(uuid4())
    for index, (group, segment) in enumerate(zip(groups, segments, strict=True)):
        engine.lifecycle.stage_segment(request, group.group_key, segment, authority=owner)
        engine.lifecycle.activate_group(
            request, group.group_key, session_id=session, authority=owner
        )
        if index == 0:
            identity = str(
                engine.lifecycle.preview_retraction(session_id=session, authority=owner)[
                    "preview_id"
                ]
            )
    with pytest.raises(RetractionError, match="revision_changed"):
        engine.lifecycle.commit_retraction(identity, str(uuid4()), authority=owner)
    assert engine.retrieval.search("member")


def test_journal_only_operation_cannot_be_reused_by_public_commit(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    capture = _capture(engine)
    request = _request(engine, "capture_retract", {"capture_id": capture})
    engine.lifecycle.retract(request, authority=_owner(engine))
    identity = str(
        engine.lifecycle.preview_retraction(capture_id=capture, authority=_owner(engine))[
            "preview_id"
        ]
    )
    with pytest.raises(RetractionError, match="operation_conflict"):
        engine.lifecycle.commit_retraction(identity, request.operation_id, authority=_owner(engine))


def test_pending_approved_copy_refuses_only_affected_previews(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks, request, authority, _ = _managed_source(tmp_path)
    assert tasks.sharing is not None
    preview = tasks.sharing.preview(request, authority=authority)

    def fail(_submission: object) -> None:
        raise RuntimeError("synthetic copy interruption")

    with monkeypatch.context() as patch:
        patch.setattr(tasks.sharing._engine.capture, "submit", fail)
        assert (
            tasks.sharing.decide(_decision(request, preview), authority=authority).copy_capture_id
            is None
        )
    # Reopening intentionally recovers sharing work; inspect the same foreground
    # engine while the accepted copy is still pending instead.
    engine = tasks.sharing._engine
    with pytest.raises(RetractionError, match="operation_pending"):
        engine.lifecycle.preview_retraction(
            capture_id=request.expected_head, authority=_owner(engine)
        )
    unrelated = _capture(engine)
    assert (
        engine.lifecycle.preview_retraction(capture_id=unrelated, authority=_owner(engine))[
            "status"
        ]
        == "preview"
    )


@pytest.mark.parametrize("size", [20_000, 20_001])
def test_public_approval_contract_membership_bound(size: int) -> None:
    captures = ["capture_" + str(uuid4()) for _ in range(size)]
    value = {
        "target": {"kind": "session", "id": "capsess_" + str(uuid4())},
        "capture_ids": captures,
        "membership_version": 1,
        "membership_sha256": digest(captures),
        "items": [
            {"kind": "capture", "id": capture, "effect": "historical_only"}
            for capture in sorted(captures)
        ],
        "counts": {
            "selected_captures": size,
            "already_retracted": 0,
            "current_groups_suppressed": 0,
            "newer_heads_preserved": 0,
            "dependent_pages": 0,
            "approved_copies": 0,
        },
        "preimage_sha256": "a" * 64,
    }
    if size == 20_000:
        assert impact_value(value) == value
    else:
        with pytest.raises(RetractionError, match="invalid_evidence"):
            impact_value(value)

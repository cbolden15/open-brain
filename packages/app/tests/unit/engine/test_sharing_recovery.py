from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from open_brain_engine.engine import open_local_engine
from open_brain_engine.engine.contracts import JournalEnvelope
from open_brain_engine.engine.local_schema import open_local_database_read_only
from open_brain_engine.engine.sharing import EligibilityMode, sharing_eligible
from open_brain_engine.engine.sharing_contracts import (
    SharingDecisionRequest,
    SharingError,
    SharingPreview,
    SharingPreviewRequest,
    SharingRevokeRequest,
)

from packages.app.tests.unit.engine.test_sharing_tasks import _managed_source


def _decision(request: SharingPreviewRequest, preview: SharingPreview) -> SharingDecisionRequest:
    return SharingDecisionRequest(
        operation_id="sharing.recovery.approve",
        preview_id=preview.preview_id,
        preview_sha256=preview.preview_sha256,
        brain_id=request.brain_id,
        issuer_epoch=request.issuer_epoch,
        destination_brain_id=request.brain_id,
        expected_decision_version=0,
        decision="approve",
    )


@pytest.mark.parametrize("accepted_before_loss", [False, True])
def test_sharing_pending_revoke_and_lost_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, accepted_before_loss: bool
) -> None:
    tasks, request, owner, _ = _managed_source(tmp_path)
    assert tasks.sharing is not None
    preview = tasks.sharing.preview(request, authority=owner)
    capture = tasks.sharing._engine.capture
    original_submit = capture.submit

    def lost_response(submission):  # type: ignore[no-untyped-def]
        if accepted_before_loss:
            original_submit(submission)
        raise RuntimeError("synthetic response loss")

    with monkeypatch.context() as patch:
        patch.setattr(capture, "submit", lost_response)
        pending = tasks.sharing.decide(_decision(request, preview), authority=owner)
    assert pending.state == "pending" and pending.copy_capture_id is None
    if accepted_before_loss:
        with open_local_database_read_only(tasks.profile) as connection:
            unlinked = connection.execute(
                "SELECT capture_id FROM captures WHERE delivery_id LIKE 'sharing-copy.v1:%'"
            ).fetchone()
            assert unlinked is not None
            assert connection.execute("SELECT count(*) FROM sharing_links").fetchone()[0] == 0
            assert not sharing_eligible(
                connection,
                unlinked["capture_id"],
                mode=EligibilityMode.EXTERNAL_READ,
                provider_id="openai",
                brain_id=request.brain_id,
                issuer_epoch=request.issuer_epoch,
                profile=tasks.profile,
            )
    resumed = open_local_engine(tasks.profile)
    assert resumed.sharing is not None
    receipt = resumed.sharing.decide(_decision(request, preview), authority=owner)
    assert receipt.copy_capture_id is not None
    assert receipt == resumed.sharing.decide(_decision(request, preview), authority=owner)
    with open_local_database_read_only(tasks.profile) as connection:
        assert connection.execute("SELECT count(*) FROM sharing_links").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT count(*) FROM captures WHERE delivery_id LIKE 'sharing-copy.v1:%'"
            ).fetchone()[0]
            == 1
        )


def test_sharing_marker_cannot_bypass_reservation_or_missing_link(tmp_path: Path) -> None:
    tasks, request, owner, _ = _managed_source(tmp_path)
    assert tasks.sharing is not None
    preview = tasks.sharing.preview(request, authority=owner)
    with open_local_database_read_only(tasks.profile) as connection:
        assert connection.execute("SELECT count(*) FROM sharing_links").fetchone()[0] == 0
    receipt = tasks.sharing.decide(_decision(request, preview), authority=owner)
    assert receipt.copy_capture_id is not None
    with open_local_database_read_only(tasks.profile) as connection:
        raw = connection.execute(
            "SELECT copy_submission_bytes FROM sharing_decisions WHERE approval_id=?",
            (receipt.approval_id,),
        ).fetchone()[0]
    reserved = JournalEnvelope.from_bytes(bytes(raw)).submission
    from dataclasses import replace

    with pytest.raises(ValueError, match="invalid reserved sharing copy"):
        tasks.sharing._engine.capture.submit(
            replace(reserved, delivery_id="sharing-copy.v1:forged")
        )
    with (
        tasks.sharing._engine._store.transaction() as connection,
        pytest.raises(sqlite3.IntegrityError, match="sharing link is immutable"),
    ):
        connection.execute("DELETE FROM sharing_links WHERE approval_id=?", (receipt.approval_id,))


def test_sharing_revoke_while_copy_pending_stays_historical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks, request, owner, _ = _managed_source(tmp_path)
    assert tasks.sharing is not None
    preview = tasks.sharing.preview(request, authority=owner)

    def blocked(_submission: object) -> None:
        raise RuntimeError("synthetic pending copy")

    with monkeypatch.context() as patch:
        patch.setattr(tasks.sharing._engine.capture, "submit", blocked)
        pending = tasks.sharing.decide(_decision(request, preview), authority=owner)
    assert pending.state == "pending"
    tasks.sharing.revoke(
        SharingRevokeRequest(
            operation_id="sharing.recovery.revoke",
            approval_id=pending.approval_id,
            expected_approval_version=1,
            brain_id=request.brain_id,
            issuer_epoch=request.issuer_epoch,
            destination_brain_id=request.brain_id,
            reason="owner_choice",
        ),
        authority=owner,
    )
    reopened = open_local_engine(tasks.profile)
    assert reopened.sharing is not None
    terminal = reopened.sharing.decide(_decision(request, preview), authority=owner)
    assert terminal.state == "history_only" and terminal.copy_capture_id is not None
    with open_local_database_read_only(tasks.profile) as connection:
        assert not sharing_eligible(
            connection,
            terminal.copy_capture_id,
            mode=EligibilityMode.EXTERNAL_READ,
            provider_id="openai",
            brain_id=request.brain_id,
            issuer_epoch=request.issuer_epoch,
            profile=tasks.profile,
        )


@pytest.mark.parametrize(
    "fault_stage", ["before_reservation", "before_admission", "after_acceptance", "after_link"]
)
def test_sharing_copy_journal_link_crash_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault_stage: str
) -> None:
    from open_brain_engine.engine import sharing as sharing_module

    tasks, request, owner, _ = _managed_source(tmp_path)
    assert tasks.sharing is not None
    preview = tasks.sharing.preview(request, authority=owner)
    decision = _decision(request, preview)
    capture = tasks.sharing._engine.capture
    original_submit = capture.submit
    original_complete = tasks.sharing._complete_approval

    def failed_original(*_args: object) -> None:
        raise SharingError("binding_mismatch")

    def failed_submission(submission):  # type: ignore[no-untyped-def]
        if fault_stage == "after_acceptance":
            original_submit(submission)
        raise RuntimeError("synthetic journal response loss")

    def linked_then_lost(approval_id: str):  # type: ignore[no-untyped-def]
        original_complete(approval_id)
        raise RuntimeError("synthetic link response loss")

    with monkeypatch.context() as patch:
        if fault_stage == "before_reservation":
            patch.setattr(sharing_module, "_original_evidence", failed_original)
            with pytest.raises(SharingError, match="binding_mismatch"):
                tasks.sharing.decide(decision, authority=owner)
        elif fault_stage == "after_link":
            patch.setattr(tasks.sharing, "_complete_approval", linked_then_lost)
            with pytest.raises(RuntimeError, match="synthetic link response loss"):
                tasks.sharing.decide(decision, authority=owner)
        else:
            patch.setattr(capture, "submit", failed_submission)
            pending = tasks.sharing.decide(decision, authority=owner)
            assert pending.state == "pending" and pending.copy_capture_id is None
    with open_local_database_read_only(tasks.profile) as connection:
        count = connection.execute("SELECT count(*) FROM sharing_decisions").fetchone()[0]
        assert count == (0 if fault_stage == "before_reservation" else 1)
    resumed = open_local_engine(tasks.profile)
    assert resumed.sharing is not None
    receipt = resumed.sharing.decide(decision, authority=owner)
    assert receipt.copy_capture_id is not None
    assert receipt == resumed.sharing.decide(decision, authority=owner)
    with open_local_database_read_only(tasks.profile) as connection:
        assert connection.execute("SELECT count(*) FROM sharing_links").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT count(*) FROM captures WHERE delivery_id LIKE 'sharing-copy.v1:%'"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize(
    "fault_stage",
    ["link", "receipt", "linked_response", "revocation", "version", "revoked_response"],
)
def test_sharing_actual_sql_atomicity_and_committed_response_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault_stage: str
) -> None:
    """Fault after real SQL, not before/after the whole completion wrapper."""
    tasks, request, owner, _ = _managed_source(tmp_path)
    assert tasks.sharing is not None
    preview = tasks.sharing.preview(request, authority=owner)
    decision = _decision(request, preview)
    revoking = fault_stage in {"revocation", "version", "revoked_response"}
    approved = tasks.sharing.decide(decision, authority=owner) if revoking else None
    revoke_request = (
        None
        if approved is None
        else SharingRevokeRequest(
            operation_id="sharing.transaction.revoke",
            approval_id=approved.approval_id,
            expected_approval_version=1,
            brain_id=request.brain_id,
            issuer_epoch=request.issuer_epoch,
            destination_brain_id=request.brain_id,
            reason="owner_choice",
        )
    )
    prefix = {
        "link": "INSERT INTO sharing_links",
        "receipt": "UPDATE sharing_decisions SET copy_capture_id",
        "linked_response": "UPDATE sharing_decisions SET copy_capture_id",
        "revocation": "INSERT INTO sharing_revocations",
        "version": "UPDATE sharing_decisions SET approval_version",
        "revoked_response": "UPDATE sharing_decisions SET approval_version",
    }[fault_stage]
    committed_loss = fault_stage in {"linked_response", "revoked_response"}
    store = tasks.sharing._engine._store
    transaction = store.transaction
    reached = False
    fired = False

    class SqlBoundary:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self.connection = connection

        def __getattr__(self, name: str) -> Any:
            return getattr(self.connection, name)

        def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
            nonlocal reached, fired
            cursor = self.connection.execute(sql, parameters)
            if not fired and sql.startswith(prefix):
                reached = True
                if not committed_loss:
                    fired = True
                    raise RuntimeError("synthetic transaction boundary loss")
            return cursor

    @contextmanager
    def faulted_transaction() -> Iterator[sqlite3.Connection]:
        nonlocal fired
        with transaction() as connection:
            yield cast(sqlite3.Connection, SqlBoundary(connection))
        if reached and committed_loss and not fired:
            fired = True
            raise RuntimeError("synthetic committed response loss")

    with monkeypatch.context() as patch:
        patch.setattr(store, "transaction", faulted_transaction)
        with pytest.raises(RuntimeError, match="synthetic .* loss"):
            if revoke_request is None:
                tasks.sharing.decide(decision, authority=owner)
            else:
                tasks.sharing.revoke(revoke_request, authority=owner)
    assert reached and fired
    with open_local_database_read_only(tasks.profile) as connection:
        row = connection.execute("SELECT * FROM sharing_decisions").fetchone()
        retained_request = bytes(row["request_bytes"])
        retained_envelope = bytes(row["copy_submission_bytes"])
        retained_delivery = row["copy_delivery_id"]
        retained_receipt = bytes(row["receipt_bytes"])
        capture = connection.execute(
            "SELECT capture_id FROM captures WHERE delivery_id=?", (retained_delivery,)
        ).fetchone()[0]
        expected_links = 1 if revoking or committed_loss else 0
        assert (
            connection.execute("SELECT count(*) FROM sharing_links").fetchone()[0] == expected_links
        )
        assert connection.execute("SELECT count(*) FROM sharing_revocations").fetchone()[0] == (
            1 if fault_stage == "revoked_response" else 0
        )
        assert row["approval_version"] == (2 if fault_stage == "revoked_response" else 1)
        eligible = sharing_eligible(
            connection,
            capture,
            mode=EligibilityMode.EXTERNAL_READ,
            provider_id="openai",
            brain_id=request.brain_id,
            issuer_epoch=request.issuer_epoch,
            profile=tasks.profile,
        )
        assert eligible is (fault_stage in {"linked_response", "revocation", "version"})
        if not revoking and not committed_loss:
            assert row["copy_capture_id"] is None
    reopened = open_local_engine(tasks.profile)
    assert reopened.sharing is not None
    terminal = reopened.sharing.decide(decision, authority=owner)
    assert terminal.copy_capture_id == capture
    assert reopened.sharing.decide(decision, authority=owner) == terminal
    if revoke_request is not None:
        revoked = reopened.sharing.revoke(revoke_request, authority=owner)
        assert revoked.state == "revoked"
        assert reopened.sharing.revoke(revoke_request, authority=owner) == revoked
    with open_local_database_read_only(tasks.profile) as connection:
        row = connection.execute("SELECT * FROM sharing_decisions").fetchone()
        assert bytes(row["request_bytes"]) == retained_request
        assert bytes(row["copy_submission_bytes"]) == retained_envelope
        assert row["copy_delivery_id"] == retained_delivery
        if revoking or committed_loss:
            assert bytes(row["receipt_bytes"]) == retained_receipt
        assert connection.execute("SELECT count(*) FROM sharing_links").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT count(*) FROM captures WHERE delivery_id=?", (retained_delivery,)
            ).fetchone()[0]
            == 1
        )
        assert sharing_eligible(
            connection,
            capture,
            mode=EligibilityMode.EXTERNAL_READ,
            provider_id="openai",
            brain_id=request.brain_id,
            issuer_epoch=request.issuer_epoch,
            profile=tasks.profile,
        ) is (not revoking)

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import cast

import open_brain_engine.engine.capture as capture_module
import pytest
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import (
    BrainEngine,
    CaptureCustodyReceipt,
    CaptureProtectionPending,
    CaptureProtectionPendingError,
    CaptureReceipt,
    CaptureSubmission,
    ProtectionAcknowledgement,
    ReceiptProtectionError,
    ReceiptProtectionRequest,
    TextPayload,
)
from open_brain_engine.engine.local_schema import PHASE1_STATE_DATABASE
from open_brain_engine.engine.t03_contracts import EffectiveAuthority
from open_brain_engine.storage.locks import FileLease

from open_brain.profile import compile_single_user_local


class _Protector:
    def __init__(self, failure: str | None = None) -> None:
        self.failure = failure
        self.requests: list[ReceiptProtectionRequest] = []

    def protect(
        self,
        request: ReceiptProtectionRequest,
        *,
        timeout_seconds: float,
    ) -> ProtectionAcknowledgement:
        assert timeout_seconds == 0.25
        self.requests.append(request)
        if self.failure is not None:
            raise ReceiptProtectionError(self.failure)
        return ProtectionAcknowledgement.for_request(
            request,
            protection_reference=f"protected:{request.commitment_sha256}",
            protected_at="2026-09-24T12:00:00Z",
        )


def _identity(root: Path) -> tuple[str, int]:
    with sqlite3.connect(root / PHASE1_STATE_DATABASE) as connection:
        row = connection.execute("SELECT brain_id, issuer_epoch FROM brain_identity").fetchone()
    assert row is not None
    return cast(str, row[0]), cast(int, row[1])


def _submission(engine: BrainEngine, delivery_id: str) -> CaptureSubmission:
    brain_id, issuer_epoch = _identity(engine.profile.root)
    authority = EffectiveAuthority(
        principal_id="receipt-protection-test",
        session_id="receipt-protection-session",
        capabilities=frozenset({"capture-submit"}),
        space_ids=None,
        allowed_capture_tiers=frozenset({PrivacyTier.WORK}),
        brain_id=brain_id,
        issuer_epoch=issuer_epoch,
    )
    return CaptureSubmission.for_destination_bound(
        profile=engine.profile,
        authority=authority,
        payload=TextPayload("receipt protection replay body"),
        delivery_id=delivery_id,
        requested_tier=PrivacyTier.WORK,
    )


def _engine(root: Path, protector: _Protector) -> BrainEngine:
    return BrainEngine.open(
        compile_single_user_local(root),
        receipt_protection_port=protector,
        receipt_protection_timeout_seconds=0.25,
    )


def test_accepted_and_duplicate_receipts_require_the_same_exact_replay_protection(
    tmp_path: Path,
) -> None:
    protector = _Protector()
    engine = _engine(tmp_path / "brain", protector)
    submission = _submission(engine, "delivery.protection.accepted")

    accepted = engine.capture.submit(submission)
    duplicate = engine.capture.submit(submission)

    assert isinstance(accepted, CaptureReceipt)
    assert isinstance(duplicate, CaptureReceipt)
    assert duplicate.duplicate is True
    assert accepted.protection_acknowledgement is not None
    assert duplicate.protection_acknowledgement is not None
    assert len(protector.requests) == 2
    assert protector.requests[0].replay_bytes == protector.requests[1].replay_bytes
    assert protector.requests[0].commitment_sha256 == protector.requests[1].commitment_sha256
    with sqlite3.connect(engine.profile.root / PHASE1_STATE_DATABASE) as connection:
        assert connection.execute("SELECT count(*) FROM captures").fetchone() == (1,)


def test_protection_failure_returns_retryable_pending_then_replay_protects_one_capture(
    tmp_path: Path,
) -> None:
    protector = _Protector("protection_unavailable")
    engine = _engine(tmp_path / "brain", protector)
    submission = _submission(engine, "delivery.protection.retry")

    with pytest.raises(CaptureProtectionPendingError) as raised:
        engine.capture.submit(submission)
    pending = raised.value.result
    assert isinstance(pending, CaptureProtectionPending)
    assert pending.status == "recovery_pending"
    assert pending.retryable is True

    protector.failure = None
    protected = engine.capture.submit(submission)

    assert isinstance(protected, CaptureReceipt)
    assert protected.duplicate is True
    assert protected.protection_acknowledgement is not None
    with sqlite3.connect(engine.profile.root / PHASE1_STATE_DATABASE) as connection:
        assert connection.execute("SELECT count(*) FROM captures").fetchone() == (1,)


def test_queued_custody_is_not_released_without_independent_protection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(capture_module, "_WRITER_WAIT_TIMEOUT_SECONDS", 0.01)
    protector = _Protector()
    engine = _engine(tmp_path / "brain", protector)
    submission = _submission(engine, "delivery.protection.queued")

    with FileLease(
        engine.profile.root / ".open-brain", "receipt-protection-competing-writer"
    ).acquire_shared_writer():
        queued = engine.capture.submit(submission)

    assert isinstance(queued, CaptureCustodyReceipt)
    assert queued.protection_acknowledgement is not None
    assert protector.requests[0].replay_bytes
    with sqlite3.connect(engine.profile.root / PHASE1_STATE_DATABASE) as connection:
        assert connection.execute("SELECT count(*) FROM captures").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM capture_ingestion_items").fetchone() == (1,)


def test_invalid_acknowledgement_is_recovery_pending(tmp_path: Path) -> None:
    class _WrongProtector(_Protector):
        def protect(
            self,
            request: ReceiptProtectionRequest,
            *,
            timeout_seconds: float,
        ) -> ProtectionAcknowledgement:
            return cast(ProtectionAcknowledgement, object())

    protector = _WrongProtector()
    engine = _engine(tmp_path / "brain", protector)
    submission = _submission(engine, "delivery.protection.invalid")

    with pytest.raises(CaptureProtectionPendingError) as raised:
        engine.capture.submit(submission)

    assert raised.value.result.reason_code == "protection_invalid_acknowledgement"

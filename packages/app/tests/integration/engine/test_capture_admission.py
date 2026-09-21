from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.core.models import (
    Authority,
    CaptureWhyOrigin,
    ContentOrigin,
    PrivacyDecision,
    PrivacyReason,
    PrivacyTier,
    Provenance,
)
from open_brain_engine.engine import (
    AdmissionLimits,
    BrainEngine,
    CaptureAdmissionError,
    CaptureAdmissionResult,
    CaptureSubmission,
    FilePayload,
    PublicJobCaptureContext,
    TextPayload,
)
from open_brain_engine.engine import (
    capture as capture_module,
)
from open_brain_engine.storage.locks import (
    _PROCESS_WRITER_WAITERS,
    FileLease,
    LockBusyError,
)

from open_brain.profile import compile_single_user_local

ENVELOPE_ONLY_LIMITS = AdmissionLimits(max_envelope_bytes=64, max_body_bytes=64)
BODY_ONLY_LIMITS = AdmissionLimits(max_envelope_bytes=8192, max_body_bytes=64)


def _engine(
    root: Path,
    *,
    limits: AdmissionLimits | None = None,
    clock: Callable[[], datetime] | None = None,
) -> BrainEngine:
    return BrainEngine.open(compile_single_user_local(root), admission_limits=limits, clock=clock)


def _count(root: Path, table: str) -> int:
    connection = sqlite3.connect(root / ".open-brain/state/phase1.sqlite3")
    try:
        return cast(int, connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        connection.close()


def _capture_artifacts(root: Path) -> int:
    """Count admitted blob and capture-record files; bootstrap writes logical-sources.json."""
    total = 0
    for relative in ("blobs", "captures"):
        tree = root / "sources" / relative
        if tree.exists():
            total += sum(1 for path in tree.rglob("*") if path.is_file())
    return total


def _assert_nothing_was_admitted(root: Path) -> None:
    assert _count(root, "captures") == 0
    assert _count(root, "search_documents") == 0
    assert _capture_artifacts(root) == 0


def test_oversized_envelope_text_capture_is_refused_before_materialization(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    engine = _engine(root, limits=ENVELOPE_ONLY_LIMITS)
    with pytest.raises(CaptureAdmissionError) as raised:
        engine.capture.accept(TextPayload("synthetic"), delivery_id="admission-envelope-1")
    assert raised.value.result is CaptureAdmissionResult.ENVELOPE_TOO_LARGE
    assert raised.value.retryable is False
    _assert_nothing_was_admitted(root)


def test_oversized_body_text_capture_is_refused_before_materialization(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    engine = _engine(root, limits=BODY_ONLY_LIMITS)
    with pytest.raises(CaptureAdmissionError) as raised:
        engine.capture.accept(
            TextPayload("synthetic-body " * 10), delivery_id="admission-text-body-1"
        )
    assert raised.value.result is CaptureAdmissionResult.BODY_TOO_LARGE
    assert raised.value.retryable is False
    _assert_nothing_was_admitted(root)


def test_oversized_file_capture_is_refused_before_materialization(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    engine = _engine(root, limits=BODY_ONLY_LIMITS)
    with pytest.raises(CaptureAdmissionError) as raised:
        engine.capture.accept(
            FilePayload("synthetic.bin", "application/octet-stream", b"s" * 128),
            delivery_id="admission-file-1",
        )
    assert raised.value.result is CaptureAdmissionResult.BODY_TOO_LARGE
    assert raised.value.retryable is False
    _assert_nothing_was_admitted(root)


def test_the_same_submissions_succeed_under_default_limits(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    engine = _engine(root)
    text_receipt = engine.capture.accept(
        TextPayload("synthetic-body " * 10), delivery_id="admission-text-body-1"
    )
    file_receipt = engine.capture.accept(
        FilePayload("synthetic.bin", "application/octet-stream", b"s" * 128),
        delivery_id="admission-file-1",
    )
    assert text_receipt.duplicate is False
    assert file_receipt.duplicate is False
    assert _count(root, "captures") == 2
    assert _count(root, "search_documents") >= 1
    assert _capture_artifacts(root) >= 1


def test_markdown_import_of_an_oversized_note_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "oversized.md").write_text(
        "# Oversized\n" + "synthetic-import-body " * 12, encoding="utf-8"
    )
    engine = _engine(root, limits=BODY_ONLY_LIMITS)
    with pytest.raises(CaptureAdmissionError) as raised:
        engine.markdown_import.import_directory(str(vault), confirm=lambda _: True)
    assert raised.value.result is CaptureAdmissionResult.BODY_TOO_LARGE
    assert raised.value.retryable is False
    _assert_nothing_was_admitted(root)


def test_markdown_import_of_a_small_note_still_succeeds(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "small.md").write_text("# Tiny\nsynthetic-tiny-token\n", encoding="utf-8")
    engine = _engine(root, limits=BODY_ONLY_LIMITS)
    summary = engine.markdown_import.import_directory(str(vault), confirm=lambda _: True)
    assert summary.imported == 1
    assert _count(root, "captures") == 1
    assert _count(root, "search_documents") >= 1
    assert engine.retrieval.search("synthetic-tiny-token") != ()


PUBLIC_ONCE_LIMITS = AdmissionLimits(requests_per_minute_per_principal=1)
OWNER_ONCE_LIMITS = AdmissionLimits(requests_per_minute_per_principal=1)
CONCURRENCY_LIMITS = AdmissionLimits(max_concurrent_admissions=1)
WAITER_CAP_LIMITS = AdmissionLimits(max_writer_waiters=2)
SYNTHETIC_SOURCE_REFERENCE = "https://example.test/synthetic-admission"
_PUBLIC_ACTOR_ID = "actor_00000000-0000-4000-8000-000000000601"

_LOCK_HOLDER_SCRIPT = """
import fcntl, sys, time
with open(sys.argv[1], "r+b") as handle:
    fcntl.lockf(handle, fcntl.LOCK_EX)
    print("held", flush=True)
    time.sleep(float(sys.argv[2]))
"""


class _FakeClock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _engine_lock_path(root: Path) -> Path:
    return root / ".open-brain" / ".open-brain-locks" / "lease.shared-writer"


def _hold_engine_writer(root: Path, seconds: float) -> subprocess.Popen[bytes]:
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _LOCK_HOLDER_SCRIPT,
            str(_engine_lock_path(root)),
            str(seconds),
        ],
        stdout=subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == b"held"
    return process


def _finish_holder(process: subprocess.Popen[bytes]) -> None:
    process.wait()
    if process.stdout is not None:
        process.stdout.close()


def _wait_for_engine_waiters(root: Path, expected: int) -> None:
    metadata = os.stat(root / ".open-brain")
    key = (metadata.st_dev, metadata.st_ino, "shared-writer")
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if _PROCESS_WRITER_WAITERS.get(key, 0) >= expected:
            return
        time.sleep(0.005)
    raise AssertionError(f"expected {expected} registered engine writer waiters")


def _public_job_submission(engine: BrainEngine, delivery_id: str) -> CaptureSubmission:
    profile = engine.profile
    context = PublicJobCaptureContext.create(
        profile=profile,
        actor_id=_PUBLIC_ACTOR_ID,
        role_claim={
            "actor_id": _PUBLIC_ACTOR_ID,
            "capabilities": ["capture.accept"],
            "role_claim_id": "role_claim_00000000-0000-4000-8000-000000000602",
            "role_id": "role_00000000-0000-4000-8000-000000000603",
            "tenant_id": profile.tenant_id,
        },
    )
    return CaptureSubmission.for_public_job(
        context=context,
        payload=TextPayload("synthetic public-job capture"),
        delivery_id=delivery_id,
        source_origin=ContentOrigin.THIRD_PARTY,
        source_reference=SYNTHETIC_SOURCE_REFERENCE,
        provenance=Provenance.create(
            source_ref=SYNTHETIC_SOURCE_REFERENCE,
            content_origin=ContentOrigin.THIRD_PARTY,
            owner_context=CaptureWhyOrigin.AUTOMATION_ABSENT,
        ),
        privacy=PrivacyDecision.create(
            tier=PrivacyTier.WORK,
            reason=PrivacyReason.POLICY_WORK,
            policy_version="privacy-v1",
            authority=Authority(cloud=False, external_egress=False),
        ),
    )


def test_owner_captures_are_not_rate_limited(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    engine = _engine(root, limits=OWNER_ONCE_LIMITS)
    for index in range(200):
        engine.capture.accept(TextPayload("synthetic"), delivery_id=f"admission-owner-{index}")
    assert _count(root, "captures") == 200


def test_markdown_import_is_not_rate_limited(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    first_vault = tmp_path / "first"
    second_vault = tmp_path / "second"
    for vault in (first_vault, second_vault):
        vault.mkdir()
        (vault / "note.md").write_text("# Note\nsynthetic-note-token\n", encoding="utf-8")
    engine = _engine(root, limits=PUBLIC_ONCE_LIMITS)
    first = engine.markdown_import.import_directory(str(first_vault), confirm=lambda _: True)
    second = engine.markdown_import.import_directory(str(second_vault), confirm=lambda _: True)
    assert first.imported == 1
    assert second.imported == 1
    assert _count(root, "captures") == 2


def test_public_job_rate_limited_while_owner_path_still_succeeds(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    engine = _engine(root, limits=PUBLIC_ONCE_LIMITS)
    first = engine.capture.submit(_public_job_submission(engine, "admission-public-1"))
    assert first.duplicate is False
    with pytest.raises(CaptureAdmissionError) as raised:
        engine.capture.submit(_public_job_submission(engine, "admission-public-2"))
    assert raised.value.result is CaptureAdmissionResult.RATE_LIMITED
    assert raised.value.retryable is True
    owner = engine.capture.accept(TextPayload("synthetic"), delivery_id="admission-owner-1")
    assert owner.duplicate is False
    assert _count(root, "captures") == 2


def test_public_job_rate_window_recovery_needs_no_engine_reopen(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    clock = _FakeClock()
    engine = _engine(root, limits=PUBLIC_ONCE_LIMITS, clock=clock)
    engine.capture.submit(_public_job_submission(engine, "admission-public-1"))
    with pytest.raises(CaptureAdmissionError) as raised:
        engine.capture.submit(_public_job_submission(engine, "admission-public-2"))
    assert raised.value.result is CaptureAdmissionResult.RATE_LIMITED
    clock.advance(61.0)
    engine.capture.submit(_public_job_submission(engine, "admission-public-3"))
    assert _count(root, "captures") == 2


def test_concurrent_admissions_beyond_the_cap_get_admission_busy(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    engine = _engine(root, limits=CONCURRENCY_LIMITS)
    submission = _public_job_submission(engine, "admission-busy-1")
    principal_key = capture_module._principal_key(submission.tenant_id, submission.actor_id)
    with engine._admit_before_writer(principal_key):
        with pytest.raises(CaptureAdmissionError) as raised:
            engine.capture.submit(submission)
        assert raised.value.result is CaptureAdmissionResult.ADMISSION_BUSY
        assert raised.value.retryable is True
        owner = engine.capture.accept(TextPayload("synthetic"), delivery_id="admission-owner-1")
        assert owner.duplicate is False
    assert _count(root, "captures") == 1
    engine.capture.submit(submission)
    assert _count(root, "captures") == 2


def test_concurrency_counter_releases_on_success_and_on_exception(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    engine = _engine(root, limits=CONCURRENCY_LIMITS)
    with engine._admit_before_writer("synthetic:principal"):
        pass
    with pytest.raises(RuntimeError), engine._admit_before_writer("synthetic:principal"):
        raise RuntimeError("synthetic gate failure")
    with engine._admit_before_writer("synthetic:principal"):
        pass
    _assert_nothing_was_admitted(root)


def test_capture_waits_for_the_writer_lease_and_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(capture_module, "_WRITER_WAIT_TIMEOUT_SECONDS", 5.0)
    root = tmp_path / "brain"
    engine = _engine(root)
    engine.capture.submit(_public_job_submission(engine, "admission-wait-1"))
    holder = _hold_engine_writer(root, 0.4)
    try:
        receipt = engine.capture.submit(_public_job_submission(engine, "admission-wait-2"))
        assert receipt.duplicate is False
    finally:
        _finish_holder(holder)
    assert _count(root, "captures") == 2


def test_capture_past_the_wait_deadline_is_writer_queue_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(capture_module, "_WRITER_WAIT_TIMEOUT_SECONDS", 0.05)
    root = tmp_path / "brain"
    engine = _engine(root)
    engine.capture.submit(_public_job_submission(engine, "admission-queue-1"))
    holder = _hold_engine_writer(root, 0.8)
    try:
        with pytest.raises(CaptureAdmissionError) as raised:
            engine.capture.submit(_public_job_submission(engine, "admission-queue-2"))
        assert raised.value.result is CaptureAdmissionResult.WRITER_QUEUE_FULL
        assert raised.value.retryable is True
        assert _count(root, "captures") == 1
        assert _count(root, "search_documents") == 1
        assert _capture_artifacts(root) == 1
    finally:
        _finish_holder(holder)
    engine.capture.submit(_public_job_submission(engine, "admission-queue-2"))
    assert _count(root, "captures") == 2


def test_writer_waiters_beyond_the_cap_are_rejected_and_earlier_waiters_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(capture_module, "_WRITER_WAIT_TIMEOUT_SECONDS", 5.0)
    root = tmp_path / "brain"
    engine = _engine(root, limits=WAITER_CAP_LIMITS)
    engine.capture.submit(_public_job_submission(engine, "admission-cap-1"))
    holder = _hold_engine_writer(root, 0.8)
    failures: list[BaseException] = []

    def wait_and_capture(delivery_id: str) -> None:
        try:
            engine.capture.submit(_public_job_submission(engine, delivery_id))
        except BaseException as error:  # noqa: BLE001
            failures.append(error)

    threads = [
        threading.Thread(target=wait_and_capture, args=(f"admission-cap-{index}",))
        for index in range(2, 4)
    ]
    try:
        for thread in threads:
            thread.start()
        _wait_for_engine_waiters(root, 2)
        with pytest.raises(CaptureAdmissionError) as raised:
            engine.capture.submit(_public_job_submission(engine, "admission-cap-4"))
        assert raised.value.result is CaptureAdmissionResult.WRITER_QUEUE_FULL
    finally:
        for thread in threads:
            thread.join()
        _finish_holder(holder)
    assert failures == []
    assert not _PROCESS_WRITER_WAITERS
    assert _count(root, "captures") == 3
    engine.capture.submit(_public_job_submission(engine, "admission-cap-4"))
    assert _count(root, "captures") == 4


def test_owner_submit_fails_fast_while_a_competing_writer_holds_the_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    engine = _engine(root)
    owner_submission = CaptureSubmission.for_local_owner(
        profile=engine.profile,
        payload=TextPayload("synthetic owner submit"),
        delivery_id="admission-owner-lease-1",
    )
    started = time.monotonic()
    with (
        FileLease(root / ".open-brain", "admission-competing-writer").acquire_shared_writer(),
        pytest.raises(LockBusyError, match="lease already held"),
    ):
        engine.capture.submit(owner_submission)
    assert time.monotonic() - started < 0.5
    _assert_nothing_was_admitted(root)

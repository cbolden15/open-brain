from __future__ import annotations

import json
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
from open_brain_engine.engine.t03_contracts import EffectiveAuthority
from open_brain_engine.storage.locks import (
    _PROCESS_WRITER_WAITERS,
    FileLease,
    LockBusyError,
)
from open_brain_engine.storage.watermarks import StorageUsage

from open_brain.profile import compile_single_user_local

ENVELOPE_ONLY_LIMITS = AdmissionLimits(max_envelope_bytes=64, max_body_bytes=64)
BODY_ONLY_LIMITS = AdmissionLimits(max_envelope_bytes=8192, max_body_bytes=64)


def _engine(
    root: Path,
    *,
    limits: AdmissionLimits | None = None,
    clock: Callable[[], datetime] | None = None,
    storage_probe: Callable[[Path], StorageUsage] | None = None,
    boundary_classifier: Callable[[CaptureSubmission], PrivacyTier | None] | None = None,
) -> BrainEngine:
    return BrainEngine.open(
        compile_single_user_local(root),
        admission_limits=limits,
        clock=clock,
        storage_probe=storage_probe,
        boundary_classifier=boundary_classifier,
    )


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


def test_markdown_import_refusal_leaves_no_reservation_row_for_the_note(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a-admitted.md").write_text("# Admitted\nsynthetic-import-body\n", encoding="utf-8")
    (vault / "z-refused.md").write_text(
        "# Refused\n" + "synthetic-import-body " * 12, encoding="utf-8"
    )
    engine = _engine(root, limits=BODY_ONLY_LIMITS)
    with pytest.raises(CaptureAdmissionError) as raised:
        engine.markdown_import.import_directory(str(vault), confirm=lambda _: True)
    assert raised.value.result is CaptureAdmissionResult.BODY_TOO_LARGE
    connection = sqlite3.connect(root / ".open-brain/state/phase1.sqlite3")
    try:
        refused_files = connection.execute(
            "SELECT COUNT(*) FROM markdown_import_files WHERE relative_path = 'z-refused.md'"
        ).fetchone()[0]
        refused_revisions = connection.execute(
            "SELECT COUNT(*) FROM markdown_import_revisions AS r "
            "JOIN markdown_import_files AS f ON r.file_id = f.file_id "
            "WHERE f.relative_path = 'z-refused.md'"
        ).fetchone()[0]
        admitted_files = connection.execute(
            "SELECT COUNT(*) FROM markdown_import_files WHERE relative_path = 'a-admitted.md'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert refused_files == 0
    assert refused_revisions == 0
    assert admitted_files == 1
    assert _count(root, "markdown_import_files") == 1
    assert _count(root, "markdown_import_revisions") == 1
    assert _count(root, "captures") == 1


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


class _FakeUsageProbe:
    """Injectable storage probe so tests fake full disks without filling one."""

    def __init__(self, usage: StorageUsage) -> None:
        self.usage = usage

    def __call__(self, path: Path) -> StorageUsage:
        return self.usage


_GIB = 1024 * 1024 * 1024
_MIB = 1024 * 1024


def _usage_with_free(free_bytes: int) -> StorageUsage:
    total_bytes = 100 * _GIB
    return StorageUsage(
        total_bytes=total_bytes, used_bytes=total_bytes - free_bytes, free_bytes=free_bytes
    )


def test_critical_watermark_rejects_an_owner_capture_without_partial_state(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    engine = _engine(root, storage_probe=_FakeUsageProbe(_usage_with_free(400 * _MIB)))
    with pytest.raises(CaptureAdmissionError) as raised:
        engine.capture.accept(TextPayload("synthetic"), delivery_id="admission-critical-owner-1")
    assert raised.value.result is CaptureAdmissionResult.STORAGE_CRITICAL
    assert raised.value.retryable is False
    _assert_nothing_was_admitted(root)


def test_critical_watermark_rejects_a_public_job_capture_without_partial_state(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    engine = _engine(root, storage_probe=_FakeUsageProbe(_usage_with_free(400 * _MIB)))
    with pytest.raises(CaptureAdmissionError) as raised:
        engine.capture.submit(_public_job_submission(engine, "admission-critical-public-1"))
    assert raised.value.result is CaptureAdmissionResult.STORAGE_CRITICAL
    assert raised.value.retryable is False
    _assert_nothing_was_admitted(root)


def test_critical_watermark_rejects_markdown_import_without_partial_state(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("# Note\nsynthetic-critical-token\n", encoding="utf-8")
    engine = _engine(root, storage_probe=_FakeUsageProbe(_usage_with_free(400 * _MIB)))
    with pytest.raises(CaptureAdmissionError) as raised:
        engine.markdown_import.import_directory(str(vault), confirm=lambda _: True)
    assert raised.value.result is CaptureAdmissionResult.STORAGE_CRITICAL
    assert raised.value.retryable is False
    _assert_nothing_was_admitted(root)


def test_high_watermark_rejects_with_a_retryable_result(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    engine = _engine(root, storage_probe=_FakeUsageProbe(_usage_with_free(_GIB)))
    with pytest.raises(CaptureAdmissionError) as raised:
        engine.capture.accept(TextPayload("synthetic"), delivery_id="admission-high-owner-1")
    assert raised.value.result is CaptureAdmissionResult.STORAGE_HIGH
    assert raised.value.retryable is True
    _assert_nothing_was_admitted(root)


def test_free_space_returning_above_high_admits_without_reopening_the_engine(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    probe = _FakeUsageProbe(_usage_with_free(_GIB))
    engine = _engine(root, storage_probe=probe)
    with pytest.raises(CaptureAdmissionError):
        engine.capture.accept(TextPayload("synthetic"), delivery_id="admission-recover-1")
    probe.usage = _usage_with_free(60 * _GIB)
    receipt = engine.capture.accept(TextPayload("synthetic"), delivery_id="admission-recover-1")
    assert receipt.duplicate is False
    assert _count(root, "captures") == 1
    assert _count(root, "search_documents") >= 1


def _classifier_returning(
    tier: PrivacyTier | None,
) -> Callable[[CaptureSubmission], PrivacyTier | None]:
    def classify(submission: CaptureSubmission) -> PrivacyTier | None:
        return tier

    return classify


def _capture_column(root: Path, capture_id: str, column: str) -> object:
    connection = sqlite3.connect(root / ".open-brain/state/phase1.sqlite3")
    try:
        row = connection.execute(
            f"SELECT {column} FROM captures WHERE capture_id = ?", (capture_id,)
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    return row[0]


def _stored_privacy_tier(root: Path, capture_id: str) -> str:
    privacy = json.loads(cast(str, _capture_column(root, capture_id, "privacy_json")))
    assert isinstance(privacy, dict)
    return cast(str, privacy["tier"])


def _search_effective_tier(root: Path, capture_id: str) -> str:
    connection = sqlite3.connect(root / ".open-brain/state/phase1.sqlite3")
    try:
        row = connection.execute(
            "SELECT effective_tier FROM search_documents "
            "WHERE capture_id = ? AND record_type = 'source'",
            (capture_id,),
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    return cast(str, row[0])


def test_secret_boundary_signal_narrows_a_work_public_job_capture(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    engine = _engine(root, boundary_classifier=_classifier_returning(PrivacyTier.SECRET))
    submission = _public_job_submission(engine, "admission-narrow-public-1")
    requested_digest = submission.request_sha256()

    receipt = engine.capture.submit(submission)

    assert receipt.requested_tier is PrivacyTier.WORK
    assert receipt.final_admitted_tier is PrivacyTier.SECRET
    assert _stored_privacy_tier(root, receipt.capture_id) == "secret"
    assert _search_effective_tier(root, receipt.capture_id) == "secret"
    # The immutable request digest still binds the requested WORK decision.
    assert _capture_column(root, receipt.capture_id, "request_sha256") == requested_digest


def test_public_boundary_signal_never_widens_a_personal_capture(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    engine = _engine(root, boundary_classifier=_classifier_returning(PrivacyTier.PUBLIC))
    receipt = engine.capture.accept(
        TextPayload("synthetic"), delivery_id="admission-narrow-owner-public-1"
    )
    assert receipt.requested_tier is PrivacyTier.PERSONAL
    assert receipt.final_admitted_tier is PrivacyTier.PERSONAL
    assert _stored_privacy_tier(root, receipt.capture_id) == "personal"
    assert _search_effective_tier(root, receipt.capture_id) == "personal"


def test_without_a_classifier_receipts_carry_equal_requested_and_final_tiers(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    engine = _engine(root)
    owner = engine.capture.accept(TextPayload("synthetic"), delivery_id="admission-tiers-owner-1")
    public = engine.capture.submit(_public_job_submission(engine, "admission-tiers-public-1"))
    assert owner.requested_tier is PrivacyTier.PERSONAL
    assert owner.final_admitted_tier is PrivacyTier.PERSONAL
    assert public.requested_tier is PrivacyTier.WORK
    assert public.final_admitted_tier is PrivacyTier.WORK


def test_owner_path_capture_is_also_narrowed_by_the_boundary_classifier(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    engine = _engine(root, boundary_classifier=_classifier_returning(PrivacyTier.SECRET))
    receipt = engine.capture.accept(
        TextPayload("synthetic owner narrowing"), delivery_id="admission-narrow-owner-1"
    )
    assert receipt.requested_tier is PrivacyTier.PERSONAL
    assert receipt.final_admitted_tier is PrivacyTier.SECRET
    assert _stored_privacy_tier(root, receipt.capture_id) == "secret"
    assert _search_effective_tier(root, receipt.capture_id) == "secret"
    record = next((root / "sources" / "captures").rglob(f"{receipt.capture_id}.json"))
    stored_record = json.loads(record.read_text(encoding="utf-8"))
    assert stored_record["privacy"]["tier"] == "secret"


def test_explicit_owner_tier_reaches_the_capture_row_search_and_record(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    engine = _engine(root)
    receipt = engine.capture.accept(
        TextPayload("synthetic explicit owner tier"),
        delivery_id="admission-owner-explicit-1",
        privacy_tier=PrivacyTier.WORK,
    )
    assert receipt.requested_tier is PrivacyTier.WORK
    assert receipt.final_admitted_tier is PrivacyTier.WORK
    privacy = json.loads(cast(str, _capture_column(root, receipt.capture_id, "privacy_json")))
    assert privacy["tier"] == "work"
    assert privacy["reason"] == "policy_work"
    assert privacy["policy_version"] == "privacy-v1"
    assert privacy["authority"] == {"cloud": False, "external_egress": False}
    assert _search_effective_tier(root, receipt.capture_id) == "work"


def test_explicit_public_owner_tier_still_narrows_at_the_boundary(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    engine = _engine(root, boundary_classifier=_classifier_returning(PrivacyTier.SECRET))
    receipt = engine.capture.accept(
        TextPayload("synthetic narrowing explicit public"),
        delivery_id="admission-owner-explicit-narrow-1",
        privacy_tier=PrivacyTier.PUBLIC,
    )
    assert receipt.requested_tier is PrivacyTier.PUBLIC
    assert receipt.final_admitted_tier is PrivacyTier.SECRET
    assert _stored_privacy_tier(root, receipt.capture_id) == "secret"
    assert _search_effective_tier(root, receipt.capture_id) == "secret"


def _engine_brain_id(root: Path) -> str:
    connection = sqlite3.connect(root / ".open-brain/state/phase1.sqlite3")
    try:
        row = connection.execute("SELECT brain_id FROM brain_identity").fetchone()
    finally:
        connection.close()
    assert row is not None
    return cast(str, row[0])


def _engine_issuer_epoch(root: Path) -> int:
    connection = sqlite3.connect(root / ".open-brain/state/phase1.sqlite3")
    try:
        row = connection.execute("SELECT issuer_epoch FROM brain_identity").fetchone()
    finally:
        connection.close()
    assert row is not None
    return cast(int, row[0])


def _destination_authority(
    root: Path,
    *,
    allowed_capture_tiers: frozenset[PrivacyTier] | None = None,
    issuer_epoch: int | None = None,
) -> EffectiveAuthority:
    return EffectiveAuthority(
        principal_id="synthetic-destination-principal",
        session_id="synthetic-destination-session",
        capabilities=frozenset(),
        space_ids=None,
        allowed_read_tiers=frozenset({PrivacyTier.PUBLIC, PrivacyTier.WORK, PrivacyTier.PERSONAL}),
        allowed_capture_tiers=(
            frozenset(set(PrivacyTier)) if allowed_capture_tiers is None else allowed_capture_tiers
        ),
        brain_id=_engine_brain_id(root),
        issuer_epoch=_engine_issuer_epoch(root) if issuer_epoch is None else issuer_epoch,
    )


def _destination_submission(
    engine: BrainEngine,
    root: Path,
    delivery_id: str,
    *,
    text: str = "synthetic destination-bound capture",
    requested_tier: PrivacyTier | None = PrivacyTier.WORK,
    allowed_capture_tiers: frozenset[PrivacyTier] | None = None,
) -> CaptureSubmission:
    return CaptureSubmission.for_destination_bound(
        profile=engine.profile,
        authority=_destination_authority(root, allowed_capture_tiers=allowed_capture_tiers),
        payload=TextPayload(text),
        delivery_id=delivery_id,
        requested_tier=requested_tier,
    )


@pytest.mark.parametrize(
    "requested_tier",
    [
        PrivacyTier.PUBLIC,
        PrivacyTier.WORK,
        PrivacyTier.PERSONAL,
        PrivacyTier.SECRET,
        PrivacyTier.UNKNOWN,
        None,
    ],
)
def test_destination_bound_submissions_produce_the_expected_durable_tiers(
    tmp_path: Path, requested_tier: PrivacyTier | None
) -> None:
    root = tmp_path / "brain"
    engine = _engine(root)
    submission = _destination_submission(
        engine, root, "admission-destination-tier-1", requested_tier=requested_tier
    )
    receipt = engine.capture.submit(submission)
    expected = PrivacyTier.UNKNOWN if requested_tier is None else requested_tier
    assert receipt.requested_tier is expected
    assert receipt.final_admitted_tier is expected
    assert _stored_privacy_tier(root, receipt.capture_id) == expected.value
    assert _search_effective_tier(root, receipt.capture_id) == expected.value


def test_destination_bound_replay_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    engine = _engine(root)
    first = engine.capture.submit(
        _destination_submission(engine, root, "admission-destination-replay-1")
    )
    second = engine.capture.submit(
        _destination_submission(engine, root, "admission-destination-replay-1")
    )
    assert first.duplicate is False
    assert second.duplicate is True
    assert second.capture_id == first.capture_id
    assert _count(root, "captures") == 1


def test_destination_bound_reused_delivery_id_with_different_bytes_conflicts(
    tmp_path: Path,
) -> None:
    from open_brain_engine.engine.capture import DeliveryConflict

    root = tmp_path / "brain"
    engine = _engine(root)
    engine.capture.submit(
        _destination_submission(
            engine, root, "admission-destination-conflict-1", text="synthetic first bytes"
        )
    )
    with pytest.raises(DeliveryConflict):
        engine.capture.submit(
            _destination_submission(
                engine,
                root,
                "admission-destination-conflict-1",
                text="synthetic different bytes",
            )
        )
    assert _count(root, "captures") == 1


def test_destination_bound_submissions_share_the_admission_rate_gate(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    engine = _engine(root, limits=AdmissionLimits(requests_per_minute_per_principal=1))
    first = engine.capture.submit(
        _destination_submission(engine, root, "admission-destination-rate-1")
    )
    assert first.duplicate is False
    with pytest.raises(CaptureAdmissionError) as raised:
        engine.capture.submit(_destination_submission(engine, root, "admission-destination-rate-2"))
    assert raised.value.result is CaptureAdmissionResult.RATE_LIMITED
    assert raised.value.retryable is True
    assert _count(root, "captures") == 1


def test_destination_bound_receipt_carries_the_public_binding(tmp_path: Path) -> None:
    from open_brain_engine.core.access_contracts import derive_brain_id

    root = tmp_path / "brain"
    engine = _engine(root)
    submission = _destination_submission(
        engine, root, "admission-destination-binding-1", requested_tier=PrivacyTier.PERSONAL
    )
    digest = submission.request_sha256()
    receipt = engine.capture.submit(submission)
    assert receipt.delivery_id == "admission-destination-binding-1"
    assert receipt.request_sha256 == digest
    assert receipt.destination_brain_id == derive_brain_id(engine.profile.tenant_id)
    assert receipt.issuer_epoch == _engine_issuer_epoch(root)
    assert receipt.requested_tier is PrivacyTier.PERSONAL
    assert receipt.final_admitted_tier is PrivacyTier.PERSONAL
    assert _capture_column(root, receipt.capture_id, "request_sha256") == digest


def _canonical_note(
    engine: BrainEngine,
    root: Path,
    *,
    text: str,
    delivery_id: str,
    privacy_tier: PrivacyTier | None = None,
) -> tuple[Path, str]:
    from open_brain_engine.engine.contracts import CaptureAction

    connection = sqlite3.connect(root / ".open-brain/state/phase1.sqlite3")
    try:
        space_id = cast(
            str, connection.execute("SELECT space_id FROM spaces LIMIT 1").fetchone()[0]
        )
    finally:
        connection.close()
    receipt = engine.capture.accept(
        TextPayload(text),
        delivery_id=delivery_id,
        action=CaptureAction.CANONICAL_NOTE,
        space_id=space_id,
        title="Synthetic canonical note",
        privacy_tier=privacy_tier,
    )
    connection = sqlite3.connect(root / ".open-brain/state/phase1.sqlite3")
    try:
        row = connection.execute(
            "SELECT canonical_path, privacy_json FROM captures WHERE capture_id = ?",
            (receipt.capture_id,),
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    return root / cast(str, row[0]), cast(str, row[1])


def _page_privacy_line(page: Path) -> str:
    for line in page.read_text(encoding="utf-8").splitlines():
        if line.startswith("privacy: "):
            return line
    raise AssertionError("canonical page carries no privacy frontmatter")


_FIXED_LOCAL_PRIVACY_LINE = "privacy: " + json.dumps(
    {
        "authority": {"cloud": False, "external_egress": False},
        "confirmation_ref": None,
        "policy_version": "privacy-v1",
        "reason": "personal_local_only",
        "tier": "personal",
    },
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=False,
)


def test_no_flag_canonical_note_frontmatter_privacy_is_byte_identical_to_today(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    engine = BrainEngine.open(compile_single_user_local(root, starter_spaces=("Synthetic Space",)))
    page, privacy_json = _canonical_note(
        engine,
        root,
        text="synthetic canonical fixed privacy note",
        delivery_id="admission-canonical-fixed-1",
    )
    assert _page_privacy_line(page) == _FIXED_LOCAL_PRIVACY_LINE
    assert json.loads(privacy_json) == {
        "authority": {"cloud": False, "external_egress": False},
        "confirmation_ref": None,
        "policy_version": "privacy-v1",
        "reason": "personal_local_only",
        "tier": "personal",
    }


def test_narrowed_canonical_note_frontmatter_equals_the_stored_admitted_decision(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    engine = BrainEngine.open(
        compile_single_user_local(root, starter_spaces=("Synthetic Space",)),
        boundary_classifier=_classifier_returning(PrivacyTier.SECRET),
    )
    page, privacy_json = _canonical_note(
        engine,
        root,
        text="synthetic canonical narrowed privacy note",
        delivery_id="admission-canonical-narrow-1",
    )
    admitted = json.loads(privacy_json)
    assert admitted["tier"] == "secret"
    from open_brain_engine.storage.markdown import parse_markdown

    assert parse_markdown(page.read_bytes()).fields["privacy"] == admitted


def test_explicit_tier_canonical_note_frontmatter_carries_the_requested_tier(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    engine = BrainEngine.open(compile_single_user_local(root, starter_spaces=("Synthetic Space",)))
    page, privacy_json = _canonical_note(
        engine,
        root,
        text="synthetic canonical explicit tier note",
        delivery_id="admission-canonical-explicit-1",
        privacy_tier=PrivacyTier.WORK,
    )
    admitted = json.loads(privacy_json)
    assert admitted["tier"] == "work"
    from open_brain_engine.storage.markdown import parse_markdown

    assert parse_markdown(page.read_bytes()).fields["privacy"] == admitted

"""Bounded writer-waiter lease behavior; every path and value here is synthetic."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from open_brain_engine.storage.locks import (
    _PROCESS_WRITER_WAITERS,
    FileLease,
    LeaseFormatError,
    LockBusyError,
    WriterQueueFullError,
)

_HOLDER_SCRIPT = """
import fcntl, sys, time
with open(sys.argv[1], "r+b") as handle:
    fcntl.lockf(handle, fcntl.LOCK_EX)
    print("held", flush=True)
    time.sleep(float(sys.argv[2]))
"""


def _lease(root: Path) -> FileLease:
    return FileLease(root, "bounded-test-identity")


def _lock_path(root: Path) -> Path:
    return root / ".open-brain-locks" / "lease.shared-writer"


def _hold_lock_file(path: Path, seconds: float) -> subprocess.Popen[bytes]:
    process = subprocess.Popen(
        [sys.executable, "-c", _HOLDER_SCRIPT, str(path), str(seconds)],
        stdout=subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == b"held"
    return process


def _finish(process: subprocess.Popen[bytes]) -> None:
    process.wait()
    if process.stdout is not None:
        process.stdout.close()


def _wait_for_waiters(expected: int) -> None:
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if sum(_PROCESS_WRITER_WAITERS.values()) >= expected:
            return
        time.sleep(0.005)
    raise AssertionError(f"expected {expected} registered writer waiters")


def test_nonblocking_acquire_still_fails_fast_inside_one_process(tmp_path: Path) -> None:
    lease = _lease(tmp_path)
    with (
        lease.acquire_shared_writer(),
        pytest.raises(LockBusyError, match="already held by this process"),
        lease.acquire_shared_writer(),
    ):
        pass


def test_bounded_acquire_waits_until_the_lease_is_free(tmp_path: Path) -> None:
    lease = _lease(tmp_path)
    with lease.acquire_shared_writer():
        pass
    holder = _hold_lock_file(_lock_path(tmp_path), 0.4)
    started = time.monotonic()
    try:
        with lease.acquire_shared_writer_bounded(max_waiters=2, timeout=5.0):
            pass
    finally:
        _finish(holder)
    assert time.monotonic() - started >= 0.3


def test_bounded_acquire_past_the_deadline_raises_queue_full(tmp_path: Path) -> None:
    lease = _lease(tmp_path)
    with lease.acquire_shared_writer():
        pass
    holder = _hold_lock_file(_lock_path(tmp_path), 0.6)
    try:
        with (
            pytest.raises(WriterQueueFullError, match="deadline"),
            lease.acquire_shared_writer_bounded(max_waiters=1, timeout=0.05),
        ):
            pass
        with (
            pytest.raises(WriterQueueFullError, match="deadline"),
            lease.acquire_shared_writer_bounded(max_waiters=1, timeout=0.05),
        ):
            pass
    finally:
        _finish(holder)
    with lease.acquire_shared_writer():
        pass


def test_waiters_beyond_the_cap_are_rejected_while_earlier_waiters_complete(
    tmp_path: Path,
) -> None:
    lease = _lease(tmp_path)
    with lease.acquire_shared_writer():
        pass
    holder = _hold_lock_file(_lock_path(tmp_path), 0.6)
    failures: list[BaseException] = []

    def wait_for_lease() -> None:
        try:
            with lease.acquire_shared_writer_bounded(max_waiters=2, timeout=5.0):
                pass
        except BaseException as error:  # noqa: BLE001
            failures.append(error)

    threads = [threading.Thread(target=wait_for_lease) for _ in range(2)]
    try:
        for thread in threads:
            thread.start()
        _wait_for_waiters(2)
        with (
            pytest.raises(WriterQueueFullError, match="queue is full"),
            lease.acquire_shared_writer_bounded(max_waiters=2, timeout=5.0),
        ):
            pass
    finally:
        for thread in threads:
            thread.join()
        _finish(holder)
    assert failures == []
    assert not _PROCESS_WRITER_WAITERS
    with lease.acquire_shared_writer():
        pass


def test_bounded_acquire_rejects_invalid_wait_configuration(tmp_path: Path) -> None:
    lease = _lease(tmp_path)
    with (
        pytest.raises(LeaseFormatError),
        lease.acquire_shared_writer_bounded(max_waiters=0, timeout=1.0),
    ):
        pass
    with (
        pytest.raises(LeaseFormatError),
        lease.acquire_shared_writer_bounded(max_waiters=1, timeout=-1.0),
    ):
        pass
    with (
        pytest.raises(LeaseFormatError),
        lease.acquire_shared_writer_bounded(max_waiters=1, timeout=1.0, poll_interval=0.0),
    ):
        pass


def test_bounded_in_process_reentry_stays_bounded(tmp_path: Path) -> None:
    lease = _lease(tmp_path)
    with (
        lease.acquire_shared_writer(),
        pytest.raises(WriterQueueFullError, match="deadline"),
        lease.acquire_shared_writer_bounded(max_waiters=1, timeout=0.05),
    ):
        pass

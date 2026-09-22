"""Bounded writer-waiter lease behavior; every path and value here is synthetic."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from open_brain_engine.core.locks import LockScope
from open_brain_engine.storage import locks as locks_module
from open_brain_engine.storage.locks import (
    _PROCESS_WRITER_WAITERS,
    FileLease,
    LeaseFormatError,
    LockBusyError,
    WriterQueueFullError,
    inspect_file_leases,
)

_HOLDER_SCRIPT = """
import fcntl, sys, time
with open(sys.argv[1], "r+b") as handle:
    fcntl.lockf(handle, fcntl.LOCK_EX)
    print("held", flush=True)
    time.sleep(float(sys.argv[2]))
"""

_TRY_EXCLUSIVE_SCRIPT = """
import errno, fcntl, sys
with open(sys.argv[1], "r+b") as handle:
    try:
        fcntl.lockf(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno not in {errno.EACCES, errno.EAGAIN}:
            raise
        print("busy")
    else:
        print("acquired")
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


def test_shared_readers_overlap_and_fence_exclusive_writers(tmp_path: Path) -> None:
    lease = _lease(tmp_path)
    with lease.acquire_shared_writer():
        pass
    with lease.acquire_shared_reader(), lease.acquire_shared_reader():
        with (
            pytest.raises(LockBusyError, match="already held by this process"),
            lease.acquire_exclusive_writer(),
        ):
            pass
        child = subprocess.run(
            [sys.executable, "-c", _TRY_EXCLUSIVE_SCRIPT, str(_lock_path(tmp_path))],
            check=True,
            capture_output=True,
            text=True,
        )
        assert child.stdout.strip() == "busy"
        snapshot = inspect_file_leases(tmp_path)
        assert snapshot.held_count == 1
        child = subprocess.run(
            [sys.executable, "-c", _TRY_EXCLUSIVE_SCRIPT, str(_lock_path(tmp_path))],
            check=True,
            capture_output=True,
            text=True,
        )
        assert child.stdout.strip() == "busy"
    with (
        lease.acquire_exclusive_writer(),
        pytest.raises(LockBusyError, match="already held by this process"),
        lease.acquire_shared_reader(),
    ):
        pass


def test_lock_file_closes_before_another_local_holder_registers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = _lease(tmp_path)
    close_started = threading.Event()
    allow_close = threading.Event()
    reader_entered = threading.Event()
    release_reader = threading.Event()
    writer_fds: list[int] = []
    failures: list[BaseException] = []
    real_open_lock_file = locks_module._open_lock_file
    real_close = os.close

    def capture_writer_fd(directory_fd: int, discriminator: str) -> tuple[int, bool]:
        file_fd, created = real_open_lock_file(directory_fd, discriminator)
        if threading.current_thread().name == "closing-writer":
            writer_fds.append(file_fd)
        return file_fd, created

    def pause_writer_close(file_fd: int) -> None:
        if (
            threading.current_thread().name == "closing-writer"
            and writer_fds
            and file_fd == writer_fds[0]
        ):
            close_started.set()
            if not allow_close.wait(2.0):
                raise AssertionError("writer close was not released")
        real_close(file_fd)

    def use_writer() -> None:
        try:
            with lease.acquire_exclusive_writer():
                pass
        except BaseException as error:  # noqa: BLE001
            failures.append(error)

    def use_reader() -> None:
        try:
            with lease.acquire_shared_reader():
                reader_entered.set()
                if not release_reader.wait(2.0):
                    raise AssertionError("reader was not released")
        except BaseException as error:  # noqa: BLE001
            failures.append(error)

    monkeypatch.setattr(locks_module, "_open_lock_file", capture_writer_fd)
    monkeypatch.setattr(os, "close", pause_writer_close)
    writer = threading.Thread(target=use_writer, name="closing-writer")
    reader = threading.Thread(target=use_reader, name="next-reader")
    writer.start()
    assert close_started.wait(2.0)
    reader.start()
    try:
        assert not reader_entered.wait(0.05)
        allow_close.set()
        assert reader_entered.wait(2.0)
        child = subprocess.run(
            [sys.executable, "-c", _TRY_EXCLUSIVE_SCRIPT, str(_lock_path(tmp_path))],
            check=True,
            capture_output=True,
            text=True,
        )
        assert child.stdout.strip() == "busy"
    finally:
        allow_close.set()
        release_reader.set()
        writer.join()
        reader.join()
    assert failures == []


def test_cursor_state_lock_has_a_separate_bounded_waiter_budget(tmp_path: Path) -> None:
    lease = _lease(tmp_path)
    with (
        lease.acquire(LockScope.CURSOR_STATE),
        pytest.raises(WriterQueueFullError, match="deadline"),
        lease.acquire_cursor_state_bounded(max_waiters=1, timeout=0.01),
    ):
        pass
    with lease.acquire_cursor_state_bounded(max_waiters=1, timeout=0.1):
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

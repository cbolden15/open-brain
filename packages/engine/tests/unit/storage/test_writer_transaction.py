"""Held writer authority is explicit and confined to its issuing lifetime."""

from __future__ import annotations

import contextvars
import copy
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from open_brain_engine.core.locks import LockScope
from open_brain_engine.storage.locks import FileLease, LeaseError, LockBusyError


def _excluded(root: Path) -> None:
    script = """
import errno, fcntl, sys
with open(sys.argv[1], 'r+b') as f:
    for mode in (fcntl.LOCK_EX, fcntl.LOCK_SH):
        try:
            fcntl.lockf(f, mode | fcntl.LOCK_NB)
        except OSError as e:
            assert e.errno in (errno.EACCES, errno.EAGAIN)
        else:
            raise AssertionError('outer writer released')
"""
    subprocess.run(
        [sys.executable, "-c", script, str(root / ".open-brain-locks/lease.shared-writer")],
        check=True, capture_output=True, timeout=5,
    )


def test_explicit_borrow_preserves_outer_kernel_lock(tmp_path: Path) -> None:
    lease = FileLease(tmp_path, "transaction-test")
    validations: list[int] = []
    nested = FileLease(tmp_path, "nested-test", validate_acquire=lambda: validations.append(1))
    with lease.hold_writer_transaction() as held:
        _excluded(tmp_path)
        with pytest.raises(LeaseError):
            copy.copy(held).validate()
        with pytest.raises(LockBusyError), nested.acquire_shared_writer():
            pass
        with held.borrow():
            with nested.acquire_shared_writer(), nested.acquire_shared_reader():
                _excluded(tmp_path)
            with nested.acquire(LockScope.CURSOR_STATE):
                _excluded(tmp_path)
        held.validate()
        _excluded(tmp_path)
    assert len(validations) >= 4
    with pytest.raises(LeaseError):
        held.validate()
    with nested.acquire_shared_writer():
        pass


def test_root_replacement_and_expired_copied_context_refuse(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    lease = FileLease(root, "transaction-test")
    with lease.hold_writer_transaction() as held:
        with held.borrow():
            copied = contextvars.copy_context()
        root.rename(tmp_path / "original")
        root.mkdir(mode=0o700)
        with pytest.raises(LeaseError):
            held.validate()
    with pytest.raises(LeaseError):
        copied.run(held.validate)


def test_copied_context_cannot_borrow_on_another_thread(tmp_path: Path) -> None:
    lease = FileLease(tmp_path, "transaction-test")
    failures: list[BaseException] = []
    with lease.hold_writer_transaction() as held, held.borrow():
        context = contextvars.copy_context()

        def contend() -> None:
            try:
                with lease.acquire_shared_reader():
                    pass
            except LeaseError as error:
                failures.append(error)

        thread = threading.Thread(target=lambda: context.run(contend))
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert len(failures) == 1
        _excluded(tmp_path)


def test_replaced_lease_file_and_nested_owner_refuse(tmp_path: Path) -> None:
    lease = FileLease(tmp_path, "transaction-test")
    with lease.hold_writer_transaction() as held:
        with pytest.raises(LeaseError), lease.hold_writer_transaction():
            pass
        path = tmp_path / ".open-brain-locks/lease.shared-writer"
        path.rename(path.with_name("old"))
        path.touch(mode=0o600)
        with pytest.raises(LeaseError):
            held.validate()


def test_fork_cannot_reuse_held_writer(tmp_path: Path) -> None:
    lease = FileLease(tmp_path, "transaction-test")
    with lease.hold_writer_transaction() as held, held.borrow():
        read, write = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(read)
            try:
                held.validate()
            except LeaseError:
                os.write(write, b"refused")
            finally:
                os._exit(0)
        os.close(write)
        try:
            assert os.read(read, 16) == b"refused"
        finally:
            os.close(read)
            os.waitpid(child, 0)
        _excluded(tmp_path)

from __future__ import annotations

import contextvars
import copy
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from open_brain_engine.engine.t03_contracts import T03Error

from open_brain.local_data import select_local_root
from open_brain.services import local_bootstrap


def test_existing_transaction_contains_startup_and_cleanup(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    selection = select_local_root(data_dir=str(root), environment={"HOME": str(tmp_path)})
    local_bootstrap.initialize_local_brain(selection)
    with local_bootstrap.hold_existing_local_brain_transaction(root) as transaction:
        transaction.validate()

        def operation() -> str:
            with local_bootstrap.open_local_brain(selection):
                assert len(list((root / ".open-brain/runtime-sessions").glob("session-*"))) == 1
                _excluded(root)
            assert not list((root / ".open-brain/runtime-sessions").glob("session-*"))
            return "complete"

        _excluded(root)
        assert transaction.run(operation) == "complete"
        _excluded(root)
        transaction.validate()
    with pytest.raises(RuntimeError):
        transaction.run(operation)


def _excluded(root: Path) -> None:
    script = """
import errno, fcntl, sys
from pathlib import Path
root = Path(sys.argv[1])
for relative, function, modes in (
    ('.open-brain/.open-brain-locks/lease.shared-writer', fcntl.lockf,
     (fcntl.LOCK_SH, fcntl.LOCK_EX)),
    ('.open-brain/runtime-sessions/registry.lock', fcntl.flock, (fcntl.LOCK_EX,)),
):
    with open(root / relative, 'r+b') as f:
        for mode in modes:
            try:
                function(f, mode | fcntl.LOCK_NB)
            except OSError as e:
                assert e.errno in (errno.EACCES, errno.EAGAIN)
            else:
                raise AssertionError('barrier released')
"""
    subprocess.run([sys.executable, "-c", script, str(root)], check=True, timeout=5)


def test_transaction_rejects_other_thread_and_nested_run(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    selection = select_local_root(data_dir=str(root), environment={"HOME": str(tmp_path)})
    local_bootstrap.initialize_local_brain(selection)
    with local_bootstrap.hold_existing_local_brain_transaction(root) as transaction:
        context = contextvars.copy_context()
        with pytest.raises(T03Error):
            copy.copy(transaction._admission).validate(transaction.profile)
        failures: list[BaseException] = []

        def wrong_owner() -> None:
            try:
                transaction.run(lambda: None)
            except RuntimeError as error:
                failures.append(error)

        thread = threading.Thread(target=lambda: context.run(wrong_owner))
        thread.start()
        thread.join(timeout=2)
        assert len(failures) == 1
        with pytest.raises(RuntimeError):
            transaction.run(lambda: transaction.run(lambda: None))
        _excluded(root)


def test_callback_exception_keeps_barriers_and_can_be_retried(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    selection = select_local_root(data_dir=str(root), environment={"HOME": str(tmp_path)})
    local_bootstrap.initialize_local_brain(selection)

    def failed_operation() -> None:
        with local_bootstrap.open_local_brain(selection):
            raise ValueError("synthetic callback failure")

    with local_bootstrap.hold_existing_local_brain_transaction(root) as transaction:
        with pytest.raises(ValueError, match="synthetic"):
            transaction.run(failed_operation)
        assert not list((root / ".open-brain/runtime-sessions").glob("session-*"))
        _excluded(root)
        assert transaction.run(lambda: "retry") == "retry"
    with local_bootstrap.open_local_brain(selection):
        pass


def test_live_peer_refuses_transaction_without_taking_writer(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    selection = select_local_root(data_dir=str(root), environment={"HOME": str(tmp_path)})
    with local_bootstrap.open_local_brain(selection):
        path = root / ".open-brain/.open-brain-locks/lease.shared-writer"
        before = path.read_bytes()
        with pytest.raises(T03Error), local_bootstrap.hold_existing_local_brain_transaction(root):
            raise AssertionError("admitted despite a live peer")
        assert path.read_bytes() == before


def test_replaced_registry_refuses_release_validation(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    selection = select_local_root(data_dir=str(root), environment={"HOME": str(tmp_path)})
    local_bootstrap.initialize_local_brain(selection)
    with local_bootstrap.hold_existing_local_brain_transaction(root) as transaction:
        path = root / ".open-brain/runtime-sessions/registry.lock"
        path.rename(path.with_name("original.lock"))
        path.touch(mode=0o600)
        with pytest.raises(RuntimeError, match="ownership"):
            transaction.validate()


def test_transaction_requires_existing_root(tmp_path: Path) -> None:
    root = tmp_path / "absent"
    with pytest.raises(ValueError), local_bootstrap.hold_existing_local_brain_transaction(root):
        pass
    assert not root.exists()


def test_transaction_refuses_async_callback_and_other_root_without_creating_it(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    local_bootstrap.initialize_local_brain(
        select_local_root(data_dir=str(root), environment={"HOME": str(tmp_path)})
    )
    other = tmp_path / "other"
    selection = select_local_root(data_dir=str(other), environment={"HOME": str(tmp_path)})

    async def asynchronous() -> None:
        pass

    with local_bootstrap.hold_existing_local_brain_transaction(root) as transaction:
        with pytest.raises(RuntimeError, match="synchronous"):
            _ = transaction.run(asynchronous)
        with pytest.raises(RuntimeError, match="root mismatch"):
            transaction.run(lambda: local_bootstrap.initialize_local_brain(selection))
        assert not other.exists()
        _excluded(root)

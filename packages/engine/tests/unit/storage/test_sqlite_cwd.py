"""The process directory must be captured inside the SQLite open lock."""

import os
import sqlite3
from pathlib import Path
from threading import Event, Lock, Thread, get_ident
from typing import Any

import pytest
from open_brain_engine.storage import sqlite as storage


@pytest.mark.parametrize("read_only", [False, True])
def test_contended_opener_never_saves_another_openers_temporary_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    read_only: bool,
) -> None:
    root = tmp_path / "brain"
    root.mkdir(mode=0o700)
    foreign = tmp_path / "other-brain"
    foreign.mkdir(mode=0o700)
    database = root / "events.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE synthetic (value INTEGER)")
    database.chmod(0o600)
    attempted_lock = Event()
    underlying_lock = Lock()
    captured_directories: list[str] = []
    errors: list[BaseException] = []
    worker_identity: int | None = None
    actual_open = os.open

    class ObservedLock:
        def __enter__(self) -> None:
            attempted_lock.set()
            underlying_lock.acquire()

        def __exit__(self, *args: object) -> None:
            underlying_lock.release()

    def observed_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if path == "." and get_ident() == worker_identity:
            captured_directories.append(os.getcwd())
        return actual_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(storage, "_SQLITE_OPEN_LOCK", ObservedLock())
    monkeypatch.setattr(os, "open", observed_open)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {observed_open})
    original_fd = actual_open(".", os.O_RDONLY | os.O_DIRECTORY)
    parent_fd = actual_open(root, os.O_RDONLY | os.O_DIRECTORY)
    foreign_fd = actual_open(foreign, os.O_RDONLY | os.O_DIRECTORY)
    original = os.getcwd()

    def worker() -> None:
        nonlocal worker_identity
        worker_identity = get_ident()
        try:
            if read_only:
                result = storage.connect_database_read_only(root=root, database_name=database.name)
            else:
                result = storage._connect_from_parent(parent_fd, database.name)
            try:
                assert result.execute("SELECT count(*) FROM synthetic").fetchone()[0] == 0
            finally:
                result.close()
        except BaseException as exc:
            errors.append(exc)

    thread = Thread(target=worker, name="synthetic-sqlite-open")
    underlying_lock.acquire()
    try:
        os.fchdir(foreign_fd)
        thread.start()
        assert attempted_lock.wait(timeout=2), "worker did not attempt the held lock"
        captures_before_release = len(captured_directories)
    finally:
        os.fchdir(original_fd)
        underlying_lock.release()
        thread.join(timeout=5)
        final_directory = os.getcwd()
        # Restore independently even when testing the broken implementation.
        os.fchdir(original_fd)
        os.close(foreign_fd)
        os.close(parent_fd)
        os.close(original_fd)
    assert not thread.is_alive() and not errors
    assert captures_before_release == 0
    assert captured_directories == [original]
    assert final_directory == original

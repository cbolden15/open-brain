from __future__ import annotations

import os
import stat
from hashlib import sha256
from pathlib import Path, PurePosixPath

import pytest
from open_brain_engine.core.ports import PutDisposition
from open_brain_engine.storage.filesystem import (
    AtomicFilesystemRawStore,
    DuplicateConflictError,
    DurabilityError,
    RootConfinementError,
    StorageError,
    WriteState,
    atomic_replace,
    atomic_write_new,
    capture_root_identity,
    raw_relative_path,
    read_confined,
)

from tests.unit.storage._factories import raw_capture


def test_filesystem_write_publishes_complete_bytes_atomically(tmp_path: Path) -> None:
    payload = b'{"synthetic":"complete"}'

    state = atomic_write_new(
        root=tmp_path,
        relative=PurePosixPath("raw/aa/cap_" + "a" * 64 + ".json"),
        data=payload,
    )

    final_path = tmp_path / "raw" / "aa" / ("cap_" + "a" * 64 + ".json")
    assert state is WriteState.CREATED
    assert final_path.read_bytes() == payload
    assert not tuple(final_path.parent.glob(".*.tmp"))


def test_expected_root_identity_rejects_replacement_before_io(tmp_path: Path) -> None:
    selected = tmp_path / "selected"
    replacement = tmp_path / "replacement"
    selected.mkdir()
    replacement.mkdir()
    identity = capture_root_identity(selected)
    selected.rename(tmp_path / "displaced-selected")
    replacement.rename(selected)

    with pytest.raises(RootConfinementError, match="identity changed"):
        atomic_write_new(
            root=selected,
            relative="record.json",
            data=b"must not persist",
            expected_root_identity=identity,
        )

    assert tuple(selected.iterdir()) == ()


def test_root_open_rejects_an_intermediate_symlink_even_when_identity_matches(
    tmp_path: Path,
) -> None:
    route = tmp_path / "route"
    selected = route / "selected"
    selected.mkdir(parents=True)
    identity = capture_root_identity(selected)
    moved_route = tmp_path / "moved-route"
    route.rename(moved_route)
    route.symlink_to(moved_route, target_is_directory=True)

    with pytest.raises(RootConfinementError, match="unsafe storage root"):
        atomic_write_new(
            root=selected,
            relative="record.json",
            data=b"must not persist",
            expected_root_identity=identity,
        )

    assert tuple((moved_route / "selected").iterdir()) == ()


def test_equal_duplicate_is_a_noop_and_conflict_never_overwrites(tmp_path: Path) -> None:
    relative = PurePosixPath("raw/bb/record.json")
    first = b'{"version":1}'

    assert atomic_write_new(root=tmp_path, relative=relative, data=first) is WriteState.CREATED
    assert (
        atomic_write_new(root=tmp_path, relative=relative, data=first) is WriteState.ALREADY_EXISTS
    )
    with pytest.raises(DuplicateConflictError, match="immutable record conflict"):
        atomic_write_new(root=tmp_path, relative=relative, data=b'{"version":2}')

    assert (tmp_path / relative).read_bytes() == first


def test_durable_write_fsyncs_file_before_final_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: list[str] = []
    real_fsync = os.fsync

    def recording_fsync(file_descriptor: int) -> None:
        mode = os.fstat(file_descriptor).st_mode
        observed.append("directory" if stat.S_ISDIR(mode) else "file")
        real_fsync(file_descriptor)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    atomic_write_new(root=tmp_path, relative="raw/cc/record.json", data=b"complete")

    assert "file" in observed
    assert observed[-1] == "directory"
    assert observed.index("file") < len(observed) - 1


def test_publish_failure_leaves_no_final_or_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_publish(*args: object, **kwargs: object) -> None:
        raise OSError("synthetic publish failure")

    monkeypatch.setattr(os, "replace", fail_publish)
    relative = PurePosixPath("raw/dd/record.json")

    with pytest.raises(DurabilityError, match="durable storage write failed"):
        atomic_write_new(root=tmp_path, relative=relative, data=b"complete")

    assert not (tmp_path / relative).exists()
    assert not tuple((tmp_path / "raw" / "dd").glob(".*.tmp"))


def test_atomic_replace_creates_then_mutates_in_place_with_private_mode(
    tmp_path: Path,
) -> None:
    relative = PurePosixPath("host/record.json")

    atomic_replace(root=tmp_path, relative=relative, data=b'{"generation":1}')
    atomic_replace(root=tmp_path, relative=relative, data=b'{"generation":2}')

    target = tmp_path / relative
    assert target.read_bytes() == b'{"generation":2}'
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not tuple(target.parent.glob(".*.tmp"))


def test_atomic_replace_require_existing_true_rejects_a_missing_target(
    tmp_path: Path,
) -> None:
    with pytest.raises(StorageError, match="replace target missing"):
        atomic_replace(
            root=tmp_path,
            relative="host/record.json",
            data=b"{}",
            require_existing=True,
        )

    assert not (tmp_path / "host" / "record.json").exists()


def test_atomic_replace_require_existing_false_rejects_an_existing_target(
    tmp_path: Path,
) -> None:
    relative = PurePosixPath("host/record.json")
    atomic_replace(root=tmp_path, relative=relative, data=b'{"generation":1}')

    with pytest.raises(DuplicateConflictError, match="replace target already exists"):
        atomic_replace(
            root=tmp_path,
            relative=relative,
            data=b'{"generation":2}',
            require_existing=False,
        )

    assert (tmp_path / relative).read_bytes() == b'{"generation":1}'


def test_atomic_replace_compares_the_existing_digest_under_its_write_lock(
    tmp_path: Path,
) -> None:
    relative = PurePosixPath("host/record.json")
    original = b'{"generation":1}'
    atomic_replace(root=tmp_path, relative=relative, data=original)

    with pytest.raises(DuplicateConflictError, match="replace target changed"):
        atomic_replace(
            root=tmp_path,
            relative=relative,
            data=b'{"generation":2}',
            require_existing=True,
            expected_existing_sha256="0" * 64,
        )

    assert (tmp_path / relative).read_bytes() == original
    atomic_replace(
        root=tmp_path,
        relative=relative,
        data=b'{"generation":2}',
        require_existing=True,
        expected_existing_sha256=sha256(original).hexdigest(),
    )
    assert (tmp_path / relative).read_bytes() == b'{"generation":2}'


def test_atomic_replace_rejects_a_symlinked_target(tmp_path: Path) -> None:
    (tmp_path / "host").mkdir()
    outside = tmp_path.parent / "outside.json"
    outside.write_bytes(b"outside")
    (tmp_path / "host" / "record.json").symlink_to(outside)

    with pytest.raises(RootConfinementError):
        atomic_replace(
            root=tmp_path,
            relative="host/record.json",
            data=b"{}",
            require_existing=True,
        )

    assert outside.read_bytes() == b"outside"


def test_atomic_replace_failure_leaves_previous_bytes_and_no_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    relative = PurePosixPath("host/record.json")
    atomic_replace(root=tmp_path, relative=relative, data=b'{"generation":1}')

    def fail_publish(*args: object, **kwargs: object) -> None:
        raise OSError("synthetic publish failure")

    monkeypatch.setattr(os, "replace", fail_publish)
    with pytest.raises(DurabilityError, match="durable storage write failed"):
        atomic_replace(root=tmp_path, relative=relative, data=b'{"generation":2}')

    assert (tmp_path / relative).read_bytes() == b'{"generation":1}'
    assert not tuple((tmp_path / "host").glob(".*.tmp"))


def test_confined_read_returns_none_when_a_parent_directory_is_absent(tmp_path: Path) -> None:
    assert read_confined(root=tmp_path, relative="missing/record.json") is None


def test_raw_store_preserves_private_canonical_capture_and_generated_path(
    tmp_path: Path,
) -> None:
    capture = raw_capture()
    store = AtomicFilesystemRawStore(root=tmp_path)

    created = store.put_if_absent(capture)
    duplicate = store.put_if_absent(capture)
    stored_path = tmp_path / raw_relative_path(capture.envelope.capture_id)

    assert created.disposition is PutDisposition.CREATED
    assert duplicate.disposition is PutDisposition.DUPLICATE
    assert store.get(capture.envelope.capture_id) == capture
    assert stat.S_IMODE(stored_path.stat().st_mode) == 0o600
    assert stored_path.parts[-3] == "raw"
    assert stored_path.parts[-2] == str(capture.envelope.capture_id)[4:6]


def test_raw_store_rejects_same_capture_id_with_changed_canonical_bytes(
    tmp_path: Path,
) -> None:
    first = raw_capture(title="First synthetic title")
    collision = raw_capture(title="Changed synthetic title")
    assert collision.envelope.capture_id == first.envelope.capture_id
    store = AtomicFilesystemRawStore(root=tmp_path)
    store.put_if_absent(first)

    with pytest.raises(DuplicateConflictError):
        store.put_if_absent(collision)

    assert store.get(first.envelope.capture_id) == first

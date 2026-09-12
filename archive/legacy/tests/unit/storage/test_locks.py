from __future__ import annotations

import ast
import stat
import subprocess
import sys
import time
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import open_brain_engine.storage.locks as storage_locks
import pytest
from open_brain_engine.core.locks import LockScope
from open_brain_engine.engine import LockScope as EngineLockScope
from open_brain_engine.storage.locks import (
    FileLease,
    LeaseDescriptor,
    LeaseFormatError,
    LockBusyError,
    LockStateSnapshot,
    inspect_file_leases,
)

from open_brain_legacy.operations.index import IndexLease
from open_brain_legacy.operations.now import NowLease
from open_brain_legacy.operations.writer_jobs import WriterLease


def test_lock_scope_is_core_owned_and_storage_has_no_operations_dependency() -> None:
    assert LockScope is EngineLockScope
    assert LockScope.__module__ == "open_brain_engine.core.locks"

    storage_source = Path(storage_locks.__file__).read_text(encoding="utf-8")
    imports = {
        node.module
        for node in ast.walk(ast.parse(storage_source))
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert "open_brain_engine.core.locks" in imports
    assert not any(
        module == "open_brain.operations"
        or module.startswith("open_brain.operations.")
        or module == "open_brain_legacy.operations"
        or module.startswith("open_brain_legacy.operations.")
        for module in imports
    )


def _attempt_lease(root: Path, scope: LockScope, *, backup_profile: str | None = None) -> str:
    script = """
from pathlib import Path
import sys
from open_brain_engine.core.locks import LockScope
from open_brain_engine.storage.locks import FileLease, LockBusyError

root = Path(sys.argv[1])
scope = LockScope(sys.argv[2])
profile = None if sys.argv[3] == "-" else sys.argv[3]
try:
    with FileLease(root, "subprocess", backup_profile=profile).acquire(scope):
        print("acquired")
except LockBusyError:
    print("busy")
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(root), scope.value, backup_profile or "-"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_lease_descriptor_has_one_canonical_round_trip() -> None:
    descriptor = LeaseDescriptor(
        version=1,
        scope=LockScope.INDEX,
        discriminator="index",
        owner_identity_id="mac-mini",
        pid=1234,
        acquired_at=datetime(2026, 8, 16, 18, 30, tzinfo=UTC),
    )

    payload = descriptor.to_bytes()

    assert payload == (
        b'{"acquired_at":"2026-08-16T18:30:00Z","discriminator":"index",'
        b'"owner_identity_id":"mac-mini","pid":1234,"scope":"index","version":1}'
    )
    assert LeaseDescriptor.from_bytes(payload) == descriptor


def test_lease_descriptor_normalizes_an_aware_timestamp_to_utc() -> None:
    descriptor = LeaseDescriptor(
        version=1,
        scope=LockScope.SHARED_WRITER,
        discriminator="shared-writer",
        owner_identity_id="mac-mini",
        pid=7,
        acquired_at=datetime(
            2026,
            8,
            16,
            13,
            30,
            tzinfo=timezone(timedelta(hours=-5)),
        ),
    )

    assert descriptor.acquired_at == datetime(2026, 8, 16, 18, 30, tzinfo=UTC)
    assert b'"acquired_at":"2026-08-16T18:30:00Z"' in descriptor.to_bytes()


@pytest.mark.parametrize(
    ("scope", "discriminator"),
    [
        (LockScope.NONE, "none"),
        (LockScope.INDEX, "shared-writer"),
        (LockScope.BACKUP_PROFILE, "unknown"),
    ],
)
def test_lease_descriptor_rejects_invalid_scope_discriminator_pairs(
    scope: LockScope,
    discriminator: str,
) -> None:
    with pytest.raises(LeaseFormatError, match="invalid lease descriptor"):
        LeaseDescriptor(
            version=1,
            scope=scope,
            discriminator=discriminator,
            owner_identity_id="mac-mini",
            pid=1234,
            acquired_at=datetime(2026, 8, 16, 18, 30, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    ("owner_identity_id", "pid", "acquired_at"),
    [
        ("Mac Mini", 1234, datetime(2026, 8, 16, 18, 30, tzinfo=UTC)),
        ("mac-mini", 0, datetime(2026, 8, 16, 18, 30, tzinfo=UTC)),
        ("mac-mini", True, datetime(2026, 8, 16, 18, 30, tzinfo=UTC)),
        ("mac-mini", 1234, datetime(2026, 8, 16, 18, 30)),
    ],
)
def test_lease_descriptor_rejects_invalid_observational_metadata(
    owner_identity_id: str,
    pid: int,
    acquired_at: datetime,
) -> None:
    with pytest.raises(LeaseFormatError, match="invalid lease descriptor"):
        LeaseDescriptor(
            version=1,
            scope=LockScope.INDEX,
            discriminator="index",
            owner_identity_id=owner_identity_id,
            pid=pid,
            acquired_at=acquired_at,
        )


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b'{"version":1}',
        (
            b'{"acquired_at":"2026-08-16T18:30:00Z","discriminator":"index",'
            b'"owner_identity_id":"mac-mini","pid":1,"scope":"index","version":1,'
            b'"unexpected":true}'
        ),
        (
            b'{"acquired_at":"2026-08-16T18:30:00Z","discriminator":"index",'
            b'"owner_identity_id":"mac-mini","pid":1,"pid":2,"scope":"index",'
            b'"version":1}'
        ),
    ],
)
def test_lease_descriptor_rejects_noncanonical_or_ambiguous_payloads(payload: bytes) -> None:
    with pytest.raises(LeaseFormatError, match="invalid lease descriptor"):
        LeaseDescriptor.from_bytes(payload)


def test_lease_descriptor_is_immutable() -> None:
    descriptor = LeaseDescriptor(
        version=1,
        scope=LockScope.INGRESS,
        discriminator="ingress",
        owner_identity_id="mac-mini",
        pid=1234,
        acquired_at=datetime(2026, 8, 16, 18, 30, tzinfo=UTC),
    )

    with pytest.raises(FrozenInstanceError):
        descriptor.pid = 999  # type: ignore[misc]


def test_file_lease_excludes_the_same_scope_in_another_process(tmp_path: Path) -> None:
    lease = FileLease(tmp_path, "mac-mini")

    with lease.acquire(LockScope.INDEX):
        assert _attempt_lease(tmp_path, LockScope.INDEX) == "busy"

    assert _attempt_lease(tmp_path, LockScope.INDEX) == "acquired"


def test_file_lease_persists_private_observational_metadata(tmp_path: Path) -> None:
    with FileLease(tmp_path, "mac-mini").acquire(LockScope.SHARED_WRITER):
        lock_path = tmp_path / ".open-brain-locks" / "lease.shared-writer"
        descriptor = LeaseDescriptor.from_bytes(lock_path.read_bytes())

        assert descriptor.scope is LockScope.SHARED_WRITER
        assert descriptor.discriminator == "shared-writer"
        assert descriptor.owner_identity_id == "mac-mini"
        assert descriptor.pid > 0
        assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(lock_path.parent.stat().st_mode) == 0o700


def test_file_lease_rejects_nested_same_process_acquisition(tmp_path: Path) -> None:
    lease = FileLease(tmp_path, "mac-mini")

    with (
        lease.acquire(LockScope.INGRESS),
        pytest.raises(LockBusyError, match="lease already held by this process"),
        lease.acquire(LockScope.INGRESS),
    ):
        pass


def test_file_lease_is_released_when_the_holder_is_killed(tmp_path: Path) -> None:
    marker = tmp_path / "holder-ready"
    script = """
from pathlib import Path
import sys
import time
from open_brain_engine.core.locks import LockScope
from open_brain_engine.storage.locks import FileLease

root = Path(sys.argv[1])
marker = Path(sys.argv[2])
with FileLease(root, "subprocess").acquire(LockScope.INDEX):
    marker.write_text("ready", encoding="utf-8")
    time.sleep(60)
"""
    holder = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path), str(marker)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and holder.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists(), holder.stderr.read() if holder.stderr is not None else ""
        assert _attempt_lease(tmp_path, LockScope.INDEX) == "busy"

        holder.kill()
        holder.wait(timeout=5)

        assert _attempt_lease(tmp_path, LockScope.INDEX) == "acquired"
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=5)


def test_file_lease_scopes_are_independent(tmp_path: Path) -> None:
    with FileLease(tmp_path, "mac-mini").acquire(LockScope.INDEX):
        assert _attempt_lease(tmp_path, LockScope.INDEX) == "busy"
        assert _attempt_lease(tmp_path, LockScope.SHARED_WRITER) == "acquired"


def test_daemon_authority_lease_is_distinct_from_shared_writer(tmp_path: Path) -> None:
    with FileLease(tmp_path, "mac-mini").acquire(LockScope.DAEMON_AUTHORITY):
        assert _attempt_lease(tmp_path, LockScope.DAEMON_AUTHORITY) == "busy"
        assert _attempt_lease(tmp_path, LockScope.SHARED_WRITER) == "acquired"


def test_backup_profile_leases_are_independent(tmp_path: Path) -> None:
    lease = FileLease(tmp_path, "mac-mini", backup_profile="capture")

    with lease.acquire(LockScope.BACKUP_PROFILE):
        assert _attempt_lease(
            tmp_path,
            LockScope.BACKUP_PROFILE,
            backup_profile="capture",
        ) == "busy"
        assert _attempt_lease(
            tmp_path,
            LockScope.BACKUP_PROFILE,
            backup_profile="full",
        ) == "acquired"


def test_file_lease_rejects_none_and_mismatched_backup_scope(tmp_path: Path) -> None:
    with (
        pytest.raises(LeaseFormatError, match="invalid lease scope"),
        FileLease(tmp_path, "mac-mini").acquire(LockScope.NONE),
    ):
        pass


def test_file_lease_structurally_satisfies_all_lease_ports(tmp_path: Path) -> None:
    lease = FileLease(tmp_path, "mac-mini")

    writer_lease: WriterLease = lease
    index_lease: IndexLease = lease
    now_lease: NowLease = lease

    assert writer_lease is lease
    assert index_lease is lease
    assert now_lease is lease


def test_lock_state_snapshot_is_empty_when_no_lease_directory_exists(tmp_path: Path) -> None:
    assert inspect_file_leases(tmp_path) == LockStateSnapshot(0, 0, None)


def test_lock_state_snapshot_reports_an_unheld_valid_descriptor(tmp_path: Path) -> None:
    with FileLease(tmp_path, "mac-mini").acquire(LockScope.INDEX):
        pass

    assert inspect_file_leases(tmp_path) == LockStateSnapshot(0, 0, None)


def test_lock_state_snapshot_observes_a_subprocess_holder(tmp_path: Path) -> None:
    marker = tmp_path / "inspection-ready"
    script = """
from pathlib import Path
import sys
import time
from open_brain_engine.core.locks import LockScope
from open_brain_engine.storage.locks import FileLease

root = Path(sys.argv[1])
marker = Path(sys.argv[2])
with FileLease(root, "subprocess").acquire(LockScope.INDEX):
    marker.write_text("ready", encoding="utf-8")
    time.sleep(60)
"""
    holder = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path), str(marker)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and holder.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists(), holder.stderr.read() if holder.stderr is not None else ""

        snapshot = inspect_file_leases(tmp_path)

        assert snapshot.held_count == 1
        assert snapshot.malformed_count == 0
        assert snapshot.oldest_acquired_at is not None
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=5)


def test_lock_state_snapshot_treats_only_unheld_malformed_metadata_as_unhealthy(
    tmp_path: Path,
) -> None:
    lock_directory = tmp_path / ".open-brain-locks"
    lock_directory.mkdir(mode=0o700)
    malformed = lock_directory / "lease.index"
    malformed.write_bytes(b"malformed")
    malformed.chmod(0o600)

    assert inspect_file_leases(tmp_path) == LockStateSnapshot(0, 1, None)


def test_lock_state_snapshot_rejects_unknown_and_symlinked_lease_files(tmp_path: Path) -> None:
    lock_directory = tmp_path / ".open-brain-locks"
    lock_directory.mkdir(mode=0o700)
    (lock_directory / "lease.unknown").write_bytes(b"unknown")
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    (lock_directory / "lease.index").symlink_to(outside)

    snapshot = inspect_file_leases(tmp_path)

    assert snapshot.malformed_count == 2
    assert outside.read_bytes() == b"outside"


def test_lock_state_snapshot_accepts_the_held_descriptor_write_window(tmp_path: Path) -> None:
    marker = tmp_path / "descriptor-window-ready"
    script = """
from pathlib import Path
import sys
import time
import open_brain_engine.storage.locks as locks
from open_brain_engine.core.locks import LockScope

root = Path(sys.argv[1])
marker = Path(sys.argv[2])
def delayed_write(file_descriptor, payload):
    marker.write_text("ready", encoding="utf-8")
    time.sleep(60)
locks._write_all = delayed_write
with locks.FileLease(root, "subprocess").acquire(LockScope.INDEX):
    pass
"""
    holder = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path), str(marker)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and holder.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists(), holder.stderr.read() if holder.stderr is not None else ""

        snapshot = inspect_file_leases(tmp_path)

        assert snapshot.held_count == 1
        assert snapshot.malformed_count == 0
        assert snapshot.oldest_acquired_at is None
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=5)
    with (
        pytest.raises(LeaseFormatError, match="backup lease profile required"),
        FileLease(tmp_path, "mac-mini").acquire(LockScope.BACKUP_PROFILE),
    ):
        pass
    with (
        pytest.raises(LeaseFormatError, match="backup lease cannot acquire another scope"),
        FileLease(tmp_path, "mac-mini", backup_profile="full").acquire(LockScope.INDEX),
    ):
        pass

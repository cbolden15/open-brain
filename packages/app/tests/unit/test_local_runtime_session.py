from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from pathlib import Path

import pytest
from open_brain_engine.core.ids import canonical_json_bytes
from open_brain_engine.storage.filesystem import capture_root_identity

import open_brain.services.local_runtime_session as runtime_session_module
from open_brain.services.local_runtime_session import (
    LocalRuntimeCompatibilityError,
    LocalRuntimeSession,
    LocalRuntimeSessionError,
    hold_local_runtime_session,
)


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "brain"
    root.mkdir(mode=0o700)
    (root / ".open-brain").mkdir(mode=0o700)
    return root


def test_live_concurrent_session_does_not_request_crash_recovery(tmp_path: Path) -> None:
    root = _root(tmp_path)
    identity = capture_root_identity(root)
    recoveries: list[str] = []

    with hold_local_runtime_session(
        root,
        identity,
        legacy_state_exists=False,
        recover_abandoned_sessions=lambda: recoveries.append("recovered"),
    ) as first:
        assert first.crash_recovery_required is False
        with hold_local_runtime_session(
            root,
            identity,
            legacy_state_exists=True,
            recover_abandoned_sessions=lambda: recoveries.append("recovered"),
        ) as second:
            assert second.crash_recovery_required is False
            assert second.live_peer_count == 1
            assert second.stale_session_count == 0
            assert recoveries == []

    assert recoveries == ["recovered"]
    assert sorted(path.name for path in (root / ".open-brain/runtime-sessions").iterdir()) == [
        "registry-version",
        "registry.lock",
    ]


def test_stale_session_marker_requests_recovery_and_is_removed(tmp_path: Path) -> None:
    root = _root(tmp_path)
    identity = capture_root_identity(root)
    recoveries: list[str] = []
    with hold_local_runtime_session(
        root,
        identity,
        legacy_state_exists=False,
        recover_abandoned_sessions=lambda: recoveries.append("recovered"),
    ):
        pass
    session_id = "a" * 32
    marker = root / f".open-brain/runtime-sessions/session-{session_id}.lock"
    marker.write_bytes(canonical_json_bytes({"pid": 12345, "session_id": session_id, "version": 1}))
    marker.chmod(0o600)

    with hold_local_runtime_session(
        root,
        identity,
        legacy_state_exists=True,
        recover_abandoned_sessions=lambda: recoveries.append("recovered"),
    ) as session:
        assert session.crash_recovery_required is True
        assert session.live_peer_count == 0
        assert session.stale_session_count == 1
        assert not marker.exists()
    assert len(recoveries) == 3


def test_unlocked_incomplete_marker_is_recovered_before_it_is_parsed(tmp_path: Path) -> None:
    root = _root(tmp_path)
    identity = capture_root_identity(root)
    with hold_local_runtime_session(
        root,
        identity,
        legacy_state_exists=False,
        recover_abandoned_sessions=lambda: None,
    ):
        pass
    marker = root / f".open-brain/runtime-sessions/session-{'c' * 32}.lock"
    marker.touch(mode=0o600)
    recoveries: list[str] = []

    with hold_local_runtime_session(
        root,
        identity,
        legacy_state_exists=True,
        recover_abandoned_sessions=lambda: recoveries.append("recovered"),
    ) as session:
        assert session.stale_session_count == 1
        assert session.crash_recovery_required is True

    assert not marker.exists()
    assert recoveries == ["recovered", "recovered"]


def test_recovery_finishes_before_a_concurrent_client_is_admitted(tmp_path: Path) -> None:
    root = _root(tmp_path)
    identity = capture_root_identity(root)
    with hold_local_runtime_session(
        root,
        identity,
        legacy_state_exists=False,
        recover_abandoned_sessions=lambda: None,
    ):
        pass
    marker = root / f".open-brain/runtime-sessions/session-{'d' * 32}.lock"
    marker.touch(mode=0o600)
    recovery_entered = threading.Event()
    recovery_resume = threading.Event()
    second_admitted = threading.Event()

    def delayed_recovery() -> None:
        recovery_entered.set()
        assert recovery_resume.wait(2)

    def recovering_client() -> None:
        with hold_local_runtime_session(
            root,
            identity,
            legacy_state_exists=True,
            recover_abandoned_sessions=delayed_recovery,
        ):
            pass

    def second_client() -> None:
        with hold_local_runtime_session(
            root,
            identity,
            legacy_state_exists=True,
            recover_abandoned_sessions=lambda: None,
        ):
            second_admitted.set()

    first_thread = threading.Thread(target=recovering_client)
    second_thread = threading.Thread(target=second_client)
    first_thread.start()
    assert recovery_entered.wait(2)
    second_thread.start()
    time.sleep(0.05)
    assert not second_admitted.is_set()
    recovery_resume.set()
    first_thread.join(2)
    second_thread.join(2)
    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert second_admitted.is_set()


def test_compatibility_rejection_preserves_stale_evidence(tmp_path: Path) -> None:
    root = _root(tmp_path)
    identity = capture_root_identity(root)
    recoveries: list[str] = []

    with hold_local_runtime_session(
        root,
        identity,
        legacy_state_exists=False,
        recover_abandoned_sessions=lambda: recoveries.append("recovered"),
    ):
        stale = root / f".open-brain/runtime-sessions/session-{'e' * 32}.lock"
        stale.touch(mode=0o600)

        def reject_live_peer(session: LocalRuntimeSession) -> None:
            assert session.live_peer_count == 1
            raise LocalRuntimeCompatibilityError("exclusive admission required")

        with (
            pytest.raises(LocalRuntimeCompatibilityError, match="exclusive admission"),
            hold_local_runtime_session(
                root,
                identity,
                legacy_state_exists=True,
                recover_abandoned_sessions=lambda: recoveries.append("unexpected"),
                admit_session=reject_live_peer,
            ),
        ):
            pass

        assert stale.exists()
        assert recoveries == []

    assert recoveries == ["recovered"]
    assert not stale.exists()


def test_admission_callback_holds_registry_lock(tmp_path: Path) -> None:
    root = _root(tmp_path)
    identity = capture_root_identity(root)
    admission_entered = threading.Event()
    admission_resume = threading.Event()
    second_admitted = threading.Event()

    def delayed_admission(_session: object) -> None:
        admission_entered.set()
        assert admission_resume.wait(2)

    def first_client() -> None:
        with hold_local_runtime_session(
            root,
            identity,
            legacy_state_exists=False,
            recover_abandoned_sessions=lambda: None,
            admit_session=delayed_admission,
        ):
            pass

    def second_client() -> None:
        with hold_local_runtime_session(
            root,
            identity,
            legacy_state_exists=False,
            recover_abandoned_sessions=lambda: None,
        ):
            second_admitted.set()

    first_thread = threading.Thread(target=first_client)
    second_thread = threading.Thread(target=second_client)
    first_thread.start()
    assert admission_entered.wait(2)
    second_thread.start()
    time.sleep(0.05)
    assert not second_admitted.is_set()
    admission_resume.set()
    first_thread.join(2)
    second_thread.join(2)
    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert second_admitted.is_set()


def test_registry_lock_wait_has_a_deadline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = _root(tmp_path)
    identity = capture_root_identity(root)
    with hold_local_runtime_session(
        root,
        identity,
        legacy_state_exists=False,
        recover_abandoned_sessions=lambda: None,
    ):
        pass
    registry = root / ".open-brain/runtime-sessions/registry.lock"
    monkeypatch.setattr(runtime_session_module, "_REGISTRY_LOCK_TIMEOUT_SECONDS", 0.02)
    with registry.open("r+b") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (
            pytest.raises(LocalRuntimeSessionError, match="registry is busy"),
            hold_local_runtime_session(
                root,
                identity,
                legacy_state_exists=True,
                recover_abandoned_sessions=lambda: None,
            ),
        ):
            pass


@pytest.mark.parametrize("legacy_state_exists", [False, True])
def test_failed_recovery_preserves_a_marker_for_the_next_opener(
    tmp_path: Path,
    legacy_state_exists: bool,
) -> None:
    root = _root(tmp_path)
    identity = capture_root_identity(root)

    def fail_recovery() -> None:
        raise RuntimeError("synthetic recovery interruption")

    with (
        pytest.raises(RuntimeError, match="synthetic recovery interruption"),
        hold_local_runtime_session(
            root,
            identity,
            legacy_state_exists=legacy_state_exists,
            recover_abandoned_sessions=fail_recovery,
        ),
    ):
        pass
    markers = tuple((root / ".open-brain/runtime-sessions").glob("session-*.lock"))
    assert len(markers) == 1
    recoveries: list[str] = []

    with hold_local_runtime_session(
        root,
        identity,
        legacy_state_exists=True,
        recover_abandoned_sessions=lambda: recoveries.append("recovered"),
    ) as recovered:
        assert recovered.crash_recovery_required is True
        assert recovered.stale_session_count == 1

    assert recoveries == ["recovered", "recovered"]
    assert not tuple((root / ".open-brain/runtime-sessions").glob("session-*.lock"))


@pytest.mark.parametrize("interrupted_file", ["marker", "version"])
def test_interrupted_registry_creation_can_be_recovered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, interrupted_file: str
) -> None:
    root = _root(tmp_path)
    identity = capture_root_identity(root)
    original_write = runtime_session_module._replace_file

    def interrupted_write(descriptor: int, payload: bytes) -> None:
        is_version = payload == runtime_session_module._REGISTRY_VERSION_BYTES
        if is_version == (interrupted_file == "version"):
            os.write(descriptor, payload[:5])
            os.fsync(descriptor)
            raise OSError("synthetic partial metadata write")
        original_write(descriptor, payload)

    with monkeypatch.context() as patch:
        patch.setattr(runtime_session_module, "_replace_file", interrupted_write)
        with (
            pytest.raises(LocalRuntimeSessionError, match="operation failed"),
            hold_local_runtime_session(
                root,
                identity,
                legacy_state_exists=False,
                recover_abandoned_sessions=lambda: None,
            ),
        ):
            pass

    with hold_local_runtime_session(
        root,
        identity,
        legacy_state_exists=True,
        recover_abandoned_sessions=lambda: None,
    ) as recovered:
        assert recovered.crash_recovery_required is True
        assert recovered.stale_session_count == 1
    registry = root / ".open-brain/runtime-sessions"
    assert not (registry / "registry-version.pending").exists()
    assert (
        registry / "registry-version"
    ).read_bytes() == runtime_session_module._REGISTRY_VERSION_BYTES


def test_existing_pre_registry_state_gets_one_conservative_recovery(tmp_path: Path) -> None:
    root = _root(tmp_path)
    identity = capture_root_identity(root)
    recoveries: list[str] = []

    with hold_local_runtime_session(
        root,
        identity,
        legacy_state_exists=True,
        recover_abandoned_sessions=lambda: recoveries.append("recovered"),
    ) as first:
        assert first.crash_recovery_required is True
    with hold_local_runtime_session(
        root,
        identity,
        legacy_state_exists=True,
        recover_abandoned_sessions=lambda: recoveries.append("recovered"),
    ) as second:
        assert second.crash_recovery_required is False
    assert len(recoveries) == 3


def test_malformed_registry_entry_fails_closed(tmp_path: Path) -> None:
    root = _root(tmp_path)
    runtime = root / ".open-brain/runtime-sessions"
    runtime.mkdir(mode=0o700)
    invalid = runtime / "unexpected"
    invalid.write_text(json.dumps({"private": "not read"}), encoding="utf-8")
    invalid.chmod(0o600)

    with (
        pytest.raises(LocalRuntimeSessionError, match="invalid entry"),
        hold_local_runtime_session(
            root,
            capture_root_identity(root),
            legacy_state_exists=False,
            recover_abandoned_sessions=lambda: None,
        ),
    ):
        pass


def test_peer_that_crashes_after_cleanup_keeps_its_marker(tmp_path: Path) -> None:
    root = _root(tmp_path)
    registry = root / ".open-brain/runtime-sessions"
    registry.mkdir(mode=0o700)
    peer = registry / f"session-{'f' * 32}.lock"
    peer.touch(mode=0o600)
    calls: list[int] = []
    with peer.open("r+b") as peer_lock:
        fcntl.flock(peer_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

        def cleanup() -> None:
            calls.append(1)
            if len(calls) == 1:
                # The peer dies after this pass has settled its journal snapshot.
                fcntl.flock(peer_lock, fcntl.LOCK_UN)

        with hold_local_runtime_session(
            root,
            capture_root_identity(root),
            legacy_state_exists=True,
            recover_abandoned_sessions=cleanup,
        ):
            assert peer.exists()
            assert len(calls) == 1
    assert len(calls) == 2
    assert not peer.exists()

from __future__ import annotations

import fcntl
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from open_brain_engine.engine.runtime_admission import (
    HeldRuntimeAdmission,
    exclusive_runtime_admission,
    hold_runtime_registry,
)
from open_brain_engine.engine.source_migration import JOURNAL, migrate_sources
from open_brain_engine.engine.t03_contracts import T03Error

from open_brain.profile import compile_single_user_local


def test_admission_proof_is_root_bound_and_expires(tmp_path: Path) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    another = compile_single_user_local(tmp_path / "another")
    with exclusive_runtime_admission(profile) as proof:
        proof.validate(profile)
        with exclusive_runtime_admission(profile) as nested:
            assert nested is proof
        with pytest.raises(T03Error):
            proof.validate(another)
    with pytest.raises(T03Error):
        proof.validate(profile)


def test_unlocked_registry_descriptor_is_not_admission(tmp_path: Path) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    with exclusive_runtime_admission(profile):
        pass
    path = profile.root / ".open-brain/runtime-sessions/registry.lock"
    descriptor = os.open(path, os.O_RDWR)
    try:
        with pytest.raises(T03Error):
            HeldRuntimeAdmission(profile, descriptor).validate(profile)
    finally:
        os.close(descriptor)


def test_live_legacy_session_blocks_exclusive_admission(tmp_path: Path) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    with exclusive_runtime_admission(profile):
        pass
    path = (
        profile.root / ".open-brain/runtime-sessions/session-00000000000000000000000000000000.lock"
    )
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(T03Error), exclusive_runtime_admission(profile):
            pytest.fail("live legacy session admitted")
    finally:
        os.close(descriptor)


def test_another_descriptor_and_live_peer_cannot_attest_admission(tmp_path: Path) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    with exclusive_runtime_admission(profile):
        pass
    registry_path = profile.root / ".open-brain/runtime-sessions/registry.lock"
    first = os.open(registry_path, os.O_RDWR)
    second = os.open(registry_path, os.O_RDWR)
    directory = os.open(registry_path.parent, os.O_RDONLY | os.O_DIRECTORY)
    session = os.open(
        registry_path.parent / "session-11111111111111111111111111111111.lock",
        os.O_RDWR | os.O_CREAT,
        0o600,
    )
    try:
        fcntl.flock(first, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(session, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(T03Error):
            HeldRuntimeAdmission(profile, second).validate(profile)
        with pytest.raises(T03Error), hold_runtime_registry(profile, directory, second):
            pytest.fail("unowned descriptor admitted")
        fcntl.flock(first, fcntl.LOCK_UN)
        with hold_runtime_registry(profile, directory, second) as proof:
            assert proof.live_peer_count == 1
            with pytest.raises(T03Error):
                migrate_sources(profile, admission=proof, clock=lambda: datetime.now(UTC))
            assert not (profile.root / JOURNAL).exists()
            with pytest.raises(T03Error), exclusive_runtime_admission(profile):
                pytest.fail("live peer admitted")
    finally:
        for descriptor in (session, directory, second, first):
            os.close(descriptor)

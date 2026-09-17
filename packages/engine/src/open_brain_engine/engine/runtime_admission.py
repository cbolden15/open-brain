"""Root-bound proof that the unchanged foreground registry lock is held."""

from __future__ import annotations

import errno
import fcntl
import os
import re
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from open_brain_engine.storage.filesystem import (
    _open_child_directory,
    _open_root,
    assert_root_identity,
)

from .contracts import LocalEngineContext
from .t03_contracts import T03Error

_ACTIVE: ContextVar[HeldRuntimeAdmission | None] = ContextVar(
    "engine_runtime_admission", default=None
)
_SESSION = re.compile(r"session-[0-9a-f]{32}\.lock")


def _private_file(directory: int, name: str, *, create: bool = False) -> int:
    descriptor = os.open(
        name, os.O_RDWR | os.O_NOFOLLOW | (os.O_CREAT if create else 0), 0o600, dir_fd=directory
    )
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        os.close(descriptor)
        raise T03Error("operation_pending")
    return descriptor


@dataclass(slots=True)
class HeldRuntimeAdmission:
    profile: LocalEngineContext
    descriptor: int
    active: bool = True

    def validate(self, profile: LocalEngineContext) -> None:
        if (
            not self.active
            or self.profile.root != profile.root
            or self.profile.root_identity != profile.root_identity
        ):
            raise T03Error("operation_pending")
        assert_root_identity(profile.root, profile.root_identity)
        held = os.fstat(self.descriptor)
        root = state = directory = probe = -1
        try:
            root = _open_root(profile.root, profile.root_identity)
            state = _open_child_directory(root, ".open-brain", create=False)
            directory = _open_child_directory(state, "runtime-sessions", create=False)
            probe = _private_file(directory, "registry.lock")
            observed = os.fstat(probe)
            if (held.st_dev, held.st_ino) != (observed.st_dev, observed.st_ino):
                raise T03Error("operation_pending")
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno in {errno.EAGAIN, errno.EACCES}:
                    return
                raise
            # An unlocked or released descriptor is not a capability.
            fcntl.flock(probe, fcntl.LOCK_UN)
            raise T03Error("operation_pending")
        finally:
            for descriptor in (probe, directory, state, root):
                if descriptor >= 0:
                    os.close(descriptor)


@contextmanager
def attest_runtime_admission(profile: LocalEngineContext, descriptor: int) -> Iterator[None]:
    proof = HeldRuntimeAdmission(profile, descriptor)
    proof.validate(profile)
    token = _ACTIVE.set(proof)
    try:
        yield
    finally:
        proof.active = False
        _ACTIVE.reset(token)


@contextmanager
def exclusive_runtime_admission(profile: LocalEngineContext) -> Iterator[HeldRuntimeAdmission]:
    existing = _ACTIVE.get()
    if existing is not None:
        existing.validate(profile)
        yield existing
        return
    root = state = directory = registry = -1
    locked = False
    try:
        root = _open_root(profile.root, profile.root_identity)
        state = _open_child_directory(root, ".open-brain", create=True)
        directory = _open_child_directory(state, "runtime-sessions", create=True)
        metadata = os.fstat(directory)
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise T03Error("operation_pending")
        registry = _private_file(directory, "registry.lock", create=True)
        deadline = time.monotonic() + 2
        while True:
            try:
                fcntl.flock(registry, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError as error:
                if error.errno not in {errno.EAGAIN, errno.EACCES}:
                    raise
                if time.monotonic() >= deadline:
                    raise T03Error("operation_pending") from None
                time.sleep(0.01)
        for name in os.listdir(directory):
            if name in {"registry.lock", "registry-version", "registry-version.pending"}:
                continue
            if _SESSION.fullmatch(name) is None:
                raise T03Error("operation_pending")
            session = _private_file(directory, name)
            try:
                try:
                    fcntl.flock(session, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    raise T03Error("operation_pending") from None
                fcntl.flock(session, fcntl.LOCK_UN)
            finally:
                os.close(session)
        with attest_runtime_admission(profile, registry):
            proof = _ACTIVE.get()
            assert proof is not None
            yield proof
    except OSError:
        raise T03Error("operation_pending") from None
    finally:
        if locked:
            fcntl.flock(registry, fcntl.LOCK_UN)
        for descriptor in (registry, directory, state, root):
            if descriptor >= 0:
                os.close(descriptor)

"""Private durable source state and bounded OS credential access."""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import cast

from open_brain_connectors.runtime.live_common import LiveSourceError, bounded_json, safe_text

_MAX_BYTES = 4_194_304
_MAX_CREDENTIAL = 65_536
_SERVICE = "io.openbrain.sources"


class PrivateJsonStore:
    """No-follow, owner-only JSON files; atomic durable writes and advisory locks."""

    def __init__(self, root: Path) -> None:
        if not isinstance(root, Path) or not root.is_absolute() or ".." in root.parts:
            raise LiveSourceError("source_invalid_storage")
        self.root = root
        with self._directory():
            pass

    @contextmanager
    def _directory(self) -> Iterator[int]:
        fd: int | None = None
        try:
            fd = os.open(self.root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            for part in self.root.parts[1:]:
                with suppress(FileExistsError):
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise LiveSourceError("source_unsafe_storage")
            yield fd
        except OSError:
            raise LiveSourceError("source_storage_unavailable") from None
        finally:
            if fd is not None:
                os.close(fd)

    @staticmethod
    def _name(name: str, *, json_file: bool = True) -> str:
        if (
            type(name) is not str
            or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,179}", name)
            or (json_file and not name.endswith(".json"))
        ):
            raise LiveSourceError("source_invalid_storage_name")
        return name

    @staticmethod
    def _check_file(fd: int) -> None:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise LiveSourceError("source_unsafe_storage")

    def read(self, name: str) -> object | None:
        name = self._name(name)
        with self._directory() as directory:
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            except FileNotFoundError:
                return None
            except OSError:
                raise LiveSourceError("source_unsafe_storage") from None
            with os.fdopen(fd, "rb") as handle:
                self._check_file(handle.fileno())
                raw = handle.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            raise LiveSourceError("source_state_too_large")
        try:
            return cast(object, json.loads(raw))
        except ValueError, UnicodeError, RecursionError:
            raise LiveSourceError("source_invalid_state") from None

    def write(self, name: str, value: object) -> None:
        name = self._name(name)
        raw = bounded_json(value, _MAX_BYTES)
        with self._directory() as directory:
            temporary = f".pending-{uuid.uuid4().hex}"
            try:
                fd = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory,
                )
                with os.fdopen(fd, "wb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    target = os.open(
                        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
                    )
                except FileNotFoundError:
                    target = None
                if target is not None:
                    try:
                        self._check_file(target)
                    finally:
                        os.close(target)
                os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
                os.fsync(directory)
            except OSError:
                raise LiveSourceError("source_storage_unavailable") from None
            finally:
                with suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=directory)

    def delete(self, name: str) -> None:
        name = self._name(name)
        with self._directory() as directory:
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            except FileNotFoundError:
                return
            try:
                self._check_file(fd)
            finally:
                os.close(fd)
            os.unlink(name, dir_fd=directory)
            os.fsync(directory)

    def names(self, prefix: str) -> tuple[str, ...]:
        if prefix:
            self._name(prefix, json_file=False)
        with self._directory() as directory:
            names = tuple(
                sorted(
                    n for n in os.listdir(directory) if n.startswith(prefix) and n.endswith(".json")
                )
            )
            if len(names) > 10_000:
                raise LiveSourceError("source_state_too_large")
            return names

    @contextmanager
    def lock(self, name: str, *, timeout_seconds: float = 5) -> Iterator[None]:
        name = self._name(name, json_file=False) + ".lock"
        if not 0 <= timeout_seconds <= 180:
            raise LiveSourceError("source_invalid_lock")
        with self._directory() as directory:
            deadline = time.monotonic() + timeout_seconds
            while True:
                try:
                    fd = os.open(
                        name,
                        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                        0o600,
                        dir_fd=directory,
                    )
                    break
                except FileNotFoundError:
                    # Concurrent first opens can race with creation on macOS.
                    if time.monotonic() >= deadline:
                        raise LiveSourceError("source_busy") from None
                    time.sleep(min(0.05, max(0, deadline - time.monotonic())))
            try:
                self._check_file(fd)
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise LiveSourceError("source_busy") from None
                        time.sleep(min(0.05, max(0, deadline - time.monotonic())))
                yield
            finally:
                os.close(fd)


class OsCredentialStore:
    """Credentials use Keychain or Secret Service, with no plaintext fallback."""

    def __init__(self) -> None:
        self._mac = sys.platform == "darwin"
        candidates = (
            ("/usr/bin/security",) if self._mac else ("/usr/bin/secret-tool", "/bin/secret-tool")
        )
        self._executable = next((p for p in candidates if Path(p).is_file()), None)

    def _run(
        self, reference: str, operation: str, payload: bytes | None = None
    ) -> tuple[int, bytes]:
        safe_text(reference, maximum=200)
        if not re.fullmatch(r"[a-zA-Z0-9._:-]+", reference) or self._executable is None:
            raise LiveSourceError("credential_store_unavailable")
        if self._mac:
            command = [sys.executable, "-I", "-m", "open_brain_connectors.runtime.live_keychain"]
            payload = bounded_json(
                {
                    "operation": operation,
                    "reference": reference,
                    "payload": payload.decode() if payload is not None else None,
                }
            )
        else:
            args = {
                "get": ["lookup"],
                "status": ["lookup"],
                "set": ["store", "--label=Open Brain source"],
                "delete": ["clear"],
            }[operation]
            args += ["application", _SERVICE, "account", reference]
            command = [self._executable, *args]
        environment = {
            key: value
            for key, value in os.environ.items()
            if key
            in {
                "HOME",
                "USER",
                "LOGNAME",
                "DBUS_SESSION_BUS_ADDRESS",
                "XDG_RUNTIME_DIR",
                "DISPLAY",
                "WAYLAND_DISPLAY",
            }
        }
        environment.update({"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
        try:
            with tempfile.TemporaryFile() as output:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    env=environment,
                    cwd="/",
                    start_new_session=True,
                )
                try:
                    process.communicate(payload, timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=2)
                    raise LiveSourceError("credential_store_locked") from None
                if output.tell() > _MAX_CREDENTIAL + 1:
                    raise LiveSourceError("credential_invalid")
                output.seek(0)
                result = output.read(_MAX_CREDENTIAL + 1) if operation == "get" else b""
                return process.returncode, result
        except OSError:
            raise LiveSourceError("credential_store_unavailable") from None

    def get(self, reference: str) -> dict[str, object] | None:
        code, raw = self._run(reference, "get")
        if code in ({44} if self._mac else {1}):
            return None
        if code:
            raise LiveSourceError("credential_store_locked")
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError
            return cast(dict[str, object], value)
        except ValueError, UnicodeError:
            raise LiveSourceError("credential_invalid") from None

    def set(self, reference: str, value: dict[str, object]) -> None:
        payload = bounded_json(value, _MAX_CREDENTIAL - 1) + b"\n"
        code, _ = self._run(reference, "set", payload)
        if code:
            raise LiveSourceError("credential_store_locked")

    def delete(self, reference: str) -> None:
        code, _ = self._run(reference, "delete")
        if code not in ({0, 44} if self._mac else {0, 1}):
            raise LiveSourceError("credential_store_locked")

    def status(self, reference: str) -> str:
        try:
            code, _ = self._run(reference, "status")
        except LiveSourceError:
            return "locked"
        if code == 0:
            return "available"
        return "missing" if code in ({44} if self._mac else {1}) else "locked"

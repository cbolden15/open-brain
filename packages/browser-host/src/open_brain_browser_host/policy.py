"""Private installation binding and user-level Chrome registration."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from open_brain_engine.engine.browser_pages import BrowserBinding

HOST_NAME = "com.open_brain.saved_items"
_POLICY_DIRECTORY = (".open-brain-browser",)
_POLICY_NAME = "policy.json"
_POLICY_LIMIT = 16 * 1024
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class Refused(Exception):
    """An error code safe to return on the native channel."""


def strict_json(raw: bytes) -> object:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON field")
            value[key] = item
        return value

    def reject_constant(_: str) -> object:
        raise ValueError("invalid JSON constant")

    return cast(
        object,
        json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=reject_constant),
    )


def encoded(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def extension_origin(extension_id: str) -> str:
    if not re.fullmatch(r"[a-p]{32}", extension_id):
        raise ValueError("invalid extension ID")
    return f"chrome-extension://{extension_id}/"


def binding_value(binding: BrowserBinding) -> dict[str, object]:
    return {
        "brain_id": binding.brain_id,
        "issuer_epoch": binding.issuer_epoch,
        "authorization_generation": binding.generation,
        "source": list(binding.source.values()),
        "workflow_principal": binding.workflow_principal,
        "operations": sorted(binding.operations),
        "tiers": sorted(binding.tiers),
        "spaces": sorted(binding.spaces, key=lambda item: item or ""),
        "complete_domain": binding.complete_domain,
    }


@dataclass(frozen=True, slots=True)
class Policy:
    extension_id: str
    brain_root: Path
    token: str
    binding: dict[str, object]

    @classmethod
    def parse(cls, raw: bytes) -> Policy:
        value = strict_json(raw)
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "version",
                "extension_id",
                "brain_root",
                "token",
                "binding",
            }
            or value["version"] != "browser-policy.v1"
        ):
            raise ValueError("invalid browser policy")
        extension_id, root, token = (value[key] for key in ("extension_id", "brain_root", "token"))
        if not isinstance(extension_id, str) or not isinstance(root, str):
            raise ValueError("invalid browser policy")
        extension_origin(extension_id)
        if not Path(root).is_absolute() or "\x00" in root:
            raise ValueError("invalid browser policy")
        if not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{64}", token) is None:
            raise ValueError("invalid browser policy")
        binding = value["binding"]
        if not isinstance(binding, dict):
            raise ValueError("invalid browser policy")
        return cls(extension_id, Path(root), token, binding)

    def require(self, binding: BrowserBinding) -> None:
        if encoded(self.binding) != encoded(binding_value(binding)):
            raise Refused("browser_unbound")

    def encode(self) -> bytes:
        return encoded(
            {
                "version": "browser-policy.v1",
                "extension_id": self.extension_id,
                "brain_root": str(self.brain_root),
                "token": self.token,
                "binding": self.binding,
            }
        )


@dataclass(frozen=True, slots=True)
class RemotePolicy:
    extension_id: str
    destination: str
    host_key: str
    identity_file: str
    canonical_id: str

    def __post_init__(self) -> None:
        extension_origin(self.extension_id)
        if len(self.destination) > 120 or not re.fullmatch(
            r"[a-z_][a-z0-9_-]{0,31}@[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}", self.destination
        ):
            raise ValueError("invalid SSH destination")
        if not re.fullmatch(r"[0-9a-f]{64}", self.canonical_id):
            raise ValueError("invalid canonical binding")
        if not re.fullmatch(r"/[a-zA-Z0-9_./-]+", self.identity_file):
            raise ValueError("invalid SSH identity path")
        prefix = "ssh-ed25519 "
        if not self.host_key.startswith(prefix):
            raise ValueError("expected an Ed25519 public host key")
        key = base64.b64decode(self.host_key[len(prefix) :], validate=True)
        if len(key) != 51 or key[:19] != b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20":
            raise ValueError("invalid Ed25519 host key")

    def encode(self) -> bytes:
        return encoded(
            {
                "version": "browser-remote-policy.v1",
                "extension_id": self.extension_id,
                "destination": self.destination,
                "host_key": self.host_key,
                "identity_file": self.identity_file,
                "canonical_id": self.canonical_id,
            }
        )


def _check_directory(fd: int, *, private: bool) -> None:
    metadata = os.fstat(fd)
    invalid_mode = 0o077 if private else 0o022
    if metadata.st_uid != os.geteuid() or metadata.st_mode & invalid_mode:
        raise PermissionError("unsafe browser setup directory")


@contextmanager
def _home_directory(parts: tuple[str, ...], *, create: bool, private: bool) -> Iterator[int]:
    home = Path.home()
    if not home.is_absolute():
        raise ValueError("invalid home directory")
    fd = os.open(home, _DIRECTORY_FLAGS)
    try:
        _check_directory(fd, private=False)
        for index, part in enumerate(parts):
            if create:
                with suppress(FileExistsError):
                    os.mkdir(part, mode=0o700, dir_fd=fd)
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
            _check_directory(fd, private=private and index == len(parts) - 1)
        yield fd
    finally:
        os.close(fd)


def _read_private(parent: int, filename: str) -> bytes:
    fd = os.open(filename, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
    with os.fdopen(fd, "rb") as source:
        meta = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(meta.st_mode)
            or meta.st_uid != os.geteuid()
            or stat.S_IMODE(meta.st_mode) != 0o600
            or meta.st_nlink != 1
            or meta.st_size > _POLICY_LIMIT
        ):
            raise PermissionError("unsafe browser setup file")
        data = source.read(_POLICY_LIMIT + 1)
        if len(data) > _POLICY_LIMIT:
            raise ValueError("oversized policy")
        return data


def _replace(parent: int, name: str, payload: bytes) -> None:
    temporary = "." + name + "." + secrets.token_hex(16)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(temporary, flags, 0o600, dir_fd=parent)
    try:
        with os.fdopen(fd, "wb") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=parent)


@contextmanager
def lock_setup(*, create: bool) -> Iterator[int]:
    """Serialize owner changes using the same verified directory for reads and writes."""
    with _home_directory(_POLICY_DIRECTORY, create=create, private=True) as directory:
        fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield directory


def read(directory: int) -> Policy | RemotePolicy:
    raw = _read_private(directory, _POLICY_NAME)
    data = strict_json(raw)
    if isinstance(data, dict) and data.get("version") == "browser-remote-policy.v1":
        fields = {"extension_id", "destination", "host_key", "identity_file", "canonical_id"}
        if set(data) != fields | {"version"} or not all(isinstance(data[k], str) for k in fields):
            raise ValueError("invalid remote policy")
        return RemotePolicy(**{k: data[k] for k in fields})
    return Policy.parse(raw)


def load_installation() -> Policy | RemotePolicy:
    try:
        with _home_directory(_POLICY_DIRECTORY, create=False, private=True) as directory:
            return read(directory)
    except OSError, ValueError, RecursionError:
        raise Refused("browser_unbound") from None


def load() -> Policy:
    policy = load_installation()
    if not isinstance(policy, Policy):
        raise Refused("browser_unbound")
    return policy


def save(directory: int, policy: Policy | RemotePolicy) -> None:
    payload = policy.encode()
    if len(payload) > _POLICY_LIMIT:
        raise ValueError("oversized policy")
    _replace(directory, _POLICY_NAME, payload)


def _chrome_directory() -> tuple[str, ...]:
    if sys.platform == "darwin":
        return ("Library", "Application Support", "Google", "Chrome", "NativeMessagingHosts")
    if sys.platform.startswith("linux"):
        return (".config", "google-chrome", "NativeMessagingHosts")
    raise ValueError("unsupported Chrome platform")


def _registration_directory(user_data_dir: Path | None) -> tuple[str, ...]:
    if user_data_dir is None:
        return _chrome_directory()
    if not user_data_dir.is_absolute() or ".." in user_data_dir.parts:
        raise ValueError("Chrome user data directory must be absolute")
    return (*user_data_dir.relative_to(Path.home()).parts, "NativeMessagingHosts")


def _registration_path(parts: tuple[str, ...]) -> Path:
    return Path.home().joinpath(*parts, HOST_NAME + ".json")


def _receipt_name(parts: tuple[str, ...]) -> str:
    return "registration-" + hashlib.sha256(encoded(parts)).hexdigest() + ".json"


def _registration_hashes(directory: int, parts: tuple[str, ...]) -> list[str]:
    try:
        hashes = strict_json(_read_private(directory, _receipt_name(parts)))
    except FileNotFoundError:
        return []
    if not (
        isinstance(hashes, list)
        and 1 <= len(hashes) <= 2
        and all(
            isinstance(digest, str) and re.fullmatch("[0-9a-f]{64}", digest) for digest in hashes
        )
    ):
        raise ValueError("invalid registration receipt")
    return hashes


def registration_status(directory: int, user_data_dir: Path | None) -> dict[str, str]:
    parts = _registration_directory(user_data_dir)
    path = _registration_path(parts)
    try:
        with _home_directory(parts, create=False, private=False) as fd:
            current = _read_private(fd, path.name)
    except FileNotFoundError:
        return {"state": "absent", "manifest": str(path)}
    owned = hashlib.sha256(current).hexdigest() in _registration_hashes(directory, parts)
    return {"state": "registered" if owned else "foreign", "manifest": str(path)}


def unregister(directory: int, user_data_dir: Path | None) -> dict[str, str]:
    parts = _registration_directory(user_data_dir)
    path = _registration_path(parts)
    status = registration_status(directory, user_data_dir)
    if status["state"] == "foreign":
        raise ValueError("native registration belongs to a different installation")
    if status["state"] == "registered":
        with _home_directory(parts, create=False, private=False) as fd:
            os.unlink(path.name, dir_fd=fd)
            os.fsync(fd)
        status["state"] = "removed"
    with suppress(FileNotFoundError):
        os.unlink(_receipt_name(parts), dir_fd=directory)
        os.fsync(directory)
    return status


def install(
    policy: Policy | RemotePolicy,
    executable: Path,
    *,
    user_data_dir: Path | None = None,
    setup_directory: int | None = None,
) -> Path:
    if setup_directory is None:
        with lock_setup(create=True) as directory:
            return install(
                policy, executable, user_data_dir=user_data_dir, setup_directory=directory
            )
    executable = executable.resolve(strict=True)
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError("browser host executable unavailable")
    payload = encoded(
        {
            "name": HOST_NAME,
            "description": "Open Brain saved items",
            "path": str(executable),
            "type": "stdio",
            "allowed_origins": [extension_origin(policy.extension_id)],
        }
    )
    parts = _registration_directory(user_data_dir)
    path = _registration_path(parts)
    receipt = _receipt_name(parts)
    digest = hashlib.sha256(payload).hexdigest()
    with _home_directory(parts, create=True, private=False) as fd:
        allowed = [digest]
        try:
            current = hashlib.sha256(_read_private(fd, path.name)).hexdigest()
            if current not in _registration_hashes(setup_directory, parts):
                raise ValueError("native registration belongs to a different installation")
            if current != digest:
                allowed.append(current)
        except FileNotFoundError:
            pass
        # Either manifest can be present if an upgrade is interrupted.
        _replace(setup_directory, receipt, encoded(allowed))
        _replace(fd, path.name, payload)
        if len(allowed) > 1:
            _replace(setup_directory, receipt, encoded([digest]))
    return path

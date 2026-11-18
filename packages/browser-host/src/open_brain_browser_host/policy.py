"""Private installation binding and user-level Chrome registration."""

from __future__ import annotations

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


def load() -> Policy:
    try:
        with _home_directory((".open-brain-browser",), create=False, private=True) as fd:
            return Policy.parse(_read_private(fd, _POLICY_NAME))
    except OSError, ValueError, RecursionError:
        raise Refused("browser_unbound") from None


def save(policy: Policy) -> None:
    payload = policy.encode()
    if len(payload) > _POLICY_LIMIT:
        raise ValueError("oversized policy")
    with _home_directory((".open-brain-browser",), create=True, private=True) as fd:
        _replace(fd, _POLICY_NAME, payload)


def _chrome_directory() -> tuple[str, ...]:
    if sys.platform == "darwin":
        return ("Library", "Application Support", "Google", "Chrome", "NativeMessagingHosts")
    if sys.platform.startswith("linux"):
        return (".config", "google-chrome", "NativeMessagingHosts")
    raise ValueError("unsupported Chrome platform")


def install(policy: Policy, executable: Path, *, user_data_dir: Path | None = None) -> Path:
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
    if user_data_dir is None:
        directory = _chrome_directory()
    else:
        home = Path.home()
        if not user_data_dir.is_absolute() or ".." in user_data_dir.parts:
            raise ValueError("Chrome user data directory must be absolute")
        directory = (*user_data_dir.relative_to(home).parts, "NativeMessagingHosts")
    filename = HOST_NAME + ".json"
    with _home_directory(directory, create=True, private=False) as fd:
        _replace(fd, filename, payload)
    return Path.home().joinpath(*directory, filename)

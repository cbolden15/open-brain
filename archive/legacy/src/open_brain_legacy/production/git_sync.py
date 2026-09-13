"""Owner-only Git inventory loading and a closed production command runner."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import cast

from open_brain_engine.core.ids import canonical_json_bytes

from open_brain_legacy.operations.git_sync_runtime import (
    GitCommand,
    GitCommandResult,
    GitRepositoryBinding,
    GitRepositoryKind,
)
from open_brain_legacy.operations.writer_jobs import WriterJobError

_MAXIMUM_INVENTORY_BYTES = 64 * 1024
_MAXIMUM_GIT_OUTPUT_BYTES = 64 * 1024
_COMMIT_MESSAGE = re.compile(r"open-brain sync [A-Za-z0-9_.-]{1,128} [0-9a-f]{12}")
_UTC_TIMESTAMP_ARGUMENT = re.compile(
    r"--(?:since|until)=[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}Z"
)
_READ_ACTIONS = frozenset(
    {
        ("rev-parse", "--show-toplevel"),
        ("status", "--porcelain=v1", "--branch"),
        ("remote", "get-url", "--push", "origin"),
    }
)
_WRITE_ACTIONS = frozenset({("add", "--all"), ("push",)})


class GitInventoryError(RuntimeError):
    """Private repository topology or a Git command failed closed."""


@dataclass(frozen=True, slots=True)
class PrivateGitInventory:
    home_root: Path
    dev_root: Path
    repositories: tuple[GitRepositoryBinding, ...]
    digest_sha256: str


def load_private_git_inventory(path: Path) -> PrivateGitInventory:
    """Load one canonical owner-only inventory without exposing topology."""
    payload = _read_owner_only(path)
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
        if (
            type(value) is not dict
            or set(value) != {"version", "home_root", "dev_root", "repositories"}
            or value["version"] != 1
            or not isinstance(value["repositories"], list)
            or not value["repositories"]
        ):
            raise ValueError
        home_root = _absolute_root(value["home_root"])
        dev_root = _absolute_root(value["dev_root"])
        repositories = tuple(_binding(item) for item in value["repositories"])
        if (
            len({item.repo_id for item in repositories}) != len(repositories)
            or canonical_json_bytes(value) != payload
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError, WriterJobError):
        raise GitInventoryError("invalid private Git inventory") from None
    return PrivateGitInventory(
        home_root=home_root,
        dev_root=dev_root,
        repositories=repositories,
        digest_sha256=sha256(payload).hexdigest(),
    )


class SubprocessGitCommandRunner:
    """Run only the Git argv emitted by the typed planner with bounded output."""

    def __init__(
        self,
        *,
        home_root: Path,
        allowed_roots: tuple[Path, ...],
        git_executable: Path = Path("/usr/bin/git"),
    ) -> None:
        try:
            self._home_root = _existing_directory(home_root)
            self._allowed_roots = tuple(_existing_directory(root) for root in allowed_roots)
            executable = git_executable.resolve(strict=True)
            metadata = executable.lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o022:
                raise ValueError
        except (OSError, ValueError):
            raise GitInventoryError("Git command runner unavailable") from None
        if not self._allowed_roots:
            raise GitInventoryError("Git command runner unavailable")
        self._git_executable = executable

    def run(self, command: GitCommand) -> GitCommandResult:
        if not isinstance(command, GitCommand):
            raise GitInventoryError("Git command refused")
        cwd = _allowed_cwd(command.cwd, self._allowed_roots)
        action = command.argv[3:] if command.argv[:3] == ("git", "-c", "core.askPass=") else ()
        if not _allowed_action(action):
            raise GitInventoryError("Git command refused")
        try:
            completed = subprocess.run(
                (str(self._git_executable), "-c", "core.askPass=", *action),
                cwd=cwd,
                env={
                    "HOME": str(self._home_root),
                    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                    "LC_ALL": "C",
                    "GIT_TERMINAL_PROMPT": "0",
                    "GIT_SSH_COMMAND": "/usr/bin/ssh -oBatchMode=yes",
                },
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=float(command.timeout_seconds),
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            raise GitInventoryError("Git command failed") from None
        stdout = completed.stdout if _allowed_read_action(action) else b""
        if len(stdout) > _MAXIMUM_GIT_OUTPUT_BYTES:
            raise GitInventoryError("Git command failed")
        return GitCommandResult(completed.returncode, stdout=stdout, stderr=b"")


def _read_owner_only(path: Path) -> bytes:
    if not isinstance(path, Path) or not path.is_absolute():
        raise GitInventoryError("private Git inventory unavailable")
    descriptor = -1
    try:
        metadata = path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_mode & 0o077
            or metadata.st_uid not in {0, os.geteuid()}
            or not 1 <= metadata.st_size <= _MAXIMUM_INVENTORY_BYTES
        ):
            raise OSError
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        current = os.fstat(descriptor)
        if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise OSError
        payload = os.read(descriptor, _MAXIMUM_INVENTORY_BYTES + 1)
        if len(payload) != metadata.st_size:
            raise OSError
        return payload
    except OSError:
        raise GitInventoryError("private Git inventory unavailable") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _absolute_root(value: object) -> Path:
    if not isinstance(value, str) or not value or "~" in value:
        raise ValueError
    return _existing_directory(Path(value))


def _existing_directory(path: Path) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise ValueError
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError
    return path.resolve(strict=True)


def _binding(value: object) -> GitRepositoryBinding:
    if type(value) is not dict or set(value) != {
        "repo_id",
        "kind",
        "relative_path",
        "record_id",
        "digest_sha256",
        "push_target_digest_sha256",
    }:
        raise ValueError
    mapping = cast(dict[str, object], value)
    raw_path = mapping["relative_path"]
    if not isinstance(raw_path, str):
        raise ValueError
    return GitRepositoryBinding(
        repo_id=_string(mapping["repo_id"]),
        kind=GitRepositoryKind(_string(mapping["kind"])),
        relative_path=PurePosixPath(raw_path),
        record_id=_string(mapping["record_id"]),
        digest_sha256=_string(mapping["digest_sha256"]),
        push_target_digest_sha256=_optional_string(
            mapping["push_target_digest_sha256"]
        ),
    )


def _allowed_cwd(cwd: Path, roots: tuple[Path, ...]) -> Path:
    try:
        resolved = _existing_directory(cwd)
        if not any(resolved == root or root in resolved.parents for root in roots):
            raise ValueError
        return resolved
    except (OSError, ValueError):
        raise GitInventoryError("Git command refused") from None


def _allowed_action(action: tuple[str, ...]) -> bool:
    if _allowed_read_action(action) or action in _WRITE_ACTIONS:
        return True
    return (
        len(action) == 3
        and action[:2] == ("commit", "--message")
        and _COMMIT_MESSAGE.fullmatch(action[2]) is not None
    )


def _allowed_read_action(action: tuple[str, ...]) -> bool:
    return action in _READ_ACTIONS or (
        len(action) == 6
        and action[0] == "log"
        and _UTC_TIMESTAMP_ARGUMENT.fullmatch(action[1]) is not None
        and action[1].startswith("--since=")
        and _UTC_TIMESTAMP_ARGUMENT.fullmatch(action[2]) is not None
        and action[2].startswith("--until=")
        and action[3:] == ("--format=%H", "--max-count=100", "--no-merges")
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError
    return value


def _optional_string(value: object) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError
    return value

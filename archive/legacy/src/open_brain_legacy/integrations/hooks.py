"""Pure, dry-run-first planning for temporary repository hooks."""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
from pathlib import Path

from open_brain_legacy._compat.open_brain.integrations.ports import (
    HookEmitResult,
    HookInstallRequest,
    HookInstallResult,
    HookInstallStatus,
    HookKind,
    HookSignalStatus,
    PostCommitSignal,
    PostCommitSignalPort,
)

_POST_COMMIT_TEMPLATE = (
    "#!/bin/sh\n"
    'revision="$(git rev-parse --verify HEAD 2>/dev/null)" || exit 0\n'
    "python -m open_brain_legacy.integrations.hooks post-commit {repository_id} "
    '"$revision" </dev/null >/dev/null 2>&1 &\n'
    "exit 0\n"
)


def _template_for(*, hook_kind: HookKind, repository_id: str) -> str:
    if hook_kind is not HookKind.POST_COMMIT:
        raise ValueError("unsupported hook kind")
    return _POST_COMMIT_TEMPLATE.format(repository_id=repository_id)


@dataclass(frozen=True, slots=True)
class HookPlan:
    """A bounded, path-free template plan for one allow-listed hook kind."""

    repository_id: str
    hook_kind: HookKind
    template: str
    template_digest: str

    def __post_init__(self) -> None:
        expected_template = _template_for(
            hook_kind=self.hook_kind,
            repository_id=self.repository_id,
        )
        if (
            self.template != expected_template
            or self.template_digest != sha256(expected_template.encode()).hexdigest()
        ):
            raise ValueError("invalid hook plan")


class HookCompatibilityAction(StrEnum):
    """Compatibility actions retained for predecessor-facing callers."""

    RETAIN = "retain"
    RETIRE = "retire"


class HookCompatibilityDisposition(StrEnum):
    """Closed outcomes for the native hook implementation."""

    IMPLEMENTATION_READY = "implementation-ready"
    RETIREMENT_BLOCKED = "retirement-blocked"


@dataclass(frozen=True, slots=True)
class HookCompatibilityResult:
    """A path-free compatibility result with no production side effect."""

    action: HookCompatibilityAction
    disposition: HookCompatibilityDisposition


@dataclass(frozen=True, slots=True, init=False)
class RepositoryHookCapability:
    """An opaque install capability bound to one repository's hooks directory."""

    repository_id: str
    _repository_root: Path = field(repr=False)
    _repository_identity: tuple[int, int] = field(repr=False)
    _git_directory: Path = field(repr=False)
    _git_identity: tuple[int, int] = field(repr=False)
    _hooks_directory: Path = field(repr=False)
    _hooks_identity: tuple[int, int] = field(repr=False)

    def __init__(self, *_: object, **__: object) -> None:
        raise TypeError("RepositoryHookCapability must be created by bind")

    @classmethod
    def bind(
        cls, *, repository_id: str, repository_root: Path
    ) -> RepositoryHookCapability:
        """Bind an opaque capability to an existing, non-symlink Git repository."""
        HookInstallRequest(
            repository_id=repository_id,
            hook_kind=HookKind.POST_COMMIT,
        )
        root = repository_root.absolute()
        git_directory = root / ".git"
        hooks_directory = git_directory / "hooks"
        repository_identity = _safe_directory_identity(root)
        git_identity = _safe_directory_identity(git_directory)
        hooks_identity = _safe_directory_identity(hooks_directory)

        capability = object.__new__(cls)
        object.__setattr__(capability, "repository_id", repository_id)
        object.__setattr__(capability, "_repository_root", root)
        object.__setattr__(capability, "_repository_identity", repository_identity)
        object.__setattr__(capability, "_git_directory", git_directory)
        object.__setattr__(capability, "_git_identity", git_identity)
        object.__setattr__(capability, "_hooks_directory", hooks_directory)
        object.__setattr__(capability, "_hooks_identity", hooks_identity)
        return capability

    def _install(self, *, hook_kind: HookKind, template: str) -> HookInstallStatus:
        if hook_kind is not HookKind.POST_COMMIT or not self._is_current():
            return HookInstallStatus.BLOCKED

        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(
            os, "O_NOFOLLOW", 0
        )
        try:
            directory_fd = os.open(self._hooks_directory, flags)
        except OSError:
            return HookInstallStatus.BLOCKED

        try:
            if _identity(os.fstat(directory_fd)) != self._hooks_identity:
                return HookInstallStatus.BLOCKED
            return _install_hook_file(
                directory_fd=directory_fd,
                filename="post-commit",
                template=template.encode(),
            )
        finally:
            os.close(directory_fd)

    def _is_current(self) -> bool:
        return all(
            _matches_directory_identity(path, identity)
            for path, identity in (
                (self._repository_root, self._repository_identity),
                (self._git_directory, self._git_identity),
                (self._hooks_directory, self._hooks_identity),
            )
        )


class TemporaryHookPlanner:
    """Dry-run-first planning with optional repository-confined installation."""

    def __init__(self, capability: RepositoryHookCapability | None = None) -> None:
        self._capability = capability

    def plan(self, request: HookInstallRequest) -> HookPlan:
        """Return the fixed fail-zero template without identifying a filesystem target."""
        template = _template_for(
            hook_kind=request.hook_kind,
            repository_id=request.repository_id,
        )
        return HookPlan(
            repository_id=request.repository_id,
            hook_kind=request.hook_kind,
            template=template,
            template_digest=sha256(template.encode()).hexdigest(),
        )

    def install(self, request: HookInstallRequest) -> HookInstallResult:
        if request.dry_run:
            status = HookInstallStatus.PLANNED
        elif (
            self._capability is None
            or self._capability.repository_id != request.repository_id
        ):
            status = HookInstallStatus.BLOCKED
        else:
            status = self._capability._install(
                hook_kind=request.hook_kind,
                template=_template_for(
                    hook_kind=request.hook_kind,
                    repository_id=request.repository_id,
                ),
            )
        return HookInstallResult(
            repository_id=request.repository_id,
            hook_kind=request.hook_kind,
            status=status,
        )

    def compatibility(
        self, action: HookCompatibilityAction
    ) -> HookCompatibilityResult:
        disposition = (
            HookCompatibilityDisposition.IMPLEMENTATION_READY
            if action is HookCompatibilityAction.RETAIN
            else HookCompatibilityDisposition.RETIREMENT_BLOCKED
        )
        return HookCompatibilityResult(
            action=action,
            disposition=disposition,
        )


def deliver_post_commit_signal(
    *,
    repository_id: str,
    revision_id: str,
    signal_port: PostCommitSignalPort,
) -> HookEmitResult:
    """Deliver bounded commit metadata and collapse adapter errors to typed failure."""
    signal_id = sha256(f"{repository_id}\0{revision_id}".encode()).hexdigest()
    signal = PostCommitSignal(
        signal_id=signal_id,
        repository_id=repository_id,
        revision_id=revision_id,
    )
    try:
        result = signal_port.emit(signal)
    except Exception:
        return HookEmitResult(signal_id=signal_id, status=HookSignalStatus.FAILED)
    if not isinstance(result, HookEmitResult) or result.signal_id != signal_id:
        return HookEmitResult(signal_id=signal_id, status=HookSignalStatus.FAILED)
    return result


def run_post_commit_hook(
    *,
    repository_id: str,
    revision_id: str,
    signal_port: PostCommitSignalPort,
) -> int:
    """Run best-effort signal delivery without allowing commit failure."""
    with suppress(Exception):
        deliver_post_commit_signal(
            repository_id=repository_id,
            revision_id=revision_id,
            signal_port=signal_port,
        )
    return 0


class _SkippedPostCommitSignalPort:
    def emit(self, signal: PostCommitSignal) -> HookEmitResult:
        return HookEmitResult(
            signal_id=signal.signal_id,
            status=HookSignalStatus.SKIPPED,
        )


def _main(argv: Sequence[str]) -> int:
    if len(argv) != 3 or argv[0] != "post-commit":
        return 0
    return run_post_commit_hook(
        repository_id=argv[1],
        revision_id=argv[2],
        signal_port=_SkippedPostCommitSignalPort(),
    )


def _identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def _safe_directory_identity(path: Path) -> tuple[int, int]:
    try:
        value = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ValueError("invalid repository capability") from error
    if not stat.S_ISDIR(value.st_mode) or resolved != path:
        raise ValueError("invalid repository capability")
    return _identity(value)


def _matches_directory_identity(path: Path, expected: tuple[int, int]) -> bool:
    try:
        value = path.lstat()
        return (
            stat.S_ISDIR(value.st_mode)
            and path.resolve(strict=True) == path
            and _identity(value) == expected
        )
    except OSError:
        return False


def _install_hook_file(
    *, directory_fd: int, filename: str, template: bytes
) -> HookInstallStatus:
    read_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        hook_fd = os.open(filename, read_flags, dir_fd=directory_fd)
    except FileNotFoundError:
        return _create_hook_file(
            directory_fd=directory_fd,
            filename=filename,
            template=template,
        )
    except OSError:
        return HookInstallStatus.BLOCKED

    try:
        value = os.fstat(hook_fd)
        content = os.read(hook_fd, len(template) + 1)
        if (
            stat.S_ISREG(value.st_mode)
            and value.st_nlink == 1
            and value.st_mode & stat.S_IXUSR
            and content == template
        ):
            return HookInstallStatus.ALREADY_INSTALLED
        return HookInstallStatus.BLOCKED
    except OSError:
        return HookInstallStatus.BLOCKED
    finally:
        os.close(hook_fd)


def _create_hook_file(
    *, directory_fd: int, filename: str, template: bytes
) -> HookInstallStatus:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        hook_fd = os.open(filename, flags, 0o700, dir_fd=directory_fd)
    except OSError:
        return HookInstallStatus.BLOCKED

    try:
        written = 0
        while written < len(template):
            written += os.write(hook_fd, template[written:])
        os.fchmod(hook_fd, 0o700)
        return HookInstallStatus.INSTALLED
    except OSError:
        return HookInstallStatus.BLOCKED
    finally:
        os.close(hook_fd)


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))

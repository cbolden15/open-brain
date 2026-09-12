"""Deterministic launchd and systemd lifecycle adapters for one appliance daemon."""

from __future__ import annotations

import json
import os
import plistlib
import secrets
import stat
import subprocess
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Final, cast

_LABEL: Final[str] = "org.open-brain.appliance-daemon"
_SYSTEMD_UNIT: Final[str] = "open-brain-appliance.service"
_SUPERVISOR_FAILURE: Final[str] = "appliance supervisor action failed"
_RESOURCE_ROOT: Final[tuple[str, str]] = ("resources", "supervisors")
_RESOURCE_NAMES: Final[frozenset[str]] = frozenset({"launchd.json", "systemd.service"})
_SYSTEMD_COMMAND_TOKEN: Final[str] = "@OPEN_BRAIN_COMMAND@"
_SYSTEMD_SOURCE_TOKEN: Final[str] = "@OPEN_BRAIN_SOURCE_CHECKOUT@"
_RUNTIME_KINDS: Final[frozenset[str]] = frozenset({"native-onedir", "python"})
_MAXIMUM_COMMAND_OUTPUT: Final[int] = 16 * 1024
_MAXIMUM_UNIT_BYTES: Final[int] = 16 * 1024

FileWriter = Callable[[Path, str], None]
FileRemover = Callable[[Path], None]
CommandRunner = Callable[[tuple[str, ...]], str]


class SupervisorCommandError(RuntimeError):
    """A host-supervisor action failed after deterministic local preparation."""


def _daemon_command(
    root: Path,
    python_executable: str,
    runtime_kind: str,
) -> tuple[str, ...]:
    if runtime_kind == "native-onedir":
        return (
            python_executable,
            "__appliance-daemon",
            "--root",
            str(root),
        )
    return (
        python_executable,
        "-m",
        "open_brain.services.appliance_daemon",
        "--root",
        str(root),
    )


def _launchd_target(user_id: int) -> str:
    return f"gui/{user_id}/{_LABEL}"


def _validate_supervisor_inputs(
    root: Path,
    checkout_root: Path | None,
    python_executable: str,
    unit_directory: Path,
    runtime_kind: str,
) -> None:
    if (
        not isinstance(root, Path)
        or (checkout_root is not None and not isinstance(checkout_root, Path))
        or not isinstance(unit_directory, Path)
        or not root.is_absolute()
        or (checkout_root is not None and not checkout_root.is_absolute())
        or not unit_directory.is_absolute()
        or not isinstance(python_executable, str)
        or not python_executable
        or runtime_kind not in _RUNTIME_KINDS
        or (runtime_kind == "native-onedir" and checkout_root is not None)
        or not Path(python_executable).is_absolute()
        or _contains_unsafe_text(python_executable)
        or any(
            _contains_unsafe_text(str(path))
            for path in (root, checkout_root, unit_directory)
            if path is not None
        )
    ):
        raise ValueError("invalid appliance supervisor configuration")


def _write_file(path: Path, content: str) -> None:
    del path, content
    raise _supervisor_failure()


def _remove_file(path: Path) -> None:
    del path
    raise _supervisor_failure()


def _run_command(argv: tuple[str, ...]) -> str:
    del argv
    raise SupervisorCommandError(_SUPERVISOR_FAILURE)


def native_supervisor_effects() -> tuple[FileWriter, FileRemover, CommandRunner]:
    """Return bounded host effects for the frozen native composition only."""
    return _native_write_file, _native_remove_file, _native_run_command


def _native_write_file(path: Path, content: str) -> None:
    payload = content.encode("utf-8")
    if (
        not path.is_absolute()
        or not payload
        or len(payload) > _MAXIMUM_UNIT_BYTES
        or b"\x00" in payload
    ):
        raise _supervisor_failure()
    parent = path.parent
    try:
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_metadata = parent.lstat()
        if not stat.S_ISDIR(parent_metadata.st_mode) or parent.is_symlink():
            raise _supervisor_failure()
        try:
            current_metadata = path.lstat()
        except FileNotFoundError:
            current_metadata = None
        if current_metadata is not None and not stat.S_ISREG(current_metadata.st_mode):
            raise _supervisor_failure()
        temporary = parent / f".{path.name}-{os.getpid()}-{secrets.token_hex(16)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600)
        created = True
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            created = False
            _fsync_directory(parent)
        finally:
            if created:
                with suppress(OSError):
                    temporary.unlink()
    except SupervisorCommandError:
        raise
    except OSError as error:
        raise _supervisor_failure() from error


def _native_remove_file(path: Path) -> None:
    if not path.is_absolute():
        raise _supervisor_failure()
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise _supervisor_failure() from error
    if not stat.S_ISREG(metadata.st_mode):
        raise _supervisor_failure()
    try:
        path.unlink()
        _fsync_directory(path.parent)
    except OSError as error:
        raise _supervisor_failure() from error


def _native_run_command(argv: tuple[str, ...]) -> str:
    if (
        not isinstance(argv, tuple)
        or not argv
        or any(
            not isinstance(argument, str) or not argument or "\x00" in argument
            for argument in argv
        )
    ):
        raise _supervisor_failure()
    try:
        result = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise _supervisor_failure() from error
    if (
        result.returncode != 0
        or len(result.stdout) > _MAXIMUM_COMMAND_OUTPUT
        or len(result.stderr) > _MAXIMUM_COMMAND_OUTPUT
    ):
        raise _supervisor_failure()
    if argv[:2] == ("launchctl", "print") or argv[:3] == (
        "systemctl",
        "--user",
        "status",
    ):
        return "active"
    try:
        return result.stdout.decode("utf-8").strip()
    except UnicodeError as error:
        raise _supervisor_failure() from error


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _contains_unsafe_text(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _systemd_escape(value: str) -> str:
    escaped: list[str] = []
    for character in value:
        if character == "%":
            escaped.append("%%")
        elif character == "\\":
            escaped.append("\\\\")
        elif character == " ":
            escaped.append("\\x20")
        elif character == '"':
            escaped.append("\\x22")
        elif character == "'":
            escaped.append("\\x27")
        else:
            escaped.append(character)
    return "".join(escaped)


def _supervisor_failure() -> SupervisorCommandError:
    return SupervisorCommandError(_SUPERVISOR_FAILURE)


def _resource_text(name: str) -> str:
    if name not in _RESOURCE_NAMES:
        raise _supervisor_failure()
    try:
        resource = files("open_brain")
        for part in _RESOURCE_ROOT:
            resource = resource.joinpath(part)
        payload = resource.joinpath(name).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise _supervisor_failure() from None
    if not payload or "\x00" in payload or len(payload.encode("utf-8")) > 4_096:
        raise _supervisor_failure()
    return payload


def _launchd_template() -> dict[str, object]:
    try:
        value = json.loads(_resource_text("launchd.json"))
    except (TypeError, ValueError):
        raise _supervisor_failure() from None
    expected = {
        "KeepAlive": True,
        "Label": _LABEL,
        "RunAtLoad": False,
        "Umask": 0o077,
    }
    if value != expected:
        raise _supervisor_failure()
    return cast(dict[str, object], value)


def _systemd_template() -> str:
    template = _resource_text("systemd.service")
    if (
        template.count(_SYSTEMD_COMMAND_TOKEN) != 1
        or template.count(_SYSTEMD_SOURCE_TOKEN) != 1
    ):
        raise _supervisor_failure()
    return template


@dataclass(frozen=True, slots=True)
class LaunchdSupervisor:
    root: Path
    checkout_root: Path | None
    python_executable: str
    unit_directory: Path
    user_id: int
    runtime_kind: str = "python"
    write_file: FileWriter = _write_file
    remove_file: FileRemover = _remove_file
    run_command: CommandRunner = _run_command

    def __post_init__(self) -> None:
        _validate_supervisor_inputs(
            self.root,
            self.checkout_root,
            self.python_executable,
            self.unit_directory,
            self.runtime_kind,
        )
        if (
            not isinstance(self.user_id, int)
            or isinstance(self.user_id, bool)
            or self.user_id < 0
        ):
            raise ValueError("invalid launchd user id")

    @property
    def unit_path(self) -> Path:
        return self.unit_directory / f"{_LABEL}.plist"

    @property
    def unit_name(self) -> str:
        return _LABEL

    def render(self) -> str:
        payload = _launchd_template()
        payload.update(
            {
                "ProgramArguments": list(
                    _daemon_command(self.root, self.python_executable, self.runtime_kind)
                ),
                "StandardErrorPath": str(
                    self.root / ".open-brain" / "run" / "daemon.stderr.log"
                ),
                "StandardOutPath": str(
                    self.root / ".open-brain" / "run" / "daemon.stdout.log"
                ),
            }
        )
        if self.checkout_root is not None:
            payload["EnvironmentVariables"] = {
                "PYTHONPATH": str(self.checkout_root / "src")
            }
            payload["WorkingDirectory"] = str(self.checkout_root)
        return plistlib.dumps(payload, sort_keys=True).decode("utf-8")

    def install(self) -> None:
        written = False
        try:
            self.write_file(self.unit_path, self.render())
            written = True
            self.run_command(("launchctl", "bootstrap", f"gui/{self.user_id}", str(self.unit_path)))
        except Exception as error:
            if written:
                with suppress(Exception):
                    self.remove_file(self.unit_path)
            raise _supervisor_failure() from error

    def start(self) -> str:
        return self.run_command(("launchctl", "kickstart", "-k", _launchd_target(self.user_id)))

    def stop(self) -> str:
        return self.run_command(("launchctl", "kill", "TERM", _launchd_target(self.user_id)))

    def quiesce(self) -> str:
        return self.run_command(
            ("launchctl", "bootout", f"gui/{self.user_id}", str(self.unit_path))
        )

    def resume(self) -> str:
        self.run_command(
            ("launchctl", "bootstrap", f"gui/{self.user_id}", str(self.unit_path))
        )
        return self.start()

    def restart(self) -> None:
        self.stop()
        self.start()

    def status(self) -> str:
        return self.run_command(("launchctl", "print", _launchd_target(self.user_id)))

    def remove(self) -> None:
        self.run_command(("launchctl", "bootout", f"gui/{self.user_id}", str(self.unit_path)))
        self.remove_file(self.unit_path)


@dataclass(frozen=True, slots=True)
class SystemdSupervisor:
    root: Path
    checkout_root: Path | None
    python_executable: str
    unit_directory: Path
    runtime_kind: str = "python"
    write_file: FileWriter = _write_file
    remove_file: FileRemover = _remove_file
    run_command: CommandRunner = _run_command

    def __post_init__(self) -> None:
        _validate_supervisor_inputs(
            self.root,
            self.checkout_root,
            self.python_executable,
            self.unit_directory,
            self.runtime_kind,
        )

    @property
    def unit_path(self) -> Path:
        return self.unit_directory / self.unit_name

    @property
    def unit_name(self) -> str:
        return _SYSTEMD_UNIT

    def render(self) -> str:
        command = " ".join(
            _systemd_escape(part)
            for part in _daemon_command(
                self.root,
                self.python_executable,
                self.runtime_kind,
            )
        )
        source_checkout = ""
        if self.checkout_root is not None:
            source_checkout = "\n".join(
                (
                    "Environment=PYTHONPATH="
                    + _systemd_escape(str(self.checkout_root / "src")),
                    "WorkingDirectory=" + _systemd_escape(str(self.checkout_root)),
                )
            )
        return _systemd_template().replace(
            _SYSTEMD_COMMAND_TOKEN,
            command,
        ).replace(
            _SYSTEMD_SOURCE_TOKEN,
            source_checkout,
        )

    def install(self) -> None:
        written = False
        try:
            self.write_file(self.unit_path, self.render())
            written = True
            self.run_command(("systemctl", "--user", "daemon-reload"))
            self.run_command(("systemctl", "--user", "enable", self.unit_name))
        except Exception as error:
            if written:
                with suppress(Exception):
                    self.remove_file(self.unit_path)
            raise _supervisor_failure() from error

    def start(self) -> str:
        return self.run_command(("systemctl", "--user", "start", self.unit_name))

    def stop(self) -> str:
        return self.run_command(("systemctl", "--user", "stop", self.unit_name))

    def quiesce(self) -> str:
        return self.stop()

    def resume(self) -> str:
        return self.start()

    def restart(self) -> str:
        return self.run_command(("systemctl", "--user", "restart", self.unit_name))

    def status(self) -> str:
        return self.run_command(("systemctl", "--user", "status", self.unit_name, "--no-pager"))

    def remove(self) -> None:
        self.run_command(("systemctl", "--user", "disable", "--now", self.unit_name))
        self.remove_file(self.unit_path)
        self.run_command(("systemctl", "--user", "daemon-reload"))


__all__ = [
    "LaunchdSupervisor",
    "SupervisorCommandError",
    "SystemdSupervisor",
    "native_supervisor_effects",
]

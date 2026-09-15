"""Owned service and process cleanup helpers for the optional collector."""

from __future__ import annotations

import json
import os
import plistlib
import re
import signal
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from open_brain_connectors.runtime.connectors import ConnectorContractError

__all__ = [
    "CollectorOwnedServiceRegistry",
    "CollectorLaunchdServiceManager",
    "CollectorProcessCleanup",
    "CollectorServiceError",
]


class CollectorServiceError(RuntimeError):
    """A service lifecycle operation was not owner-safe."""


@dataclass(frozen=True, slots=True)
class CollectorProcessCleanup:
    """Terminate only a process explicitly spawned and owned by the collector."""

    pid: int
    process_group_id: int
    process: subprocess.Popen[bytes] | None = None

    @classmethod
    def from_process(cls, process: subprocess.Popen[bytes]) -> CollectorProcessCleanup:
        if not isinstance(process, subprocess.Popen) or process.pid is None:
            raise ConnectorContractError("invalid collector process")
        return cls(pid=process.pid, process_group_id=os.getpgid(process.pid), process=process)

    def terminate(self, *, timeout_seconds: float = 1.0) -> dict[str, object]:
        if timeout_seconds <= 0 or timeout_seconds > 10:
            raise ConnectorContractError("invalid collector cleanup timeout")
        try:
            os.killpg(self.process_group_id, signal.SIGTERM)
        except ProcessLookupError:
            return {"cleaned": True, "method": "already_exited", "pid": self.pid}
        if self.process is not None:
            try:
                self.process.wait(timeout=timeout_seconds)
                return {"cleaned": True, "method": "terminated", "pid": self.pid}
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.process_group_id, signal.SIGKILL)
                except ProcessLookupError:
                    return {"cleaned": True, "method": "already_exited", "pid": self.pid}
                self.process.wait(timeout=timeout_seconds)
                return {"cleaned": True, "method": "killed", "pid": self.pid}
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                os.kill(self.pid, 0)
            except ProcessLookupError:
                return {"cleaned": True, "method": "terminated", "pid": self.pid}
            time.sleep(0.02)
        os.killpg(self.process_group_id, signal.SIGKILL)
        return {"cleaned": True, "method": "killed", "pid": self.pid}


class CollectorOwnedServiceRegistry:
    """JSON fixture for launch-at-login registrations owned by this collector."""

    def __init__(self, path: Path) -> None:
        if not isinstance(path, Path):
            raise ConnectorContractError("invalid collector service registry")
        self._path = path

    def install(self, *, label: str, owner_token: str, command: tuple[str, ...]) -> None:
        _validate(label, owner_token)
        if not command or any(type(item) is not str or not item for item in command):
            raise ConnectorContractError("invalid collector service command")
        state = self._load()
        state[label] = {
            "command": list(command),
            "owner_token": owner_token,
            "schema_version": 1,
        }
        self._save(state)

    def remove_owned(self, *, label: str, owner_token: str) -> bool:
        _validate(label, owner_token)
        state = self._load()
        existing = state.get(label)
        if existing is None:
            return False
        if not isinstance(existing, dict) or existing.get("owner_token") != owner_token:
            raise CollectorServiceError("collector_service_not_owned")
        del state[label]
        self._save(state)
        return True

    def _load(self) -> dict[str, object]:
        if not self._path.exists():
            return {}
        loaded = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ConnectorContractError("invalid collector service registry")
        return loaded

    def _save(self, state: dict[str, object]) -> None:
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        self._path.chmod(0o600)


LaunchdStatus = Literal["loaded", "not_loaded"]
_LABEL = re.compile(r"^open-brain\.collector\.[A-Za-z0-9][A-Za-z0-9._-]{0,96}$")
_OWNER_TOKEN = re.compile(r"^collector-owned:[A-Za-z0-9][A-Za-z0-9._:-]{0,96}$")


class CollectorLaunchdServiceManager:
    """Install, inspect, and remove one opt-in user launchd service."""

    def __init__(
        self,
        *,
        plist_dir: Path,
        runner: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] | None = None,
        uid: int | None = None,
    ) -> None:
        if not isinstance(plist_dir, Path):
            raise ConnectorContractError("invalid collector service")
        self._plist_dir = plist_dir
        self._runner = runner if runner is not None else _run_launchctl
        self._uid = os.getuid() if uid is None else uid

    def install(
        self,
        *,
        label: str,
        owner_token: str,
        command: tuple[str, ...],
    ) -> dict[str, object]:
        _validate(label, owner_token)
        if not command or any(type(item) is not str or not item for item in command):
            raise ConnectorContractError("invalid collector service command")
        path = self._plist_path(label)
        self._plist_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.exists():
            self._assert_owned_plist(path=path, label=label, owner_token=owner_token)
            status = self.status(label)
            if status == "loaded":
                bootout = self._runner(["launchctl", "bootout", f"{self._domain()}/{label}"])
                if bootout.returncode not in (0, 113):
                    raise CollectorServiceError("collector_service_remove_failed")
        path.write_bytes(
            plistlib.dumps(
                {
                    "EnvironmentVariables": {
                        "OPEN_BRAIN_COLLECTOR_OWNER": owner_token,
                    },
                    "KeepAlive": False,
                    "Label": label,
                    "ProgramArguments": list(command),
                    "RunAtLoad": True,
                    "StandardErrorPath": str(self._plist_dir / f"{label}.stderr.log"),
                    "StandardOutPath": str(self._plist_dir / f"{label}.stdout.log"),
                    "WorkingDirectory": str(Path.cwd()),
                },
                sort_keys=True,
            )
        )
        path.chmod(0o600)
        result = self._runner(["launchctl", "bootstrap", self._domain(), str(path)])
        if result.returncode != 0:
            path.unlink(missing_ok=True)
            raise CollectorServiceError("collector_service_install_failed")
        return {
            "label": label,
            "outcome": "installed",
            "plist": str(path),
            "schema_version": 1,
            "status": self.status(label),
        }

    def status(self, label: str) -> LaunchdStatus:
        _validate_label(label)
        result = self._runner(["launchctl", "print", f"{self._domain()}/{label}"])
        return "loaded" if result.returncode == 0 else "not_loaded"

    def remove_owned(self, *, label: str, owner_token: str) -> dict[str, object]:
        _validate(label, owner_token)
        path = self._plist_path(label)
        if not path.exists():
            return {
                "label": label,
                "outcome": "absent",
                "plist_removed": False,
                "schema_version": 1,
                "status": "not_loaded",
            }
        self._assert_owned_plist(path=path, label=label, owner_token=owner_token)
        result = self._runner(["launchctl", "bootout", f"{self._domain()}/{label}"])
        if result.returncode not in (0, 113):
            raise CollectorServiceError("collector_service_remove_failed")
        removed = False
        if path.exists():
            path.unlink()
            removed = True
        return {
            "label": label,
            "outcome": "removed",
            "plist_removed": removed,
            "schema_version": 1,
            "status": self.status(label),
        }

    def _domain(self) -> str:
        return f"gui/{self._uid}"

    def _plist_path(self, label: str) -> Path:
        _validate_label(label)
        return self._plist_dir / f"{label}.plist"

    def _assert_owned_plist(self, *, path: Path, label: str, owner_token: str) -> None:
        try:
            payload = plistlib.loads(path.read_bytes())
        except (OSError, plistlib.InvalidFileException) as error:
            raise CollectorServiceError("collector_service_not_owned") from error
        if (
            not isinstance(payload, dict)
            or payload.get("Label") != label
            or not isinstance(payload.get("EnvironmentVariables"), dict)
            or payload["EnvironmentVariables"].get("OPEN_BRAIN_COLLECTOR_OWNER")
            != owner_token
        ):
            raise CollectorServiceError("collector_service_not_owned")


def _validate(label: str, owner_token: str) -> None:
    _validate_label(label)
    if type(owner_token) is not str or _OWNER_TOKEN.fullmatch(owner_token) is None:
        raise ConnectorContractError("invalid collector service ownership")


def _validate_label(label: str) -> None:
    if type(label) is not str or _LABEL.fullmatch(label) is None:
        raise ConnectorContractError("invalid collector service ownership")


def _run_launchctl(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=False)

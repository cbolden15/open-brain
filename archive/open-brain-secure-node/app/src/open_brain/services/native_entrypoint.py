"""Single executable entry point for the native one-folder artifact."""

from __future__ import annotations

import json
import os
import plistlib
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from open_brain_engine import __version__
from open_brain_engine.engine import acquire_daemon_authority

from open_brain.extensions.connector_worker_v1 import (
    _child_main as run_connector_worker,
)
from open_brain.extensions.connector_worker_v1 import _worker_command
from open_brain.profile import open_existing_single_user_local
from open_brain.services.appliance_application import ApplianceApplication
from open_brain.services.appliance_daemon import main as run_appliance_daemon
from open_brain.services.appliance_entrypoints import run_cli
from open_brain.services.appliance_lifecycle import (
    ApplianceLifecycleService,
    ArtifactCandidate,
    LifecycleMigrationReceipt,
    MigrationStep,
    _supervisor,
    read_status_via_control,
)
from open_brain.services.appliance_recovery import (
    ApplianceBackupResult,
    ApplianceReplacementPreflight,
)
from open_brain.services.appliance_supervisors import LaunchdSupervisor, SystemdSupervisor
from open_brain.services.native_artifacts import (
    NATIVE_ARTIFACT_KIND,
    NATIVE_EXECUTABLE_NAME,
    NativeArtifactError,
    NativeArtifactLifecycleAdapter,
    native_platform_tag,
)

_DAEMON_COMMAND = "__appliance-daemon"
_CONNECTOR_WORKER_COMMAND = "__connector-worker"
_SELF_CHECK_COMMAND = "__native-self-check"
_SELF_CHECK_ROOT = Path("/open-brain-native-self-check")


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> int:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    selected_environment = dict(os.environ if environment is None else environment)
    if arguments[:1] == (_DAEMON_COMMAND,):
        return run_appliance_daemon(arguments[1:], environment=selected_environment)
    if arguments == (_CONNECTOR_WORKER_COMMAND,):
        return run_connector_worker()
    if arguments == (_SELF_CHECK_COMMAND,):
        return _native_self_check()
    lifecycle = None
    if arguments[:1] in {("upgrade",), ("uninstall",)} and getattr(sys, "frozen", False):
        try:
            lifecycle = _native_lifecycle(selected_environment)
        except (NativeArtifactError, OSError, ValueError):
            lifecycle = None
    if lifecycle is None:
        return int(run_cli(arguments, environment=selected_environment))
    return int(run_cli(arguments, environment=selected_environment, lifecycle=lifecycle))


@dataclass(frozen=True, slots=True)
class _NativeRecoveryLifecycle:
    root: Path

    def create_backup(self, destination: Path, *, backup_id: str) -> ApplianceBackupResult:
        profile = open_existing_single_user_local(self.root)
        with acquire_daemon_authority(profile) as authority:
            application = ApplianceApplication.open_mutating(self.root, authority)
            return application.recovery().create_backup(destination, backup_id=backup_id)

    def preflight_replacement(
        self,
        source: Path,
        disposable_root: Path,
    ) -> ApplianceReplacementPreflight:
        profile = open_existing_single_user_local(self.root)
        with acquire_daemon_authority(profile) as authority:
            application = ApplianceApplication.open_mutating(self.root, authority)
            return application.recovery().preflight_replacement(source, disposable_root)


def _native_lifecycle(environment: Mapping[str, str]) -> ApplianceLifecycleService:
    root = _native_root(environment)
    install_root = _native_install_root()
    return ApplianceLifecycleService(
        root,
        recovery=_NativeRecoveryLifecycle(root),
        artifact_port=NativeArtifactLifecycleAdapter(
            install_root=install_root,
            current_version=__version__,
        ),
        supervisor=_supervisor(root),
        migrations=(_native_migration("engine"), _native_migration("app")),
        doctor_reader=lambda: _native_doctor(root),
    )


def _native_migration(component: str) -> MigrationStep:
    def migrate(candidate: ArtifactCandidate) -> LifecycleMigrationReceipt:
        return LifecycleMigrationReceipt(
            component=component,
            from_version=__version__,
            to_version=candidate.version,
            status="applied",
        )

    migrate.__name__ = f"migrate:{component}"
    return migrate


def _native_doctor(root: Path) -> Mapping[str, object]:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            doctor = read_status_via_control(root).envelope.get("doctor")
            if isinstance(doctor, dict) and isinstance(doctor.get("state"), str):
                return cast(dict[str, object], doctor)
        except Exception:
            pass
        time.sleep(0.05)
    raise RuntimeError("native lifecycle doctor unavailable")


def _native_root(environment: Mapping[str, str]) -> Path:
    value = environment.get("OPEN_BRAIN_ROOT")
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("native runtime unavailable")
    root = Path(value)
    if not root.is_absolute():
        raise ValueError("native runtime unavailable")
    return root.resolve(strict=True)


def _native_install_root() -> Path:
    executable = Path(sys.executable).resolve(strict=True)
    candidate = executable.parent
    if executable.name != NATIVE_EXECUTABLE_NAME or candidate.parent.name != "candidates":
        raise NativeArtifactError("native artifact operation failed")
    return candidate.parent.parent


def _native_self_check() -> int:
    try:
        executable = Path(sys.executable)
        if not getattr(sys, "frozen", False) or not executable.is_absolute():
            raise ValueError("native runtime unavailable")
        launchd = LaunchdSupervisor(
            root=_SELF_CHECK_ROOT,
            checkout_root=None,
            python_executable=str(executable),
            unit_directory=_SELF_CHECK_ROOT / "launchd",
            user_id=0,
            runtime_kind=NATIVE_ARTIFACT_KIND,
        )
        systemd = SystemdSupervisor(
            root=_SELF_CHECK_ROOT,
            checkout_root=None,
            python_executable=str(executable),
            unit_directory=_SELF_CHECK_ROOT / "systemd",
            runtime_kind=NATIVE_ARTIFACT_KIND,
        )
        launchd_payload = cast(dict[str, object], plistlib.loads(launchd.render().encode()))
        daemon_command = [
            str(executable),
            _DAEMON_COMMAND,
            "--root",
            str(_SELF_CHECK_ROOT),
        ]
        systemd_payload = systemd.render()
        if (
            launchd_payload.get("ProgramArguments") != daemon_command
            or "PYTHONPATH" in systemd_payload
            or " -m " in systemd_payload
            or tuple(_worker_command())
            != (str(executable), _CONNECTOR_WORKER_COMMAND)
        ):
            raise ValueError("native runtime unavailable")
        payload: dict[str, object] = {
            "artifact_kind": NATIVE_ARTIFACT_KIND,
            "connector_child": "direct-frozen-executable",
            "daemon_launch": "direct-frozen-executable",
            "frozen": True,
            "package_resources": "available",
            "platform": native_platform_tag(),
            "status": "ok",
        }
    except Exception:
        payload = {"status": "failed"}
        exit_code = 1
    else:
        exit_code = 0
    sys.stdout.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]

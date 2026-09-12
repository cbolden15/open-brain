"""App-owned idempotent initialization and bounded preflight for the appliance path."""

from __future__ import annotations

import os
import platform
import secrets
import shutil
import stat
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from open_brain_engine.engine import (
    PHASE1_STATE_SCHEMA_VERSION,
    StateSchemaUnavailableError,
    canonical_json_bytes,
    inspect_phase1_state,
    open_local_engine,
    read_maintenance_snapshot,
)

from open_brain.profile import (
    SingleUserLocalProfile,
    compile_single_user_local,
    open_existing_single_user_local,
)

APPLIANCE_OWNER_CREDENTIAL = Path(".open-brain/state/appliance-owner-credential")
APPLIANCE_INIT_RECEIPT = Path(".open-brain/state/appliance-init.json")
DEFAULT_STARTER_SPACES = ("Personal", "Work", "Projects", "Health", "Learning")
_MINIMUM_FREE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class AppliancePreflightCheck:
    check: str
    status: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"check": self.check, "status": self.status, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class ApplianceInitReceipt:
    status: str
    tenant_id: str | None
    owner_actor_id: str | None
    credential_state: str | None
    starter_spaces: tuple[str, ...]
    state_schema_version: int | None
    index_generation: int | None
    preflight: tuple[AppliancePreflightCheck, ...]
    failed_check: str | None = None
    cleanup: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "tenant_id": self.tenant_id,
            "owner_actor_id": self.owner_actor_id,
            "credential_state": self.credential_state,
            "starter_spaces": list(self.starter_spaces),
            "state_schema_version": self.state_schema_version,
            "index_generation": self.index_generation,
            "preflight": [check.to_dict() for check in self.preflight],
            "failed_check": self.failed_check,
            "cleanup": list(self.cleanup),
        }


class ApplianceInitError(RuntimeError):
    """Appliance initialization failed after a bounded preflight or state check."""

    def __init__(self, receipt: ApplianceInitReceipt) -> None:
        self.receipt = receipt
        super().__init__(receipt.failed_check or receipt.status)


def initialize_appliance(
    root: Path,
    *,
    starter_spaces: Sequence[str] = (),
    host_family_probe: Callable[[], str] | None = None,
    architecture_probe: Callable[[], str] | None = None,
    runtime_probe: Callable[[], tuple[int, int]] | None = None,
    disk_probe: Callable[[Path], int] | None = None,
    supervisor_probe: Callable[[str], str | None] | None = None,
) -> ApplianceInitReceipt:
    """Initialize one Brain root idempotently for the Phase 3 appliance surface."""
    normalized_starters = _starter_spaces(starter_spaces)
    preflight = _preflight(
        root,
        host_family_probe=host_family_probe,
        architecture_probe=architecture_probe,
        runtime_probe=runtime_probe,
        disk_probe=disk_probe,
        supervisor_probe=supervisor_probe,
    )
    failure = next((check for check in preflight if check.status == "failed"), None)
    if failure is not None:
        raise ApplianceInitError(
            ApplianceInitReceipt(
                status="failed",
                tenant_id=None,
                owner_actor_id=None,
                credential_state=None,
                starter_spaces=normalized_starters,
                state_schema_version=None,
                index_generation=None,
                preflight=preflight,
                failed_check=failure.check,
                cleanup=(
                    "No partial writer was created.",
                    "Fix the failed preflight check and rerun init.",
                ),
            )
        )

    profile = compile_single_user_local(root, starter_spaces=normalized_starters)
    credential_path = profile.root / APPLIANCE_OWNER_CREDENTIAL
    receipt_path = profile.root / APPLIANCE_INIT_RECEIPT
    existing_profile = open_existing_single_user_local(profile.root)
    schema_before = inspect_phase1_state(existing_profile)
    if schema_before.state in {"invalid", "newer"}:
        raise ApplianceInitError(
            _state_failure_receipt(
                profile=profile,
                starter_spaces=normalized_starters,
                preflight=preflight,
                failed_check="state_schema",
                cleanup=(
                    "Existing state was not migrated or recovered.",
                    "Use a compatible application or verified recovery path.",
                ),
            )
        )
    try:
        credential_exists = _owner_file_exists(credential_path)
    except OSError as error:
        raise ApplianceInitError(
            _state_failure_receipt(
                profile=profile,
                starter_spaces=normalized_starters,
                preflight=preflight,
                failed_check="credential",
                cleanup=(
                    "Existing credential state was not replaced.",
                    "Repair owner-only credential permissions and rerun init.",
                ),
            )
        ) from error
    try:
        initialized_before = _owner_file_exists(receipt_path)
    except OSError as error:
        raise ApplianceInitError(
            _state_failure_receipt(
                profile=profile,
                starter_spaces=normalized_starters,
                preflight=preflight,
                failed_check="init_receipt",
                cleanup=(
                    "Existing initialization evidence was not replaced.",
                    "Repair owner-only receipt permissions and rerun init.",
                ),
            )
        ) from error
    try:
        _ensure_owner_file(
            credential_path,
            payload_factory=lambda: (secrets.token_urlsafe(32) + "\n").encode("utf-8"),
        )
    except OSError as error:
        raise ApplianceInitError(
            _state_failure_receipt(
                profile=profile,
                starter_spaces=normalized_starters,
                preflight=preflight,
                failed_check="credential",
                cleanup=(
                    "Credential creation did not establish writer authority.",
                    "Remove only an incomplete temporary credential and rerun init.",
                ),
            )
        ) from error
    credential_state = "preserved" if credential_exists else "created"
    try:
        tasks = open_local_engine(profile)
    except StateSchemaUnavailableError as error:
        raise ApplianceInitError(
            _state_failure_receipt(
                profile=profile,
                starter_spaces=normalized_starters,
                preflight=preflight,
                failed_check="state_schema",
                cleanup=(
                    "Existing state was not migrated or recovered.",
                    "Use a compatible application or verified recovery path.",
                ),
            )
        ) from error
    maintenance = read_maintenance_snapshot(open_existing_single_user_local(profile.root))
    index_generation = maintenance.index.generation
    if maintenance.index.state != "current":
        index_generation = tasks.portability.rebuild_index().index_generation
    state_schema_version = inspect_phase1_state(
        open_existing_single_user_local(profile.root)
    ).version
    if state_schema_version != PHASE1_STATE_SCHEMA_VERSION:
        raise ApplianceInitError(
            _state_failure_receipt(
                profile=profile,
                starter_spaces=normalized_starters,
                preflight=preflight,
                failed_check="state_schema",
                cleanup=(
                    "Initialization did not publish a current state schema.",
                    "Use the verified recovery path before retrying init.",
                ),
            )
        )
    receipt = ApplianceInitReceipt(
        status="already_initialized" if initialized_before else "initialized",
        tenant_id=profile.tenant_id,
        owner_actor_id=profile.owner_actor_id,
        credential_state=credential_state,
        starter_spaces=normalized_starters,
        state_schema_version=state_schema_version,
        index_generation=index_generation,
        preflight=preflight,
    )
    _write_owner_json(
        receipt_path,
        {
            "owner_actor_id": profile.owner_actor_id,
            "owner_role_claim_id": profile.owner_role_claim["role_claim_id"],
            "profile": "single-user-local",
            "provider_mode": profile.provider_mode.value,
            "schema_version": 1,
            "starter_spaces": list(normalized_starters),
            "state_schema_version": state_schema_version,
            "tenant_id": profile.tenant_id,
        },
    )
    return receipt


def _preflight(
    root: Path,
    *,
    host_family_probe: Callable[[], str] | None,
    architecture_probe: Callable[[], str] | None,
    runtime_probe: Callable[[], tuple[int, int]] | None,
    disk_probe: Callable[[Path], int] | None,
    supervisor_probe: Callable[[str], str | None] | None,
) -> tuple[AppliancePreflightCheck, ...]:
    host_family = (host_family_probe or _host_family)()
    architecture = (architecture_probe or _architecture)()
    runtime = (runtime_probe or _runtime)()
    available_bytes = (disk_probe or _free_bytes)(_probe_path(root))
    supervisor = (supervisor_probe or _supervisor)(host_family)
    checks = [
        AppliancePreflightCheck(
            check="host_family",
            status="ok" if host_family in {"darwin", "linux"} else "failed",
            detail=host_family,
        ),
        AppliancePreflightCheck(
            check="architecture",
            status="ok" if architecture in {"arm64", "aarch64", "x86_64"} else "failed",
            detail=architecture,
        ),
        AppliancePreflightCheck(
            check="runtime",
            status="ok" if (3, 12) <= runtime < (3, 15) else "failed",
            detail=f"{runtime[0]}.{runtime[1]}",
        ),
        AppliancePreflightCheck(
            check="permissions",
            status="ok" if _permissions_ok(root) else "failed",
            detail="owner-writable",
        ),
        AppliancePreflightCheck(
            check="disk",
            status="ok" if available_bytes >= _MINIMUM_FREE_BYTES else "failed",
            detail=str(available_bytes),
        ),
        AppliancePreflightCheck(
            check="provider",
            status="ok",
            detail="none",
        ),
        AppliancePreflightCheck(
            check="supervisor",
            status="ok" if supervisor is not None else "failed",
            detail=supervisor or "unavailable",
        ),
    ]
    return tuple(checks)


def _starter_spaces(value: Sequence[str]) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ValueError("invalid starter spaces")
    result = tuple(item.strip() for item in value)
    if any(not item or len(item) > 120 for item in result) or len(set(result)) != len(result):
        raise ValueError("invalid starter spaces")
    return result


def _probe_path(root: Path) -> Path:
    candidate = root.expanduser().absolute()
    while not candidate.exists():
        if candidate.parent == candidate:
            break
        candidate = candidate.parent
    return candidate


def _permissions_ok(root: Path) -> bool:
    return os.access(_probe_path(root), os.W_OK | os.X_OK)


def _host_family() -> str:
    return platform.system().strip().casefold()


def _architecture() -> str:
    return platform.machine().strip().casefold()


def _runtime() -> tuple[int, int]:
    return (sys.version_info.major, sys.version_info.minor)


def _free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def _supervisor(host_family: str) -> str | None:
    if host_family == "darwin":
        return "launchd" if shutil.which("launchctl") is not None else None
    if host_family == "linux":
        return "systemd" if shutil.which("systemctl") is not None else None
    return None


def _state_failure_receipt(
    *,
    profile: SingleUserLocalProfile,
    starter_spaces: tuple[str, ...],
    preflight: tuple[AppliancePreflightCheck, ...],
    failed_check: str,
    cleanup: tuple[str, ...],
) -> ApplianceInitReceipt:
    return ApplianceInitReceipt(
        status="failed",
        tenant_id=profile.tenant_id,
        owner_actor_id=profile.owner_actor_id,
        credential_state=None,
        starter_spaces=starter_spaces,
        state_schema_version=None,
        index_generation=None,
        preflight=preflight,
        failed_check=failed_check,
        cleanup=cleanup,
    )


def _owner_file_exists(path: Path) -> bool:
    try:
        _read_owner_file(path)
    except FileNotFoundError:
        return False
    return True


def _read_owner_file(path: Path) -> bytes:
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid not in {0, os.geteuid()}
    ):
        raise OSError("unsafe appliance file")
    return path.read_bytes()


def _ensure_owner_file(path: Path, *, payload_factory: Callable[[], bytes]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        _read_owner_file(path)
        return
    except FileNotFoundError:
        pass
    payload = payload_factory()
    _write_owner_bytes(path, payload)


def _write_owner_json(path: Path, value: object) -> None:
    payload = canonical_json_bytes(value)
    existing: bytes | None
    try:
        existing = _read_owner_file(path)
    except FileNotFoundError:
        existing = None
    if existing == payload:
        return
    _write_owner_bytes(path, payload)


def _write_owner_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    file_descriptor = os.open(temporary, flags, 0o600)
    try:
        os.write(file_descriptor, payload)
        os.fsync(file_descriptor)
    finally:
        os.close(file_descriptor)
    os.replace(temporary, path)
    os.chmod(path, 0o600)

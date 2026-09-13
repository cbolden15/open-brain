from __future__ import annotations

import json
import os
import platform
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Protocol, cast

from open_brain_engine.engine import LockScope, canonical_json_bytes
from open_brain_engine.storage.operational import (
    FileLease,
    RootIdentity,
    StorageError,
    WriteState,
    atomic_replace,
    atomic_write_new,
    capture_root_identity,
    read_confined,
)

from .appliance_daemon import (
    CliControlReceipt,
    CliControlRequest,
    ControlReceipt,
    ControlRequest,
    StatusControlReceipt,
    request_cli_dispatch,
    request_control,
    request_status,
)
from .appliance_recovery import ApplianceBackupResult, ApplianceReplacementPreflight
from .appliance_supervisors import (
    LaunchdSupervisor,
    SystemdSupervisor,
    native_supervisor_effects,
)

_SUPERVISOR_ACTIONS = frozenset(
    {"discover", "install", "start", "stop", "restart", "status", "remove"}
)
_OWNER_CONFIRMATION = "owner-approved"
_OWNER_REQUEST = re.compile(
    r"^(?P<prefix>upgrade|uninstall)_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_BACKUP_ID = re.compile(
    r"^backup_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_CANDIDATE_ID = re.compile(r"^candidate_[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_ARTIFACT_KINDS = frozenset({"native-onedir", "source-checkout"})
_FAILURE_MESSAGE = "appliance lifecycle failed"
_LIFECYCLE_DIRECTORY = Path(".open-brain/state/appliance-lifecycle")
_MAXIMUM_LIFECYCLE_RECORD_BYTES = 16 * 1024
_UPGRADE_STAGES = frozenset(
    {
        "requested",
        "compatibility",
        "quiesce",
        "backup",
        "preflight",
        "migrations",
        "engine",
        "app",
        "activate",
        "restart",
        "doctor",
        "interrupted",
    }
)
_UNINSTALL_STAGES = frozenset(
    {"requested", "stop", "remove", "artifact-remove", "interrupted"}
)
_ROLLBACK_REQUIRED_STAGES = frozenset(
    {"migrations", "engine", "app", "activate", "restart", "doctor"}
)
_DAEMON_RESTORE_REQUIRED_STAGES = frozenset(
    {
        "quiesce",
        "backup",
        "preflight",
        "migrations",
        "engine",
        "app",
        "activate",
        "restart",
        "doctor",
    }
)


@dataclass(frozen=True, slots=True)
class OwnerLifecycleRequest:
    request_id: str
    requested_at: str
    owner_confirmation: str = _OWNER_CONFIRMATION

    def __post_init__(self) -> None:
        match = (
            _OWNER_REQUEST.fullmatch(self.request_id)
            if isinstance(self.request_id, str)
            else None
        )
        if match is None or self.owner_confirmation != _OWNER_CONFIRMATION:
            raise ValueError("explicit owner request is required")
        _parse_timestamp(self.requested_at)


@dataclass(frozen=True, slots=True)
class ArtifactCandidate:
    candidate_id: str
    version: str
    artifact_kind: str = "source-checkout"

    def __post_init__(self) -> None:
        if (
            _CANDIDATE_ID.fullmatch(self.candidate_id) is None
            or _VERSION.fullmatch(self.version) is None
            or self.artifact_kind not in _ARTIFACT_KINDS
        ):
            raise ValueError("invalid artifact candidate")


@dataclass(frozen=True, slots=True)
class ArtifactCompatibilityReceipt:
    candidate_id: str
    artifact_kind: str
    current_version: str
    target_version: str
    status: str

    def __post_init__(self) -> None:
        _validate_candidate_identifier(self.candidate_id)
        _validate_version(self.current_version)
        _validate_version(self.target_version)
        if self.artifact_kind not in _ARTIFACT_KINDS or self.status not in {
            "compatible",
            "incompatible",
        }:
            raise ValueError("invalid artifact compatibility receipt")


@dataclass(frozen=True, slots=True)
class ArtifactSwitchReceipt:
    candidate_id: str
    artifact_kind: str
    active_candidate_id: str
    status: str

    def __post_init__(self) -> None:
        _validate_candidate_identifier(self.candidate_id)
        _validate_candidate_identifier(self.active_candidate_id)
        if (
            self.artifact_kind not in _ARTIFACT_KINDS
            or self.status != "activated"
            or self.active_candidate_id != self.candidate_id
        ):
            raise ValueError("invalid artifact switch receipt")


@dataclass(frozen=True, slots=True)
class ArtifactRollbackReceipt:
    candidate_id: str
    artifact_kind: str
    active_candidate_id: str | None
    status: str

    def __post_init__(self) -> None:
        _validate_candidate_identifier(self.candidate_id)
        if self.active_candidate_id is not None:
            _validate_candidate_identifier(self.active_candidate_id)
        if self.artifact_kind not in _ARTIFACT_KINDS or self.status != "rolled_back":
            raise ValueError("invalid artifact rollback receipt")


@dataclass(frozen=True, slots=True)
class ArtifactRemovalReceipt:
    artifact_kind: str
    removed_candidate_id: str | None
    status: str

    def __post_init__(self) -> None:
        if self.removed_candidate_id is not None:
            _validate_candidate_identifier(self.removed_candidate_id)
        if self.artifact_kind not in _ARTIFACT_KINDS or self.status != "removed":
            raise ValueError("invalid artifact removal receipt")


@dataclass(frozen=True, slots=True)
class LifecycleMigrationReceipt:
    component: str
    from_version: str
    to_version: str
    status: str

    def __post_init__(self) -> None:
        if self.component not in {"engine", "app"}:
            raise ValueError("invalid lifecycle migration component")
        _validate_version(self.from_version)
        _validate_version(self.to_version)
        if self.status != "applied":
            raise ValueError("invalid lifecycle migration receipt")


@dataclass(frozen=True, slots=True)
class ApplianceUpgradeReceipt:
    request_id: str
    status: str
    candidate_id: str
    prior_candidate_id: str | None
    active_candidate_id: str
    compatibility_state: str
    backup_id: str
    manifest_digest_sha256: str
    preflight_state: str
    migrations: tuple[LifecycleMigrationReceipt, ...]
    activation_state: str
    restart_state: str
    doctor_state: str

    def __post_init__(self) -> None:
        _validate_owner_request_id(self.request_id, prefix="upgrade")
        _validate_candidate_identifier(self.candidate_id)
        if self.prior_candidate_id is not None:
            _validate_candidate_identifier(self.prior_candidate_id)
        _validate_candidate_identifier(self.active_candidate_id)
        if (
            self.status not in {"upgraded", "replayed"}
            or self.active_candidate_id != self.candidate_id
            or self.compatibility_state != "compatible"
            or self.preflight_state != "ready"
            or self.activation_state != "activated"
            or self.restart_state != "restarted"
            or self.doctor_state != "healthy"
            or _HEX64.fullmatch(self.manifest_digest_sha256) is None
            or tuple(migration.component for migration in self.migrations)
            != ("engine", "app")
            or len(
                {
                    (migration.from_version, migration.to_version)
                    for migration in self.migrations
                }
            )
            != 1
        ):
            raise ValueError("invalid appliance upgrade receipt")
        _validate_backup_identifier(self.backup_id)

    def to_dict(self) -> dict[str, object]:
        return {
            "active_candidate_id": self.active_candidate_id,
            "activation_state": self.activation_state,
            "backup_id": self.backup_id,
            "candidate_id": self.candidate_id,
            "compatibility_state": self.compatibility_state,
            "doctor_state": self.doctor_state,
            "manifest_digest_sha256": self.manifest_digest_sha256,
            "migrations": [
                {
                    "component": migration.component,
                    "from_version": migration.from_version,
                    "status": migration.status,
                    "to_version": migration.to_version,
                }
                for migration in self.migrations
            ],
            "preflight_state": self.preflight_state,
            "prior_candidate_id": self.prior_candidate_id,
            "request_id": self.request_id,
            "restart_state": self.restart_state,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class ApplianceUninstallReceipt:
    request_id: str
    status: str
    prior_candidate_id: str | None
    daemon_stop_state: str
    supervisor_remove_state: str
    artifact_remove_state: str
    brain_root_state: str

    def __post_init__(self) -> None:
        _validate_owner_request_id(self.request_id, prefix="uninstall")
        if self.prior_candidate_id is not None:
            _validate_candidate_identifier(self.prior_candidate_id)
        if (
            self.status not in {"uninstalled", "replayed"}
            or self.daemon_stop_state != "stopped"
            or self.supervisor_remove_state != "removed"
            or self.artifact_remove_state != "removed"
            or self.brain_root_state != "preserved"
        ):
            raise ValueError("invalid appliance uninstall receipt")

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_remove_state": self.artifact_remove_state,
            "brain_root_state": self.brain_root_state,
            "daemon_stop_state": self.daemon_stop_state,
            "prior_candidate_id": self.prior_candidate_id,
            "request_id": self.request_id,
            "status": self.status,
            "supervisor_remove_state": self.supervisor_remove_state,
        }


@dataclass(frozen=True, slots=True)
class ApplianceLifecycleFailureReceipt:
    operation: str
    request_id: str
    status: str
    failure_stage: str
    candidate_id: str | None
    prior_candidate_id: str | None
    active_candidate_id: str | None
    rollback_state: str
    daemon_restore_state: str = "not_needed"

    def __post_init__(self) -> None:
        if self.operation not in {"upgrade", "uninstall"}:
            raise ValueError("invalid appliance lifecycle failure receipt")
        _validate_owner_request_id(self.request_id, prefix=self.operation)
        for candidate_id in (
            self.candidate_id,
            self.prior_candidate_id,
            self.active_candidate_id,
        ):
            if candidate_id is not None:
                _validate_candidate_identifier(candidate_id)
        stages = _UPGRADE_STAGES if self.operation == "upgrade" else _UNINSTALL_STAGES
        if (
            self.status != "failed"
            or self.failure_stage not in stages
            or self.rollback_state
            not in {"not_needed", "rolled_back", "rollback_failed"}
            or self.daemon_restore_state
            not in {"not_needed", "restored", "restore_failed"}
        ):
            raise ValueError("invalid appliance lifecycle failure receipt")

    def to_dict(self) -> dict[str, object]:
        return {
            "active_candidate_id": self.active_candidate_id,
            "candidate_id": self.candidate_id,
            "daemon_restore_state": self.daemon_restore_state,
            "failure_stage": self.failure_stage,
            "operation": self.operation,
            "prior_candidate_id": self.prior_candidate_id,
            "request_id": self.request_id,
            "rollback_state": self.rollback_state,
            "status": self.status,
        }


class ApplianceLifecycleError(RuntimeError):
    def __init__(self, receipt: ApplianceLifecycleFailureReceipt) -> None:
        self.receipt = receipt
        super().__init__(_FAILURE_MESSAGE)


class ArtifactLifecyclePort(Protocol):
    active_candidate_id: str | None

    def compatibility_preflight(
        self,
        candidate: ArtifactCandidate,
    ) -> ArtifactCompatibilityReceipt: ...

    def activate(self, candidate: ArtifactCandidate) -> ArtifactSwitchReceipt: ...

    def rollback(
        self,
        candidate: ArtifactCandidate,
        *,
        prior_candidate_id: str | None,
    ) -> ArtifactRollbackReceipt: ...

    def remove(self, *, current_candidate_id: str | None = None) -> ArtifactRemovalReceipt: ...


class RecoveryLifecyclePort(Protocol):
    def create_backup(self, destination: Path, *, backup_id: str) -> ApplianceBackupResult: ...

    def preflight_replacement(
        self,
        source: Path,
        disposable_root: Path,
    ) -> ApplianceReplacementPreflight: ...


class SupervisorLifecyclePort(Protocol):
    def quiesce(self) -> object: ...

    def resume(self) -> object: ...

    def restart(self) -> object: ...

    def status(self) -> str: ...

    def stop(self) -> object: ...

    def remove(self) -> object: ...


MigrationStep = Callable[[ArtifactCandidate], LifecycleMigrationReceipt]


class ApplianceLifecycleService:
    def __init__(
        self,
        root: Path,
        *,
        recovery: RecoveryLifecyclePort,
        artifact_port: ArtifactLifecyclePort | None = None,
        supervisor: SupervisorLifecyclePort,
        migrations: Sequence[MigrationStep],
        doctor_reader: Callable[[], Mapping[str, object]],
    ) -> None:
        if not isinstance(root, Path) or not root.is_absolute():
            raise ValueError("invalid appliance lifecycle root")
        if not callable(doctor_reader):
            raise ValueError("invalid appliance lifecycle doctor")
        self._root = root
        self._root_identity = capture_root_identity(root)
        self._lifecycle_lease = FileLease(
            root / ".open-brain",
            "lifecycle-" + sha256(str(root).encode("utf-8")).hexdigest()[:32],
            parent_root_identity=self._root_identity,
        )
        self._recovery = recovery
        self._artifact_port = (
            _ClosedArtifactLifecyclePort() if artifact_port is None else artifact_port
        )
        self._supervisor = supervisor
        self._migrations = tuple(migrations)
        self._doctor_reader = doctor_reader

    def upgrade(
        self,
        *,
        owner_request: OwnerLifecycleRequest | None,
        candidate: ArtifactCandidate,
        backup_destination: Path,
        disposable_root: Path,
    ) -> ApplianceUpgradeReceipt:
        with self._lifecycle_lease.acquire(LockScope.APPLIANCE_LIFECYCLE):
            return self._upgrade(
                owner_request=owner_request,
                candidate=candidate,
                backup_destination=backup_destination,
                disposable_root=disposable_root,
            )

    def _upgrade(
        self,
        *,
        owner_request: OwnerLifecycleRequest | None,
        candidate: ArtifactCandidate,
        backup_destination: Path,
        disposable_root: Path,
    ) -> ApplianceUpgradeReceipt:
        request = _require_owner_request(owner_request, prefix="upgrade")
        _require_absolute_path(backup_destination)
        _require_absolute_path(disposable_root)
        fingerprint = _request_fingerprint(
            "upgrade",
            request.request_id,
            request.requested_at,
            request.owner_confirmation,
            candidate.candidate_id,
            candidate.version,
            candidate.artifact_kind,
            str(backup_destination),
            str(disposable_root),
        )
        prior_candidate_id = _current_candidate_id(self._artifact_port)
        replayed = self._begin_request(
            operation="upgrade",
            request=request,
            fingerprint=fingerprint,
            candidate=candidate,
            prior_candidate_id=prior_candidate_id,
        )
        if replayed is not None:
            if not isinstance(replayed, ApplianceUpgradeReceipt):
                raise AssertionError("unexpected lifecycle replay type")
            return replayed

        compatibility = self._run_stage(
            fingerprint,
            "upgrade",
            request.request_id,
            "compatibility",
            candidate,
            prior_candidate_id,
            lambda: self._artifact_port.compatibility_preflight(candidate),
            rollback_candidate=None,
        )
        assert isinstance(compatibility, ArtifactCompatibilityReceipt)
        if (
            compatibility.candidate_id != candidate.candidate_id
            or compatibility.artifact_kind != candidate.artifact_kind
            or compatibility.target_version != candidate.version
            or compatibility.status != "compatible"
        ):
            self._store_failure(
                fingerprint,
                ApplianceLifecycleFailureReceipt(
                    operation="upgrade",
                    request_id=request.request_id,
                    status="failed",
                    failure_stage="compatibility",
                    candidate_id=candidate.candidate_id,
                    prior_candidate_id=prior_candidate_id,
                    active_candidate_id=prior_candidate_id,
                    rollback_state="not_needed",
                ),
            )
        self._run_stage(
            fingerprint,
            "upgrade",
            request.request_id,
            "quiesce",
            candidate,
            prior_candidate_id,
            self._supervisor.quiesce,
            rollback_candidate=None,
        )
        backup_id = "backup_" + request.request_id.removeprefix("upgrade_")
        backup = self._run_stage(
            fingerprint,
            "upgrade",
            request.request_id,
            "backup",
            candidate,
            prior_candidate_id,
            lambda: self._recovery.create_backup(backup_destination, backup_id=backup_id),
            rollback_candidate=None,
        )
        assert isinstance(backup, ApplianceBackupResult)
        if (
            backup.created.status != "created"
            or backup.created.backup_id != backup_id
            or backup.verified.status != "verified"
            or backup.verified.backup_id != backup_id
            or backup.created.manifest_digest_sha256
            != backup.verified.manifest_digest_sha256
        ):
            self._fail(
                fingerprint=fingerprint,
                operation="upgrade",
                request_id=request.request_id,
                failure_stage="backup",
                candidate=candidate,
                prior_candidate_id=prior_candidate_id,
                rollback_candidate=None,
            )
        preflight = self._run_stage(
            fingerprint,
            "upgrade",
            request.request_id,
            "preflight",
            candidate,
            prior_candidate_id,
            lambda: self._recovery.preflight_replacement(backup_destination, disposable_root),
            rollback_candidate=None,
        )
        assert isinstance(preflight, ApplianceReplacementPreflight)
        if (
            preflight.status != "ready"
            or preflight.backup_id != backup.verified.backup_id
            or preflight.manifest_digest_sha256
            != backup.verified.manifest_digest_sha256
            or preflight.credential_state != "created"
            or preflight.doctor_state != "healthy"
            or type(preflight.index_generation) is not int
            or preflight.index_generation < 1
        ):
            self._fail(
                fingerprint=fingerprint,
                operation="upgrade",
                request_id=request.request_id,
                failure_stage="preflight",
                candidate=candidate,
                prior_candidate_id=prior_candidate_id,
                rollback_candidate=None,
            )

        migration_components = tuple(
            _migration_component(migration) for migration in self._migrations
        )
        if migration_components != ("engine", "app"):
            self._set_stage(request.request_id, fingerprint, "migrations")
            self._fail(
                fingerprint=fingerprint,
                operation="upgrade",
                request_id=request.request_id,
                failure_stage="migrations",
                candidate=candidate,
                prior_candidate_id=prior_candidate_id,
                rollback_candidate=candidate,
            )
        migrations = tuple(
            self._run_migrations(
                candidate,
                request.request_id,
                prior_candidate_id,
                fingerprint=fingerprint,
                current_version=compatibility.current_version,
            )
        )
        activation = self._run_stage(
            fingerprint,
            "upgrade",
            request.request_id,
            "activate",
            candidate,
            prior_candidate_id,
            lambda: self._artifact_port.activate(candidate),
            rollback_candidate=candidate,
        )
        assert isinstance(activation, ArtifactSwitchReceipt)
        if (
            activation.candidate_id != candidate.candidate_id
            or activation.artifact_kind != candidate.artifact_kind
            or activation.active_candidate_id != candidate.candidate_id
            or _current_candidate_id(self._artifact_port) != candidate.candidate_id
        ):
            self._fail(
                fingerprint=fingerprint,
                operation="upgrade",
                request_id=request.request_id,
                failure_stage="activate",
                candidate=candidate,
                prior_candidate_id=prior_candidate_id,
                rollback_candidate=candidate,
            )
        self._run_stage(
            fingerprint,
            "upgrade",
            request.request_id,
            "restart",
            candidate,
            prior_candidate_id,
            self._restart_and_check,
            rollback_candidate=candidate,
        )
        doctor_state = self._run_stage(
            fingerprint,
            "upgrade",
            request.request_id,
            "doctor",
            candidate,
            prior_candidate_id,
            self._doctor_state,
            rollback_candidate=candidate,
        )
        assert isinstance(doctor_state, str)
        if doctor_state != "healthy":
            self._fail(
                fingerprint=fingerprint,
                operation="upgrade",
                request_id=request.request_id,
                failure_stage="doctor",
                candidate=candidate,
                prior_candidate_id=prior_candidate_id,
                rollback_candidate=candidate,
            )

        receipt = ApplianceUpgradeReceipt(
            request_id=request.request_id,
            status="upgraded",
            candidate_id=candidate.candidate_id,
            prior_candidate_id=prior_candidate_id,
            active_candidate_id=activation.active_candidate_id,
            compatibility_state=compatibility.status,
            backup_id=backup.verified.backup_id,
            manifest_digest_sha256=backup.verified.manifest_digest_sha256,
            preflight_state=preflight.status,
            migrations=migrations,
            activation_state=activation.status,
            restart_state="restarted",
            doctor_state=doctor_state,
        )
        self._store_success(fingerprint, receipt)
        return receipt

    def uninstall(
        self,
        *,
        owner_request: OwnerLifecycleRequest | None,
    ) -> ApplianceUninstallReceipt:
        with self._lifecycle_lease.acquire(LockScope.APPLIANCE_LIFECYCLE):
            return self._uninstall(owner_request=owner_request)

    def _uninstall(
        self,
        *,
        owner_request: OwnerLifecycleRequest | None,
    ) -> ApplianceUninstallReceipt:
        request = _require_owner_request(owner_request, prefix="uninstall")
        prior_candidate_id = _current_candidate_id(self._artifact_port)
        fingerprint = _request_fingerprint(
            "uninstall",
            request.request_id,
            request.requested_at,
            request.owner_confirmation,
        )
        replayed = self._begin_request(
            operation="uninstall",
            request=request,
            fingerprint=fingerprint,
            candidate=None,
            prior_candidate_id=prior_candidate_id,
        )
        if replayed is not None:
            if not isinstance(replayed, ApplianceUninstallReceipt):
                raise AssertionError("unexpected lifecycle replay type")
            return replayed

        self._run_stage(
            fingerprint,
            "uninstall",
            request.request_id,
            "stop",
            None,
            prior_candidate_id,
            self._supervisor.stop,
            rollback_candidate=None,
        )
        self._run_stage(
            fingerprint,
            "uninstall",
            request.request_id,
            "remove",
            None,
            prior_candidate_id,
            self._supervisor.remove,
            rollback_candidate=None,
        )
        removal = self._run_stage(
            fingerprint,
            "uninstall",
            request.request_id,
            "artifact-remove",
            None,
            prior_candidate_id,
            lambda: self._artifact_port.remove(current_candidate_id=prior_candidate_id),
            rollback_candidate=None,
        )
        assert isinstance(removal, ArtifactRemovalReceipt)
        if (
            removal.removed_candidate_id != prior_candidate_id
            or _current_candidate_id(self._artifact_port) is not None
        ):
            self._fail(
                fingerprint=fingerprint,
                operation="uninstall",
                request_id=request.request_id,
                failure_stage="artifact-remove",
                candidate=None,
                prior_candidate_id=prior_candidate_id,
                rollback_candidate=None,
            )
        if capture_root_identity(self._root) != self._root_identity:
            self._fail(
                fingerprint=fingerprint,
                operation="uninstall",
                request_id=request.request_id,
                failure_stage="artifact-remove",
                candidate=None,
                prior_candidate_id=prior_candidate_id,
                rollback_candidate=None,
            )
        receipt = ApplianceUninstallReceipt(
            request_id=request.request_id,
            status="uninstalled",
            prior_candidate_id=prior_candidate_id,
            daemon_stop_state="stopped",
            supervisor_remove_state="removed",
            artifact_remove_state=removal.status,
            brain_root_state="preserved",
        )
        self._store_success(fingerprint, receipt)
        return receipt

    def _begin_request(
        self,
        *,
        operation: str,
        request: OwnerLifecycleRequest,
        fingerprint: str,
        candidate: ArtifactCandidate | None,
        prior_candidate_id: str | None,
    ) -> ApplianceUpgradeReceipt | ApplianceUninstallReceipt | None:
        record = _new_lifecycle_record(
            operation=operation,
            request=request,
            fingerprint=fingerprint,
            candidate=candidate,
            prior_candidate_id=prior_candidate_id,
        )
        existing = _read_lifecycle_record(
            self._root,
            self._root_identity,
            request.request_id,
        )
        if existing is None:
            try:
                state = atomic_write_new(
                    root=self._root,
                    relative=_lifecycle_relative(request.request_id).as_posix(),
                    data=_lifecycle_record_bytes(record),
                    expected_root_identity=self._root_identity,
                )
            except StorageError:
                existing = _read_lifecycle_record(
                    self._root,
                    self._root_identity,
                    request.request_id,
                )
                if existing is None:
                    raise
            else:
                if state is WriteState.CREATED:
                    return None
                existing = _read_lifecycle_record(
                    self._root,
                    self._root_identity,
                    request.request_id,
                )
        if existing is None:
            raise ValueError("invalid appliance lifecycle journal")
        return self._replay_record(existing, fingerprint=fingerprint)

    def _replay_record(
        self,
        record: dict[str, object],
        *,
        fingerprint: str,
    ) -> ApplianceUpgradeReceipt | ApplianceUninstallReceipt:
        if record["fingerprint_sha256"] != fingerprint:
            raise ValueError("conflicting appliance lifecycle request")
        status = cast(str, record["status"])
        if status == "completed":
            stored = _success_receipt_from_record(record)
            return _as_replayed(stored)
        if status == "failed":
            raise ApplianceLifecycleError(_failure_receipt_from_record(record))
        candidate = _candidate_from_record(record)
        prior_candidate_id = cast(str | None, record["prior_candidate_id"])
        rollback_candidate = (
            candidate
            if record["operation"] == "upgrade"
            and record["stage"] in _ROLLBACK_REQUIRED_STAGES
            else None
        )
        rollback_state, active_candidate_id = self._rollback(
            rollback_candidate,
            prior_candidate_id=prior_candidate_id,
        )
        daemon_restore_state = self._restore_daemon(
            required=(
                record["operation"] == "upgrade"
                and record["stage"] in _DAEMON_RESTORE_REQUIRED_STAGES
            )
        )
        receipt = ApplianceLifecycleFailureReceipt(
            operation=cast(str, record["operation"]),
            request_id=cast(str, record["request_id"]),
            status="failed",
            failure_stage="interrupted",
            candidate_id=None if candidate is None else candidate.candidate_id,
            prior_candidate_id=prior_candidate_id,
            active_candidate_id=active_candidate_id,
            rollback_state=rollback_state,
            daemon_restore_state=daemon_restore_state,
        )
        self._persist_terminal(fingerprint, receipt)
        raise ApplianceLifecycleError(receipt)

    def _set_stage(self, request_id: str, fingerprint: str, stage: str) -> None:
        record = _required_lifecycle_record(
            self._root,
            self._root_identity,
            request_id,
        )
        if record["fingerprint_sha256"] != fingerprint or record["status"] != "pending":
            raise ValueError("invalid appliance lifecycle journal")
        updated = dict(record)
        updated["stage"] = stage
        atomic_replace(
            root=self._root,
            relative=_lifecycle_relative(request_id).as_posix(),
            data=_lifecycle_record_bytes(updated),
            require_existing=True,
            expected_root_identity=self._root_identity,
        )

    def _store_success(
        self,
        fingerprint: str,
        receipt: ApplianceUpgradeReceipt | ApplianceUninstallReceipt,
    ) -> None:
        self._persist_terminal(fingerprint, receipt)

    def _persist_terminal(
        self,
        fingerprint: str,
        receipt: ApplianceUpgradeReceipt
        | ApplianceUninstallReceipt
        | ApplianceLifecycleFailureReceipt,
    ) -> None:
        record = _required_lifecycle_record(
            self._root,
            self._root_identity,
            receipt.request_id,
        )
        if record["fingerprint_sha256"] != fingerprint or record["status"] != "pending":
            raise ValueError("invalid appliance lifecycle journal")
        updated = dict(record)
        updated["receipt"] = receipt.to_dict()
        updated["status"] = (
            "failed" if isinstance(receipt, ApplianceLifecycleFailureReceipt) else "completed"
        )
        atomic_replace(
            root=self._root,
            relative=_lifecycle_relative(receipt.request_id).as_posix(),
            data=_lifecycle_record_bytes(updated),
            require_existing=True,
            expected_root_identity=self._root_identity,
        )

    def _run_migrations(
        self,
        candidate: ArtifactCandidate,
        request_id: str,
        prior_candidate_id: str | None,
        *,
        fingerprint: str,
        current_version: str,
    ) -> Sequence[LifecycleMigrationReceipt]:
        receipts: list[LifecycleMigrationReceipt] = []
        for migration in self._migrations:
            component = _migration_component(migration)

            def run_migration(current: MigrationStep = migration) -> LifecycleMigrationReceipt:
                return current(candidate)

            receipt = self._run_stage(
                fingerprint,
                "upgrade",
                request_id,
                component,
                candidate,
                prior_candidate_id,
                run_migration,
                rollback_candidate=candidate,
            )
            assert isinstance(receipt, LifecycleMigrationReceipt)
            if (
                receipt.component != component
                or receipt.from_version != current_version
                or receipt.to_version != candidate.version
                or receipt.status != "applied"
            ):
                self._fail(
                    fingerprint=fingerprint,
                    operation="upgrade",
                    request_id=request_id,
                    failure_stage=component,
                    candidate=candidate,
                    prior_candidate_id=prior_candidate_id,
                    rollback_candidate=candidate,
                )
            receipts.append(receipt)
        return tuple(receipts)

    def _restart_and_check(self) -> str:
        self._supervisor.resume()
        status = self._supervisor.status()
        if status != "active":
            raise RuntimeError("invalid appliance restart status")
        return "restarted"

    def _doctor_state(self) -> str:
        payload = self._doctor_reader()
        if not isinstance(payload, Mapping):
            raise RuntimeError("invalid appliance doctor evidence")
        state = payload.get("state")
        if not isinstance(state, str) or not state:
            raise RuntimeError("invalid appliance doctor evidence")
        return state

    def _run_stage(
        self,
        fingerprint: str,
        operation: str,
        request_id: str,
        failure_stage: str,
        candidate: ArtifactCandidate | None,
        prior_candidate_id: str | None,
        action: Callable[[], object],
        *,
        rollback_candidate: ArtifactCandidate | None,
    ) -> object:
        self._set_stage(request_id, fingerprint, failure_stage)
        try:
            return action()
        except ApplianceLifecycleError:
            raise
        except Exception:
            self._fail(
                fingerprint=fingerprint,
                operation=operation,
                request_id=request_id,
                failure_stage=failure_stage,
                candidate=candidate,
                prior_candidate_id=prior_candidate_id,
                rollback_candidate=rollback_candidate,
            )
        raise AssertionError("unreachable")

    def _fail(
        self,
        *,
        fingerprint: str,
        operation: str,
        request_id: str,
        failure_stage: str,
        candidate: ArtifactCandidate | None,
        prior_candidate_id: str | None,
        rollback_candidate: ArtifactCandidate | None,
    ) -> None:
        rollback_state, active_candidate_id = self._rollback(
            rollback_candidate,
            prior_candidate_id=prior_candidate_id,
        )
        daemon_restore_state = self._restore_daemon(
            required=(
                operation == "upgrade"
                and failure_stage in _DAEMON_RESTORE_REQUIRED_STAGES
            )
        )
        receipt = ApplianceLifecycleFailureReceipt(
            operation=operation,
            request_id=request_id,
            status="failed",
            failure_stage=failure_stage,
            candidate_id=None if candidate is None else candidate.candidate_id,
            prior_candidate_id=prior_candidate_id,
            active_candidate_id=active_candidate_id,
            rollback_state=rollback_state,
            daemon_restore_state=daemon_restore_state,
        )
        self._persist_terminal(fingerprint, receipt)
        raise ApplianceLifecycleError(receipt)

    def _rollback(
        self,
        candidate: ArtifactCandidate | None,
        *,
        prior_candidate_id: str | None,
    ) -> tuple[str, str | None]:
        if candidate is None:
            return "not_needed", prior_candidate_id
        try:
            rollback = self._artifact_port.rollback(
                candidate,
                prior_candidate_id=prior_candidate_id,
            )
            active_candidate_id = _current_candidate_id(self._artifact_port)
            if (
                rollback.candidate_id != candidate.candidate_id
                or rollback.artifact_kind != candidate.artifact_kind
                or rollback.active_candidate_id != prior_candidate_id
                or active_candidate_id != prior_candidate_id
            ):
                raise ValueError("invalid artifact rollback receipt")
        except Exception:
            try:
                active_candidate_id = _current_candidate_id(self._artifact_port)
            except ValueError:
                active_candidate_id = None
            return "rollback_failed", active_candidate_id
        return "rolled_back", active_candidate_id

    def _restore_daemon(self, *, required: bool) -> str:
        if not required:
            return "not_needed"
        try:
            self._restart_and_check()
        except Exception:
            return "restore_failed"
        return "restored"

    def _store_failure(self, fingerprint: str, receipt: ApplianceLifecycleFailureReceipt) -> None:
        self._persist_terminal(fingerprint, receipt)
        raise ApplianceLifecycleError(receipt)


def _new_lifecycle_record(
    *,
    operation: str,
    request: OwnerLifecycleRequest,
    fingerprint: str,
    candidate: ArtifactCandidate | None,
    prior_candidate_id: str | None,
) -> dict[str, object]:
    return _validate_lifecycle_record(
        {
            "candidate": (
                None
                if candidate is None
                else {
                    "artifact_kind": candidate.artifact_kind,
                    "candidate_id": candidate.candidate_id,
                    "version": candidate.version,
                }
            ),
            "fingerprint_sha256": fingerprint,
            "operation": operation,
            "prior_candidate_id": prior_candidate_id,
            "receipt": None,
            "request_id": request.request_id,
            "requested_at": request.requested_at,
            "schema_version": 1,
            "stage": "requested",
            "status": "pending",
        }
    )


def _lifecycle_relative(request_id: str) -> Path:
    match = _OWNER_REQUEST.fullmatch(request_id) if isinstance(request_id, str) else None
    if match is None:
        raise ValueError("invalid appliance lifecycle request")
    return _LIFECYCLE_DIRECTORY / match.group("prefix") / f"{request_id}.json"


def _read_lifecycle_record(
    root: Path,
    root_identity: RootIdentity,
    request_id: str,
) -> dict[str, object] | None:
    payload = read_confined(
        root=root,
        relative=_lifecycle_relative(request_id).as_posix(),
        expected_root_identity=root_identity,
        maximum_bytes=_MAXIMUM_LIFECYCLE_RECORD_BYTES,
    )
    if payload is None:
        return None
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid appliance lifecycle journal") from error
    record = _validate_lifecycle_record(value)
    if canonical_json_bytes(record) != payload:
        raise ValueError("invalid appliance lifecycle journal")
    return record


def _required_lifecycle_record(
    root: Path,
    root_identity: RootIdentity,
    request_id: str,
) -> dict[str, object]:
    record = _read_lifecycle_record(root, root_identity, request_id)
    if record is None:
        raise ValueError("invalid appliance lifecycle journal")
    return record


def _lifecycle_record_bytes(record: dict[str, object]) -> bytes:
    payload = canonical_json_bytes(_validate_lifecycle_record(record))
    if len(payload) > _MAXIMUM_LIFECYCLE_RECORD_BYTES:
        raise ValueError("appliance lifecycle journal exceeds bounded size")
    return payload


def _validate_lifecycle_record(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise ValueError("invalid appliance lifecycle journal")
    record = cast(dict[str, object], value)
    if set(record) != {
        "candidate",
        "fingerprint_sha256",
        "operation",
        "prior_candidate_id",
        "receipt",
        "request_id",
        "requested_at",
        "schema_version",
        "stage",
        "status",
    }:
        raise ValueError("invalid appliance lifecycle journal")
    operation = record.get("operation")
    request_id = record.get("request_id")
    fingerprint = record.get("fingerprint_sha256")
    status = record.get("status")
    stage = record.get("stage")
    if (
        operation not in {"upgrade", "uninstall"}
        or not isinstance(request_id, str)
        or not isinstance(fingerprint, str)
        or _HEX64.fullmatch(fingerprint) is None
        or status not in {"pending", "completed", "failed"}
        or not isinstance(stage, str)
        or stage not in (_UPGRADE_STAGES if operation == "upgrade" else _UNINSTALL_STAGES)
        or record.get("schema_version") != 1
    ):
        raise ValueError("invalid appliance lifecycle journal")
    _validate_owner_request_id(request_id, prefix=operation)
    _parse_timestamp(cast(str, record.get("requested_at")))
    prior_candidate_id = record.get("prior_candidate_id")
    if prior_candidate_id is not None:
        _validate_candidate_identifier(cast(str, prior_candidate_id))
    candidate = _candidate_from_record(record)
    if (operation == "upgrade") is (candidate is None):
        raise ValueError("invalid appliance lifecycle journal")
    receipt = record.get("receipt")
    if status == "pending":
        if receipt is not None:
            raise ValueError("invalid appliance lifecycle journal")
    elif type(receipt) is not dict:
        raise ValueError("invalid appliance lifecycle journal")
    elif status == "completed":
        parsed = _success_receipt_from_record(record)
        if parsed.request_id != request_id:
            raise ValueError("invalid appliance lifecycle journal")
    else:
        parsed_failure = _failure_receipt_from_record(record)
        if parsed_failure.request_id != request_id or parsed_failure.operation != operation:
            raise ValueError("invalid appliance lifecycle journal")
    return record


def _candidate_from_record(record: dict[str, object]) -> ArtifactCandidate | None:
    value = record.get("candidate")
    if value is None:
        return None
    if type(value) is not dict or set(value) != {
        "artifact_kind",
        "candidate_id",
        "version",
    }:
        raise ValueError("invalid appliance lifecycle journal")
    candidate = cast(dict[str, object], value)
    return ArtifactCandidate(
        artifact_kind=_record_string(candidate, "artifact_kind"),
        candidate_id=_record_string(candidate, "candidate_id"),
        version=_record_string(candidate, "version"),
    )


def _success_receipt_from_record(
    record: dict[str, object],
) -> ApplianceUpgradeReceipt | ApplianceUninstallReceipt:
    receipt = record.get("receipt")
    if type(receipt) is not dict:
        raise ValueError("invalid appliance lifecycle journal")
    if record.get("operation") == "upgrade":
        return _upgrade_receipt_from_dict(cast(dict[str, object], receipt))
    return _uninstall_receipt_from_dict(cast(dict[str, object], receipt))


def _upgrade_receipt_from_dict(value: dict[str, object]) -> ApplianceUpgradeReceipt:
    if set(value) != {
        "active_candidate_id",
        "activation_state",
        "backup_id",
        "candidate_id",
        "compatibility_state",
        "doctor_state",
        "manifest_digest_sha256",
        "migrations",
        "preflight_state",
        "prior_candidate_id",
        "request_id",
        "restart_state",
        "status",
    }:
        raise ValueError("invalid appliance lifecycle journal")
    migration_values = value.get("migrations")
    if not isinstance(migration_values, list):
        raise ValueError("invalid appliance lifecycle journal")
    migrations: list[LifecycleMigrationReceipt] = []
    for migration_value in migration_values:
        if type(migration_value) is not dict or set(migration_value) != {
            "component",
            "from_version",
            "status",
            "to_version",
        }:
            raise ValueError("invalid appliance lifecycle journal")
        migration = cast(dict[str, object], migration_value)
        migrations.append(
            LifecycleMigrationReceipt(
                component=_record_string(migration, "component"),
                from_version=_record_string(migration, "from_version"),
                status=_record_string(migration, "status"),
                to_version=_record_string(migration, "to_version"),
            )
        )
    return ApplianceUpgradeReceipt(
        active_candidate_id=_record_string(value, "active_candidate_id"),
        activation_state=_record_string(value, "activation_state"),
        backup_id=_record_string(value, "backup_id"),
        candidate_id=_record_string(value, "candidate_id"),
        compatibility_state=_record_string(value, "compatibility_state"),
        doctor_state=_record_string(value, "doctor_state"),
        manifest_digest_sha256=_record_string(value, "manifest_digest_sha256"),
        migrations=tuple(migrations),
        preflight_state=_record_string(value, "preflight_state"),
        prior_candidate_id=_record_optional_string(value, "prior_candidate_id"),
        request_id=_record_string(value, "request_id"),
        restart_state=_record_string(value, "restart_state"),
        status=_record_string(value, "status"),
    )


def _uninstall_receipt_from_dict(value: dict[str, object]) -> ApplianceUninstallReceipt:
    if set(value) != {
        "artifact_remove_state",
        "brain_root_state",
        "daemon_stop_state",
        "prior_candidate_id",
        "request_id",
        "status",
        "supervisor_remove_state",
    }:
        raise ValueError("invalid appliance lifecycle journal")
    return ApplianceUninstallReceipt(
        artifact_remove_state=_record_string(value, "artifact_remove_state"),
        brain_root_state=_record_string(value, "brain_root_state"),
        daemon_stop_state=_record_string(value, "daemon_stop_state"),
        prior_candidate_id=_record_optional_string(value, "prior_candidate_id"),
        request_id=_record_string(value, "request_id"),
        status=_record_string(value, "status"),
        supervisor_remove_state=_record_string(value, "supervisor_remove_state"),
    )


def _failure_receipt_from_record(record: dict[str, object]) -> ApplianceLifecycleFailureReceipt:
    value = record.get("receipt")
    if type(value) is not dict or set(value) != {
        "active_candidate_id",
        "candidate_id",
        "daemon_restore_state",
        "failure_stage",
        "operation",
        "prior_candidate_id",
        "request_id",
        "rollback_state",
        "status",
    }:
        raise ValueError("invalid appliance lifecycle journal")
    receipt = cast(dict[str, object], value)
    return ApplianceLifecycleFailureReceipt(
        active_candidate_id=_record_optional_string(receipt, "active_candidate_id"),
        candidate_id=_record_optional_string(receipt, "candidate_id"),
        daemon_restore_state=_record_string(receipt, "daemon_restore_state"),
        failure_stage=_record_string(receipt, "failure_stage"),
        operation=_record_string(receipt, "operation"),
        prior_candidate_id=_record_optional_string(receipt, "prior_candidate_id"),
        request_id=_record_string(receipt, "request_id"),
        rollback_state=_record_string(receipt, "rollback_state"),
        status=_record_string(receipt, "status"),
    )


def _record_string(value: dict[str, object], key: str) -> str:
    selected = value.get(key)
    if not isinstance(selected, str):
        raise ValueError("invalid appliance lifecycle journal")
    return selected


def _record_optional_string(value: dict[str, object], key: str) -> str | None:
    selected = value.get(key)
    if selected is not None and not isinstance(selected, str):
        raise ValueError("invalid appliance lifecycle journal")
    return selected


def submit_control_request(root: Path, request: ControlRequest) -> ControlReceipt:
    if not isinstance(root, Path) or not isinstance(request, ControlRequest):
        raise ValueError("invalid appliance control request")
    return request_control(root, request)


def dispatch_phase1_command(
    root: Path,
    *,
    command: str,
    argv: tuple[str, ...],
) -> CliControlReceipt:
    if not isinstance(root, Path) or not isinstance(command, str) or not isinstance(argv, tuple):
        raise ValueError("invalid appliance control request")
    return request_cli_dispatch(root, CliControlRequest(command=command, argv=argv))


def read_status_via_control(root: Path) -> StatusControlReceipt:
    if not isinstance(root, Path):
        raise ValueError("invalid appliance control request")
    return request_status(root)


def run_supervisor_action(root: Path, *, action: str) -> dict[str, object]:
    if (
        not isinstance(root, Path)
        or not isinstance(action, str)
        or action not in _SUPERVISOR_ACTIONS
    ):
        raise ValueError("invalid appliance supervisor request")
    supervisor = _supervisor(root)
    if action == "discover":
        return {
            "action": action,
            "command": "supervisor",
            "status": "ok",
            "supervisor": type(supervisor).__name__.removesuffix("Supervisor").casefold(),
            "unit_name": supervisor.unit_name,
        }
    operation = {
        "install": supervisor.install,
        "start": supervisor.start,
        "stop": supervisor.stop,
        "restart": supervisor.restart,
        "status": supervisor.status,
        "remove": supervisor.remove,
    }[action]
    operation()
    return {
        "action": action,
        "command": "supervisor",
        "status": "ok",
        "supervisor": type(supervisor).__name__.removesuffix("Supervisor").casefold(),
        "unit_name": supervisor.unit_name,
    }


def _source_checkout_root() -> Path | None:
    module = Path(__file__).resolve()
    try:
        candidate = module.parents[3]
    except IndexError:
        return None
    expected = candidate / "src/open_brain/services/appliance_lifecycle.py"
    try:
        return candidate if expected.resolve(strict=True) == module else None
    except OSError:
        return None


def _supervisor(root: Path) -> LaunchdSupervisor | SystemdSupervisor:
    native_runtime = bool(getattr(sys, "frozen", False))
    checkout_root = None if native_runtime else _source_checkout_root()
    runtime_kind = "native-onedir" if native_runtime else "python"
    native_effects = native_supervisor_effects() if native_runtime else None
    host = platform.system()
    if host == "Darwin":
        if native_effects is not None:
            return LaunchdSupervisor(
                root=root,
                checkout_root=checkout_root,
                python_executable=sys.executable,
                unit_directory=Path.home() / "Library" / "LaunchAgents",
                user_id=os.getuid(),
                runtime_kind=runtime_kind,
                write_file=native_effects[0],
                remove_file=native_effects[1],
                run_command=native_effects[2],
            )
        return LaunchdSupervisor(
            root=root,
            checkout_root=checkout_root,
            python_executable=sys.executable,
            unit_directory=Path.home() / "Library" / "LaunchAgents",
            user_id=os.getuid(),
            runtime_kind=runtime_kind,
        )
    if host == "Linux":
        if native_effects is not None:
            return SystemdSupervisor(
                root=root,
                checkout_root=checkout_root,
                python_executable=sys.executable,
                unit_directory=Path.home() / ".config" / "systemd" / "user",
                runtime_kind=runtime_kind,
                write_file=native_effects[0],
                remove_file=native_effects[1],
                run_command=native_effects[2],
            )
        return SystemdSupervisor(
            root=root,
            checkout_root=checkout_root,
            python_executable=sys.executable,
            unit_directory=Path.home() / ".config" / "systemd" / "user",
            runtime_kind=runtime_kind,
        )
    raise ValueError("unsupported appliance supervisor")


def _as_replayed(
    receipt: ApplianceUpgradeReceipt | ApplianceUninstallReceipt,
) -> ApplianceUpgradeReceipt | ApplianceUninstallReceipt:
    if isinstance(receipt, ApplianceUpgradeReceipt):
        return ApplianceUpgradeReceipt(
            request_id=receipt.request_id,
            status="replayed",
            candidate_id=receipt.candidate_id,
            prior_candidate_id=receipt.prior_candidate_id,
            active_candidate_id=receipt.active_candidate_id,
            compatibility_state=receipt.compatibility_state,
            backup_id=receipt.backup_id,
            manifest_digest_sha256=receipt.manifest_digest_sha256,
            preflight_state=receipt.preflight_state,
            migrations=receipt.migrations,
            activation_state=receipt.activation_state,
            restart_state=receipt.restart_state,
            doctor_state=receipt.doctor_state,
        )
    return ApplianceUninstallReceipt(
        request_id=receipt.request_id,
        status="replayed",
        prior_candidate_id=receipt.prior_candidate_id,
        daemon_stop_state=receipt.daemon_stop_state,
        supervisor_remove_state=receipt.supervisor_remove_state,
        artifact_remove_state=receipt.artifact_remove_state,
        brain_root_state=receipt.brain_root_state,
    )


def _current_candidate_id(port: ArtifactLifecyclePort) -> str | None:
    value = port.active_candidate_id
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("invalid active artifact candidate")
    _validate_candidate_identifier(value)
    return value


def _migration_component(migration: MigrationStep) -> str:
    name = getattr(migration, "__name__", "")
    if ":" in name:
        name = name.split(":", maxsplit=1)[1]
    return "engine" if "engine" in name else "app"


def _parse_timestamp(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("invalid lifecycle timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00").astimezone(UTC)
    except ValueError as error:
        raise ValueError("invalid lifecycle timestamp") from error
    if parsed.isoformat().replace("+00:00", "Z") != value:
        raise ValueError("invalid lifecycle timestamp")
    return parsed


def _request_fingerprint(*parts: str) -> str:
    return sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _require_owner_request(
    request: OwnerLifecycleRequest | None,
    *,
    prefix: str,
) -> OwnerLifecycleRequest:
    if not isinstance(request, OwnerLifecycleRequest):
        raise ValueError("explicit owner request is required")
    try:
        _validate_owner_request_id(request.request_id, prefix=prefix)
    except ValueError as error:
        raise ValueError("explicit owner request is required") from error
    return request


def _require_absolute_path(path: Path) -> None:
    if not isinstance(path, Path) or not path.is_absolute():
        raise ValueError("invalid lifecycle path")


def _validate_candidate_identifier(value: str) -> None:
    if not isinstance(value, str) or _CANDIDATE_ID.fullmatch(value) is None:
        raise ValueError("invalid artifact candidate")


def _validate_owner_request_id(value: str, *, prefix: str) -> None:
    match = _OWNER_REQUEST.fullmatch(value) if isinstance(value, str) else None
    if match is None or match.group("prefix") != prefix:
        raise ValueError("invalid appliance lifecycle request")


def _validate_backup_identifier(value: str) -> None:
    if not isinstance(value, str) or _BACKUP_ID.fullmatch(value) is None:
        raise ValueError("invalid appliance backup receipt")


def _validate_version(value: str) -> None:
    if not isinstance(value, str) or _VERSION.fullmatch(value) is None:
        raise ValueError("invalid artifact version")


class _ClosedArtifactLifecyclePort:
    def __init__(self) -> None:
        self.active_candidate_id: str | None = None

    def compatibility_preflight(self, candidate: ArtifactCandidate) -> ArtifactCompatibilityReceipt:
        del candidate
        raise RuntimeError("artifact lifecycle port unavailable")

    def activate(self, candidate: ArtifactCandidate) -> ArtifactSwitchReceipt:
        del candidate
        raise RuntimeError("artifact lifecycle port unavailable")

    def rollback(
        self,
        candidate: ArtifactCandidate,
        *,
        prior_candidate_id: str | None,
    ) -> ArtifactRollbackReceipt:
        del candidate, prior_candidate_id
        raise RuntimeError("artifact lifecycle port unavailable")

    def remove(self, *, current_candidate_id: str | None = None) -> ArtifactRemovalReceipt:
        del current_candidate_id
        raise RuntimeError("artifact lifecycle port unavailable")


__all__ = [
    "ApplianceLifecycleError",
    "ApplianceLifecycleFailureReceipt",
    "ApplianceLifecycleService",
    "ApplianceUninstallReceipt",
    "ApplianceUpgradeReceipt",
    "ArtifactCandidate",
    "ArtifactCompatibilityReceipt",
    "ArtifactLifecyclePort",
    "ArtifactRemovalReceipt",
    "ArtifactRollbackReceipt",
    "ArtifactSwitchReceipt",
    "LifecycleMigrationReceipt",
    "OwnerLifecycleRequest",
    "dispatch_phase1_command",
    "read_status_via_control",
    "run_supervisor_action",
    "submit_control_request",
]

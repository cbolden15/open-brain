from __future__ import annotations

import asyncio
import inspect
import json
import math
import multiprocessing as mp
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess

from .models import ExitClass, OperationsValidationError


class CutoverProbeName(StrEnum):
    CONFIG_SECRETS = "config-secrets"
    ROOTS_REMOTES = "roots-remotes"
    DEPENDENCIES = "dependencies"
    SCHEMAS = "schemas"
    RECOVERY = "recovery"
    NETWORK_BINDS = "network-binds"
    WRITERS = "writers"
    GATES_SCOPE = "gates-scope"


class AcceptanceRow(StrEnum):
    DOC_001 = "DOC-001"
    DOC_002 = "DOC-002"
    DOC_003 = "DOC-003"
    DOC_004 = "DOC-004"
    DOC_005 = "DOC-005"
    DOC_006 = "DOC-006"
    DOC_007 = "DOC-007"
    DOC_008 = "DOC-008"


class SecretState(StrEnum):
    PRESENT = "present"
    MISSING = "missing"
    EMPTY = "empty"


class DependencyKind(StrEnum):
    PROVIDER = "provider"
    SERVICE = "service"


class SchemaKind(StrEnum):
    QUEUE = "queue"
    DATABASE = "database"


class BindExposure(StrEnum):
    DISABLED = "disabled"
    LOOPBACK = "loopback"
    UNIX_SOCKET = "unix-socket"
    WILDCARD = "wildcard"
    PUBLIC = "public"


class RecoveryOperation(StrEnum):
    BACKUP = "backup"
    RESTORE = "restore"


class WriterGeneration(StrEnum):
    LEGACY = "legacy"
    NEW = "new"


class WriterRole(StrEnum):
    LEGACY = "legacy"
    CANONICAL = "canonical"


class CutoverCheckState(StrEnum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    UNAVAILABLE = "unavailable"


class CutoverDoctorOutcome(StrEnum):
    SYNTHETIC_READY = "synthetic-ready"
    NOT_READY = "not-ready"
    UNAVAILABLE = "unavailable"


class CutoverFindingClass(StrEnum):
    MANIFEST_DIGEST_MISMATCH = "manifest-digest-mismatch"
    CONFIG_INVALID = "config-invalid"
    SECRET_EVIDENCE_INCOMPLETE = "secret-evidence-incomplete"
    REQUIRED_SECRET_MISSING = "required-secret-missing"
    REQUIRED_SECRET_EMPTY = "required-secret-empty"
    ROOT_EVIDENCE_INCOMPLETE = "root-evidence-incomplete"
    ROOT_MISSING = "root-missing"
    ROOT_PERMISSIONS_UNSAFE = "root-permissions-unsafe"
    PROHIBITED_REMOTE = "prohibited-remote"
    DEPENDENCY_EVIDENCE_INCOMPLETE = "dependency-evidence-incomplete"
    DEPENDENCY_UNREACHABLE = "dependency-unreachable"
    SCHEMA_EVIDENCE_INCOMPLETE = "schema-evidence-incomplete"
    SCHEMA_NOT_CURRENT = "schema-not-current"
    BACKUP_DESTINATION_UNAVAILABLE = "backup-destination-unavailable"
    BACKUP_EVIDENCE_MISSING = "backup-evidence-missing"
    BACKUP_EVIDENCE_STALE = "backup-evidence-stale"
    RESTORE_EVIDENCE_MISSING = "restore-evidence-missing"
    RESTORE_EVIDENCE_STALE = "restore-evidence-stale"
    RECOVERY_NOT_SUCCESSFUL = "recovery-not-successful"
    RECOVERY_RECEIPT_MISMATCH = "recovery-receipt-mismatch"
    RECOVERY_EVIDENCE_CONTRADICTORY = "recovery-evidence-contradictory"
    BIND_EVIDENCE_INCOMPLETE = "bind-evidence-incomplete"
    UNSAFE_NETWORK_BIND = "unsafe-network-bind"
    WRITER_EVIDENCE_INCOMPLETE = "writer-evidence-incomplete"
    LEGACY_WRITER_ACTIVE = "legacy-writer-active"
    WRITER_COLLISION = "writer-collision"
    WRITER_IDENTITY_UNAPPROVED = "writer-identity-unapproved"
    WRITER_ROLE_UNAPPROVED = "writer-role-unapproved"
    CANONICAL_WRITER_INVALID = "canonical-writer-invalid"
    WRITER_LEASE_MISSING = "writer-lease-missing"
    WRITER_LEASE_NOT_CURRENT = "writer-lease-not-current"
    VALIDATED_ROWS_INCOMPLETE = "validated-rows-incomplete"
    OWNER_GATE_INVENTORY_INCOMPLETE = "owner-gate-inventory-incomplete"
    PROBE_MISSING = "probe-missing"
    PROBE_TIMEOUT = "probe-timeout"
    PROBE_FAILURE = "probe-failure"


MAX_PROBE_TIMEOUT_SECONDS = 30.0
_WORKER_POLL_SECONDS = 0.001
_WORKER_STARTUP_GRACE_SECONDS = 0.2
_WORKER_STOP_GRACE_SECONDS = 0.05
_IDENTIFIER = re.compile(r"[a-z][a-z0-9-]{0,63}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_RETAINED_ROOT_IDS = ("capture", "personal", "saved-content", "state", "work")
_SAFE_BIND_EXPOSURES = {
    BindExposure.DISABLED,
    BindExposure.LOOPBACK,
    BindExposure.UNIX_SOCKET,
}


@dataclass(frozen=True, slots=True)
class DependencyRequirement:
    dependency_id: str
    kind: DependencyKind

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        _identifier(self.dependency_id)
        if not isinstance(self.kind, DependencyKind):
            raise OperationsValidationError("invalid dependency requirement")


@dataclass(frozen=True, slots=True)
class SchemaRequirement:
    schema_id: str
    kind: SchemaKind
    current_version: int

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        _identifier(self.schema_id)
        if not isinstance(self.kind, SchemaKind):
            raise OperationsValidationError("invalid schema requirement")
        _positive_integer(self.current_version, "schema version")


@dataclass(frozen=True, slots=True)
class WriterRequirement:
    writer_id: str
    generation: WriterGeneration
    identity_id: str
    role: WriterRole
    active: bool
    canonical: bool

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        _identifier(self.writer_id)
        _identifier(self.identity_id)
        if (
            not isinstance(self.generation, WriterGeneration)
            or not isinstance(self.role, WriterRole)
            or not isinstance(self.active, bool)
            or not isinstance(self.canonical, bool)
        ):
            raise OperationsValidationError("invalid writer requirement")
        if self.canonical != (self.role is WriterRole.CANONICAL):
            raise OperationsValidationError("contradictory writer requirement")


@dataclass(frozen=True, slots=True, init=False)
class CutoverManifest:
    schema_version: int
    manifest_id: str
    required_secret_ids: tuple[str, ...]
    required_root_ids: tuple[str, ...]
    required_dependencies: tuple[DependencyRequirement, ...]
    current_schemas: tuple[SchemaRequirement, ...]
    required_bind_ids: tuple[str, ...]
    required_writers: tuple[WriterRequirement, ...]
    expected_owner_gate_ids: tuple[str, ...]
    max_recovery_age_seconds: int

    def __init__(self) -> None:
        raise TypeError("CutoverManifest is fixed by the public Phase 6 contract")

    @classmethod
    def _create(
        cls,
        *,
        schema_version: int,
        manifest_id: str,
        required_secret_ids: tuple[str, ...],
        required_root_ids: tuple[str, ...],
        required_dependencies: tuple[DependencyRequirement, ...],
        current_schemas: tuple[SchemaRequirement, ...],
        required_bind_ids: tuple[str, ...],
        required_writers: tuple[WriterRequirement, ...],
        expected_owner_gate_ids: tuple[str, ...],
        max_recovery_age_seconds: int,
    ) -> CutoverManifest:
        manifest = cls.__new__(cls)
        object.__setattr__(manifest, "schema_version", schema_version)
        object.__setattr__(manifest, "manifest_id", manifest_id)
        object.__setattr__(manifest, "required_secret_ids", required_secret_ids)
        object.__setattr__(manifest, "required_root_ids", required_root_ids)
        object.__setattr__(manifest, "required_dependencies", required_dependencies)
        object.__setattr__(manifest, "current_schemas", current_schemas)
        object.__setattr__(manifest, "required_bind_ids", required_bind_ids)
        object.__setattr__(manifest, "required_writers", required_writers)
        object.__setattr__(manifest, "expected_owner_gate_ids", expected_owner_gate_ids)
        object.__setattr__(
            manifest,
            "max_recovery_age_seconds",
            max_recovery_age_seconds,
        )
        _validate_manifest(manifest)
        return manifest

    @property
    def manifest_digest(self) -> str:
        payload = {
            "current_schemas": [
                {
                    "current_version": requirement.current_version,
                    "kind": requirement.kind.value,
                    "schema_id": requirement.schema_id,
                }
                for requirement in self.current_schemas
            ],
            "expected_owner_gate_ids": self.expected_owner_gate_ids,
            "manifest_id": self.manifest_id,
            "max_recovery_age_seconds": self.max_recovery_age_seconds,
            "required_bind_ids": self.required_bind_ids,
            "required_dependencies": [
                {
                    "dependency_id": requirement.dependency_id,
                    "kind": requirement.kind.value,
                }
                for requirement in self.required_dependencies
            ],
            "required_root_ids": self.required_root_ids,
            "required_secret_ids": self.required_secret_ids,
            "required_writers": [
                {
                    "active": requirement.active,
                    "canonical": requirement.canonical,
                    "generation": requirement.generation.value,
                    "identity_id": requirement.identity_id,
                    "role": requirement.role.value,
                    "writer_id": requirement.writer_id,
                }
                for requirement in self.required_writers
            ],
            "schema_version": self.schema_version,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class SecretEvidence:
    secret_id: str
    state: SecretState

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        _identifier(self.secret_id)
        if not isinstance(self.state, SecretState):
            raise OperationsValidationError("invalid secret evidence")


@dataclass(frozen=True, slots=True)
class ConfigEvidence:
    manifest_digest: str
    config_valid: bool
    secrets: tuple[SecretEvidence, ...]

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        _digest(self.manifest_digest, "manifest digest")
        if not isinstance(self.config_valid, bool):
            raise OperationsValidationError("invalid config evidence")
        _typed_rows(self.secrets, SecretEvidence, "secret evidence")


@dataclass(frozen=True, slots=True)
class RootReading:
    root_id: str
    exists: bool
    permissions_safe: bool

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        _identifier(self.root_id)
        if not isinstance(self.exists, bool) or not isinstance(self.permissions_safe, bool):
            raise OperationsValidationError("invalid root evidence")
        if not self.exists and self.permissions_safe:
            raise OperationsValidationError("contradictory root evidence")


@dataclass(frozen=True, slots=True)
class RootEvidence:
    manifest_digest: str
    roots: tuple[RootReading, ...]
    prohibited_remote_count: int

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        _digest(self.manifest_digest, "manifest digest")
        _typed_rows(self.roots, RootReading, "root evidence")
        _nonnegative_integer(self.prohibited_remote_count, "prohibited remote count")


@dataclass(frozen=True, slots=True)
class DependencyReading:
    dependency_id: str
    kind: DependencyKind
    reachable: bool

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        _identifier(self.dependency_id)
        if not isinstance(self.kind, DependencyKind) or not isinstance(self.reachable, bool):
            raise OperationsValidationError("invalid dependency evidence")


@dataclass(frozen=True, slots=True)
class DependencyEvidence:
    manifest_digest: str
    dependencies: tuple[DependencyReading, ...]

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        _digest(self.manifest_digest, "manifest digest")
        _typed_rows(self.dependencies, DependencyReading, "dependency evidence")


@dataclass(frozen=True, slots=True)
class SchemaReading:
    schema_id: str
    kind: SchemaKind
    observed_version: int

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        _identifier(self.schema_id)
        if not isinstance(self.kind, SchemaKind):
            raise OperationsValidationError("invalid schema evidence")
        _positive_integer(self.observed_version, "schema version")


@dataclass(frozen=True, slots=True)
class SchemaEvidence:
    manifest_digest: str
    schemas: tuple[SchemaReading, ...]

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        _digest(self.manifest_digest, "manifest digest")
        _typed_rows(self.schemas, SchemaReading, "schema evidence")


@dataclass(frozen=True, slots=True)
class RecoveryReceiptEvidence:
    receipt_id: str
    operation: RecoveryOperation
    migration_digest: str
    manifest_digest: str
    pair_digest: str
    artifact_digest: str
    source_receipt_id: str | None
    completed_at: datetime
    succeeded: bool
    item_count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "completed_at", _utc(self.completed_at, "recovery timestamp"))
        self.validate()

    def validate(self) -> None:
        _identifier(self.receipt_id)
        if not isinstance(self.operation, RecoveryOperation):
            raise OperationsValidationError("invalid recovery operation")
        _digest(self.migration_digest, "migration digest")
        _digest(self.manifest_digest, "manifest digest")
        _digest(self.pair_digest, "recovery pair digest")
        _digest(self.artifact_digest, "recovery artifact digest")
        if self.operation is RecoveryOperation.BACKUP:
            if self.source_receipt_id is not None:
                raise OperationsValidationError("backup receipt cannot have a source receipt")
        elif self.source_receipt_id is None:
            raise OperationsValidationError("restore receipt requires a source receipt")
        else:
            _identifier(self.source_receipt_id)
        _optional_utc(self.completed_at, "recovery timestamp")
        if not isinstance(self.succeeded, bool):
            raise OperationsValidationError("invalid recovery success evidence")
        _positive_integer(self.item_count, "recovery item count")


@dataclass(frozen=True, slots=True)
class RecoveryEvidence:
    manifest_digest: str
    backup_destination_available: bool
    backup: RecoveryReceiptEvidence | None
    restore: RecoveryReceiptEvidence | None

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        _digest(self.manifest_digest, "manifest digest")
        if not isinstance(self.backup_destination_available, bool):
            raise OperationsValidationError("invalid recovery evidence")
        if self.backup is not None and type(self.backup) is not RecoveryReceiptEvidence:
            raise OperationsValidationError("invalid backup evidence")
        if self.restore is not None and type(self.restore) is not RecoveryReceiptEvidence:
            raise OperationsValidationError("invalid restore evidence")
        if self.backup is not None:
            self.backup.validate()
        if self.restore is not None:
            self.restore.validate()


@dataclass(frozen=True, slots=True)
class BindReading:
    bind_id: str
    exposure: BindExposure

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        _identifier(self.bind_id)
        if not isinstance(self.exposure, BindExposure):
            raise OperationsValidationError("invalid bind evidence")


@dataclass(frozen=True, slots=True)
class BindEvidence:
    manifest_digest: str
    binds: tuple[BindReading, ...]

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        _digest(self.manifest_digest, "manifest digest")
        _typed_rows(self.binds, BindReading, "bind evidence")


@dataclass(frozen=True, slots=True)
class WriterLeaseEvidence:
    lease_id: str
    owner_identity_id: str
    manifest_digest: str
    generation: int
    acquired_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "acquired_at", _utc(self.acquired_at, "lease acquisition"))
        object.__setattr__(self, "expires_at", _utc(self.expires_at, "lease expiry"))
        self.validate()

    def validate(self) -> None:
        _identifier(self.lease_id)
        _identifier(self.owner_identity_id)
        _digest(self.manifest_digest, "manifest digest")
        _positive_integer(self.generation, "lease generation")
        _optional_utc(self.acquired_at, "lease acquisition")
        _optional_utc(self.expires_at, "lease expiry")
        if self.acquired_at >= self.expires_at:
            raise OperationsValidationError("invalid writer lease interval")


@dataclass(frozen=True, slots=True)
class WriterReading:
    writer_id: str
    generation: WriterGeneration
    identity_id: str
    role: WriterRole
    active: bool
    canonical: bool
    lease: WriterLeaseEvidence | None

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        _identifier(self.writer_id)
        _identifier(self.identity_id)
        if (
            not isinstance(self.generation, WriterGeneration)
            or not isinstance(self.role, WriterRole)
            or not isinstance(self.active, bool)
            or not isinstance(self.canonical, bool)
        ):
            raise OperationsValidationError("invalid writer evidence")
        if self.lease is not None and type(self.lease) is not WriterLeaseEvidence:
            raise OperationsValidationError("invalid writer lease evidence")
        if self.lease is not None:
            self.lease.validate()


@dataclass(frozen=True, slots=True)
class WriterEvidence:
    manifest_digest: str
    writers: tuple[WriterReading, ...]

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        _digest(self.manifest_digest, "manifest digest")
        _typed_rows(self.writers, WriterReading, "writer evidence")


@dataclass(frozen=True, slots=True)
class GateEvidence:
    manifest_digest: str
    validated_rows: tuple[AcceptanceRow, ...]
    unresolved_owner_gate_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        _digest(self.manifest_digest, "manifest digest")
        if (
            not isinstance(self.validated_rows, tuple)
            or any(not isinstance(row, AcceptanceRow) for row in self.validated_rows)
            or len(set(self.validated_rows)) != len(self.validated_rows)
            or self.validated_rows != tuple(sorted(self.validated_rows, key=lambda row: row.value))
        ):
            raise OperationsValidationError("invalid validated rows")
        _identifiers(
            self.unresolved_owner_gate_ids,
            "owner gate ids",
            empty=False,
        )


CutoverEvidence = (
    ConfigEvidence
    | RootEvidence
    | DependencyEvidence
    | SchemaEvidence
    | RecoveryEvidence
    | BindEvidence
    | WriterEvidence
    | GateEvidence
)
CutoverProbe = Callable[[], Awaitable[CutoverEvidence]]


@dataclass(slots=True)
class _ProbeWorker:
    receiver: Connection
    process: BaseProcess
    hard_deadline: float


@dataclass(frozen=True, slots=True)
class CutoverCheck:
    probe: CutoverProbeName
    state: CutoverCheckState
    findings: tuple[CutoverFindingClass, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "probe": self.probe.value,
            "state": self.state.value,
            "findings": [finding.value for finding in self.findings],
        }


@dataclass(frozen=True, slots=True, init=False)
class CutoverDoctorResult:
    schema_version: int
    manifest_version: int
    manifest_digest: str
    strict: bool
    outcome: CutoverDoctorOutcome
    exit_code: int
    synthetic_ready: bool
    cutover_ready: bool
    checks: tuple[CutoverCheck, ...]

    def __init__(self) -> None:
        raise TypeError("CutoverDoctorResult must be created by run_cutover_doctor")

    @classmethod
    def _create(
        cls,
        *,
        manifest: CutoverManifest,
        strict: bool,
        outcome: CutoverDoctorOutcome,
        exit_code: int,
        checks: tuple[CutoverCheck, ...],
    ) -> CutoverDoctorResult:
        result = cls.__new__(cls)
        object.__setattr__(result, "schema_version", 2)
        object.__setattr__(result, "manifest_version", manifest.schema_version)
        object.__setattr__(result, "manifest_digest", manifest.manifest_digest)
        object.__setattr__(result, "strict", strict)
        object.__setattr__(result, "outcome", outcome)
        object.__setattr__(result, "exit_code", exit_code)
        object.__setattr__(
            result,
            "synthetic_ready",
            outcome is CutoverDoctorOutcome.SYNTHETIC_READY,
        )
        object.__setattr__(result, "cutover_ready", False)
        object.__setattr__(result, "checks", checks)
        return result

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "manifest_version": self.manifest_version,
            "manifest_digest": self.manifest_digest,
            "strict": self.strict,
            "outcome": self.outcome.value,
            "exit_code": self.exit_code,
            "synthetic_ready": self.synthetic_ready,
            "cutover_ready": self.cutover_ready,
            "checks": [check.to_dict() for check in self.checks],
            "findings": [
                {"probe": check.probe.value, "finding_class": finding.value}
                for check in self.checks
                for finding in check.findings
            ],
        }


def phase6_cutover_manifest() -> CutoverManifest:
    return _new_phase6_cutover_manifest()


async def run_cutover_doctor(
    *,
    probes: Mapping[CutoverProbeName, CutoverProbe],
    evaluated_at: datetime,
    timeout_seconds: float,
    strict: bool,
) -> CutoverDoctorResult:
    if not isinstance(strict, bool):
        raise OperationsValidationError("invalid strict flag")
    observed_at = _utc(evaluated_at, "cutover evaluation timestamp")
    timeout = _probe_timeout(timeout_seconds)
    normalized_probes = _probes(probes)
    manifest = _new_phase6_cutover_manifest()
    _validate_manifest(manifest)
    workers, initial_checks = _start_probe_workers(normalized_probes, timeout)
    worker_results = await _supervise_probe_workers(workers)
    checks = tuple(
        initial_checks.get(probe)
        or _worker_check(
            probe,
            worker_results.get(probe),
            manifest,
            observed_at,
        )
        for probe in CutoverProbeName
    )
    outcome = _outcome(checks)
    return CutoverDoctorResult._create(
        manifest=manifest,
        strict=strict,
        outcome=outcome,
        exit_code=_exit_code(outcome, strict),
        checks=checks,
    )


def _start_probe_workers(
    probes: Mapping[CutoverProbeName, CutoverProbe],
    timeout_seconds: float,
) -> tuple[
    dict[CutoverProbeName, _ProbeWorker],
    dict[CutoverProbeName, CutoverCheck],
]:
    workers: dict[CutoverProbeName, _ProbeWorker] = {}
    initial_checks: dict[CutoverProbeName, CutoverCheck] = {}
    try:
        context = mp.get_context("fork")
    except ValueError:
        context = None
    for probe in CutoverProbeName:
        collector = probes.get(probe)
        if collector is None:
            initial_checks[probe] = _unavailable_check(
                probe,
                CutoverFindingClass.PROBE_MISSING,
            )
            continue
        if context is None:
            initial_checks[probe] = _unavailable_check(
                probe,
                CutoverFindingClass.PROBE_FAILURE,
            )
            continue
        try:
            workers[probe] = _start_probe_worker(
                context,
                collector,
                timeout_seconds,
            )
        except Exception:
            initial_checks[probe] = _unavailable_check(
                probe,
                CutoverFindingClass.PROBE_FAILURE,
            )
    return workers, initial_checks


def _start_probe_worker(
    context: mp.context.BaseContext,
    collector: CutoverProbe,
    timeout_seconds: float,
) -> _ProbeWorker:
    receiver, sender = context.Pipe(duplex=False)
    process_factory = context.Process  # type: ignore[attr-defined]
    process: BaseProcess = process_factory(
        target=_probe_worker_entrypoint,
        args=(receiver, sender, collector, timeout_seconds),
        daemon=True,
    )
    try:
        process.start()
    except BaseException:
        receiver.close()
        sender.close()
        raise
    sender.close()
    return _ProbeWorker(
        receiver=receiver,
        process=process,
        hard_deadline=(time.monotonic() + timeout_seconds + _WORKER_STARTUP_GRACE_SECONDS),
    )


def _probe_worker_entrypoint(
    receiver: Connection,
    sender: Connection,
    collector: CutoverProbe,
    timeout_seconds: float,
) -> None:
    receiver.close()
    try:
        result = asyncio.run(_invoke_probe(collector, timeout_seconds))
        sender.send(result)
    except BaseException:
        with suppress(BaseException):
            sender.send(("failure", None))
    finally:
        sender.close()


async def _invoke_probe(
    collector: CutoverProbe,
    timeout_seconds: float,
) -> tuple[str, CutoverEvidence | None]:
    started_at = asyncio.get_running_loop().time()
    try:
        evidence = await asyncio.wait_for(collector(), timeout=timeout_seconds)
    except TimeoutError:
        return "timeout", None
    except BaseException:
        return "failure", None
    if asyncio.get_running_loop().time() - started_at >= timeout_seconds:
        return "timeout", None
    return "healthy", evidence


async def _supervise_probe_workers(
    workers: Mapping[CutoverProbeName, _ProbeWorker],
) -> dict[CutoverProbeName, tuple[str, CutoverEvidence | None]]:
    pending = dict(workers)
    results: dict[CutoverProbeName, tuple[str, CutoverEvidence | None]] = {}
    try:
        while pending:
            now = time.monotonic()
            for probe, worker in tuple(pending.items()):
                received = _receive_worker_result(worker)
                if received is not None:
                    results[probe] = received
                    _stop_probe_worker(worker, terminate=False)
                    del pending[probe]
                elif now >= worker.hard_deadline:
                    results[probe] = ("timeout", None)
                    _stop_probe_worker(worker, terminate=True)
                    del pending[probe]
                elif not worker.process.is_alive():
                    worker.process.join(_WORKER_STOP_GRACE_SECONDS)
                    results[probe] = _receive_worker_result(worker) or ("failure", None)
                    _stop_probe_worker(worker, terminate=False)
                    del pending[probe]
            if pending:
                await asyncio.sleep(_WORKER_POLL_SECONDS)
    finally:
        for worker in pending.values():
            _stop_probe_worker(worker, terminate=True)
    return results


def _receive_worker_result(
    worker: _ProbeWorker,
) -> tuple[str, CutoverEvidence | None] | None:
    try:
        if not worker.receiver.poll():
            return None
        message = worker.receiver.recv()
    except (EOFError, OSError):
        return "failure", None
    if (
        not isinstance(message, tuple)
        or len(message) != 2
        or message[0] not in {"healthy", "timeout", "failure"}
    ):
        return "failure", None
    return message


def _stop_probe_worker(worker: _ProbeWorker, *, terminate: bool) -> None:
    try:
        if terminate and worker.process.is_alive():
            worker.process.terminate()
        worker.process.join(_WORKER_STOP_GRACE_SECONDS)
        if worker.process.is_alive():
            worker.process.kill()
            worker.process.join(_WORKER_STOP_GRACE_SECONDS)
    finally:
        worker.receiver.close()
        with suppress(ValueError):
            worker.process.close()


def _worker_check(
    probe: CutoverProbeName,
    result: tuple[str, CutoverEvidence | None] | None,
    manifest: CutoverManifest,
    evaluated_at: datetime,
) -> CutoverCheck:
    if result is None or result[0] == "failure":
        return _unavailable_check(probe, CutoverFindingClass.PROBE_FAILURE)
    if result[0] == "timeout":
        return _unavailable_check(probe, CutoverFindingClass.PROBE_TIMEOUT)
    evidence = result[1]
    if evidence is None:
        return _unavailable_check(probe, CutoverFindingClass.PROBE_FAILURE)
    try:
        findings = _evaluate(probe, evidence, manifest, evaluated_at)
    except Exception:
        return _unavailable_check(probe, CutoverFindingClass.PROBE_FAILURE)
    return CutoverCheck(
        probe=probe,
        state=CutoverCheckState.UNHEALTHY if findings else CutoverCheckState.HEALTHY,
        findings=findings,
    )


def _evaluate(
    probe: CutoverProbeName,
    evidence: CutoverEvidence,
    manifest: CutoverManifest,
    evaluated_at: datetime,
) -> tuple[CutoverFindingClass, ...]:
    expected_type = {
        CutoverProbeName.CONFIG_SECRETS: ConfigEvidence,
        CutoverProbeName.ROOTS_REMOTES: RootEvidence,
        CutoverProbeName.DEPENDENCIES: DependencyEvidence,
        CutoverProbeName.SCHEMAS: SchemaEvidence,
        CutoverProbeName.RECOVERY: RecoveryEvidence,
        CutoverProbeName.NETWORK_BINDS: BindEvidence,
        CutoverProbeName.WRITERS: WriterEvidence,
        CutoverProbeName.GATES_SCOPE: GateEvidence,
    }[probe]
    if type(evidence) is not expected_type:
        raise OperationsValidationError("probe returned invalid evidence")
    evidence.validate()
    if evidence.manifest_digest != manifest.manifest_digest:
        return (CutoverFindingClass.MANIFEST_DIGEST_MISMATCH,)
    if type(evidence) is ConfigEvidence:
        return _config_findings(manifest, evidence)
    if type(evidence) is RootEvidence:
        return _root_findings(manifest, evidence)
    if type(evidence) is DependencyEvidence:
        return _dependency_findings(manifest, evidence)
    if type(evidence) is SchemaEvidence:
        return _schema_findings(manifest, evidence)
    if type(evidence) is RecoveryEvidence:
        return _recovery_findings(manifest, evidence, evaluated_at)
    if type(evidence) is BindEvidence:
        return _bind_findings(manifest, evidence)
    if type(evidence) is WriterEvidence:
        return _writer_findings(manifest, evidence, evaluated_at)
    if type(evidence) is GateEvidence:
        return _gate_findings(manifest, evidence)
    raise OperationsValidationError("probe returned invalid evidence")


def _config_findings(
    manifest: CutoverManifest, evidence: ConfigEvidence
) -> tuple[CutoverFindingClass, ...]:
    findings: list[CutoverFindingClass] = []
    if tuple(secret.secret_id for secret in evidence.secrets) != manifest.required_secret_ids:
        findings.append(CutoverFindingClass.SECRET_EVIDENCE_INCOMPLETE)
    if not evidence.config_valid:
        findings.append(CutoverFindingClass.CONFIG_INVALID)
    states = {secret.state for secret in evidence.secrets}
    if SecretState.MISSING in states:
        findings.append(CutoverFindingClass.REQUIRED_SECRET_MISSING)
    if SecretState.EMPTY in states:
        findings.append(CutoverFindingClass.REQUIRED_SECRET_EMPTY)
    return tuple(findings)


def _root_findings(
    manifest: CutoverManifest, evidence: RootEvidence
) -> tuple[CutoverFindingClass, ...]:
    findings: list[CutoverFindingClass] = []
    if tuple(root.root_id for root in evidence.roots) != manifest.required_root_ids:
        findings.append(CutoverFindingClass.ROOT_EVIDENCE_INCOMPLETE)
    if any(not root.exists for root in evidence.roots):
        findings.append(CutoverFindingClass.ROOT_MISSING)
    if any(root.exists and not root.permissions_safe for root in evidence.roots):
        findings.append(CutoverFindingClass.ROOT_PERMISSIONS_UNSAFE)
    if evidence.prohibited_remote_count:
        findings.append(CutoverFindingClass.PROHIBITED_REMOTE)
    return tuple(findings)


def _dependency_findings(
    manifest: CutoverManifest, evidence: DependencyEvidence
) -> tuple[CutoverFindingClass, ...]:
    findings: list[CutoverFindingClass] = []
    actual = tuple((reading.dependency_id, reading.kind) for reading in evidence.dependencies)
    expected = tuple(
        (requirement.dependency_id, requirement.kind)
        for requirement in manifest.required_dependencies
    )
    if actual != expected:
        findings.append(CutoverFindingClass.DEPENDENCY_EVIDENCE_INCOMPLETE)
    if any(not dependency.reachable for dependency in evidence.dependencies):
        findings.append(CutoverFindingClass.DEPENDENCY_UNREACHABLE)
    return tuple(findings)


def _schema_findings(
    manifest: CutoverManifest, evidence: SchemaEvidence
) -> tuple[CutoverFindingClass, ...]:
    findings: list[CutoverFindingClass] = []
    actual = tuple((reading.schema_id, reading.kind) for reading in evidence.schemas)
    expected = tuple(
        (requirement.schema_id, requirement.kind) for requirement in manifest.current_schemas
    )
    if actual != expected:
        findings.append(CutoverFindingClass.SCHEMA_EVIDENCE_INCOMPLETE)
    current_versions = {
        (requirement.schema_id, requirement.kind): requirement.current_version
        for requirement in manifest.current_schemas
    }
    if any(
        current_versions.get((reading.schema_id, reading.kind)) != reading.observed_version
        for reading in evidence.schemas
    ):
        findings.append(CutoverFindingClass.SCHEMA_NOT_CURRENT)
    return tuple(findings)


def _recovery_findings(
    manifest: CutoverManifest,
    evidence: RecoveryEvidence,
    evaluated_at: datetime,
) -> tuple[CutoverFindingClass, ...]:
    findings: list[CutoverFindingClass] = []
    if not evidence.backup_destination_available:
        findings.append(CutoverFindingClass.BACKUP_DESTINATION_UNAVAILABLE)
    if evidence.backup is None:
        findings.append(CutoverFindingClass.BACKUP_EVIDENCE_MISSING)
    if evidence.restore is None:
        findings.append(CutoverFindingClass.RESTORE_EVIDENCE_MISSING)
    if evidence.backup is None or evidence.restore is None:
        return tuple(findings)

    backup = evidence.backup
    restore = evidence.restore
    findings.extend(
        _recovery_age_findings(
            backup.completed_at,
            evaluated_at,
            manifest.max_recovery_age_seconds,
            CutoverFindingClass.BACKUP_EVIDENCE_STALE,
        )
    )
    findings.extend(
        _recovery_age_findings(
            restore.completed_at,
            evaluated_at,
            manifest.max_recovery_age_seconds,
            CutoverFindingClass.RESTORE_EVIDENCE_STALE,
        )
    )
    if not backup.succeeded or not restore.succeeded:
        findings.append(CutoverFindingClass.RECOVERY_NOT_SUCCESSFUL)
    if backup.manifest_digest != manifest.manifest_digest or (
        restore.manifest_digest != manifest.manifest_digest
    ):
        findings.append(CutoverFindingClass.MANIFEST_DIGEST_MISMATCH)
    if (
        backup.operation is not RecoveryOperation.BACKUP
        or restore.operation is not RecoveryOperation.RESTORE
        or backup.receipt_id == restore.receipt_id
        or restore.source_receipt_id != backup.receipt_id
        or backup.migration_digest != restore.migration_digest
        or backup.pair_digest != restore.pair_digest
        or backup.artifact_digest != restore.artifact_digest
        or backup.item_count != restore.item_count
        or restore.completed_at < backup.completed_at
    ):
        findings.append(CutoverFindingClass.RECOVERY_RECEIPT_MISMATCH)
    return _unique_findings(findings)


def _recovery_age_findings(
    completed_at: datetime,
    evaluated_at: datetime,
    max_age_seconds: int,
    stale: CutoverFindingClass,
) -> tuple[CutoverFindingClass, ...]:
    age_seconds = (evaluated_at - completed_at).total_seconds()
    if age_seconds < 0:
        return (CutoverFindingClass.RECOVERY_EVIDENCE_CONTRADICTORY,)
    if age_seconds > max_age_seconds:
        return (stale,)
    return ()


def _bind_findings(
    manifest: CutoverManifest, evidence: BindEvidence
) -> tuple[CutoverFindingClass, ...]:
    findings: list[CutoverFindingClass] = []
    if tuple(bind.bind_id for bind in evidence.binds) != manifest.required_bind_ids:
        findings.append(CutoverFindingClass.BIND_EVIDENCE_INCOMPLETE)
    if any(bind.exposure not in _SAFE_BIND_EXPOSURES for bind in evidence.binds):
        findings.append(CutoverFindingClass.UNSAFE_NETWORK_BIND)
    return tuple(findings)


def _writer_findings(
    manifest: CutoverManifest,
    evidence: WriterEvidence,
    evaluated_at: datetime,
) -> tuple[CutoverFindingClass, ...]:
    findings: list[CutoverFindingClass] = []
    expected_by_id = {
        requirement.writer_id: requirement for requirement in manifest.required_writers
    }
    actual_ids = tuple(writer.writer_id for writer in evidence.writers)
    expected_ids = tuple(requirement.writer_id for requirement in manifest.required_writers)
    if actual_ids != expected_ids:
        findings.append(CutoverFindingClass.WRITER_EVIDENCE_INCOMPLETE)

    for writer in evidence.writers:
        requirement = expected_by_id.get(writer.writer_id)
        if requirement is None:
            continue
        if writer.identity_id != requirement.identity_id:
            findings.append(CutoverFindingClass.WRITER_IDENTITY_UNAPPROVED)
        if writer.generation is not requirement.generation or writer.role is not requirement.role:
            findings.append(CutoverFindingClass.WRITER_ROLE_UNAPPROVED)
        if writer.generation is WriterGeneration.LEGACY and writer.active:
            findings.append(CutoverFindingClass.LEGACY_WRITER_ACTIVE)
        if writer.active != requirement.active or writer.canonical != requirement.canonical:
            findings.append(CutoverFindingClass.CANONICAL_WRITER_INVALID)
        if requirement.active:
            if writer.lease is None:
                findings.append(CutoverFindingClass.WRITER_LEASE_MISSING)
            elif not _lease_is_current(
                writer.lease,
                requirement.identity_id,
                manifest.manifest_digest,
                evaluated_at,
            ):
                findings.append(CutoverFindingClass.WRITER_LEASE_NOT_CURRENT)
        elif writer.lease is not None:
            findings.append(CutoverFindingClass.WRITER_LEASE_NOT_CURRENT)

    if any(
        writer.generation is WriterGeneration.LEGACY and writer.active
        for writer in evidence.writers
    ) and any(
        writer.generation is WriterGeneration.NEW and writer.active for writer in evidence.writers
    ):
        findings.append(CutoverFindingClass.WRITER_COLLISION)
    return _unique_findings(findings)


def _lease_is_current(
    lease: WriterLeaseEvidence,
    expected_identity_id: str,
    manifest_digest: str,
    evaluated_at: datetime,
) -> bool:
    return (
        lease.owner_identity_id == expected_identity_id
        and lease.manifest_digest == manifest_digest
        and lease.acquired_at <= evaluated_at < lease.expires_at
    )


def _gate_findings(
    manifest: CutoverManifest, evidence: GateEvidence
) -> tuple[CutoverFindingClass, ...]:
    findings: list[CutoverFindingClass] = []
    if evidence.validated_rows != tuple(AcceptanceRow):
        findings.append(CutoverFindingClass.VALIDATED_ROWS_INCOMPLETE)
    if evidence.unresolved_owner_gate_ids != manifest.expected_owner_gate_ids:
        findings.append(CutoverFindingClass.OWNER_GATE_INVENTORY_INCOMPLETE)
    return tuple(findings)


def _unavailable_check(probe: CutoverProbeName, finding: CutoverFindingClass) -> CutoverCheck:
    return CutoverCheck(
        probe=probe,
        state=CutoverCheckState.UNAVAILABLE,
        findings=(finding,),
    )


def _outcome(checks: tuple[CutoverCheck, ...]) -> CutoverDoctorOutcome:
    if any(check.state is CutoverCheckState.UNHEALTHY for check in checks):
        return CutoverDoctorOutcome.NOT_READY
    if any(check.state is CutoverCheckState.UNAVAILABLE for check in checks):
        return CutoverDoctorOutcome.UNAVAILABLE
    return CutoverDoctorOutcome.SYNTHETIC_READY


def _exit_code(outcome: CutoverDoctorOutcome, strict: bool) -> int:
    if outcome is CutoverDoctorOutcome.NOT_READY:
        return 1
    if strict and outcome is CutoverDoctorOutcome.UNAVAILABLE:
        return int(ExitClass.CONFIGURATION)
    return int(ExitClass.SUCCESS)


def _probes(
    probes: Mapping[CutoverProbeName, CutoverProbe],
) -> dict[CutoverProbeName, CutoverProbe]:
    if not isinstance(probes, Mapping):
        raise OperationsValidationError("invalid cutover probes")
    normalized: dict[CutoverProbeName, CutoverProbe] = {}
    for name, collector in probes.items():
        if (
            not isinstance(name, CutoverProbeName)
            or not callable(collector)
            or not inspect.iscoroutinefunction(collector)
        ):
            raise OperationsValidationError("cutover probes must be async")
        normalized[name] = collector
    return normalized


def _probe_timeout(value: float) -> float:
    if (
        not isinstance(value, int | float)
        or isinstance(value, bool)
        or not math.isfinite(value)
        or not 0 < value <= MAX_PROBE_TIMEOUT_SECONDS
    ):
        raise OperationsValidationError("invalid probe timeout")
    return float(value)


def _validate_manifest(manifest: CutoverManifest) -> None:
    _positive_integer(manifest.schema_version, "manifest version")
    _identifier(manifest.manifest_id)
    _identifiers(manifest.required_secret_ids, "required secret ids", empty=False)
    if manifest.required_root_ids != _RETAINED_ROOT_IDS:
        raise OperationsValidationError("invalid retained-root manifest")
    _typed_rows(
        manifest.required_dependencies,
        DependencyRequirement,
        "dependency requirements",
        empty=False,
    )
    _typed_rows(
        manifest.current_schemas,
        SchemaRequirement,
        "schema requirements",
        empty=False,
    )
    _identifiers(manifest.required_bind_ids, "required bind ids", empty=False)
    _typed_rows(
        manifest.required_writers,
        WriterRequirement,
        "writer requirements",
        empty=False,
    )
    _identifiers(
        manifest.expected_owner_gate_ids,
        "owner gate ids",
        empty=False,
    )
    if {requirement.kind for requirement in manifest.required_dependencies} != set(DependencyKind):
        raise OperationsValidationError("dependencies must cover provider and service")
    if {requirement.kind for requirement in manifest.current_schemas} != set(SchemaKind):
        raise OperationsValidationError("schemas must cover queue and database")
    if {requirement.generation for requirement in manifest.required_writers} != set(
        WriterGeneration
    ):
        raise OperationsValidationError("writers must cover legacy and new generations")
    canonical = [requirement for requirement in manifest.required_writers if requirement.canonical]
    if len(canonical) != 1 or not canonical[0].active:
        raise OperationsValidationError("manifest requires one active canonical writer")
    _positive_integer(manifest.max_recovery_age_seconds, "recovery age")


def _typed_rows(
    rows: tuple[object, ...],
    row_type: type[object],
    label: str,
    *,
    empty: bool = True,
) -> None:
    if (
        not isinstance(rows, tuple)
        or (not empty and not rows)
        or any(type(row) is not row_type for row in rows)
    ):
        raise OperationsValidationError(f"invalid {label}")
    for row in rows:
        validator = getattr(row, "validate", None)
        if not callable(validator):
            raise OperationsValidationError(f"invalid {label}")
        validator()
    ids = tuple(_row_id(row) for row in rows)
    if len(set(ids)) != len(ids) or ids != tuple(sorted(ids)):
        raise OperationsValidationError(f"invalid {label}")


def _row_id(row: object) -> str:
    for attribute in (
        "secret_id",
        "root_id",
        "dependency_id",
        "schema_id",
        "bind_id",
        "writer_id",
    ):
        value = getattr(row, attribute, None)
        if isinstance(value, str):
            return value
    raise OperationsValidationError("invalid evidence row")


def _identifiers(values: tuple[str, ...], label: str, *, empty: bool = True) -> None:
    if (
        not isinstance(values, tuple)
        or (not empty and not values)
        or any(
            not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None for value in values
        )
        or len(set(values)) != len(values)
        or values != tuple(sorted(values))
    ):
        raise OperationsValidationError(f"invalid {label}")


def _identifier(value: str) -> None:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise OperationsValidationError("invalid metadata identifier")


def _digest(value: str, label: str) -> None:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise OperationsValidationError(f"invalid {label}")


def _positive_integer(value: int, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise OperationsValidationError(f"invalid {label}")


def _nonnegative_integer(value: int, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise OperationsValidationError(f"invalid {label}")


def _utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise OperationsValidationError(f"invalid {label}")
    return value.astimezone(UTC)


def _optional_utc(value: datetime | None, label: str) -> None:
    if value is not None and (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != UTC.utcoffset(None)
    ):
        raise OperationsValidationError(f"invalid {label}")


def _unique_findings(
    findings: list[CutoverFindingClass],
) -> tuple[CutoverFindingClass, ...]:
    return tuple(dict.fromkeys(findings))


def _new_phase6_cutover_manifest() -> CutoverManifest:
    return CutoverManifest._create(
        schema_version=1,
        manifest_id="phase6-public-cutover-preflight-v1",
        required_secret_ids=("provider-key",),
        required_root_ids=_RETAINED_ROOT_IDS,
        required_dependencies=(
            DependencyRequirement("capture-service", DependencyKind.SERVICE),
            DependencyRequirement("local-provider", DependencyKind.PROVIDER),
        ),
        current_schemas=(
            SchemaRequirement("capture-queue", SchemaKind.QUEUE, 2),
            SchemaRequirement("events", SchemaKind.DATABASE, 3),
        ),
        required_bind_ids=("capture-service",),
        required_writers=(
            WriterRequirement(
                writer_id="legacy-writer",
                generation=WriterGeneration.LEGACY,
                identity_id="legacy-writer",
                role=WriterRole.LEGACY,
                active=False,
                canonical=False,
            ),
            WriterRequirement(
                writer_id="primary-writer",
                generation=WriterGeneration.NEW,
                identity_id="canonical-writer",
                role=WriterRole.CANONICAL,
                active=True,
                canonical=True,
            ),
        ),
        expected_owner_gate_ids=("cutover-approval", "host-readiness-adapter"),
        max_recovery_age_seconds=3_600,
    )

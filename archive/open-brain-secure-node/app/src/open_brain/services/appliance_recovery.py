"""App-owned recovery orchestration over public engine backup and Portable tasks."""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, cast

from open_brain_engine.engine import (
    BackupReceipt,
    EngineTaskSet,
    PortabilityReceipt,
    acquire_daemon_authority,
    canonical_json_bytes,
    open_authoritative_local_engine,
)
from open_brain_engine.storage.operational import (
    RootIdentity,
    StorageError,
    atomic_replace,
    atomic_write_new,
    capture_root_identity,
    read_confined,
    read_confined_tree,
)

from open_brain.profile import open_existing_single_user_local

from .appliance_init import APPLIANCE_OWNER_CREDENTIAL, _ensure_owner_file
from .appliance_scheduler import ApplianceJobResult, ApplianceRunContext, ApplianceScheduler
from .appliance_status import read_appliance_status

if TYPE_CHECKING:
    from .appliance_application import ApplianceApplication

APPLIANCE_RECOVERY_DIRECTORY = Path(".open-brain/state/appliance-recovery")
_MAXIMUM_REQUEST_BYTES = 8 * 1024
_MAXIMUM_REQUEST_ENTRIES = 64
_RECOVERY_JOBS = frozenset({"backup-create", "portable-export", "portable-import"})
_REQUEST_ID = re.compile(
    r"^(?P<prefix>backup|export|import)_"
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_REQUEST_PREFIX = {
    "backup-create": "backup",
    "portable-export": "export",
    "portable-import": "import",
}


@dataclass(frozen=True, slots=True)
class ApplianceBackupResult:
    created: BackupReceipt
    verified: BackupReceipt


@dataclass(frozen=True, slots=True)
class ApplianceRestoreResult:
    status: str
    backup_id: str
    credential_state: str
    doctor_state: str
    index_generation: int
    manifest_digest_sha256: str


@dataclass(frozen=True, slots=True)
class ApplianceReplacementPreflight:
    status: str
    backup_id: str
    manifest_digest_sha256: str
    credential_state: str
    doctor_state: str
    index_generation: int


@dataclass(frozen=True, slots=True)
class ApplianceJobSubmission:
    job_name: str
    request_id: str
    status: str


class ApplianceRecoveryService:
    """Compose engine recovery tasks through the appliance-owned authority boundary."""

    def __init__(
        self,
        root: Path,
        application: ApplianceApplication,
        *,
        scheduler: ApplianceScheduler | None = None,
        now: datetime | None = None,
    ) -> None:
        if not isinstance(root, Path) or not root.is_absolute() or application.root != root:
            raise ValueError("invalid appliance root")
        profile = open_existing_single_user_local(root)
        self._root = profile.root
        self._root_identity = profile.root_identity
        self._application = application
        self._scheduler = scheduler
        self._fixed_now = now
        _timestamp(self._now())

    def _now(self) -> datetime:
        return datetime.now(UTC) if self._fixed_now is None else self._fixed_now

    def create_backup(self, destination: Path, *, backup_id: str) -> ApplianceBackupResult:
        tasks = _mutations(self._application)
        created = tasks.backup.create(destination, backup_id=backup_id)
        verified = tasks.backup.verify(destination)
        _write_backup_evidence(self._root, self._root_identity, verified)
        return ApplianceBackupResult(created=created, verified=verified)

    def restore_backup(self, source: Path, destination: Path) -> ApplianceRestoreResult:
        tasks = _mutations(self._application)
        verified = tasks.backup.verify(source)
        restored = tasks.backup.restore(source, destination)
        destination_identity = capture_root_identity(destination)
        _write_backup_evidence(destination, destination_identity, verified)
        credential_path = destination / APPLIANCE_OWNER_CREDENTIAL
        credential = read_confined(
            root=destination,
            relative=APPLIANCE_OWNER_CREDENTIAL.as_posix(),
            expected_root_identity=destination_identity,
            maximum_bytes=1024,
        )
        if credential is not None:
            raise ValueError("restored backup unexpectedly contains a credential")
        _ensure_owner_file(
            credential_path,
            payload_factory=lambda: (secrets.token_urlsafe(32) + "\n").encode("utf-8"),
        )
        restored_profile = open_existing_single_user_local(destination)
        ApplianceScheduler(restored_profile, now=self._now())
        with acquire_daemon_authority(restored_profile) as authority:
            reopened = open_authoritative_local_engine(restored_profile, authority)
            rebuilt = reopened.portability.rebuild_index()
            status = read_appliance_status(
                destination,
                daemon_authority_held=True,
            ).to_dict()
        doctor = cast(dict[str, object], status["doctor"])
        return ApplianceRestoreResult(
            status=restored.status,
            backup_id=restored.backup_id,
            credential_state="created",
            doctor_state=cast(str, doctor["state"]),
            index_generation=cast(int, rebuilt.index_generation),
            manifest_digest_sha256=verified.manifest_digest_sha256,
        )

    def preflight_replacement(
        self,
        source: Path,
        disposable_root: Path,
    ) -> ApplianceReplacementPreflight:
        restored = self.restore_backup(source, disposable_root)
        if restored.doctor_state != "healthy":
            raise ValueError("disposable replacement preflight doctor did not pass")
        return ApplianceReplacementPreflight(
            status="ready",
            backup_id=restored.backup_id,
            manifest_digest_sha256=restored.manifest_digest_sha256,
            credential_state=restored.credential_state,
            doctor_state=restored.doctor_state,
            index_generation=restored.index_generation,
        )

    def export_portable(self, destination: Path, *, export_id: str) -> PortabilityReceipt:
        tasks = _mutations(self._application)
        receipt = tasks.portability.export(destination, export_id=export_id)
        _write_export_evidence(
            self._root,
            self._root_identity,
            destination,
            export_id=export_id,
            recorded_at=self._now(),
        )
        return receipt

    def import_portable(
        self,
        source: Path,
        destination: Path,
        *,
        import_id: str,
    ) -> PortabilityReceipt:
        tasks = _mutations(self._application)
        receipt = tasks.portability.import_clean(source, destination, import_id=import_id)
        destination_identity = capture_root_identity(destination)
        if (
            read_confined(
                root=destination,
                relative=APPLIANCE_OWNER_CREDENTIAL.as_posix(),
                expected_root_identity=destination_identity,
                maximum_bytes=1024,
            )
            is not None
        ):
            raise ValueError("Portable import unexpectedly contains a credential")
        _ensure_owner_file(
            destination / APPLIANCE_OWNER_CREDENTIAL,
            payload_factory=lambda: (secrets.token_urlsafe(32) + "\n").encode("utf-8"),
        )
        return receipt

    def request_portable_export(
        self,
        destination: Path,
        *,
        export_id: str,
    ) -> ApplianceJobSubmission:
        submission = self._request(
            "portable-export",
            export_id,
            {"destination": str(destination), "export_id": export_id},
        )
        if submission.status == "scheduled":
            self._require_scheduler().request("portable-export")
        return submission

    def request_portable_import(
        self,
        source: Path,
        destination: Path,
        *,
        import_id: str,
    ) -> ApplianceJobSubmission:
        submission = self._request(
            "portable-import",
            import_id,
            {
                "destination": str(destination),
                "import_id": import_id,
                "source": str(source),
            },
        )
        if submission.status == "scheduled":
            self._require_scheduler().request("portable-import")
        return submission

    def request_backup(
        self,
        destination: Path,
        *,
        backup_id: str,
    ) -> ApplianceJobSubmission:
        submission = self._request(
            "backup-create",
            backup_id,
            {"backup_id": backup_id, "destination": str(destination)},
        )
        if submission.status == "scheduled":
            self._require_scheduler().request("backup-create")
        return submission

    def handle_job(self, job_name: str, context: ApplianceRunContext) -> ApplianceJobResult:
        _validate_job_name(job_name)
        if context.root != self._root or context.job_name != job_name:
            raise ValueError("appliance recovery job context is invalid")
        pending = self._next_pending(job_name)
        if pending is None:
            return (
                ApplianceJobResult.completed()
                if self._latest_completed(job_name) is not None
                else ApplianceJobResult.empty()
            )
        if job_name == "portable-export":
            payload = pending["payload"]
            assert isinstance(payload, dict)
            self.export_portable(
                Path(cast(str, payload["destination"])),
                export_id=cast(str, payload["export_id"]),
            )
        elif job_name == "portable-import":
            payload = pending["payload"]
            assert isinstance(payload, dict)
            self.import_portable(
                Path(cast(str, payload["source"])),
                Path(cast(str, payload["destination"])),
                import_id=cast(str, payload["import_id"]),
            )
        elif job_name == "backup-create":
            payload = pending["payload"]
            assert isinstance(payload, dict)
            self.create_backup(
                Path(cast(str, payload["destination"])),
                backup_id=cast(str, payload["backup_id"]),
            )
        else:
            raise ValueError("unknown appliance recovery job")
        self._complete(job_name, cast(str, pending["request_id"]), pending)
        return ApplianceJobResult.completed()

    def _request(
        self,
        job_name: str,
        request_id: str,
        payload: dict[str, object],
    ) -> ApplianceJobSubmission:
        record = {
            "payload": payload,
            "request_id": request_id,
            "requested_at": _timestamp(self._now()),
            "schema_version": 1,
            "status": "pending",
        }
        _validate_request_record(job_name, record)
        relative = _request_relative(job_name, request_id)
        existing = read_confined(
            root=self._root,
            relative=relative.as_posix(),
            expected_root_identity=self._root_identity,
            maximum_bytes=_MAXIMUM_REQUEST_BYTES,
        )
        if existing is not None:
            current = _load_request_record(job_name, existing)
            if (
                current["request_id"] != record["request_id"]
                or current["payload"] != record["payload"]
            ):
                raise ValueError("conflicting appliance recovery request")
            return ApplianceJobSubmission(
                job_name=job_name,
                request_id=request_id,
                status="completed" if current["status"] == "completed" else "scheduled",
            )
        atomic_write_new(
            root=self._root,
            relative=relative.as_posix(),
            data=canonical_json_bytes(record),
            expected_root_identity=self._root_identity,
        )
        return ApplianceJobSubmission(
            job_name=job_name,
            request_id=request_id,
            status="scheduled",
        )

    def _next_pending(self, job_name: str) -> dict[str, object] | None:
        for value in self._request_records(job_name):
            if value["status"] == "pending":
                return value
        return None

    def _complete(self, job_name: str, request_id: str, record: dict[str, object]) -> None:
        completed = dict(record)
        requested_at = _parse_timestamp(record.get("requested_at"))
        completed["completed_at"] = _timestamp(max(self._now(), requested_at))
        completed["status"] = "completed"
        _validate_request_record(job_name, completed)
        atomic_replace(
            root=self._root,
            relative=_request_relative(job_name, request_id).as_posix(),
            data=canonical_json_bytes(completed),
            require_existing=True,
            expected_root_identity=self._root_identity,
        )

    def _require_scheduler(self) -> ApplianceScheduler:
        if self._scheduler is None:
            raise RuntimeError("appliance recovery scheduler is unavailable")
        return self._scheduler

    def _latest_completed(self, job_name: str) -> dict[str, object] | None:
        completed: dict[str, object] | None = None
        for value in self._request_records(job_name):
            if value["status"] == "completed":
                completed = value
        return completed

    def _request_records(self, job_name: str) -> tuple[dict[str, object], ...]:
        _validate_job_name(job_name)
        try:
            files = read_confined_tree(
                root=self._root,
                relative=(APPLIANCE_RECOVERY_DIRECTORY / job_name).as_posix(),
                expected_root_identity=self._root_identity,
                maximum_entries=_MAXIMUM_REQUEST_ENTRIES,
                maximum_file_bytes=_MAXIMUM_REQUEST_BYTES,
                maximum_total_bytes=_MAXIMUM_REQUEST_ENTRIES * _MAXIMUM_REQUEST_BYTES,
            )
        except StorageError as error:
            raise ValueError(
                "appliance recovery request inventory is unsafe or unbounded"
            ) from error
        records: list[dict[str, object]] = []
        for relative, payload in files:
            if len(relative.parts) != 1 or relative.suffix != ".json":
                raise ValueError("appliance recovery request inventory is invalid")
            value = _load_request_record(job_name, payload)
            if relative.name != f"{value['request_id']}.json":
                raise ValueError("appliance recovery request identity is invalid")
            records.append(value)
        return tuple(records)


def _request_relative(job_name: str, request_id: str) -> Path:
    _validate_request_id(job_name, request_id)
    return APPLIANCE_RECOVERY_DIRECTORY / job_name / f"{request_id}.json"


def _validate_job_name(job_name: str) -> None:
    if job_name not in _RECOVERY_JOBS:
        raise ValueError("unknown appliance recovery job")


def _validate_request_id(job_name: str, request_id: str) -> None:
    _validate_job_name(job_name)
    match = _REQUEST_ID.fullmatch(request_id) if isinstance(request_id, str) else None
    if match is None or match.group("prefix") != _REQUEST_PREFIX[job_name]:
        raise ValueError("invalid appliance recovery request identity")


def _validate_request_record(job_name: str, value: object) -> dict[str, object]:
    _validate_job_name(job_name)
    if not isinstance(value, dict):
        raise ValueError("invalid appliance recovery request")
    record = cast(dict[str, object], value)
    status = record.get("status")
    expected_keys = {"payload", "request_id", "requested_at", "schema_version", "status"}
    if status == "completed":
        expected_keys.add("completed_at")
    if set(record) != expected_keys or status not in {"pending", "completed"}:
        raise ValueError("invalid appliance recovery request")
    request_id = record.get("request_id")
    if not isinstance(request_id, str):
        raise ValueError("invalid appliance recovery request")
    _validate_request_id(job_name, request_id)
    _parse_timestamp(record.get("requested_at"))
    if status == "completed":
        _parse_timestamp(record.get("completed_at"))
    if record.get("schema_version") != 1 or not isinstance(record.get("payload"), dict):
        raise ValueError("invalid appliance recovery request")
    payload = cast(dict[str, object], record["payload"])
    identifier_key = {
        "backup-create": "backup_id",
        "portable-export": "export_id",
        "portable-import": "import_id",
    }[job_name]
    expected_payload_keys = {identifier_key, "destination"}
    if job_name == "portable-import":
        expected_payload_keys.add("source")
    if set(payload) != expected_payload_keys or payload.get(identifier_key) != request_id:
        raise ValueError("invalid appliance recovery request")
    for key in expected_payload_keys - {identifier_key}:
        raw_path = payload.get(key)
        if (
            not isinstance(raw_path, str)
            or not raw_path
            or "\x00" in raw_path
            or not Path(raw_path).is_absolute()
        ):
            raise ValueError("invalid appliance recovery request")
    return record


def _load_request_record(job_name: str, payload: bytes) -> dict[str, object]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid appliance recovery request") from error
    record = _validate_request_record(job_name, value)
    if canonical_json_bytes(record) != payload:
        raise ValueError("invalid appliance recovery request")
    return record


def _mutations(application: ApplianceApplication) -> EngineTaskSet:
    tasks = application.mutations
    if tasks is None:
        raise RuntimeError("appliance recovery requires a mutating application")
    return tasks


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("invalid appliance recovery clock")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("invalid appliance recovery timestamp")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as error:
        raise ValueError("invalid appliance recovery timestamp") from error
    if _timestamp(parsed) != value:
        raise ValueError("invalid appliance recovery timestamp")
    return parsed


def _write_backup_evidence(
    root: Path,
    root_identity: RootIdentity,
    receipt: BackupReceipt,
) -> None:
    atomic_replace(
        root=root,
        relative=".open-brain/state/appliance-backup-evidence.json",
        data=canonical_json_bytes(
            {
                "backup_id": "backup_" + receipt.manifest_digest_sha256,
                "created_at": receipt.created_at,
                "manifest_digest_sha256": receipt.manifest_digest_sha256,
                "schema_version": 1,
            }
        ),
        expected_root_identity=root_identity,
    )


def _write_export_evidence(
    root: Path,
    root_identity: RootIdentity,
    destination: Path,
    *,
    export_id: str,
    recorded_at: datetime,
) -> None:
    destination_identity = capture_root_identity(destination)
    manifest = read_confined(
        root=destination,
        relative="portable-manifest.json",
        expected_root_identity=destination_identity,
        maximum_bytes=1024 * 1024,
    )
    if manifest is None:
        raise ValueError("Portable export manifest is unavailable")
    digest = sha256(manifest).hexdigest()
    atomic_replace(
        root=root,
        relative=".open-brain/state/appliance-export-evidence.json",
        data=canonical_json_bytes(
            {
                "created_at": _timestamp(recorded_at),
                "export_id": "export_" + digest,
                "manifest_digest_sha256": digest,
                "schema_version": 1,
            }
        ),
        expected_root_identity=root_identity,
    )

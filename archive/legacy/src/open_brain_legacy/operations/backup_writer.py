from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
from base64 import b64decode, b64encode
from binascii import Error as Base64Error
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath

from open_brain_engine.core.ids import canonical_json_bytes
from open_brain_engine.storage.filesystem import (
    DuplicateConflictError,
    StorageError,
    atomic_write_new,
    read_confined,
)
from open_brain_engine.storage.writer_record import CanonicalWriterRecord

from .backup import (
    BackupError,
    BackupObject,
    BackupSourceObject,
    BackupTier,
    PreparedBackup,
    get_backup_job,
    parse_backup_manifest,
    publish_prepared_backup,
)
from .writer_jobs import (
    EffectCommand,
    EffectParameter,
    EffectReceipt,
    EffectRecord,
    PreparedEffect,
    ScheduledEffect,
    WriterJobError,
    WriterJobInvocation,
)

_BACKUP_ID = re.compile(r"backup-[0-9a-f]{24}")
_BACKUP_JOB_IDS = frozenset({"JOB-011", "JOB-014", "JOB-023", "JOB-025"})
_RECEIPT_BASE_FIELDS = frozenset(
    {
        "version",
        "job_id",
        "replay_key",
        "request_digest_sha256",
        "effect",
        "effect_digest_sha256",
        "records",
        "review_item_ids",
        "approval_bindings",
    }
)
_RECORD_FIELDS = frozenset({"record_id", "digest_sha256", "approval"})
_PARAMETER_FIELDS = frozenset({"name", "value"})
_POINTER_FIELDS = frozenset(
    {"version", "effect_digest_sha256", "backup_id", "manifest_digest_sha256"}
)
_PLAN_FIELDS = frozenset(
    {
        "version",
        "backup_id",
        "effect_digest_sha256",
        "manifest_digest_sha256",
        "writer_generation",
        "writer_record_digest_sha256",
    }
)
_SNAPSHOT_FIELDS = frozenset(
    {
        "version",
        "effect_digest_sha256",
        "manifest_base64",
        "objects",
        "receipt_base64",
        "writer_generation",
        "writer_record_digest_sha256",
    }
)
_SNAPSHOT_OBJECT_FIELDS = frozenset({"payload_base64", "relative_path", "tier"})
_MAX_RECEIPT_BYTES = 16 * 1024
_MAX_POINTER_BYTES = 4 * 1024
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_SQLITE_HEADER = b"SQLite format 3\x00"
_SQLITE_SIDECAR_SUFFIXES = ("-journal", "-shm", "-wal")


class CanonicalWriterAuthorityError(WriterJobError):
    """The configured host no longer holds the durable writer designation."""


@dataclass(frozen=True, slots=True)
class BackupEvidenceInspection:
    manifest_count: int
    malformed_count: int
    profile_latest: tuple[tuple[str, datetime], ...]


@dataclass(frozen=True, slots=True)
class FilesystemBackupSource:
    work_root: Path
    personal_root: Path
    capture_root: Path
    saved_content_root: Path
    state_root: Path

    def __post_init__(self) -> None:
        roots = (
            self.work_root,
            self.personal_root,
            self.capture_root,
            self.saved_content_root,
            self.state_root,
        )
        resolved = tuple(_validate_directory(root) for root in roots)
        if any(
            left == right or left in right.parents or right in left.parents
            for index, left in enumerate(resolved)
            for right in resolved[index + 1 :]
        ):
            raise BackupError("backup source roots must be non-overlapping")

    def collect(self) -> tuple[BackupSourceObject, ...]:
        mappings = (
            ("work", self.work_root, BackupTier.WORK),
            ("personal", self.personal_root, BackupTier.PERSONAL),
            ("capture", self.capture_root, BackupTier.CAPTURE),
            ("saved-content", self.saved_content_root, BackupTier.SAVED_CONTENT),
            ("runtime", self.state_root, BackupTier.RUNTIME_STATE),
        )
        objects: list[BackupSourceObject] = []
        for namespace, root, tier in mappings:
            for path in root.rglob("*"):
                if path.is_symlink():
                    raise BackupError("backup sources cannot contain symbolic links")
                if not path.is_file():
                    continue
                source_relative = path.relative_to(root)
                if namespace == "runtime" and path.name.endswith(_SQLITE_SIDECAR_SUFFIXES):
                    continue
                output_relative = source_relative
                if namespace == "runtime" and source_relative.parts[:1] == (
                    ".open-brain-locks",
                ):
                    output_relative = Path("locks", *source_relative.parts[1:])
                try:
                    payload = read_confined(
                        root=root,
                        relative=PurePosixPath(*source_relative.parts),
                    )
                except StorageError:
                    raise BackupError("backup source could not be read") from None
                if payload is None:
                    raise BackupError("backup source could not be read")
                if namespace == "runtime" and payload.startswith(_SQLITE_HEADER):
                    payload = _snapshot_sqlite(path)
                objects.append(
                    BackupSourceObject(
                        relative_path=PurePosixPath(namespace, *output_relative.parts),
                        payload=payload,
                        tier=tier,
                    )
                )
        return tuple(sorted(objects, key=lambda item: str(item.relative_path)))


def inspect_backup_evidence(root: Path) -> BackupEvidenceInspection:
    """Inspect published backup manifests without creating or repairing state."""
    _validate_directory(root)
    backups = root / "backups"
    if not backups.exists():
        return BackupEvidenceInspection(0, 0, ())
    if backups.is_symlink() or not backups.is_dir():
        return BackupEvidenceInspection(0, 1, ())
    manifest_count = 0
    malformed_count = 0
    profile_latest: dict[str, datetime] = {}
    try:
        entries = tuple(backups.iterdir())
    except OSError:
        raise BackupError("backup evidence inspection failed") from None
    for entry in entries:
        if (
            entry.is_symlink()
            or not entry.is_dir()
            or _BACKUP_ID.fullmatch(entry.name) is None
        ):
            malformed_count += 1
            continue
        try:
            payload = read_confined(
                root=root,
                relative=PurePosixPath("backups", entry.name, "manifest.json"),
            )
            if payload is None or len(payload) > _MAX_MANIFEST_BYTES:
                raise ValueError
            receipt = parse_backup_manifest(payload)
            if receipt.backup_id != entry.name:
                raise BackupError("invalid backup manifest")
            store = FilesystemBackupStore(root=root)
            for manifest_entry in receipt.entries:
                object_payload = store.read_object(
                    backup_id=receipt.backup_id,
                    relative_path=manifest_entry.relative_path,
                )
                if (
                    len(object_payload) != manifest_entry.size_bytes
                    or sha256(object_payload).hexdigest()
                    != manifest_entry.digest_sha256
                ):
                    raise BackupError("backup object verification failed")
            profile = receipt.profile
            timestamp = receipt.created_at
        except (
            BackupError,
            StorageError,
            TypeError,
            ValueError,
            UnicodeError,
            json.JSONDecodeError,
        ):
            malformed_count += 1
            continue
        manifest_count += 1
        if profile in {"capture", "full", "personal", "runtime-state"}:
            prior = profile_latest.get(profile)
            profile_latest[profile] = timestamp if prior is None else max(prior, timestamp)
    return BackupEvidenceInspection(
        manifest_count,
        malformed_count,
        tuple(sorted(profile_latest.items())),
    )


class FilesystemBackupStore:
    def __init__(self, *, root: Path) -> None:
        _validate_directory(root)
        self.root = root

    def stage_objects(
        self,
        *,
        backup_id: str,
        objects: tuple[BackupObject, ...],
    ) -> None:
        _validate_backup_id(backup_id)
        if not isinstance(objects, tuple) or any(
            not isinstance(item, BackupObject) for item in objects
        ):
            raise BackupError("invalid backup objects")
        for item in objects:
            relative = _safe_relative(item.relative_path)
            try:
                atomic_write_new(
                    root=self.root,
                    relative=PurePosixPath("backups", backup_id, "objects", relative),
                    data=item.payload,
                )
            except DuplicateConflictError:
                existing = self.read_object(
                    backup_id=backup_id,
                    relative_path=relative,
                )
                if existing != item.payload:
                    raise BackupError("immutable backup conflict") from None

    def publish_manifest(self, *, backup_id: str, manifest: bytes) -> None:
        _validate_backup_id(backup_id)
        if not isinstance(manifest, bytes) or not manifest:
            raise BackupError("invalid backup manifest")
        try:
            atomic_write_new(
                root=self.root,
                relative=PurePosixPath("backups", backup_id, "manifest.json"),
                data=manifest,
            )
        except DuplicateConflictError:
            if self.read_manifest(backup_id=backup_id) != manifest:
                raise BackupError("immutable backup conflict") from None

    def read_manifest(self, *, backup_id: str) -> bytes:
        _validate_backup_id(backup_id)
        payload = read_confined(
            root=self.root,
            relative=PurePosixPath("backups", backup_id, "manifest.json"),
        )
        if payload is None:
            raise BackupError("backup manifest unavailable")
        return payload

    def read_object(self, *, backup_id: str, relative_path: PurePosixPath) -> bytes:
        _validate_backup_id(backup_id)
        relative = _safe_relative(relative_path)
        payload = read_confined(
            root=self.root,
            relative=PurePosixPath("backups", backup_id, "objects", relative),
        )
        if payload is None:
            raise BackupError("backup object unavailable")
        return payload


@dataclass(frozen=True, slots=True)
class BackupAppliedPointer:
    backup_id: str
    effect_digest_sha256: str
    manifest_digest_sha256: str


@dataclass(frozen=True, slots=True)
class BackupWriterApplication:
    job_id: str
    created_at: datetime

    def __post_init__(self) -> None:
        if self.job_id not in _BACKUP_JOB_IDS:
            raise WriterJobError("invalid backup writer application")
        object.__setattr__(self, "created_at", _validated_created_at(self.created_at))

    def prepare(self, invocation: WriterJobInvocation) -> PreparedEffect:
        if (
            not isinstance(invocation, WriterJobInvocation)
            or invocation.job_id != self.job_id
            or invocation.effect is not ScheduledEffect.BACKUP_SNAPSHOT
            or invocation.approved_records
            or invocation.approval_bindings
        ):
            raise WriterJobError("invalid backup writer invocation")
        return _prepared_backup_effect(
            job_id=invocation.job_id,
            replay_key=invocation.replay_key,
            created_at=self.created_at,
        )


class BackupEffectCapability:
    effect = ScheduledEffect.BACKUP_SNAPSHOT
    local_only = True
    dry_run = False

    def __init__(
        self,
        *,
        root: Path,
        source: FilesystemBackupSource,
        store: FilesystemBackupStore,
        writer_record: CanonicalWriterRecord,
        writer_record_reader: Callable[[], CanonicalWriterRecord | None],
    ) -> None:
        if (
            not isinstance(source, FilesystemBackupSource)
            or not isinstance(store, FilesystemBackupStore)
            or store.root != root
            or not isinstance(writer_record, CanonicalWriterRecord)
            or not callable(writer_record_reader)
        ):
            raise WriterJobError("invalid backup effect capability")
        _validate_directory(root)
        self.root = root
        self._source = source
        self._store = store
        self._writer_record = writer_record
        self._writer_record_reader = writer_record_reader

    def recover(self, job_id: str, replay_key: str) -> EffectReceipt | None:
        self._validate_writer_authority()
        reservation, _pointer = _effect_paths(job_id, replay_key)
        payload = read_confined(root=self.root, relative=reservation)
        if payload is None:
            snapshot = read_confined(
                root=self.root,
                relative=_snapshot_path(job_id, replay_key),
            )
            if snapshot is None:
                return None
            receipt, prepared = _snapshot_from_bytes(
                snapshot,
                writer_record=self._writer_record,
            )
            if receipt.job_id != job_id or receipt.replay_key != replay_key:
                raise WriterJobError("invalid backup source snapshot")
            self._persist_reservation(receipt, prepared)
            payload = read_confined(root=self.root, relative=reservation)
            if payload is None:
                raise WriterJobError("backup effect reservation unavailable")
        receipt = _effect_receipt_from_bytes(payload)
        if receipt.job_id != job_id or receipt.replay_key != replay_key:
            raise WriterJobError("invalid backup effect receipt")
        self._load_plan(receipt)
        return receipt

    def reserve(self, command: EffectCommand) -> EffectReceipt:
        self._validate_writer_authority()
        created_at, generation = self._validate_command(command)
        receipt = EffectReceipt.from_command(command)
        snapshot_path = _snapshot_path(command.job_id, command.replay_key)
        snapshot_payload = read_confined(root=self.root, relative=snapshot_path)
        if snapshot_payload is None:
            prepared = get_backup_job(command.job_id).prepare(
                source=self._source,
                created_at=created_at,
                generation=generation,
            )
            snapshot_payload = _snapshot_bytes(
                prepared=prepared,
                receipt=receipt,
                writer_record=self._writer_record,
            )
            _write_same_or_new(
                root=self.root,
                relative=snapshot_path,
                data=snapshot_payload,
                error="backup source snapshot conflict",
            )
        snapshot_receipt, prepared = _snapshot_from_bytes(
            snapshot_payload,
            writer_record=self._writer_record,
        )
        if snapshot_receipt != receipt:
            raise WriterJobError("backup effect reservation conflict")
        self._persist_reservation(receipt, prepared)
        recovered = self.recover(command.job_id, command.replay_key)
        if recovered != receipt:
            raise WriterJobError("backup effect reservation conflict")
        return receipt

    def _persist_reservation(
        self,
        receipt: EffectReceipt,
        prepared: PreparedBackup,
    ) -> None:
        self._store.stage_objects(
            backup_id=prepared.receipt.backup_id,
            objects=prepared.objects,
        )
        reservation, _pointer = _effect_paths(receipt.job_id, receipt.replay_key)
        plan_path, manifest_path = _plan_paths(receipt.job_id, receipt.replay_key)
        manifest = prepared.receipt.manifest_bytes()
        _write_same_or_new(
            root=self.root,
            relative=manifest_path,
            data=manifest,
            error="backup plan conflict",
        )
        plan_payload = canonical_json_bytes(
            {
                "version": 1,
                "backup_id": prepared.receipt.backup_id,
                "effect_digest_sha256": receipt.effect_digest_sha256,
                "manifest_digest_sha256": prepared.receipt.manifest_digest_sha256,
                "writer_generation": self._writer_record.generation,
                "writer_record_digest_sha256": self._writer_record.digest_sha256,
            }
        )
        _write_same_or_new(
            root=self.root,
            relative=plan_path,
            data=plan_payload,
            error="backup plan conflict",
        )
        _write_same_or_new(
            root=self.root,
            relative=reservation,
            data=_effect_receipt_bytes(receipt),
            error="backup effect reservation conflict",
        )

    def apply(self, command: EffectCommand, receipt: EffectReceipt) -> None:
        self._validate_writer_authority()
        self._validate_command(command)
        expected = EffectReceipt.from_command(command)
        if receipt != expected or self.recover(command.job_id, command.replay_key) != receipt:
            raise WriterJobError("backup effect reservation conflict")
        prepared = self._load_plan(receipt)
        publish_prepared_backup(prepared=prepared, store=self._store)
        backup = prepared.receipt
        _reservation, pointer = _effect_paths(command.job_id, command.replay_key)
        payload = canonical_json_bytes(
            {
                "version": 1,
                "effect_digest_sha256": receipt.effect_digest_sha256,
                "backup_id": backup.backup_id,
                "manifest_digest_sha256": backup.manifest_digest_sha256,
            }
        )
        try:
            atomic_write_new(root=self.root, relative=pointer, data=payload)
        except DuplicateConflictError:
            raise WriterJobError("backup applied pointer conflict") from None

    def read(self, receipt: EffectReceipt) -> PreparedEffect | None:
        self._validate_writer_authority()
        if not isinstance(receipt, EffectReceipt) or receipt.effect is not self.effect:
            raise WriterJobError("invalid backup effect receipt")
        _reservation, pointer_path = _effect_paths(receipt.job_id, receipt.replay_key)
        payload = read_confined(root=self.root, relative=pointer_path)
        if payload is None:
            return None
        pointer = _applied_pointer_from_bytes(payload)
        if pointer.effect_digest_sha256 != receipt.effect_digest_sha256:
            raise WriterJobError("backup applied pointer conflict")
        prepared = self._load_plan(receipt)
        if prepared.receipt.backup_id != pointer.backup_id:
            raise WriterJobError("backup durable read-back conflict")
        manifest = self._store.read_manifest(backup_id=pointer.backup_id)
        if sha256(manifest).hexdigest() != pointer.manifest_digest_sha256:
            raise WriterJobError("backup durable read-back conflict")
        try:
            manifest_receipt = parse_backup_manifest(manifest)
        except BackupError:
            raise WriterJobError("backup durable read-back conflict") from None
        if manifest_receipt != prepared.receipt:
            raise WriterJobError("backup durable read-back conflict")
        return PreparedEffect(
            receipt.effect,
            receipt.records,
            receipt.review_item_ids,
            receipt.parameters,
        )

    def applied_pointer(self, receipt: EffectReceipt) -> BackupAppliedPointer:
        self._validate_writer_authority()
        if not isinstance(receipt, EffectReceipt) or receipt.effect is not self.effect:
            raise WriterJobError("invalid backup effect receipt")
        _reservation, pointer_path = _effect_paths(receipt.job_id, receipt.replay_key)
        payload = read_confined(root=self.root, relative=pointer_path)
        if payload is None:
            raise WriterJobError("backup applied pointer unavailable")
        return _applied_pointer_from_bytes(payload)

    def _load_plan(self, receipt: EffectReceipt) -> PreparedBackup:
        plan_path, manifest_path = _plan_paths(receipt.job_id, receipt.replay_key)
        plan_payload = read_confined(root=self.root, relative=plan_path)
        manifest = read_confined(root=self.root, relative=manifest_path)
        if plan_payload is None or manifest is None or len(manifest) > _MAX_MANIFEST_BYTES:
            raise WriterJobError("backup plan unavailable")
        try:
            value = json.loads(plan_payload.decode("utf-8"), object_pairs_hook=_unique_object)
            if type(value) is not dict or frozenset(value) != _PLAN_FIELDS or value["version"] != 1:
                raise WriterJobError("invalid backup plan")
            prepared_receipt = parse_backup_manifest(manifest)
        except WriterJobError:
            raise
        except (BackupError, KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            raise WriterJobError("invalid backup plan") from None
        if (
            value["effect_digest_sha256"] != receipt.effect_digest_sha256
            or value["backup_id"] != prepared_receipt.backup_id
            or value["manifest_digest_sha256"]
            != prepared_receipt.manifest_digest_sha256
            or value["writer_generation"] != self._writer_record.generation
            or value["writer_record_digest_sha256"]
            != self._writer_record.digest_sha256
        ):
            raise WriterJobError("backup plan conflict")
        objects: list[BackupObject] = []
        for entry in prepared_receipt.entries:
            payload = self._store.read_object(
                backup_id=prepared_receipt.backup_id,
                relative_path=entry.relative_path,
            )
            if (
                len(payload) != entry.size_bytes
                or sha256(payload).hexdigest() != entry.digest_sha256
            ):
                raise WriterJobError("backup plan object conflict")
            objects.append(BackupObject(entry.relative_path, payload, entry.tier))
        return PreparedBackup(prepared_receipt, tuple(objects))

    def _validate_writer_authority(self) -> None:
        try:
            current = self._writer_record_reader()
        except Exception:
            raise CanonicalWriterAuthorityError(
                "canonical writer authority unavailable"
            ) from None
        if current != self._writer_record:
            raise CanonicalWriterAuthorityError("canonical writer authority changed")

    def _validate_command(self, command: EffectCommand) -> tuple[datetime, str | None]:
        if (
            not isinstance(command, EffectCommand)
            or command.job_id not in _BACKUP_JOB_IDS
            or command.prepared.effect is not self.effect
        ):
            raise WriterJobError("invalid backup effect command")
        created_at, generation = _prepared_values(command.prepared)
        if command.prepared != _prepared_backup_effect(
            job_id=command.job_id,
            replay_key=command.replay_key,
            created_at=created_at,
        ):
            raise WriterJobError("invalid backup effect command")
        if command.job_id == "JOB-025" and generation != command.replay_key:
            raise WriterJobError("invalid backup effect command")
        if command.job_id != "JOB-025" and generation is not None:
            raise WriterJobError("invalid backup effect command")
        return created_at, generation


def _prepared_backup_effect(
    *,
    job_id: str,
    replay_key: str,
    created_at: datetime,
) -> PreparedEffect:
    job = get_backup_job(job_id)
    timestamp = _timestamp(_validated_created_at(created_at))
    generation = replay_key if job_id == "JOB-025" else None
    payload = {
        "created_at": timestamp,
        "generation": generation,
        "job_id": job_id,
        "profile": job.profile.name,
        "replay_key": replay_key,
    }
    digest = sha256(canonical_json_bytes(payload)).hexdigest()
    parameters = [
        EffectParameter("created_at", timestamp),
        EffectParameter("profile", job.profile.name),
    ]
    if generation is not None:
        parameters.insert(1, EffectParameter("generation", generation))
    return PreparedEffect(
        effect=ScheduledEffect.BACKUP_SNAPSHOT,
        records=(EffectRecord("backup_" + digest, digest),),
        parameters=tuple(parameters),
    )


def _prepared_values(prepared: PreparedEffect) -> tuple[datetime, str | None]:
    values = {parameter.name: parameter.value for parameter in prepared.parameters}
    if frozenset(values) not in {
        frozenset({"created_at", "profile"}),
        frozenset({"created_at", "generation", "profile"}),
    }:
        raise WriterJobError("invalid backup effect command")
    return _parse_timestamp(values["created_at"]), values.get("generation")


def _effect_paths(job_id: str, replay_key: str) -> tuple[PurePosixPath, PurePosixPath]:
    if job_id not in _BACKUP_JOB_IDS:
        raise WriterJobError("invalid backup effect identity")
    identity = sha256(
        canonical_json_bytes({"job_id": job_id, "replay_key": replay_key})
    ).hexdigest()
    base = PurePosixPath("reservations", job_id, identity)
    return base.with_suffix(".json"), base.with_suffix(".applied.json")


def _plan_paths(job_id: str, replay_key: str) -> tuple[PurePosixPath, PurePosixPath]:
    reservation, _pointer = _effect_paths(job_id, replay_key)
    stem = reservation.with_suffix("")
    return stem.with_suffix(".plan.json"), stem.with_suffix(".manifest.json")


def _snapshot_path(job_id: str, replay_key: str) -> PurePosixPath:
    reservation, _pointer = _effect_paths(job_id, replay_key)
    return reservation.with_suffix("").with_suffix(".snapshot.json")


def _snapshot_bytes(
    *,
    prepared: PreparedBackup,
    receipt: EffectReceipt,
    writer_record: CanonicalWriterRecord,
) -> bytes:
    return canonical_json_bytes(
        {
            "version": 1,
            "effect_digest_sha256": receipt.effect_digest_sha256,
            "manifest_base64": b64encode(prepared.receipt.manifest_bytes()).decode("ascii"),
            "objects": [
                {
                    "payload_base64": b64encode(item.payload).decode("ascii"),
                    "relative_path": str(item.relative_path),
                    "tier": item.tier.value,
                }
                for item in prepared.objects
            ],
            "receipt_base64": b64encode(_effect_receipt_bytes(receipt)).decode("ascii"),
            "writer_generation": writer_record.generation,
            "writer_record_digest_sha256": writer_record.digest_sha256,
        }
    )


def _snapshot_from_bytes(
    payload: bytes,
    *,
    writer_record: CanonicalWriterRecord,
) -> tuple[EffectReceipt, PreparedBackup]:
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
        if (
            type(value) is not dict
            or frozenset(value) != _SNAPSHOT_FIELDS
            or value["version"] != 1
            or value["writer_generation"] != writer_record.generation
            or value["writer_record_digest_sha256"]
            != writer_record.digest_sha256
            or type(value["objects"]) is not list
        ):
            raise WriterJobError("invalid backup source snapshot")
        receipt = _effect_receipt_from_bytes(
            b64decode(value["receipt_base64"], validate=True)
        )
        if value["effect_digest_sha256"] != receipt.effect_digest_sha256:
            raise WriterJobError("invalid backup source snapshot")
        manifest = b64decode(value["manifest_base64"], validate=True)
        prepared_receipt = parse_backup_manifest(manifest)
        objects: list[BackupObject] = []
        for raw in value["objects"]:
            if type(raw) is not dict or frozenset(raw) != _SNAPSHOT_OBJECT_FIELDS:
                raise WriterJobError("invalid backup source snapshot")
            objects.append(
                BackupObject(
                    _safe_relative(PurePosixPath(raw["relative_path"])),
                    b64decode(raw["payload_base64"], validate=True),
                    BackupTier(raw["tier"]),
                )
            )
    except WriterJobError:
        raise
    except (
        BackupError,
        Base64Error,
        KeyError,
        TypeError,
        ValueError,
        UnicodeError,
        json.JSONDecodeError,
    ):
        raise WriterJobError("invalid backup source snapshot") from None
    object_tuple = tuple(objects)
    if (
        len(object_tuple) != len(prepared_receipt.entries)
        or canonical_json_bytes(value) != payload
    ):
        raise WriterJobError("invalid backup source snapshot")
    for item, entry in zip(object_tuple, prepared_receipt.entries, strict=True):
        if (
            item.relative_path != entry.relative_path
            or item.tier is not entry.tier
            or len(item.payload) != entry.size_bytes
            or sha256(item.payload).hexdigest() != entry.digest_sha256
        ):
            raise WriterJobError("invalid backup source snapshot")
    return receipt, PreparedBackup(prepared_receipt, object_tuple)


def _write_same_or_new(
    *,
    root: Path,
    relative: PurePosixPath,
    data: bytes,
    error: str,
) -> None:
    try:
        atomic_write_new(root=root, relative=relative, data=data)
    except DuplicateConflictError:
        if read_confined(root=root, relative=relative) != data:
            raise WriterJobError(error) from None


def _snapshot_sqlite(path: Path) -> bytes:
    source: sqlite3.Connection | None = None
    target: sqlite3.Connection | None = None
    try:
        source = sqlite3.connect(f"{path.resolve(strict=True).as_uri()}?mode=ro", uri=True)
        source.execute("PRAGMA query_only = ON")
        target = sqlite3.connect(":memory:")
        source.backup(target)
        if target.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise BackupError("SQLite backup snapshot failed validation")
        payload = target.serialize()
        if not isinstance(payload, bytes) or not payload.startswith(_SQLITE_HEADER):
            raise BackupError("SQLite backup snapshot failed validation")
        return payload
    except BackupError:
        raise
    except (OSError, sqlite3.Error):
        raise BackupError("SQLite backup snapshot failed") from None
    finally:
        if target is not None:
            target.close()
        if source is not None:
            source.close()


def _effect_receipt_bytes(receipt: EffectReceipt) -> bytes:
    value: dict[str, object] = {
        "version": 1,
        "job_id": receipt.job_id,
        "replay_key": receipt.replay_key,
        "request_digest_sha256": receipt.request_digest_sha256,
        "effect": receipt.effect.value,
        "effect_digest_sha256": receipt.effect_digest_sha256,
        "records": [record.to_dict() for record in receipt.records],
        "review_item_ids": list(receipt.review_item_ids),
        "approval_bindings": [binding.to_dict() for binding in receipt.approval_bindings],
    }
    if receipt.parameters:
        value["parameters"] = [parameter.to_dict() for parameter in receipt.parameters]
    return canonical_json_bytes(value)


def _effect_receipt_from_bytes(payload: bytes) -> EffectReceipt:
    if not isinstance(payload, bytes) or not payload or len(payload) > _MAX_RECEIPT_BYTES:
        raise WriterJobError("invalid backup effect receipt")
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
        expected_fields = _RECEIPT_BASE_FIELDS | (
            {"parameters"} if isinstance(value, dict) and "parameters" in value else set()
        )
        if (
            type(value) is not dict
            or frozenset(value) != expected_fields
            or value["version"] != 1
            or value["approval_bindings"] != []
            or type(value["review_item_ids"]) is not list
            or type(value["records"]) is not list
        ):
            raise WriterJobError("invalid backup effect receipt")
        records: list[EffectRecord] = []
        for raw in value["records"]:
            if (
                type(raw) is not dict
                or frozenset(raw) != _RECORD_FIELDS
                or raw["approval"] is not None
            ):
                raise WriterJobError("invalid backup effect receipt")
            records.append(EffectRecord(raw["record_id"], raw["digest_sha256"]))
        raw_parameters = value.get("parameters", [])
        if type(raw_parameters) is not list:
            raise WriterJobError("invalid backup effect receipt")
        parameters: list[EffectParameter] = []
        for raw in raw_parameters:
            if type(raw) is not dict or frozenset(raw) != _PARAMETER_FIELDS:
                raise WriterJobError("invalid backup effect receipt")
            parameters.append(EffectParameter(raw["name"], raw["value"]))
        receipt = EffectReceipt(
            job_id=value["job_id"],
            replay_key=value["replay_key"],
            request_digest_sha256=value["request_digest_sha256"],
            effect=ScheduledEffect(value["effect"]),
            effect_digest_sha256=value["effect_digest_sha256"],
            records=tuple(records),
            review_item_ids=tuple(value["review_item_ids"]),
            approval_bindings=(),
            parameters=tuple(parameters),
        )
    except WriterJobError:
        raise
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        raise WriterJobError("invalid backup effect receipt") from None
    if (
        receipt.effect is not ScheduledEffect.BACKUP_SNAPSHOT
        or _effect_receipt_bytes(receipt) != payload
    ):
        raise WriterJobError("invalid backup effect receipt")
    return receipt


def _applied_pointer_from_bytes(payload: bytes) -> BackupAppliedPointer:
    if not isinstance(payload, bytes) or not payload or len(payload) > _MAX_POINTER_BYTES:
        raise WriterJobError("invalid backup applied pointer")
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
        if type(value) is not dict or frozenset(value) != _POINTER_FIELDS or value["version"] != 1:
            raise WriterJobError("invalid backup applied pointer")
        pointer = BackupAppliedPointer(
            backup_id=value["backup_id"],
            effect_digest_sha256=value["effect_digest_sha256"],
            manifest_digest_sha256=value["manifest_digest_sha256"],
        )
        _validate_backup_id(pointer.backup_id)
        if not all(
            isinstance(digest, str)
            and len(digest) == 64
            and all(character in "0123456789abcdef" for character in digest)
            for digest in (
                pointer.effect_digest_sha256,
                pointer.manifest_digest_sha256,
            )
        ):
            raise WriterJobError("invalid backup applied pointer")
    except WriterJobError:
        raise
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        raise WriterJobError("invalid backup applied pointer") from None
    if canonical_json_bytes(value) != payload:
        raise WriterJobError("invalid backup applied pointer")
    return pointer


def _validated_created_at(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise WriterJobError("invalid backup created_at")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_timestamp(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except (TypeError, ValueError):
        raise WriterJobError("invalid backup created_at") from None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def _validate_directory(root: Path) -> Path:
    if not isinstance(root, Path) or not root.is_absolute():
        raise BackupError("backup root must be a safe directory")
    try:
        metadata = os.lstat(root)
    except OSError:
        raise BackupError("backup root must be a safe directory") from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise BackupError("backup root must be a safe directory")
    return root.resolve(strict=True)


def _validate_backup_id(backup_id: str) -> None:
    if not isinstance(backup_id, str) or _BACKUP_ID.fullmatch(backup_id) is None:
        raise BackupError("invalid backup identifier")


def _safe_relative(value: PurePosixPath) -> PurePosixPath:
    if (
        not isinstance(value, PurePosixPath)
        or value.is_absolute()
        or not value.parts
        or any(part in {"", ".", ".."} for part in value.parts)
    ):
        raise BackupError("invalid backup object path")
    return value

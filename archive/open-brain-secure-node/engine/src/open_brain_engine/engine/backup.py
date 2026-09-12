"""Engine-owned immutable backup creation, verification, and restore tasks."""

from __future__ import annotations

import json
import stat
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, cast

from open_brain_engine.core.ids import canonical_json_bytes
from open_brain_engine.core.locks import LockScope
from open_brain_engine.portable import validate_portable_file_set
from open_brain_engine.storage.filesystem import (
    RootIdentity,
    capture_root_identity,
    read_confined,
)
from open_brain_engine.storage.locks import FileLease
from open_brain_engine.storage.staging import (
    StagingError,
    capture_destination_parent,
    destination_child_identity,
    remove_empty_destination,
    sibling_stage,
)

from .backup_ports import (
    BackupSourceEntry,
    LocalBackupSource,
    validate_backup_app_state_entry,
    validate_sqlite_backup_bytes,
)
from .contracts import BackupFault, BackupReceipt

if TYPE_CHECKING:
    from .local import BrainEngine

_BACKUP_MANIFEST = "backup-manifest.json"
_RESTORE_RECEIPT = ".open-brain/state/appliance-restore.json"
_HEX64 = frozenset("0123456789abcdef")
_MAXIMUM_MANIFEST_BYTES = 1024 * 1024
_MAXIMUM_BACKUP_ENTRIES = 4096
_MAXIMUM_BACKUP_FILE_BYTES = 512 * 1024 * 1024
_MAXIMUM_BACKUP_TOTAL_BYTES = 2 * 1024 * 1024 * 1024


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("invalid backup clock")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _portable_id(value: str, prefix: str) -> str:
    from .normalization import _portable_id as validate_identifier

    return validate_identifier(value, prefix)


def _backup_lease(
    destination: Path,
    actor_id: str,
    parent_identity: RootIdentity,
) -> FileLease:
    identity = "backup-" + sha256(actor_id.encode("utf-8")).hexdigest()[:32]
    return FileLease(
        destination.parent,
        identity,
        root_identity=parent_identity,
        required_root_mode=0o700,
    )


def _reject_containment(source: Path, destination: Path) -> None:
    try:
        source_real = source.resolve(strict=True)
        destination_real = destination.resolve(strict=False)
    except OSError as error:
        raise ValueError("backup source and destination cannot be resolved") from error
    if (
        source_real == destination_real
        or source_real.is_relative_to(destination_real)
        or destination_real.is_relative_to(source_real)
    ):
        raise ValueError("backup source and destination must not contain one another")


def _manifest(
    *,
    backup_id: str,
    created_at: str,
    tenant_id: str,
    entries: tuple[BackupSourceEntry, ...],
) -> dict[str, object]:
    return {
        "backup_id": backup_id,
        "created_at": created_at,
        "files": [
            {
                "path": entry.path,
                "sha256": sha256(entry.payload).hexdigest(),
                "size_bytes": len(entry.payload),
            }
            for entry in entries
        ],
        "schema_version": 1,
        "tenant_id": tenant_id,
    }


def _manifest_bytes(manifest: dict[str, object]) -> bytes:
    return canonical_json_bytes(manifest)


def _manifest_digest(manifest: dict[str, object]) -> str:
    return sha256(_manifest_bytes(manifest)).hexdigest()


def _receipt(manifest: dict[str, object], *, status: str, duplicate: bool = False) -> BackupReceipt:
    files = cast(list[dict[str, object]], manifest["files"])
    return BackupReceipt(
        backup_id=cast(str, manifest["backup_id"]),
        created_at=cast(str, manifest["created_at"]),
        manifest_digest_sha256=_manifest_digest(manifest),
        status=status,
        portable_files=sum(str(entry["path"]).startswith("portable/") for entry in files),
        sqlite_snapshots=sum(str(entry["path"]).startswith("sqlite/") for entry in files),
        app_state_files=sum(str(entry["path"]).startswith("app-state/") for entry in files),
        duplicate=duplicate,
    )


def _validated_timestamp(value: object) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("backup manifest timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as error:
        raise ValueError("backup manifest timestamp is invalid") from error
    if _timestamp(parsed) != value:
        raise ValueError("backup manifest timestamp is invalid")
    return value


def _validated_manifest(manifest: object) -> dict[str, object]:
    if not isinstance(manifest, dict) or set(manifest) != {
        "backup_id",
        "created_at",
        "files",
        "schema_version",
        "tenant_id",
    }:
        raise ValueError("backup manifest is invalid")
    normalized = cast(dict[str, object], manifest)
    backup_id = normalized.get("backup_id")
    tenant_id = normalized.get("tenant_id")
    if not isinstance(backup_id, str) or not isinstance(tenant_id, str):
        raise ValueError("backup manifest is invalid")
    _portable_id(backup_id, "backup")
    _portable_id(tenant_id, "tenant")
    _validated_timestamp(normalized.get("created_at"))
    if normalized.get("schema_version") != 1:
        raise ValueError("backup manifest is invalid")
    files = normalized.get("files")
    if not isinstance(files, list) or not 1 <= len(files) <= _MAXIMUM_BACKUP_ENTRIES:
        raise ValueError("backup manifest inventory is invalid")
    previous: str | None = None
    total_bytes = 0
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "size_bytes"}:
            raise ValueError("backup manifest inventory is invalid")
        path = item.get("path")
        digest = item.get("sha256")
        size_bytes = item.get("size_bytes")
        if (
            not isinstance(path, str)
            or not path
            or path.startswith("/")
            or "\\" in path
            or any(part in {"", ".", ".."} for part in PurePosixPath(path).parts)
            or previous is not None
            and path <= previous
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in _HEX64 for character in digest)
            or type(size_bytes) is not int
            or not 0 <= size_bytes <= _MAXIMUM_BACKUP_FILE_BYTES
        ):
            raise ValueError("backup manifest inventory is invalid")
        previous = path
        total_bytes += size_bytes
        if total_bytes > _MAXIMUM_BACKUP_TOTAL_BYTES:
            raise ValueError("backup manifest exceeds the bounded total size")
    return normalized


def _read_manifest(
    root: Path,
    *,
    expected_root_identity: RootIdentity,
) -> dict[str, object]:
    payload = read_confined(
        root=root,
        relative=_BACKUP_MANIFEST,
        expected_root_identity=expected_root_identity,
        maximum_bytes=_MAXIMUM_MANIFEST_BYTES,
    )
    if payload is None:
        raise ValueError("backup manifest is unavailable")
    try:
        manifest = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("backup manifest is invalid") from error
    manifest = _validated_manifest(manifest)
    if _manifest_bytes(manifest) != payload:
        raise ValueError("backup manifest is invalid")
    return manifest


def _validate_backup_root(
    source: Path,
    *,
    expected_root_identity: RootIdentity,
) -> tuple[dict[str, object], tuple[BackupSourceEntry, ...]]:
    manifest = _read_manifest(source, expected_root_identity=expected_root_identity)
    files = cast(list[dict[str, object]], manifest["files"])
    entries: list[BackupSourceEntry] = []
    portable: dict[str, bytes] = {}
    sqlite_payload: bytes | None = None
    for entry in files:
        path = cast(str, entry["path"])
        size_bytes = cast(int, entry["size_bytes"])
        payload = read_confined(
            root=source,
            relative=path,
            expected_root_identity=expected_root_identity,
            maximum_bytes=max(1, size_bytes + 1),
        )
        if payload is None:
            raise ValueError("backup file is unavailable")
        if (
            len(payload) != size_bytes
            or sha256(payload).hexdigest() != cast(str, entry["sha256"])
        ):
            raise ValueError("backup file failed integrity verification")
        if path.startswith("portable/"):
            portable[path.removeprefix("portable/")] = payload
        elif path == "sqlite/phase1.sqlite3":
            if sqlite_payload is not None:
                raise ValueError("backup manifest inventory is invalid")
            sqlite_payload = payload
        elif path.startswith("app-state/"):
            validate_backup_app_state_entry(
                path,
                payload,
                tenant_id=cast(str, manifest["tenant_id"]),
            )
        else:
            raise ValueError("backup manifest inventory is invalid")
        entries.append(BackupSourceEntry(path=path, payload=payload))
    if sqlite_payload is None:
        raise ValueError("backup SQLite snapshot is unavailable")
    validate_sqlite_backup_bytes(sqlite_payload)
    try:
        validate_portable_file_set(
            portable,
            tenant_id=cast(str, manifest["tenant_id"]),
        )
    except ValueError as error:
        raise ValueError("backup Portable inventory is invalid") from error
    return manifest, tuple(entries)


def _replay_matches(
    manifest: dict[str, object],
    entries: tuple[BackupSourceEntry, ...],
    *,
    backup_id: str,
) -> bool:
    if manifest.get("backup_id") != backup_id:
        return False
    expected = _manifest(
        backup_id=backup_id,
        created_at=cast(str, manifest["created_at"]),
        tenant_id=cast(str, manifest["tenant_id"]),
        entries=entries,
    )
    return expected["files"] == manifest["files"]


class BackupTasks:
    """Public task capability for immutable engine-owned backups."""

    def __init__(self, engine: BrainEngine) -> None:
        self._engine = engine

    def create(self, destination: Path, *, backup_id: str) -> BackupReceipt:
        self._engine._assert_root()
        _portable_id(backup_id, "backup")
        _reject_containment(self._engine.profile.root, destination)
        entries = LocalBackupSource(
            root=self._engine.profile.root,
            tenant_id=self._engine.profile.tenant_id,
            root_identity=self._engine.profile.root_identity,
        ).entries()
        parent_identity = capture_destination_parent(
            destination,
            forbidden_ancestor_identity=self._engine.profile.root_identity,
        )
        with (
            self._engine._writer_lease.acquire_shared_writer(),
            _backup_lease(
                destination,
                self._engine.profile.owner_actor_id,
                parent_identity,
            ).acquire(LockScope.PORTABILITY_PROMOTION),
        ):
            capture_destination_parent(
                destination,
                forbidden_ancestor_identity=self._engine.profile.root_identity,
                expected_identity=parent_identity,
            )
            destination_identity = destination_child_identity(
                destination,
                parent_identity=parent_identity,
            )
            if destination_identity is not None:
                manifest, _ = _validate_backup_root(
                    destination,
                    expected_root_identity=destination_identity,
                )
                if _replay_matches(manifest, entries, backup_id=backup_id):
                    return _receipt(manifest, status="created", duplicate=True)
                raise ValueError("backup destination conflicts")
            manifest = _manifest(
                backup_id=backup_id,
                created_at=_timestamp(self._engine._clock()),
                tenant_id=self._engine.profile.tenant_id,
                entries=entries,
            )
            return self._create(
                destination,
                parent_identity=parent_identity,
                manifest=manifest,
                entries=entries,
            )

    def verify(self, source: Path) -> BackupReceipt:
        self._engine._assert_root()
        source_identity = capture_root_identity(source.resolve(strict=True))
        manifest, _ = _validate_backup_root(source, expected_root_identity=source_identity)
        return _receipt(manifest, status="verified")

    def restore(self, source: Path, destination: Path) -> BackupReceipt:
        self._engine._assert_root()
        _reject_containment(source, destination)
        _reject_containment(self._engine.profile.root, destination)
        source_identity = capture_root_identity(source.resolve(strict=True))
        manifest, entries = _validate_backup_root(source, expected_root_identity=source_identity)
        parent_identity = capture_destination_parent(
            destination,
            forbidden_ancestor_identity=source_identity,
        )
        capture_destination_parent(
            destination,
            forbidden_ancestor_identity=self._engine.profile.root_identity,
            expected_identity=parent_identity,
        )
        destination_identity = destination_child_identity(
            destination,
            parent_identity=parent_identity,
        )
        if destination_identity is not None:
            if _restored_destination_matches(
                destination,
                destination_identity=destination_identity,
                manifest=manifest,
                entries=entries,
            ):
                return _receipt(manifest, status="restored", duplicate=True)
            _require_empty_disposable_root(destination)
            remove_empty_destination(
                destination,
                parent_identity=parent_identity,
                child_identity=destination_identity,
            )
        marker = _restore_marker(manifest)
        try:
            with sibling_stage(
                destination,
                expected_parent_identity=parent_identity,
                forbidden_ancestor_identity=source_identity,
            ) as stage:
                for entry in entries:
                    mapped = _restore_path(entry.path)
                    if mapped is not None:
                        stage.write_bytes(mapped, entry.payload)
                        self._engine._fault(BackupFault.AFTER_RESTORE_FILE)
                stage.write_bytes(_RESTORE_RECEIPT, canonical_json_bytes(marker))
                stage_root = stage.root
                stage_identity = stage.identity
                _validate_restored_destination(
                    stage_root,
                    destination_identity=stage_identity,
                    manifest=manifest,
                    entries=entries,
                )
                stage.assert_identity()

                def verify_staged_restore() -> None:
                    capture_destination_parent(
                        destination,
                        forbidden_ancestor_identity=self._engine.profile.root_identity,
                        expected_identity=parent_identity,
                    )
                    _validate_restored_destination(
                        stage_root,
                        destination_identity=stage_identity,
                        manifest=manifest,
                        entries=entries,
                    )

                self._engine._fault(BackupFault.BEFORE_RESTORE_PROMOTION)
                stage.promote(pre_rename=verify_staged_restore)
                self._engine._fault(BackupFault.AFTER_RESTORE_PROMOTION)
        except StagingError as error:
            raise ValueError("backup restore staging failed") from error
        return _receipt(manifest, status="restored")

    def _create(
        self,
        destination: Path,
        *,
        parent_identity: RootIdentity,
        manifest: dict[str, object],
        entries: tuple[BackupSourceEntry, ...],
    ) -> BackupReceipt:
        try:
            with sibling_stage(
                destination,
                expected_parent_identity=parent_identity,
                forbidden_ancestor_identity=self._engine.profile.root_identity,
            ) as stage:
                self._engine._fault(BackupFault.AFTER_STAGE_CREATED)
                for entry in entries:
                    stage.write_bytes(entry.path, entry.payload)
                    self._engine._fault(BackupFault.AFTER_BACKUP_FILE)
                stage_root = stage.root
                stage_identity = stage.identity
                _validate_backup_files(stage_root, stage_identity, manifest)
                stage.write_bytes(_BACKUP_MANIFEST, _manifest_bytes(manifest))
                self._engine._fault(BackupFault.AFTER_MANIFEST)
                _validate_backup_root(stage_root, expected_root_identity=stage_identity)
                stage.assert_identity()
                self._engine._fault(BackupFault.BEFORE_PROMOTION)
                stage.promote(
                    pre_rename=lambda: _assert_valid_backup_root(
                        stage_root,
                        expected_root_identity=stage_identity,
                    )
                )
                self._engine._fault(BackupFault.AFTER_PROMOTION)
        except StagingError as error:
            raise ValueError("backup staging failed") from error
        return _receipt(manifest, status="created")


def _validate_backup_files(
    root: Path,
    root_identity: RootIdentity,
    manifest: dict[str, object],
) -> None:
    for entry in cast(list[dict[str, object]], manifest["files"]):
        path = cast(str, entry["path"])
        payload = read_confined(
            root=root,
            relative=path,
            expected_root_identity=root_identity,
        )
        if payload is None:
            raise ValueError("backup file is unavailable")
        digest = sha256(payload).hexdigest()
        if digest != cast(str, entry["sha256"]) or len(payload) != cast(int, entry["size_bytes"]):
            raise ValueError("backup file failed integrity verification")


def _assert_valid_backup_root(
    root: Path,
    *,
    expected_root_identity: RootIdentity,
) -> None:
    _validate_backup_root(root, expected_root_identity=expected_root_identity)


def _restore_path(path: str) -> str | None:
    if path.startswith("portable/"):
        return path.removeprefix("portable/")
    if path == "sqlite/phase1.sqlite3":
        return ".open-brain/state/phase1.sqlite3"
    if path.startswith("app-state/"):
        return ".open-brain/state/" + path.removeprefix("app-state/")
    return None


def _restore_marker(manifest: dict[str, object]) -> dict[str, object]:
    return {
        "backup_id": manifest["backup_id"],
        "manifest_digest_sha256": _manifest_digest(manifest),
        "schema_version": 1,
    }


def _restored_destination_matches(
    destination: Path,
    *,
    destination_identity: RootIdentity,
    manifest: dict[str, object],
    entries: tuple[BackupSourceEntry, ...],
) -> bool:
    payload = read_confined(
        root=destination,
        relative=_RESTORE_RECEIPT,
        expected_root_identity=destination_identity,
        maximum_bytes=1024,
    )
    if payload is None:
        return False
    try:
        marker = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    expected = _restore_marker(manifest)
    if marker != expected or canonical_json_bytes(marker) != payload:
        return False
    _validate_restored_destination(
        destination,
        destination_identity=destination_identity,
        manifest=manifest,
        entries=entries,
    )
    return True


def _validate_restored_destination(
    destination: Path,
    *,
    destination_identity: RootIdentity,
    manifest: dict[str, object],
    entries: tuple[BackupSourceEntry, ...],
) -> None:
    portable: dict[str, bytes] = {}
    for entry in entries:
        mapped = _restore_path(entry.path)
        if mapped is None:
            continue
        payload = read_confined(
            root=destination,
            relative=mapped,
            expected_root_identity=destination_identity,
            maximum_bytes=max(1, len(entry.payload) + 1),
        )
        if payload != entry.payload:
            raise ValueError("restored backup bytes do not match the verified source")
        if entry.path.startswith("portable/"):
            portable[entry.path.removeprefix("portable/")] = payload
        elif entry.path == "sqlite/phase1.sqlite3":
            validate_sqlite_backup_bytes(payload)
    validate_portable_file_set(portable, tenant_id=cast(str, manifest["tenant_id"]))
    marker = read_confined(
        root=destination,
        relative=_RESTORE_RECEIPT,
        expected_root_identity=destination_identity,
        maximum_bytes=1024,
    )
    if marker != canonical_json_bytes(_restore_marker(manifest)):
        raise ValueError("restored backup receipt is invalid")


def _require_empty_disposable_root(destination: Path) -> None:
    if not isinstance(destination, Path) or not destination.is_absolute():
        raise ValueError("backup restore target must be an empty disposable root")
    metadata = destination.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("backup restore target must be an empty disposable root")
    if any(destination.iterdir()):
        raise ValueError("backup restore target must be an empty disposable root")

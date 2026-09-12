"""Read-only production SQLite backup probes using the SQLite snapshot API."""

from __future__ import annotations

import sqlite3
import stat
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path, PurePosixPath

from open_brain_engine.core.ids import canonical_json_bytes
from open_brain_engine.core.ports import Clock

from open_brain_legacy.operations.backup import (
    BackupObject,
    BackupStore,
    get_backup_job,
)

_MAX_DATABASES = 128
_MAX_DATABASE_BYTES = 4 * 1024 * 1024 * 1024
_SQLITE_HEADER = b"SQLite format 3\x00"


class SQLiteBackupProbeError(RuntimeError):
    """A local SQLite source could not be safely snapshotted and verified."""


@dataclass(frozen=True, slots=True)
class SQLiteBackupProbeResult:
    database_count: int
    object_count: int
    manifest_set_digest_sha256: str


@dataclass(frozen=True, slots=True)
class _SQLiteSnapshotSource:
    path: Path = field(repr=False)

    def snapshot_via_api(self) -> bytes:
        metadata = self.path.lstat()
        if (
            self.path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or not 0 < metadata.st_size <= _MAX_DATABASE_BYTES
        ):
            raise SQLiteBackupProbeError("invalid SQLite backup source")
        source: sqlite3.Connection | None = None
        destination: sqlite3.Connection | None = None
        try:
            source = sqlite3.connect(
                self.path.as_uri() + "?mode=ro",
                uri=True,
                timeout=5.0,
            )
            source.execute("PRAGMA query_only=ON")
            destination = sqlite3.connect(":memory:")
            source.backup(destination, pages=256, sleep=0.01)
            if destination.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise SQLiteBackupProbeError("SQLite backup verification failed")
            payload = destination.serialize()
        except SQLiteBackupProbeError:
            raise
        except (OSError, sqlite3.Error) as error:
            raise SQLiteBackupProbeError("SQLite backup probe failed") from error
        finally:
            if destination is not None:
                destination.close()
            if source is not None:
                source.close()
        if not payload.startswith(_SQLITE_HEADER) or len(payload) > _MAX_DATABASE_BYTES:
            raise SQLiteBackupProbeError("SQLite backup verification failed")
        return payload


class _MemoryBackupStore(BackupStore):
    def __init__(self) -> None:
        self.manifests: dict[str, bytes] = {}
        self.objects: dict[tuple[str, PurePosixPath], bytes] = {}

    def stage_objects(
        self,
        *,
        backup_id: str,
        objects: tuple[BackupObject, ...],
    ) -> None:
        for item in objects:
            key = (backup_id, item.relative_path)
            existing = self.objects.get(key)
            if existing is not None and existing != item.payload:
                raise SQLiteBackupProbeError("SQLite backup probe conflict")
            self.objects[key] = item.payload

    def publish_manifest(self, *, backup_id: str, manifest: bytes) -> None:
        existing = self.manifests.get(backup_id)
        if existing is not None and existing != manifest:
            raise SQLiteBackupProbeError("SQLite backup probe conflict")
        self.manifests[backup_id] = manifest

    def read_manifest(self, *, backup_id: str) -> bytes:
        try:
            return self.manifests[backup_id]
        except KeyError:
            raise SQLiteBackupProbeError("SQLite backup probe incomplete") from None

    def read_object(self, *, backup_id: str, relative_path: PurePosixPath) -> bytes:
        try:
            return self.objects[(backup_id, relative_path)]
        except KeyError:
            raise SQLiteBackupProbeError("SQLite backup probe incomplete") from None


def probe_local_sqlite_backups(
    *,
    state_root: Path,
    clock: Clock,
) -> SQLiteBackupProbeResult:
    """Snapshot every confined state database into memory and verify its backup manifest."""

    root = _validated_root(state_root)
    if not callable(getattr(clock, "now", None)):
        raise SQLiteBackupProbeError("invalid SQLite backup probe")
    database_paths: list[Path] = []
    try:
        for path in root.rglob("*"):
            if path.is_symlink():
                raise SQLiteBackupProbeError("invalid SQLite backup source")
            if path.is_file() and path.name.endswith(".sqlite3"):
                database_paths.append(path)
                if len(database_paths) > _MAX_DATABASES:
                    raise SQLiteBackupProbeError("too many SQLite backup sources")
    except SQLiteBackupProbeError:
        raise
    except OSError as error:
        raise SQLiteBackupProbeError("SQLite backup discovery failed") from error
    if not database_paths:
        raise SQLiteBackupProbeError("SQLite backup source unavailable")

    created_at = clock.now()
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise SQLiteBackupProbeError("invalid SQLite backup probe clock")
    job = get_backup_job("JOB-004")
    identities: list[dict[str, object]] = []
    object_count = 0
    for path in sorted(database_paths, key=lambda item: item.relative_to(root).as_posix()):
        store = _MemoryBackupStore()
        try:
            receipt = job.run_sqlite(
                source=_SQLiteSnapshotSource(path),
                store=store,
                created_at=created_at,
            )
            manifest = store.read_manifest(backup_id=receipt.backup_id)
            if sha256(manifest).hexdigest() != receipt.manifest_digest_sha256:
                raise SQLiteBackupProbeError("SQLite backup manifest verification failed")
            for entry in receipt.entries:
                payload = store.read_object(
                    backup_id=receipt.backup_id,
                    relative_path=entry.relative_path,
                )
                if (
                    len(payload) != entry.size_bytes
                    or sha256(payload).hexdigest() != entry.digest_sha256
                ):
                    raise SQLiteBackupProbeError("SQLite backup object verification failed")
        except SQLiteBackupProbeError:
            raise
        except Exception as error:
            raise SQLiteBackupProbeError("SQLite backup probe failed") from error
        object_count += receipt.object_count
        identities.append(
            {
                "manifest_digest_sha256": receipt.manifest_digest_sha256,
                "object_count": receipt.object_count,
                "source_ref_sha256": sha256(
                    path.relative_to(root).as_posix().encode("utf-8")
                ).hexdigest(),
            }
        )
    return SQLiteBackupProbeResult(
        database_count=len(database_paths),
        object_count=object_count,
        manifest_set_digest_sha256=sha256(canonical_json_bytes(identities)).hexdigest(),
    )


def _validated_root(path: Path) -> Path:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except (AttributeError, OSError) as error:
        raise SQLiteBackupProbeError("invalid SQLite backup root") from error
    if (
        not isinstance(path, Path)
        or not path.is_absolute()
        or path.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or resolved != path
    ):
        raise SQLiteBackupProbeError("invalid SQLite backup root")
    return resolved


__all__ = [
    "SQLiteBackupProbeError",
    "SQLiteBackupProbeResult",
    "probe_local_sqlite_backups",
]

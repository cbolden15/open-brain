from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from pathlib import PurePosixPath
from typing import Protocol


class BackupError(RuntimeError):
    """Backup input or durable evidence violated the operations contract."""


class SQLiteSnapshotSource(Protocol):
    def snapshot_via_api(self) -> bytes: ...


class BackupSource(Protocol):
    def collect(self) -> tuple[BackupSourceObject, ...]: ...


class BackupStore(Protocol):
    def stage_objects(
        self,
        *,
        backup_id: str,
        objects: tuple[BackupObject, ...],
    ) -> None: ...

    def publish_manifest(self, *, backup_id: str, manifest: bytes) -> None: ...

    def read_manifest(self, *, backup_id: str) -> bytes: ...

    def read_object(self, *, backup_id: str, relative_path: PurePosixPath) -> bytes: ...


class BackupTier(StrEnum):
    CAPTURE = "capture"
    LOCAL_SQLITE = "local-sqlite"
    PERSONAL = "personal"
    RUNTIME_STATE = "runtime-state"
    SAVED_CONTENT = "saved-content"
    WORK = "work"


@dataclass(frozen=True, slots=True)
class BackupSourceObject:
    relative_path: PurePosixPath
    payload: bytes
    tier: BackupTier


@dataclass(frozen=True, slots=True)
class BackupObject:
    relative_path: PurePosixPath
    payload: bytes
    tier: BackupTier


@dataclass(frozen=True, slots=True)
class BackupManifestEntry:
    relative_path: PurePosixPath
    digest_sha256: str
    size_bytes: int
    tier: BackupTier


@dataclass(frozen=True, slots=True)
class RetentionMetadata:
    keep_daily: int
    keep_weekly: int
    keep_monthly: int

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 1
            for value in (self.keep_daily, self.keep_weekly, self.keep_monthly)
        ):
            raise BackupError("invalid retention metadata")

    def to_dict(self) -> dict[str, int]:
        return {
            "keep_daily": self.keep_daily,
            "keep_monthly": self.keep_monthly,
            "keep_weekly": self.keep_weekly,
        }


@dataclass(frozen=True, slots=True)
class BackupProfile:
    name: str
    included_tiers: frozenset[BackupTier]
    excluded_prefixes: tuple[PurePosixPath, ...]
    retention: RetentionMetadata


@dataclass(frozen=True, slots=True)
class BackupReceipt:
    backup_id: str
    created_at: datetime
    profile: str
    retention: RetentionMetadata
    generation: str | None
    entries: tuple[BackupManifestEntry, ...]
    manifest_digest_sha256: str

    @property
    def object_count(self) -> int:
        return len(self.entries)

    def manifest_bytes(self) -> bytes:
        return _manifest_bytes(
            backup_id=self.backup_id,
            created_at=self.created_at,
            profile=self.profile,
            retention=self.retention,
            generation=self.generation,
            entries=self.entries,
        )


@dataclass(frozen=True, slots=True)
class PreparedBackup:
    """One immutable source snapshot ready for durable publication."""

    receipt: BackupReceipt
    objects: tuple[BackupObject, ...]


@dataclass(frozen=True, slots=True)
class BackupJob:
    job_id: str
    profile: BackupProfile

    def run_sqlite(
        self,
        *,
        source: SQLiteSnapshotSource,
        store: BackupStore,
        created_at: datetime,
    ) -> BackupReceipt:
        if self.job_id != "JOB-004":
            raise BackupError("backup job does not accept a SQLite source")
        _validate_created_at(created_at)
        payload = source.snapshot_via_api()
        if not isinstance(payload, bytes):
            raise BackupError("SQLite snapshot must be bytes")
        objects = (
            BackupObject(
                PurePosixPath("sqlite/snapshot.db"), payload, BackupTier.LOCAL_SQLITE
            ),
        )
        return _save_backup(
            profile=self.profile.name,
            retention=self.profile.retention,
            generation=None,
            objects=objects,
            store=store,
            created_at=created_at,
        )

    def run(
        self,
        *,
        source: BackupSource,
        store: BackupStore,
        created_at: datetime,
        generation: str | None = None,
    ) -> BackupReceipt:
        prepared = self.prepare(
            source=source,
            created_at=created_at,
            generation=generation,
        )
        publish_prepared_backup(prepared=prepared, store=store)
        return prepared.receipt

    def prepare(
        self,
        *,
        source: BackupSource,
        created_at: datetime,
        generation: str | None = None,
    ) -> PreparedBackup:
        """Freeze one exact source inventory without publishing it."""
        if self.job_id == "JOB-004":
            raise BackupError("SQLite backup requires the snapshot API")
        _validate_created_at(created_at)
        if self.profile.name == "runtime-state":
            if not isinstance(generation, str) or _GENERATION.fullmatch(generation) is None:
                raise BackupError("runtime-state backup requires a generation")
        elif generation is not None:
            raise BackupError("backup profile does not accept a generation")
        candidates = source.collect()
        if not isinstance(candidates, tuple):
            raise BackupError("backup source must return an immutable snapshot")
        selected: list[BackupObject] = []
        seen: set[PurePosixPath] = set()
        for candidate in candidates:
            if not isinstance(candidate, BackupSourceObject):
                raise BackupError("invalid backup source object")
            relative = _safe_relative(candidate.relative_path)
            if relative in seen:
                raise BackupError("duplicate backup object path")
            seen.add(relative)
            if candidate.tier not in self.profile.included_tiers or _is_excluded(
                relative, self.profile.excluded_prefixes
            ):
                continue
            if not isinstance(candidate.payload, bytes):
                raise BackupError("backup object payload must be bytes")
            selected.append(BackupObject(relative, candidate.payload, candidate.tier))
        objects = tuple(sorted(selected, key=lambda item: str(item.relative_path)))
        return _prepare_backup(
            profile=self.profile.name,
            retention=self.profile.retention,
            generation=generation,
            objects=objects,
            created_at=created_at,
        )


_LOCAL_RETENTION = RetentionMetadata(keep_daily=7, keep_weekly=4, keep_monthly=3)
_CAPTURE_RETENTION = RetentionMetadata(keep_daily=14, keep_weekly=8, keep_monthly=12)
_FULL_RETENTION = RetentionMetadata(keep_daily=7, keep_weekly=8, keep_monthly=12)
_PERSONAL_RETENTION = RetentionMetadata(keep_daily=14, keep_weekly=8, keep_monthly=12)
_RUNTIME_STATE_RETENTION = RetentionMetadata(
    keep_daily=14,
    keep_weekly=8,
    keep_monthly=6,
)
_LOCAL_SQLITE_PROFILE = BackupProfile(
    name="local-sqlite",
    included_tiers=frozenset({BackupTier.LOCAL_SQLITE}),
    excluded_prefixes=(PurePosixPath("sqlite/live"),),
    retention=_LOCAL_RETENTION,
)
_CAPTURE_PROFILE = BackupProfile(
    name="capture",
    included_tiers=frozenset({BackupTier.CAPTURE}),
    excluded_prefixes=(
        PurePosixPath("capture/secrets"),
        PurePosixPath("capture/transient"),
    ),
    retention=_CAPTURE_RETENTION,
)
_FULL_PROFILE = BackupProfile(
    name="full",
    included_tiers=frozenset(
        {
            BackupTier.CAPTURE,
            BackupTier.PERSONAL,
            BackupTier.RUNTIME_STATE,
            BackupTier.SAVED_CONTENT,
            BackupTier.WORK,
        }
    ),
    excluded_prefixes=(
        PurePosixPath("cache"),
        PurePosixPath("capture/secrets"),
        PurePosixPath("capture/transient"),
        PurePosixPath("personal/cache"),
        PurePosixPath("personal/secrets"),
        PurePosixPath("runtime/locks"),
        PurePosixPath("secrets"),
        PurePosixPath("tmp"),
    ),
    retention=_FULL_RETENTION,
)
_PERSONAL_PROFILE = BackupProfile(
    name="personal",
    included_tiers=frozenset({BackupTier.PERSONAL}),
    excluded_prefixes=(
        PurePosixPath("personal/cache"),
        PurePosixPath("personal/secrets"),
    ),
    retention=_PERSONAL_RETENTION,
)
_RUNTIME_STATE_PROFILE = BackupProfile(
    name="runtime-state",
    included_tiers=frozenset({BackupTier.RUNTIME_STATE}),
    excluded_prefixes=(PurePosixPath("runtime/locks"),),
    retention=_RUNTIME_STATE_RETENTION,
)
_BACKUP_JOBS = {
    "JOB-004": BackupJob("JOB-004", _LOCAL_SQLITE_PROFILE),
    "JOB-011": BackupJob("JOB-011", _CAPTURE_PROFILE),
    "JOB-014": BackupJob("JOB-014", _FULL_PROFILE),
    "JOB-023": BackupJob("JOB-023", _PERSONAL_PROFILE),
    "JOB-025": BackupJob("JOB-025", _RUNTIME_STATE_PROFILE),
}

_GENERATION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")


def get_backup_job(job_id: str) -> BackupJob:
    try:
        return _BACKUP_JOBS[job_id]
    except KeyError:
        raise BackupError("unknown backup job") from None


def _save_backup(
    *,
    profile: str,
    retention: RetentionMetadata,
    generation: str | None,
    objects: tuple[BackupObject, ...],
    store: BackupStore,
    created_at: datetime,
) -> BackupReceipt:
    prepared = _prepare_backup(
        profile=profile,
        retention=retention,
        generation=generation,
        objects=objects,
        created_at=created_at,
    )
    publish_prepared_backup(prepared=prepared, store=store)
    return prepared.receipt


def _prepare_backup(
    *,
    profile: str,
    retention: RetentionMetadata,
    generation: str | None,
    objects: tuple[BackupObject, ...],
    created_at: datetime,
) -> PreparedBackup:
    entries = tuple(
        BackupManifestEntry(
            relative_path=item.relative_path,
            digest_sha256=sha256(item.payload).hexdigest(),
            size_bytes=len(item.payload),
            tier=item.tier,
        )
        for item in objects
    )
    identity = _backup_identity(
        created_at=created_at,
        entries=entries,
        generation=generation,
        profile=profile,
    )
    backup_id = "backup-" + sha256(identity).hexdigest()[:24]
    provisional = BackupReceipt(
        backup_id=backup_id,
        created_at=created_at,
        profile=profile,
        retention=retention,
        generation=generation,
        entries=entries,
        manifest_digest_sha256="",
    )
    manifest = provisional.manifest_bytes()
    receipt = BackupReceipt(
        backup_id=backup_id,
        created_at=created_at,
        profile=profile,
        retention=retention,
        generation=generation,
        entries=entries,
        manifest_digest_sha256=sha256(manifest).hexdigest(),
    )
    return PreparedBackup(receipt=receipt, objects=objects)


def publish_prepared_backup(*, prepared: PreparedBackup, store: BackupStore) -> None:
    """Publish a previously frozen backup without consulting mutable sources."""
    if not isinstance(prepared, PreparedBackup):
        raise BackupError("invalid prepared backup")
    receipt = prepared.receipt
    objects = prepared.objects
    manifest = receipt.manifest_bytes()
    if sha256(manifest).hexdigest() != receipt.manifest_digest_sha256:
        raise BackupError("invalid prepared backup")
    store.stage_objects(backup_id=receipt.backup_id, objects=objects)
    try:
        for item in objects:
            if (
                store.read_object(
                    backup_id=receipt.backup_id,
                    relative_path=item.relative_path,
                )
                != item.payload
            ):
                raise BackupError("backup store verification failed")
    except BackupError:
        raise
    except Exception:
        raise BackupError("backup store verification failed") from None
    store.publish_manifest(backup_id=receipt.backup_id, manifest=manifest)
    try:
        if store.read_manifest(backup_id=receipt.backup_id) != manifest:
            raise BackupError("backup store verification failed")
    except BackupError:
        raise
    except Exception:
        raise BackupError("backup store verification failed") from None


def parse_backup_manifest(payload: bytes) -> BackupReceipt:
    """Parse and canonically validate one complete backup manifest."""
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
        if type(value) is not dict or frozenset(value) != {
            "backup_id",
            "created_at",
            "entries",
            "generation",
            "profile",
            "retention",
            "schema_version",
        }:
            raise BackupError("invalid backup manifest")
        if value["schema_version"] != 1:
            raise BackupError("invalid backup manifest")
        profile = value["profile"]
        if not isinstance(profile, str):
            raise BackupError("invalid backup manifest")
        job = next((job for job in _BACKUP_JOBS.values() if job.profile.name == profile), None)
        if job is None:
            raise BackupError("invalid backup manifest")
        raw_retention = value["retention"]
        if type(raw_retention) is not dict or frozenset(raw_retention) != {
            "keep_daily",
            "keep_monthly",
            "keep_weekly",
        }:
            raise BackupError("invalid backup manifest")
        retention = RetentionMetadata(
            keep_daily=raw_retention["keep_daily"],
            keep_weekly=raw_retention["keep_weekly"],
            keep_monthly=raw_retention["keep_monthly"],
        )
        if retention != job.profile.retention:
            raise BackupError("invalid backup manifest")
        created_at = _parse_timestamp(value["created_at"])
        generation = value["generation"]
        if profile == "runtime-state":
            if not isinstance(generation, str) or _GENERATION.fullmatch(generation) is None:
                raise BackupError("invalid backup manifest")
        elif generation is not None:
            raise BackupError("invalid backup manifest")
        raw_entries = value["entries"]
        if type(raw_entries) is not list:
            raise BackupError("invalid backup manifest")
        entries: list[BackupManifestEntry] = []
        for raw in raw_entries:
            if type(raw) is not dict or frozenset(raw) != {
                "digest_sha256",
                "relative_path",
                "size_bytes",
                "tier",
            }:
                raise BackupError("invalid backup manifest")
            relative = _safe_relative(PurePosixPath(raw["relative_path"]))
            digest = raw["digest_sha256"]
            size = raw["size_bytes"]
            tier = BackupTier(raw["tier"])
            if (
                not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                or type(size) is not int
                or size < 0
                or tier not in job.profile.included_tiers
                or _is_excluded(relative, job.profile.excluded_prefixes)
            ):
                raise BackupError("invalid backup manifest")
            entries.append(BackupManifestEntry(relative, digest, size, tier))
        entry_tuple = tuple(entries)
        sorted_entries = tuple(
            sorted(entry_tuple, key=lambda item: str(item.relative_path))
        )
        if entry_tuple != sorted_entries or len(
            {entry.relative_path for entry in entry_tuple}
        ) != len(entry_tuple):
            raise BackupError("invalid backup manifest")
        identity = _backup_identity(
            created_at=created_at,
            entries=entry_tuple,
            generation=generation,
            profile=profile,
        )
        backup_id = "backup-" + sha256(identity).hexdigest()[:24]
        if value["backup_id"] != backup_id:
            raise BackupError("invalid backup manifest")
        receipt = BackupReceipt(
            backup_id=backup_id,
            created_at=created_at,
            profile=profile,
            retention=retention,
            generation=generation,
            entries=entry_tuple,
            manifest_digest_sha256=sha256(payload).hexdigest(),
        )
    except BackupError:
        raise
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        raise BackupError("invalid backup manifest") from None
    if receipt.manifest_bytes() != payload:
        raise BackupError("invalid backup manifest")
    return receipt


def _backup_identity(
    *,
    created_at: datetime,
    entries: tuple[BackupManifestEntry, ...],
    generation: str | None,
    profile: str,
) -> bytes:
    return json.dumps(
        {
            "created_at": _timestamp(created_at),
            "entries": [
                [str(entry.relative_path), entry.digest_sha256, entry.tier.value]
                for entry in entries
            ],
            "generation": generation,
            "profile": profile,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _manifest_bytes(
    *,
    backup_id: str,
    created_at: datetime,
    profile: str,
    retention: RetentionMetadata,
    generation: str | None,
    entries: tuple[BackupManifestEntry, ...],
) -> bytes:
    value = {
        "backup_id": backup_id,
        "created_at": _timestamp(created_at),
        "entries": [
            {
                "digest_sha256": entry.digest_sha256,
                "relative_path": str(entry.relative_path),
                "size_bytes": entry.size_bytes,
                "tier": entry.tier.value,
            }
            for entry in entries
        ],
        "generation": generation,
        "profile": profile,
        "retention": retention.to_dict(),
        "schema_version": 1,
    }
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _validate_created_at(created_at: datetime) -> None:
    if not isinstance(created_at, datetime) or created_at.tzinfo is None:
        raise BackupError("backup timestamp must be timezone-aware")


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise BackupError("invalid backup manifest")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise BackupError("invalid backup manifest") from None
    if _timestamp(parsed) != value:
        raise BackupError("invalid backup manifest")
    return parsed.astimezone(UTC)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise BackupError("invalid backup manifest")
        value[key] = item
    return value


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _safe_relative(value: PurePosixPath) -> PurePosixPath:
    if (
        not isinstance(value, PurePosixPath)
        or value.is_absolute()
        or not value.parts
        or any(part in {"", ".", ".."} or "\\" in part for part in value.parts)
    ):
        raise BackupError("unsafe backup object path")
    return value


def _is_excluded(
    relative: PurePosixPath, excluded_prefixes: tuple[PurePosixPath, ...]
) -> bool:
    return any(relative == prefix or prefix in relative.parents for prefix in excluded_prefixes)

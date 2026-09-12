from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath

from .backup import BackupError, BackupReceipt, BackupStore


@dataclass(frozen=True, slots=True)
class DisposableRestoreRoot:
    path: Path

    @classmethod
    def create(cls, path: Path) -> DisposableRestoreRoot:
        if (
            not isinstance(path, Path)
            or not path.is_absolute()
            or not path.is_dir()
            or path.is_symlink()
            or any(path.iterdir())
        ):
            raise BackupError("restore target must be an empty disposable root")
        return cls(path)


@dataclass(frozen=True, slots=True)
class RestoreEvidence:
    backup_id: str
    checksums_verified: int
    generation: str | None
    manifest_digest_sha256: str
    object_count: int
    profile: str
    restored: bool

    def to_redacted_dict(self) -> dict[str, object]:
        return {
            "backup_id": self.backup_id,
            "checksums_verified": self.checksums_verified,
            "generation": self.generation,
            "manifest_digest_sha256": self.manifest_digest_sha256,
            "object_count": self.object_count,
            "profile": self.profile,
            "restored": self.restored,
        }


def restore_backup(
    receipt: BackupReceipt,
    *,
    store: BackupStore,
    target: DisposableRestoreRoot,
) -> RestoreEvidence:
    if not isinstance(receipt, BackupReceipt) or not isinstance(target, DisposableRestoreRoot):
        raise BackupError("invalid restore request")
    manifest = store.read_manifest(backup_id=receipt.backup_id)
    if (
        not isinstance(manifest, bytes)
        or sha256(manifest).hexdigest() != receipt.manifest_digest_sha256
        or manifest != receipt.manifest_bytes()
    ):
        raise BackupError("backup manifest verification failed")

    payloads: list[tuple[PurePosixPath, bytes]] = []
    for entry in receipt.entries:
        relative = _safe_relative(entry.relative_path)
        payload = store.read_object(backup_id=receipt.backup_id, relative_path=relative)
        if (
            not isinstance(payload, bytes)
            or len(payload) != entry.size_bytes
            or sha256(payload).hexdigest() != entry.digest_sha256
        ):
            raise BackupError("backup object verification failed")
        payloads.append((relative, payload))

    for relative, payload in payloads:
        destination = target.path.joinpath(*relative.parts)
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination.write_bytes(payload)
        destination.chmod(0o600)

    return RestoreEvidence(
        backup_id=receipt.backup_id,
        checksums_verified=1 + len(payloads),
        generation=receipt.generation,
        manifest_digest_sha256=receipt.manifest_digest_sha256,
        object_count=len(payloads),
        profile=receipt.profile,
        restored=True,
    )


def _safe_relative(value: PurePosixPath) -> PurePosixPath:
    if (
        not isinstance(value, PurePosixPath)
        or value.is_absolute()
        or not value.parts
        or any(part in {"", ".", ".."} or "\\" in part for part in value.parts)
    ):
        raise BackupError("unsafe backup object path")
    return value

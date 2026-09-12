from __future__ import annotations

import json
import os
import secrets
import stat
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path, PurePosixPath

from open_brain_engine.storage.filesystem import RootConfinementError

from ._models import (
    BackupEntry,
    BackupReceipt,
    MigrationError,
    RestoreReceipt,
)


def safe_relative(raw: str | PurePosixPath) -> PurePosixPath:
    text = str(raw)
    parts = text.split("/")
    if (
        not text
        or text.startswith("/")
        or "\\" in text
        or "\x00" in text
        or any(
            not part
            or part in {".", ".."}
            or any(unicodedata.category(character) == "Cc" for character in part)
            for part in parts
        )
    ):
        raise RootConfinementError("unsafe migration path")
    path = PurePosixPath(text)
    if path.is_absolute():
        raise RootConfinementError("unsafe migration path")
    return path


def validate_root(root: Path) -> None:
    descriptor = _open_root(root)
    os.close(descriptor)


def walk_markdown(root: Path) -> tuple[PurePosixPath, ...]:
    found: list[PurePosixPath] = []
    root_fd = _open_root(root)

    def visit(directory_fd: int, prefix: PurePosixPath | None) -> None:
        try:
            entries = sorted(os.scandir(directory_fd), key=lambda entry: entry.name)
        except OSError:
            raise RootConfinementError("unsafe migration directory") from None
        for entry in entries:
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError:
                raise RootConfinementError("unsafe migration entry") from None
            if stat.S_ISLNK(metadata.st_mode):
                raise RootConfinementError("migration refuses symlink")
            relative = PurePosixPath(entry.name) if prefix is None else prefix / entry.name
            if stat.S_ISDIR(metadata.st_mode):
                if not entry.name.startswith("."):
                    child_fd = _open_directory_at(directory_fd, entry.name)
                    try:
                        visit(child_fd, relative)
                    finally:
                        os.close(child_fd)
            elif stat.S_ISREG(metadata.st_mode) and entry.name.endswith(".md"):
                found.append(relative)
            elif not stat.S_ISREG(metadata.st_mode):
                raise RootConfinementError("unsafe migration entry")

    try:
        visit(root_fd, None)
    finally:
        os.close(root_fd)
    return tuple(found)


def read_file(root: Path, relative: str | PurePosixPath) -> bytes | None:
    safe = safe_relative(relative)
    descriptor = -1
    try:
        with _open_parent(root, safe, create=False) as (parent_fd, name):
            descriptor = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise RootConfinementError("unsafe migration file")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 65_536)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
    except FileNotFoundError:
        return None
    except RootConfinementError:
        raise
    except OSError:
        raise MigrationError("migration read failed") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def replace_file(
    root: Path,
    relative: str | PurePosixPath,
    payload: bytes,
    *,
    require_existing: bool | None,
) -> None:
    safe = safe_relative(relative)
    temp_name = ".migration-" + secrets.token_hex(16) + ".tmp"
    descriptor = -1
    try:
        with _open_parent(root, safe, create=True) as (parent_fd, name):
            existing = _regular_entry_exists(parent_fd, name)
            if require_existing is True and not existing:
                raise MigrationError("migration target disappeared")
            if require_existing is False and existing:
                raise MigrationError("migration target already exists")
            descriptor = os.open(
                temp_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=parent_fd,
            )
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temp_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.fsync(parent_fd)
            temp_name = ""
    except RootConfinementError:
        raise
    except MigrationError:
        raise
    except OSError:
        raise MigrationError("migration write failed") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temp_name:
            try:
                with _open_parent(root, safe, create=False) as (parent_fd, _name):
                    os.unlink(temp_name, dir_fd=parent_fd)
            except (FileNotFoundError, OSError, RootConfinementError):
                pass


def move_file(root: Path, source: PurePosixPath, target: PurePosixPath) -> None:
    try:
        with _open_parent(root, safe_relative(source), create=False) as (
            source_parent_fd,
            source_name,
        ), _open_parent(root, safe_relative(target), create=True) as (
            target_parent_fd,
            target_name,
        ):
            if not _regular_entry_exists(source_parent_fd, source_name):
                raise MigrationError("migration source disappeared")
            if _regular_entry_exists(target_parent_fd, target_name):
                raise MigrationError("migration target already exists")
            os.replace(
                source_name,
                target_name,
                src_dir_fd=source_parent_fd,
                dst_dir_fd=target_parent_fd,
            )
            os.fsync(source_parent_fd)
            if target_parent_fd != source_parent_fd:
                os.fsync(target_parent_fd)
    except (RootConfinementError, MigrationError):
        raise
    except OSError:
        raise MigrationError("migration move failed") from None


def delete_file(root: Path, relative: PurePosixPath) -> bool:
    try:
        with _open_parent(root, safe_relative(relative), create=False) as (parent_fd, name):
            if not _regular_entry_exists(parent_fd, name):
                return False
            os.unlink(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
    except FileNotFoundError:
        return False
    except RootConfinementError:
        raise
    except OSError:
        raise MigrationError("migration delete failed") from None
    return True


def create_backup(
    *,
    target_root: Path,
    backup_root: Path,
    relatives: tuple[PurePosixPath, ...],
) -> BackupReceipt:
    validate_root(target_root)
    validate_root(backup_root)
    roots_overlap = (
        target_root == backup_root
        or target_root in backup_root.parents
        or backup_root in target_root.parents
    )
    if roots_overlap:
        raise RootConfinementError("backup root must be separate")
    backup_id = "backup-" + secrets.token_hex(12)
    receipt_root = backup_root / backup_id
    try:
        backup_root_fd = _open_root(backup_root)
        try:
            os.mkdir(backup_id, mode=0o700, dir_fd=backup_root_fd)
            os.fsync(backup_root_fd)
        finally:
            os.close(backup_root_fd)
    except OSError:
        raise MigrationError("backup creation failed") from None
    entries: list[BackupEntry] = []
    for index, relative in enumerate(sorted(set(relatives))):
        payload = read_file(target_root, relative)
        if payload is None:
            entries.append(BackupEntry(relative, False, None, None))
            continue
        backup_relative = PurePosixPath(f"files/{index:06d}.bin")
        replace_file(receipt_root, backup_relative, payload, require_existing=False)
        entries.append(
            BackupEntry(relative, True, backup_relative, sha256(payload).hexdigest())
        )
    manifest = _manifest_bytes(backup_id, tuple(entries))
    replace_file(
        receipt_root,
        PurePosixPath("manifest.json"),
        manifest,
        require_existing=False,
    )
    return BackupReceipt(
        backup_id=backup_id,
        backup_root=backup_root,
        target_root=target_root,
        entries=tuple(entries),
        manifest_digest=sha256(manifest).hexdigest(),
    )


def restore_backup(receipt: BackupReceipt, *, target_root: Path) -> RestoreReceipt:
    if not isinstance(receipt, BackupReceipt) or target_root != receipt.target_root:
        raise MigrationError("invalid backup receipt")
    restored_payloads = _verified_backup_payloads(receipt)
    restored = 0
    removed = 0
    for entry in receipt.entries:
        if entry.existed:
            replace_file(
                target_root,
                entry.relative,
                restored_payloads[entry.relative],
                require_existing=None,
            )
            restored += 1
        elif delete_file(target_root, entry.relative):
            removed += 1
    return RestoreReceipt(receipt.backup_id, receipt.manifest_digest, restored, removed)


def restore_backup_copy(receipt: BackupReceipt, *, target_root: Path) -> RestoreReceipt:
    """Restore original files into a separate empty verification root."""
    if not isinstance(receipt, BackupReceipt):
        raise MigrationError("invalid backup receipt")
    validate_root(target_root)
    if any(target_root.iterdir()):
        raise MigrationError("verification restore root must be empty")
    canonical_target = target_root.resolve(strict=True)
    canonical_original = receipt.target_root.resolve(strict=True)
    canonical_backup = receipt.backup_root.resolve(strict=True)
    target_stat = canonical_target.stat()
    original_stat = canonical_original.stat()
    backup_stat = canonical_backup.stat()
    if (
        canonical_target in (canonical_original, canonical_backup)
        or canonical_target in canonical_original.parents
        or canonical_original in canonical_target.parents
        or canonical_target in canonical_backup.parents
        or canonical_backup in canonical_target.parents
        or (target_stat.st_dev, target_stat.st_ino)
        == (original_stat.st_dev, original_stat.st_ino)
        or (target_stat.st_dev, target_stat.st_ino)
        == (backup_stat.st_dev, backup_stat.st_ino)
    ):
        raise MigrationError("verification restore root must be separate")
    restored_payloads = _verified_backup_payloads(receipt)
    restored = 0
    for entry in receipt.entries:
        if not entry.existed:
            continue
        replace_file(
            target_root,
            entry.relative,
            restored_payloads[entry.relative],
            require_existing=False,
        )
        restored += 1
    return RestoreReceipt(receipt.backup_id, receipt.manifest_digest, restored, 0)


def _verified_backup_payloads(
    receipt: BackupReceipt,
) -> dict[PurePosixPath, bytes]:
    receipt_root = receipt.backup_root / receipt.backup_id
    manifest = read_file(receipt_root, PurePosixPath("manifest.json"))
    if manifest is None or sha256(manifest).hexdigest() != receipt.manifest_digest:
        raise MigrationError("backup manifest verification failed")
    expected = _manifest_bytes(receipt.backup_id, receipt.entries)
    if manifest != expected:
        raise MigrationError("backup manifest verification failed")
    restored_payloads: dict[PurePosixPath, bytes] = {}
    for entry in receipt.entries:
        if not entry.existed:
            continue
        if entry.backup_relative is None or entry.digest_sha256 is None:
            raise MigrationError("invalid backup receipt")
        payload = read_file(receipt_root, entry.backup_relative)
        if payload is None or sha256(payload).hexdigest() != entry.digest_sha256:
            raise MigrationError("backup file verification failed")
        restored_payloads[entry.relative] = payload
    return restored_payloads


def _open_root(root: Path) -> int:
    if not isinstance(root, Path) or not root.is_absolute():
        raise RootConfinementError("unsafe migration root")
    try:
        descriptor = os.open(
            root,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError:
        raise RootConfinementError("unsafe migration root") from None
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise RootConfinementError("unsafe migration root")
    return descriptor


def _open_directory_at(parent_fd: int, name: str) -> int:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
    except FileNotFoundError:
        raise
    except OSError:
        raise RootConfinementError("unsafe migration path") from None
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise RootConfinementError("unsafe migration path")
    return descriptor


@contextmanager
def _open_parent(
    root: Path,
    relative: PurePosixPath,
    *,
    create: bool,
) -> Iterator[tuple[int, str]]:
    safe = safe_relative(relative)
    descriptors = [_open_root(root)]
    try:
        for part in safe.parts[:-1]:
            try:
                child = _open_directory_at(descriptors[-1], part)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptors[-1])
                    os.fsync(descriptors[-1])
                except FileExistsError:
                    pass
                except OSError:
                    raise MigrationError("migration directory creation failed") from None
                child = _open_directory_at(descriptors[-1], part)
            descriptors.append(child)
        yield descriptors[-1], safe.parts[-1]
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _regular_entry_exists(parent_fd: int, name: str) -> bool:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError:
        raise RootConfinementError("unsafe migration file") from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RootConfinementError("unsafe migration file")
    return True


def _manifest_bytes(backup_id: str, entries: tuple[BackupEntry, ...]) -> bytes:
    value = {
        "backup_id": backup_id,
        "entries": [
            {
                "backup_relative": (
                    str(entry.backup_relative) if entry.backup_relative is not None else None
                ),
                "digest_sha256": entry.digest_sha256,
                "existed": entry.existed,
                "relative": str(entry.relative),
            }
            for entry in entries
        ],
        "schema_version": 1,
    }
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")

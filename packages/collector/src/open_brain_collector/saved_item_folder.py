"""Descriptor-anchored reads of one selected saved-item folder.

Other writers control the folder's entry names, so names are hashed before they reach
state, notices or errors, and no entry is opened through a path that could follow a link.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from open_brain_connectors.runtime.live_common import LiveSourceError, bounded_json
from open_brain_connectors.runtime.source_registry import (
    SAVED_ITEM_SOURCE,
    SourceResourceSelection,
)

MAX_RECORD_BYTES = 131_072
MAX_RECORD_FILES = 10_000
RESOURCE_TYPE = "local_folder"
SKIPPED_SUFFIXES = (".icloud", ".tmp", ".partial")
# macOS marks cloud placeholders dataless; Linux has no st_flags, so the check is inert there.
SF_DATALESS = int(getattr(stat, "SF_DATALESS", 0x40000000))
EntryStatus = Literal["unchanged", "deferred", "refused", "read"]


def name_hash(name: str) -> str:
    """Fixed-size key for an entry name."""
    return hashlib.sha256(os.fsencode(name)).hexdigest()[:32]


def stat_fingerprint(info: os.stat_result) -> str:
    value = f"{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}"
    return hashlib.sha256(value.encode("ascii")).hexdigest()[:24]


def folder_selection(path: Path, info: os.stat_result) -> SourceResourceSelection:
    digest = hashlib.sha256(bounded_json([str(path), info.st_dev, info.st_ino])).hexdigest()
    return SourceResourceSelection(
        connector_name=SAVED_ITEM_SOURCE,
        connection_id="folder:" + digest[:32],
        resource_id="items:" + digest[32:],
        resource_type=RESOURCE_TYPE,
    )


@dataclass(frozen=True, slots=True)
class OpenFolder:
    fd: int
    path: Path
    selection: SourceResourceSelection


@contextmanager
def open_folder(path: Path) -> Iterator[OpenFolder]:
    """Open the folder once, no-follow, and check owner, mode and type on the descriptor."""
    if not isinstance(path, Path) or not path.is_absolute():
        raise LiveSourceError("folder_unsafe")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise LiveSourceError("folder_missing") from None
    except OSError:
        raise LiveSourceError("folder_unsafe") from None
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise LiveSourceError("folder_unsafe")
        yield OpenFolder(fd, path, folder_selection(path, info))
    finally:
        os.close(fd)


def select_folder(raw: str) -> tuple[SourceResourceSelection, Path]:
    """Derive the selection from a folder path the owner typed."""
    if type(raw) is not str or not raw or "\x00" in raw:
        raise LiveSourceError("folder_unsafe")
    path = Path(raw)
    if not path.is_absolute() or path.is_symlink():
        raise LiveSourceError("folder_unsafe")
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        raise LiveSourceError("folder_missing") from None
    with open_folder(resolved) as folder:
        return folder.selection, resolved


@dataclass(frozen=True, slots=True)
class FolderListing:
    records: tuple[str, ...]
    placeholders: int


def list_records(folder: OpenFolder) -> FolderListing:
    """List ``*.json`` names in name-hash order; refuse folders over the file limit."""
    records: list[str] = []
    placeholders = json_entries = 0
    with os.scandir(folder.fd) as entries:
        for entry in entries:
            name = entry.name
            if name.endswith(".json"):
                json_entries += 1
                if json_entries > MAX_RECORD_FILES:
                    raise LiveSourceError("folder_over_limit")
            if name.startswith(".") or name.endswith(SKIPPED_SUFFIXES):
                placeholders += int(".json" in name)
                continue
            if name.endswith(".json"):
                records.append(name)
    return FolderListing(tuple(sorted(records, key=name_hash)), placeholders)


@dataclass(frozen=True, slots=True)
class EntryRead:
    name_hash: str
    status: EntryStatus
    fingerprint: str = ""
    code: str = ""
    data: bytes = b""


def _entry_flags(info: os.stat_result) -> int:
    return int(getattr(info, "st_flags", 0))


def _open_entry(name: str, folder_fd: int) -> int:
    return os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder_fd)


def _identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def read_entry(folder: OpenFolder, name: str, known: str | None) -> EntryRead:
    """Read one entry through the folder descriptor without following or blocking."""
    hashed = name_hash(name)
    try:
        info = os.stat(name, dir_fd=folder.fd, follow_symlinks=False)
    except FileNotFoundError:
        return EntryRead(hashed, "deferred", code="vanished")
    except OSError:
        raise LiveSourceError("folder_unsafe") from None
    if _entry_flags(info) & SF_DATALESS:
        return EntryRead(hashed, "deferred", code="placeholder")
    fingerprint = stat_fingerprint(info)
    if fingerprint == known:
        return EntryRead(hashed, "unchanged", fingerprint)
    if not stat.S_ISREG(info.st_mode):
        return EntryRead(hashed, "refused", fingerprint, "not_regular_file")
    if info.st_size > MAX_RECORD_BYTES:
        return EntryRead(hashed, "refused", fingerprint, "file_too_large")
    try:
        fd = _open_entry(name, folder.fd)
    except FileNotFoundError:
        return EntryRead(hashed, "deferred", code="vanished")
    except OSError:
        return EntryRead(hashed, "refused", fingerprint, "not_regular_file")
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            return EntryRead(hashed, "refused", stat_fingerprint(before), "not_regular_file")
        chunks: list[bytes] = []
        remaining = MAX_RECORD_BYTES + 1
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if not _identity(info) == _identity(before) == _identity(after):
        return EntryRead(hashed, "deferred", code="unstable")
    data = b"".join(chunks)
    if len(data) > MAX_RECORD_BYTES:
        return EntryRead(hashed, "refused", stat_fingerprint(after), "file_too_large")
    return EntryRead(hashed, "read", stat_fingerprint(after), data=data)

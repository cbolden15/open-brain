from __future__ import annotations

import os
import re
import stat
from contextlib import suppress
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from open_brain_engine.core.models import RawAssetRef

from .errors import ProductionRuntimeError, RuntimeFailureCode

_DIGEST = re.compile(r"[0-9a-f]{64}")
_MEDIA_TYPE = re.compile(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+")
_READ_SIZE = 64 * 1024


@dataclass(frozen=True, slots=True)
class DerivedAssetRef:
    sha256: str
    media_type: str
    byte_length: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.sha256, str)
            or _DIGEST.fullmatch(self.sha256) is None
            or not isinstance(self.media_type, str)
            or _MEDIA_TYPE.fullmatch(self.media_type) is None
            or not isinstance(self.byte_length, int)
            or isinstance(self.byte_length, bool)
            or self.byte_length < 0
        ):
            raise ValueError("invalid derived asset reference")

    @property
    def asset_id(self) -> str:
        return "derived_" + self.sha256


class ContentAddressedDerivedAssetStore:
    """A root-confined, immutable SHA-256 object store for derived artifacts."""

    def __init__(self, *, root: Path, enabled: bool = False) -> None:
        if not isinstance(root, Path) or not root.is_absolute() or not isinstance(enabled, bool):
            raise ValueError("invalid derived asset store configuration")
        self._root = root
        self._enabled = enabled

    def put(self, *, data: bytes, media_type: str) -> DerivedAssetRef:
        self._require_enabled()
        _require_confinement_controls()
        if (
            not isinstance(data, bytes)
            or not isinstance(media_type, str)
            or _MEDIA_TYPE.fullmatch(media_type) is None
        ):
            raise ProductionRuntimeError(RuntimeFailureCode.INVALID_INPUT)
        digest = sha256(data).hexdigest()
        ref = DerivedAssetRef(digest, media_type, len(data))
        try:
            with self._object_directory(digest, create=True) as directory:
                try:
                    descriptor = os.open(
                        digest,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _no_follow(),
                        0o600,
                        dir_fd=directory,
                    )
                except FileExistsError:
                    self._read_object(directory, ref)
                    return ref
                try:
                    with os.fdopen(descriptor, "wb") as stream:
                        _write_all(stream.fileno(), data)
                        stream.flush()
                        os.fsync(stream.fileno())
                        metadata = os.fstat(stream.fileno())
                        if not _safe_regular_file(metadata) or metadata.st_size != len(data):
                            raise ProductionRuntimeError(RuntimeFailureCode.CONFINEMENT)
                    os.fsync(directory)
                except Exception:
                    with suppress(OSError):
                        os.unlink(digest, dir_fd=directory)
                    raise
        except ProductionRuntimeError:
            raise
        except OSError as error:
            raise ProductionRuntimeError(RuntimeFailureCode.CONFINEMENT) from error
        return ref

    def replay(self, ref: DerivedAssetRef) -> bytes:
        self._require_enabled()
        _require_confinement_controls()
        if not isinstance(ref, DerivedAssetRef):
            raise ProductionRuntimeError(RuntimeFailureCode.INVALID_INPUT)
        try:
            with self._object_directory(ref.sha256, create=False) as directory:
                return self._read_object(directory, ref)
        except ProductionRuntimeError:
            raise
        except OSError as error:
            raise ProductionRuntimeError(RuntimeFailureCode.CONFINEMENT) from error

    def _require_enabled(self) -> None:
        if not self._enabled:
            raise ProductionRuntimeError(RuntimeFailureCode.DISABLED)

    def _object_directory(self, digest: str, *, create: bool) -> _DirectoryHandle:
        return _DirectoryHandle(self._root, digest, create=create)

    def _read_object(self, directory: int, ref: DerivedAssetRef) -> bytes:
        try:
            descriptor = os.open(ref.sha256, os.O_RDONLY | _no_follow(), dir_fd=directory)
            with os.fdopen(descriptor, "rb") as stream:
                metadata = os.fstat(stream.fileno())
                if not _safe_regular_file(metadata):
                    raise ProductionRuntimeError(RuntimeFailureCode.CONFINEMENT)
                digest = sha256()
                data = bytearray()
                while chunk := stream.read(_READ_SIZE):
                    data.extend(chunk)
                    digest.update(chunk)
                if len(data) != ref.byte_length or digest.hexdigest() != ref.sha256:
                    raise ProductionRuntimeError(RuntimeFailureCode.INTEGRITY)
                return bytes(data)
        except ProductionRuntimeError:
            raise
        except OSError as error:
            raise ProductionRuntimeError(RuntimeFailureCode.CONFINEMENT) from error


class ContentAddressedRawAssetStore:
    """Adapt the immutable object store to capture and staged-runtime asset ports."""

    def __init__(self, *, root: Path, enabled: bool = False) -> None:
        self._store = ContentAddressedDerivedAssetStore(root=root, enabled=enabled)

    def put(self, *, data: bytes, media_type: str) -> RawAssetRef:
        ref = self._store.put(data=data, media_type=media_type)
        return RawAssetRef.create(
            asset_id="asset_" + ref.sha256,
            sha256=ref.sha256,
            media_type=ref.media_type,
            byte_length=ref.byte_length,
        )

    def read(self, asset: RawAssetRef) -> bytes:
        if not isinstance(asset, RawAssetRef):
            raise ProductionRuntimeError(RuntimeFailureCode.INVALID_INPUT)
        return self._store.replay(
            DerivedAssetRef(
                sha256=asset.sha256,
                media_type=asset.media_type,
                byte_length=asset.byte_length,
            )
        )


class _DirectoryHandle:
    def __init__(self, root: Path, digest: str, *, create: bool) -> None:
        self._root = root
        self._digest = digest
        self._create = create
        self._descriptors: list[int] = []

    def __enter__(self) -> int:
        root = _open_directory_path(self._root)
        self._descriptors.append(root)
        sha_root = _open_child_directory(root, "sha256", create=self._create)
        self._descriptors.append(sha_root)
        prefix = _open_child_directory(sha_root, self._digest[:2], create=self._create)
        self._descriptors.append(prefix)
        return prefix

    def __exit__(self, *_: object) -> None:
        for descriptor in reversed(self._descriptors):
            with suppress(OSError):
                os.close(descriptor)


def _open_directory_path(path: Path) -> int:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | _no_follow())
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        os.close(descriptor)
        raise ProductionRuntimeError(RuntimeFailureCode.CONFINEMENT)
    return descriptor


def _open_child_directory(parent: int, name: str, *, create: bool) -> int:
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | _no_follow(), dir_fd=parent)
    except FileNotFoundError:
        if not create:
            raise ProductionRuntimeError(RuntimeFailureCode.INTEGRITY) from None
        os.mkdir(name, mode=0o700, dir_fd=parent)
        os.fsync(parent)
        descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | _no_follow(), dir_fd=parent)
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        os.close(descriptor)
        raise ProductionRuntimeError(RuntimeFailureCode.CONFINEMENT)
    return descriptor


def _safe_regular_file(metadata: os.stat_result) -> bool:
    return stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1


def _no_follow() -> int:
    return getattr(os, "O_NOFOLLOW", 0)


def _require_confinement_controls() -> None:
    supported = (
        os.name == "posix"
        and hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and os.open in os.supports_dir_fd
        and os.mkdir in os.supports_dir_fd
        and os.unlink in os.supports_dir_fd
    )
    if not supported:
        raise ProductionRuntimeError(RuntimeFailureCode.UNSUPPORTED_CONTROL)


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short object write")
        view = view[written:]

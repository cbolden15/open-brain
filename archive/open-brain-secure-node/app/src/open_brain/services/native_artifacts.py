"""Confined native onedir manifest and lifecycle adapter."""

from __future__ import annotations

import json
import os
import platform
import secrets
import shutil
import stat
from contextlib import suppress
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Final, Self

from .appliance_lifecycle import (
    ArtifactCandidate,
    ArtifactCompatibilityReceipt,
    ArtifactRemovalReceipt,
    ArtifactRollbackReceipt,
    ArtifactSwitchReceipt,
)

NATIVE_ARTIFACT_KIND: Final[str] = "native-onedir"
NATIVE_MANIFEST_NAME: Final[str] = "open-brain-native.json"
NATIVE_EXECUTABLE_NAME: Final[str] = "open-brain"
_CANDIDATES_DIRECTORY: Final[str] = "candidates"
_CURRENT_LINK: Final[str] = "current"
_MANAGED_CANDIDATES: Final[str] = ".open-brain-managed-candidates.json"
_REMOVAL_DIRECTORY_PREFIX: Final[str] = ".open-brain-removal-"
_FAILURE: Final[str] = "native artifact operation failed"
_MAXIMUM_MANIFEST_BYTES: Final[int] = 8 * 1024
_MAXIMUM_INVENTORY_BYTES: Final[int] = 64 * 1024
_MAXIMUM_MANAGED_CANDIDATES: Final[int] = 128
_PLATFORMS: Final[frozenset[str]] = frozenset({"linux-x86_64", "macos-arm64"})
_HEX = frozenset("0123456789abcdef")


class NativeArtifactError(RuntimeError):
    """A native artifact failed a bounded manifest or lifecycle operation."""


@dataclass(frozen=True, slots=True)
class NativeArtifactManifest:
    candidate_id: str
    version: str
    platform_tag: str
    tree_digest_sha256: str
    executable: str = NATIVE_EXECUTABLE_NAME
    artifact_kind: str = NATIVE_ARTIFACT_KIND
    portable_schema_minimum: int = 1
    portable_schema_maximum: int = 1
    schema_version: int = 1

    def __post_init__(self) -> None:
        try:
            ArtifactCandidate(
                candidate_id=self.candidate_id,
                version=self.version,
                artifact_kind=self.artifact_kind,
            )
        except ValueError as error:
            raise NativeArtifactError(_FAILURE) from error
        if (
            self.platform_tag not in _PLATFORMS
            or self.executable != NATIVE_EXECUTABLE_NAME
            or self.portable_schema_minimum != 1
            or self.portable_schema_maximum != 1
            or self.schema_version != 1
            or not _is_hex_digest(self.tree_digest_sha256)
        ):
            raise NativeArtifactError(_FAILURE)

    @classmethod
    def create(
        cls,
        candidate_directory: Path,
        *,
        candidate_id: str,
        version: str,
        platform_tag: str,
    ) -> Self:
        try:
            directory = _candidate_directory(candidate_directory)
            executable = directory / NATIVE_EXECUTABLE_NAME
            metadata = executable.lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o111 == 0:
                raise NativeArtifactError(_FAILURE)
            return cls(
                candidate_id=candidate_id,
                version=version,
                platform_tag=platform_tag,
                tree_digest_sha256=_tree_digest(directory),
            )
        except NativeArtifactError:
            raise
        except (OSError, ValueError) as error:
            raise NativeArtifactError(_FAILURE) from error

    @classmethod
    def from_dict(cls, value: object) -> Self:
        expected = {
            "artifact_kind",
            "candidate_id",
            "executable",
            "platform_tag",
            "portable_schema_maximum",
            "portable_schema_minimum",
            "schema_version",
            "tree_digest_sha256",
            "version",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise NativeArtifactError(_FAILURE)
        try:
            return cls(
                artifact_kind=_string(value, "artifact_kind"),
                candidate_id=_string(value, "candidate_id"),
                executable=_string(value, "executable"),
                platform_tag=_string(value, "platform_tag"),
                portable_schema_maximum=_integer(value, "portable_schema_maximum"),
                portable_schema_minimum=_integer(value, "portable_schema_minimum"),
                schema_version=_integer(value, "schema_version"),
                tree_digest_sha256=_string(value, "tree_digest_sha256"),
                version=_string(value, "version"),
            )
        except (TypeError, ValueError) as error:
            raise NativeArtifactError(_FAILURE) from error

    @classmethod
    def load(cls, candidate_directory: Path) -> Self:
        try:
            directory = _candidate_directory(candidate_directory)
            path = directory / NATIVE_MANIFEST_NAME
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAXIMUM_MANIFEST_BYTES:
                raise NativeArtifactError(_FAILURE)
            value = json.loads(path.read_bytes())
            manifest = cls.from_dict(value)
            if (
                manifest.candidate_id != directory.name
                or manifest.tree_digest_sha256 != _tree_digest(directory)
            ):
                raise NativeArtifactError(_FAILURE)
            return manifest
        except NativeArtifactError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise NativeArtifactError(_FAILURE) from error

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_kind": self.artifact_kind,
            "candidate_id": self.candidate_id,
            "executable": self.executable,
            "platform_tag": self.platform_tag,
            "portable_schema_maximum": self.portable_schema_maximum,
            "portable_schema_minimum": self.portable_schema_minimum,
            "schema_version": self.schema_version,
            "tree_digest_sha256": self.tree_digest_sha256,
            "version": self.version,
        }

    def write(self, candidate_directory: Path) -> None:
        try:
            directory = _candidate_directory(candidate_directory)
            if (
                directory.name != self.candidate_id
                or _tree_digest(directory) != self.tree_digest_sha256
            ):
                raise NativeArtifactError(_FAILURE)
            payload = (
                json.dumps(self.to_dict(), indent=2, sort_keys=True, ensure_ascii=True) + "\n"
            ).encode("ascii")
            path = directory / NATIVE_MANIFEST_NAME
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            try:
                with os.fdopen(descriptor, "wb", closefd=True) as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                with suppress(OSError):
                    os.close(descriptor)
                raise
            _fsync_directory(directory)
        except NativeArtifactError:
            raise
        except OSError as error:
            raise NativeArtifactError(_FAILURE) from error


class NativeArtifactLifecycleAdapter:
    """Activate exact native candidates through one confined relative symlink."""

    def __init__(self, *, install_root: Path, current_version: str) -> None:
        try:
            if not isinstance(install_root, Path) or not install_root.is_absolute():
                raise NativeArtifactError(_FAILURE)
            root_metadata = install_root.lstat()
            candidates = install_root / _CANDIDATES_DIRECTORY
            candidates_metadata = candidates.lstat()
            if (
                not stat.S_ISDIR(root_metadata.st_mode)
                or not stat.S_ISDIR(candidates_metadata.st_mode)
                or not isinstance(current_version, str)
            ):
                raise NativeArtifactError(_FAILURE)
            ArtifactCandidate(
                candidate_id="candidate_version-check",
                version=current_version,
                artifact_kind=NATIVE_ARTIFACT_KIND,
            )
            self._install_root = install_root
            self._candidates = candidates
            self._current_version = current_version
            self._managed_candidates = self._load_or_create_inventory()
            self.active_candidate_id = self._read_active_candidate_id()
        except NativeArtifactError:
            raise
        except (OSError, ValueError) as error:
            raise NativeArtifactError(_FAILURE) from error

    def compatibility_preflight(
        self,
        candidate: ArtifactCandidate,
    ) -> ArtifactCompatibilityReceipt:
        manifest = self._manifest(candidate)
        self._register_manifest(manifest)
        return ArtifactCompatibilityReceipt(
            candidate_id=candidate.candidate_id,
            artifact_kind=candidate.artifact_kind,
            current_version=self._current_version,
            target_version=manifest.version,
            status="compatible",
        )

    def activate(self, candidate: ArtifactCandidate) -> ArtifactSwitchReceipt:
        manifest = self._manifest(candidate)
        self._register_manifest(manifest)
        self._replace_current(candidate.candidate_id)
        self.active_candidate_id = candidate.candidate_id
        return ArtifactSwitchReceipt(
            candidate_id=candidate.candidate_id,
            artifact_kind=candidate.artifact_kind,
            active_candidate_id=candidate.candidate_id,
            status="activated",
        )

    def rollback(
        self,
        candidate: ArtifactCandidate,
        *,
        prior_candidate_id: str | None,
    ) -> ArtifactRollbackReceipt:
        if (
            not isinstance(candidate, ArtifactCandidate)
            or candidate.artifact_kind != NATIVE_ARTIFACT_KIND
        ):
            raise NativeArtifactError(_FAILURE)
        active_candidate_id = self._read_active_candidate_id()
        allowed_active_ids = {candidate.candidate_id, prior_candidate_id}
        if active_candidate_id not in allowed_active_ids:
            raise NativeArtifactError(_FAILURE)
        if prior_candidate_id is None:
            self._remove_current_link()
        else:
            prior = ArtifactCandidate(
                candidate_id=prior_candidate_id,
                version=self._manifest_by_id(prior_candidate_id).version,
                artifact_kind=NATIVE_ARTIFACT_KIND,
            )
            self._manifest(prior)
            self._replace_current(prior_candidate_id)
        self.active_candidate_id = prior_candidate_id
        return ArtifactRollbackReceipt(
            candidate_id=candidate.candidate_id,
            artifact_kind=candidate.artifact_kind,
            active_candidate_id=prior_candidate_id,
            status="rolled_back",
        )

    def remove(self, *, current_candidate_id: str | None = None) -> ArtifactRemovalReceipt:
        try:
            if current_candidate_id != self._read_active_candidate_id():
                raise NativeArtifactError(_FAILURE)
            managed_candidates: list[Path] = []
            for candidate_id, expected_manifest in sorted(self._managed_candidates.items()):
                candidate_path = self._candidate_path(candidate_id)
                metadata = candidate_path.lstat()
                if not stat.S_ISDIR(metadata.st_mode):
                    raise NativeArtifactError(_FAILURE) from None
                try:
                    manifest = NativeArtifactManifest.load(candidate_path)
                except NativeArtifactError:
                    manifest = None
                if manifest is not None and manifest != expected_manifest:
                    raise NativeArtifactError(_FAILURE)
                managed_candidates.append(candidate_path)
            self._quarantine_and_remove(managed_candidates)
            self._remove_current_link()
            self._remove_inventory()
            self.active_candidate_id = None
            return ArtifactRemovalReceipt(
                artifact_kind=NATIVE_ARTIFACT_KIND,
                removed_candidate_id=current_candidate_id,
                status="removed",
            )
        except NativeArtifactError:
            raise
        except OSError as error:
            raise NativeArtifactError(_FAILURE) from error

    def _load_or_create_inventory(self) -> dict[str, NativeArtifactManifest]:
        try:
            return self._load_inventory()
        except FileNotFoundError:
            manifests: dict[str, NativeArtifactManifest] = {}
            candidate_id = self._current_link_candidate_id()
            if candidate_id is not None:
                manifest = self._manifest_by_id(candidate_id)
                manifests[manifest.candidate_id] = manifest
            try:
                _write_exclusive(
                    self._install_root / _MANAGED_CANDIDATES,
                    _inventory_bytes(manifests),
                )
                _fsync_directory(self._install_root)
            except FileExistsError:
                return self._load_inventory()
            return manifests

    def _load_inventory(self) -> dict[str, NativeArtifactManifest]:
        path = self._install_root / _MANAGED_CANDIDATES
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > _MAXIMUM_INVENTORY_BYTES
        ):
            raise NativeArtifactError(_FAILURE)
        payload = path.read_bytes()
        manifests = _inventory_from_bytes(payload)
        if payload != _inventory_bytes(manifests):
            raise NativeArtifactError(_FAILURE)
        return manifests

    def _register_manifest(self, manifest: NativeArtifactManifest) -> None:
        existing = self._managed_candidates.get(manifest.candidate_id)
        if existing is not None:
            if existing != manifest:
                raise NativeArtifactError(_FAILURE)
            return
        if len(self._managed_candidates) >= _MAXIMUM_MANAGED_CANDIDATES:
            raise NativeArtifactError(_FAILURE)
        updated = {**self._managed_candidates, manifest.candidate_id: manifest}
        _atomic_replace_file(
            self._install_root / _MANAGED_CANDIDATES,
            _inventory_bytes(updated),
        )
        _fsync_directory(self._install_root)
        self._managed_candidates = updated

    def _quarantine_and_remove(self, candidates: list[Path]) -> None:
        if not candidates:
            return
        if not shutil.rmtree.avoids_symlink_attacks:
            raise NativeArtifactError(_FAILURE)
        quarantine = self._install_root / (
            _REMOVAL_DIRECTORY_PREFIX + secrets.token_hex(16)
        )
        quarantine.mkdir(mode=0o700)
        moved: list[tuple[Path, Path]] = []
        try:
            for candidate_path in candidates:
                quarantined = quarantine / candidate_path.name
                os.replace(candidate_path, quarantined)
                moved.append((candidate_path, quarantined))
            _fsync_directory(self._candidates)
            for _, quarantined in moved:
                shutil.rmtree(quarantined)
            quarantine.rmdir()
            _fsync_directory(self._install_root)
        except Exception:
            for candidate_path, quarantined in reversed(moved):
                if quarantined.exists() and not candidate_path.exists():
                    with suppress(OSError):
                        os.replace(quarantined, candidate_path)
            with suppress(OSError):
                quarantine.rmdir()
            raise

    def _remove_inventory(self) -> None:
        path = self._install_root / _MANAGED_CANDIDATES
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise NativeArtifactError(_FAILURE)
        path.unlink()
        _fsync_directory(self._install_root)
        self._managed_candidates = {}

    def _manifest(self, candidate: ArtifactCandidate) -> NativeArtifactManifest:
        if (
            not isinstance(candidate, ArtifactCandidate)
            or candidate.artifact_kind != NATIVE_ARTIFACT_KIND
        ):
            raise NativeArtifactError(_FAILURE)
        manifest = self._manifest_by_id(candidate.candidate_id)
        if manifest.version != candidate.version:
            raise NativeArtifactError(_FAILURE)
        return manifest

    def _manifest_by_id(self, candidate_id: str) -> NativeArtifactManifest:
        manifest = NativeArtifactManifest.load(self._candidate_path(candidate_id))
        if manifest.platform_tag != native_platform_tag():
            raise NativeArtifactError(_FAILURE)
        return manifest

    def _candidate_path(self, candidate_id: str) -> Path:
        try:
            ArtifactCandidate(
                candidate_id=candidate_id,
                version=self._current_version,
                artifact_kind=NATIVE_ARTIFACT_KIND,
            )
        except ValueError as error:
            raise NativeArtifactError(_FAILURE) from error
        return self._candidates / candidate_id

    def _read_active_candidate_id(self) -> str | None:
        candidate_id = self._current_link_candidate_id()
        if candidate_id is None:
            return None
        if candidate_id not in self._managed_candidates:
            raise NativeArtifactError(_FAILURE)
        return candidate_id

    def _current_link_candidate_id(self) -> str | None:
        link = self._install_root / _CURRENT_LINK
        try:
            metadata = link.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISLNK(metadata.st_mode):
            raise NativeArtifactError(_FAILURE)
        target = link.readlink()
        if (
            target.is_absolute()
            or len(target.parts) != 2
            or target.parts[0] != _CANDIDATES_DIRECTORY
        ):
            raise NativeArtifactError(_FAILURE)
        candidate_id = target.parts[1]
        self._candidate_path(candidate_id)
        return candidate_id

    def _replace_current(self, candidate_id: str) -> None:
        self._manifest_by_id(candidate_id)
        current = self._install_root / _CURRENT_LINK
        try:
            current_metadata = current.lstat()
        except FileNotFoundError:
            current_metadata = None
        if current_metadata is not None and not stat.S_ISLNK(current_metadata.st_mode):
            raise NativeArtifactError(_FAILURE)
        temporary = self._install_root / (
            f".{_CURRENT_LINK}-{os.getpid()}-{secrets.token_hex(16)}.tmp"
        )
        created = False
        try:
            temporary.symlink_to(
                Path(_CANDIDATES_DIRECTORY) / candidate_id,
                target_is_directory=True,
            )
            created = True
            os.replace(temporary, current)
            created = False
            _fsync_directory(self._install_root)
        except OSError as error:
            if created:
                with suppress(OSError):
                    temporary.unlink()
            raise NativeArtifactError(_FAILURE) from error

    def _remove_current_link(self) -> None:
        current = self._install_root / _CURRENT_LINK
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISLNK(metadata.st_mode):
            raise NativeArtifactError(_FAILURE)
        current.unlink()
        _fsync_directory(self._install_root)


def _inventory_bytes(manifests: dict[str, NativeArtifactManifest]) -> bytes:
    if len(manifests) > _MAXIMUM_MANAGED_CANDIDATES:
        raise NativeArtifactError(_FAILURE)
    payload = json.dumps(
        {
            "candidates": [
                manifests[candidate_id].to_dict()
                for candidate_id in sorted(manifests)
            ],
            "schema_version": 1,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    if len(payload) > _MAXIMUM_INVENTORY_BYTES:
        raise NativeArtifactError(_FAILURE)
    return payload


def _inventory_from_bytes(payload: bytes) -> dict[str, NativeArtifactManifest]:
    try:
        value = json.loads(payload.decode("ascii"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise NativeArtifactError(_FAILURE) from error
    if (
        not isinstance(value, dict)
        or set(value) != {"candidates", "schema_version"}
        or value.get("schema_version") != 1
        or not isinstance(value.get("candidates"), list)
        or len(value["candidates"]) > _MAXIMUM_MANAGED_CANDIDATES
    ):
        raise NativeArtifactError(_FAILURE)
    manifests: dict[str, NativeArtifactManifest] = {}
    for item in value["candidates"]:
        manifest = NativeArtifactManifest.from_dict(item)
        if (
            manifest.candidate_id in manifests
        ):
            raise NativeArtifactError(_FAILURE)
        manifests[manifest.candidate_id] = manifest
    return manifests


def _write_exclusive(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        with suppress(OSError):
            path.unlink()
        raise


def _atomic_replace_file(path: Path, payload: bytes) -> None:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise NativeArtifactError(_FAILURE)
    temporary = path.parent / f".{path.name}-{secrets.token_hex(16)}.tmp"
    _write_exclusive(temporary, payload)
    try:
        os.replace(temporary, path)
    except OSError:
        with suppress(OSError):
            temporary.unlink()
        raise


def native_platform_tag() -> str:
    host = platform.system()
    machine = platform.machine().casefold()
    if host == "Darwin" and machine == "arm64":
        return "macos-arm64"
    if host == "Linux" and machine in {"amd64", "x86_64"}:
        return "linux-x86_64"
    raise NativeArtifactError(_FAILURE)


def _candidate_directory(value: Path) -> Path:
    if not isinstance(value, Path) or not value.is_absolute():
        raise NativeArtifactError(_FAILURE)
    metadata = value.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise NativeArtifactError(_FAILURE)
    return value


def _tree_digest(root: Path) -> str:
    root_identity = root.resolve(strict=True)
    entries: list[dict[str, object]] = []

    def visit(directory: Path) -> None:
        with os.scandir(directory) as scanner:
            children = sorted(scanner, key=lambda entry: entry.name)
        for child in children:
            path = Path(child.path)
            relative = path.relative_to(root).as_posix()
            if relative == NATIVE_MANIFEST_NAME:
                continue
            metadata = child.stat(follow_symlinks=False)
            mode = stat.S_IMODE(metadata.st_mode)
            if stat.S_ISLNK(metadata.st_mode):
                target = path.readlink()
                if target.is_absolute():
                    raise NativeArtifactError(_FAILURE)
                resolved = (path.parent / target).resolve(strict=True)
                if not resolved.is_relative_to(root_identity):
                    raise NativeArtifactError(_FAILURE)
                entries.append(
                    {"kind": "symlink", "mode": mode, "path": relative, "target": target.as_posix()}
                )
            elif stat.S_ISDIR(metadata.st_mode):
                entries.append({"kind": "directory", "mode": mode, "path": relative})
                visit(path)
            elif stat.S_ISREG(metadata.st_mode):
                entries.append(
                    {
                        "content_sha256": _file_digest(path),
                        "kind": "file",
                        "mode": mode,
                        "path": relative,
                        "size": metadata.st_size,
                    }
                )
            else:
                raise NativeArtifactError(_FAILURE)
    try:
        visit(root)
    except NativeArtifactError:
        raise
    except OSError as error:
        raise NativeArtifactError(_FAILURE) from error
    payload = json.dumps(entries, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return sha256(payload.encode("ascii")).hexdigest()


def _file_digest(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _string(value: dict[object, object], key: str) -> str:
    selected = value.get(key)
    if not isinstance(selected, str):
        raise ValueError("invalid native manifest")
    return selected


def _integer(value: dict[object, object], key: str) -> int:
    selected = value.get(key)
    if not isinstance(selected, int) or isinstance(selected, bool):
        raise ValueError("invalid native manifest")
    return selected


def _is_hex_digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX


__all__ = [
    "NATIVE_ARTIFACT_KIND",
    "NATIVE_EXECUTABLE_NAME",
    "NATIVE_MANIFEST_NAME",
    "NativeArtifactError",
    "NativeArtifactLifecycleAdapter",
    "NativeArtifactManifest",
    "native_platform_tag",
]

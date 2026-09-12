# Private legacy compatibility snapshot; excluded from every shipping artifact.
"""Single-user local profile compilation from one Brain root."""

from __future__ import annotations

import json
import os
import stat
import tomllib
import uuid
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from types import MappingProxyType

from open_brain_engine.engine import LocalEngineContext, ProviderMode


class ProfileError(ValueError):
    """A Brain root cannot compile into the safe local profile."""


SingleUserLocalProfile = LocalEngineContext

_PROFILE_NAME = "single-user-local"
_OWNER_CAPABILITIES = ("canonical.publish", "capture.accept", "space.write")
_LAYOUT_ONLY_BRAIN_TOML = b"layout_version = 1\n"
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
_FILE_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
_MAX_IDENTITY_BYTES = 16_384
_LAYOUT_DIRECTORIES = (
    ".open-brain/indexes",
    ".open-brain/quarantine",
    ".open-brain/run",
    ".open-brain/state",
    "content/spaces",
    "history/decisions",
    "history/proposals",
    "history/publications",
    "sources/blobs/sha256",
    "sources/captures",
)
_PREFLIGHT_DIRECTORIES = tuple(
    dict.fromkeys(
        part
        for relative in _LAYOUT_DIRECTORIES
        for part in (
            "/".join(relative.split("/")[:index])
            for index in range(1, len(relative.split("/")) + 1)
        )
    )
)


def compile_single_user_local(
    root: Path, *, starter_spaces: Sequence[str] = ()
) -> SingleUserLocalProfile:
    """Create or reopen one root without replacing stable local identity."""
    if not isinstance(root, Path):
        raise ProfileError("Brain root must be a path")
    candidate = root.expanduser().absolute()
    if candidate.exists() and candidate.is_symlink():
        raise ProfileError("Brain root must not be a symlink")
    root_fd = -1
    try:
        candidate.mkdir(mode=0o700, parents=True, exist_ok=True)
        root_fd = os.open(candidate, _DIRECTORY_FLAGS)
        absolute_root = candidate.resolve(strict=True)
        _assert_root_identity(absolute_root, root_fd)
    except (OSError, ProfileError) as error:
        if root_fd >= 0:
            os.close(root_fd)
        if isinstance(error, ProfileError):
            raise
        raise ProfileError("Brain root is unavailable") from error
    try:
        metadata = os.fstat(root_fd)
        if not stat.S_ISDIR(metadata.st_mode):
            raise ProfileError("Brain root is unavailable")
        had_portable_content = _has_portable_content(root_fd)
        normalized_starters = _starter_spaces(starter_spaces)
        _preflight_layout(root_fd)
        identity = _load_or_create_identity(root_fd, had_portable_content=had_portable_content)
        os.fchmod(root_fd, 0o700)
        metadata = os.fstat(root_fd)
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ProfileError("Brain root permissions are unsafe")
        root_identity = (metadata.st_dev, metadata.st_ino)
        _create_layout(root_fd)
        _assert_root_identity(absolute_root, root_fd)
    finally:
        os.close(root_fd)
    role_claim = _mapping(identity, "owner_role_claim")
    capabilities = role_claim.get("capabilities")
    assert isinstance(capabilities, list)
    role_claim["capabilities"] = tuple(capabilities)
    return LocalEngineContext(
        root=absolute_root,
        root_identity=root_identity,
        tenant_id=_string(identity, "tenant_id"),
        owner_actor_id=_string(identity, "owner_actor_id"),
        owner_role_claim=MappingProxyType(role_claim),
        provider_mode=ProviderMode.NONE,
        starter_spaces=normalized_starters,
    )


def open_existing_single_user_local(root: Path) -> SingleUserLocalProfile:
    """Open one existing root read-only without creating layout or rewriting identity."""
    if not isinstance(root, Path):
        raise ProfileError("Brain root must be a path")
    candidate = root.expanduser().absolute()
    if not candidate.exists() or candidate.is_symlink():
        raise ProfileError("portable identity is missing")
    root_fd = -1
    try:
        root_fd = os.open(candidate, _DIRECTORY_FLAGS)
        absolute_root = candidate.resolve(strict=True)
        _assert_root_identity(absolute_root, root_fd)
        metadata = os.fstat(root_fd)
        if not stat.S_ISDIR(metadata.st_mode):
            raise ProfileError("Brain root is unavailable")
        identity = _load_existing_identity(root_fd)
        root_identity = (metadata.st_dev, metadata.st_ino)
    except (OSError, ProfileError) as error:
        if isinstance(error, ProfileError):
            raise
        raise ProfileError("Brain root is unavailable") from error
    finally:
        if root_fd >= 0:
            os.close(root_fd)
    role_claim = _mapping(identity, "owner_role_claim")
    capabilities = role_claim.get("capabilities")
    assert isinstance(capabilities, list)
    role_claim["capabilities"] = tuple(capabilities)
    return LocalEngineContext(
        root=absolute_root,
        root_identity=root_identity,
        tenant_id=_string(identity, "tenant_id"),
        owner_actor_id=_string(identity, "owner_actor_id"),
        owner_role_claim=MappingProxyType(role_claim),
        provider_mode=ProviderMode.NONE,
        starter_spaces=(),
    )


def _create_layout(root_fd: int) -> None:
    for relative in _LAYOUT_DIRECTORIES:
        descriptor = _open_directory_path(root_fd, relative.split("/"), create=True)
        assert descriptor is not None
        try:
            os.fchmod(descriptor, 0o700)
            os.fsync(descriptor)
        except OSError as error:
            raise ProfileError("local profile file is unavailable") from error
        finally:
            os.close(descriptor)


def _load_existing_identity(root_fd: int) -> dict[str, object]:
    brain_payload = _read_file(root_fd, "brain.toml")
    if brain_payload is None or brain_payload == _LAYOUT_ONLY_BRAIN_TOML:
        raise ProfileError("portable identity is missing")
    return _identity_from_brain_toml(brain_payload)


def _preflight_layout(root_fd: int) -> None:
    """Reject poisoned existing operational components before any mutation."""
    for relative in _PREFLIGHT_DIRECTORIES:
        descriptor = _open_directory_path(root_fd, relative.split("/"), create=False)
        if descriptor is not None:
            os.close(descriptor)


def _load_or_create_identity(root_fd: int, *, had_portable_content: bool) -> dict[str, object]:
    brain_payload = _read_file(root_fd, "brain.toml")
    legacy_payload = _read_file(root_fd, ".open-brain/identity.json")
    legacy_identity = _load_legacy_identity(legacy_payload)
    if brain_payload is None:
        if had_portable_content or legacy_identity is not None:
            raise ProfileError("portable identity is missing")
        identity = _new_identity()
        _write_new(root_fd, "brain.toml", _canonical_brain_toml(identity))
        return identity
    if brain_payload == _LAYOUT_ONLY_BRAIN_TOML:
        if legacy_identity is None:
            raise ProfileError("portable identity is missing")
        _replace_file(root_fd, "brain.toml", _canonical_brain_toml(legacy_identity))
        _retire_legacy_identity(root_fd)
        return legacy_identity
    identity = _identity_from_brain_toml(brain_payload)
    if legacy_identity is not None:
        if legacy_identity != identity:
            raise ProfileError("portable and legacy identities disagree")
        _retire_legacy_identity(root_fd)
    return identity


def _load_legacy_identity(payload: bytes | None) -> dict[str, object] | None:
    if payload is None:
        return None
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProfileError("local identity is invalid") from error
    if not isinstance(value, dict):
        raise ProfileError("local identity is invalid")
    _validate_identity(value)
    return value


def _identity_from_brain_toml(payload: bytes) -> dict[str, object]:
    try:
        value = tomllib.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ProfileError("portable identity is invalid") from error
    if not isinstance(value, dict):
        raise ProfileError("portable identity is invalid")
    expected_keys = {
        "layout_version",
        "profile",
        "tenant_id",
        "owner_actor_id",
        "owner_role_id",
        "owner_role_claim_id",
        "owner_capabilities",
    }
    if set(value) != expected_keys:
        raise ProfileError("portable identity is invalid")
    capabilities = value.get("owner_capabilities")
    identity: dict[str, object] = {
        "tenant_id": value.get("tenant_id"),
        "owner_actor_id": value.get("owner_actor_id"),
        "owner_role_claim": {
            "role_claim_id": value.get("owner_role_claim_id"),
            "tenant_id": value.get("tenant_id"),
            "actor_id": value.get("owner_actor_id"),
            "role_id": value.get("owner_role_id"),
            "capabilities": capabilities,
        },
    }
    if value.get("layout_version") != 1 or value.get("profile") != _PROFILE_NAME:
        raise ProfileError("portable identity is invalid")
    _validate_identity(identity)
    if (
        identity["owner_role_claim"]
        != {
            "role_claim_id": value.get("owner_role_claim_id"),
            "tenant_id": value.get("tenant_id"),
            "actor_id": value.get("owner_actor_id"),
            "role_id": value.get("owner_role_id"),
            "capabilities": list(_OWNER_CAPABILITIES),
        }
        or _canonical_brain_toml(identity) != payload
    ):
        raise ProfileError("portable identity is invalid")
    return identity


def _canonical_brain_toml(identity: Mapping[str, object]) -> bytes:
    _validate_identity(identity)
    role_claim = _mapping(identity, "owner_role_claim")
    tenant_id = _string(identity, "tenant_id")
    actor_id = _string(identity, "owner_actor_id")
    role_id = _string(role_claim, "role_id")
    role_claim_id = _string(role_claim, "role_claim_id")
    capabilities = role_claim.get("capabilities")
    if capabilities != list(_OWNER_CAPABILITIES):
        raise ProfileError("local identity is invalid")
    return (
        "layout_version = 1\n"
        f'profile = "{_PROFILE_NAME}"\n'
        f'tenant_id = "{tenant_id}"\n'
        f'owner_actor_id = "{actor_id}"\n'
        f'owner_role_id = "{role_id}"\n'
        f'owner_role_claim_id = "{role_claim_id}"\n'
        'owner_capabilities = ["canonical.publish", "capture.accept", "space.write"]\n'
    ).encode()


def _has_portable_content(root_fd: int) -> bool:
    for name in ("content", "history", "sources"):
        try:
            metadata = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError as error:
            raise ProfileError("local profile file is unavailable") from error
        if not stat.S_ISDIR(metadata.st_mode):
            return True
        descriptor = _open_child_directory(root_fd, name, create=False)
        assert descriptor is not None
        try:
            if _directory_has_content(descriptor):
                return True
        finally:
            os.close(descriptor)
    return False


def _directory_has_content(directory_fd: int) -> bool:
    try:
        entries = sorted(os.scandir(directory_fd), key=lambda entry: entry.name)
    except OSError as error:
        raise ProfileError("local profile file is unavailable") from error
    for entry in entries:
        try:
            metadata = entry.stat(follow_symlinks=False)
        except OSError as error:
            raise ProfileError("local profile file is unavailable") from error
        if not stat.S_ISDIR(metadata.st_mode):
            return True
        child_fd = _open_child_directory(directory_fd, entry.name, create=False)
        assert child_fd is not None
        try:
            current = os.fstat(child_fd)
            if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise ProfileError("local profile file changed during validation")
            if _directory_has_content(child_fd):
                return True
        finally:
            os.close(child_fd)
    return False


def _new_identity() -> dict[str, object]:
    tenant_id = _portable_id("tenant")
    actor_id = _portable_id("actor")
    role_claim = {
        "actor_id": actor_id,
        "capabilities": list(_OWNER_CAPABILITIES),
        "role_claim_id": _portable_id("role_claim"),
        "role_id": _portable_id("role"),
        "tenant_id": tenant_id,
    }
    value: dict[str, object] = {
        "owner_actor_id": actor_id,
        "owner_role_claim": role_claim,
        "tenant_id": tenant_id,
    }
    _validate_identity(value)
    return value


def _assert_root_identity(root: Path, root_fd: int) -> None:
    try:
        path_metadata = os.stat(root, follow_symlinks=False)
        descriptor_metadata = os.fstat(root_fd)
    except OSError as error:
        raise ProfileError("Brain root is unavailable") from error
    if not stat.S_ISDIR(path_metadata.st_mode) or (path_metadata.st_dev, path_metadata.st_ino) != (
        descriptor_metadata.st_dev,
        descriptor_metadata.st_ino,
    ):
        raise ProfileError("Brain root changed during profile compilation")


def _open_child_directory(parent_fd: int, name: str, *, create: bool) -> int | None:
    if not name or "/" in name or name in {".", ".."}:
        raise ProfileError("local profile path is unsafe")
    try:
        return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            return None
    except OSError as error:
        raise ProfileError("local profile file is unsafe") from error
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except FileExistsError:
        pass
    except OSError as error:
        raise ProfileError("local profile file is unavailable") from error
    try:
        child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        os.fchmod(child_fd, 0o700)
        return child_fd
    except OSError as error:
        raise ProfileError("local profile file is unsafe") from error


def _open_directory_path(root_fd: int, parts: Sequence[str], *, create: bool) -> int | None:
    current_fd = os.dup(root_fd)
    try:
        for part in parts:
            child_fd = _open_child_directory(current_fd, part, create=create)
            if child_fd is None:
                os.close(current_fd)
                return None
            if create:
                try:
                    os.fchmod(child_fd, 0o700)
                except OSError as error:
                    os.close(child_fd)
                    raise ProfileError("local profile file is unavailable") from error
            os.close(current_fd)
            current_fd = child_fd
        return current_fd
    except BaseException:
        with suppress(OSError):
            os.close(current_fd)
        raise


def _open_parent(root_fd: int, relative: str) -> tuple[int | None, str]:
    parts = relative.split("/")
    if not parts or any(not part or part in {".", ".."} for part in parts):
        raise ProfileError("local profile path is unsafe")
    parent_fd = _open_directory_path(root_fd, parts[:-1], create=False)
    return parent_fd, parts[-1]


def _file_metadata(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ProfileError("local profile file is unavailable") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size > _MAX_IDENTITY_BYTES
    ):
        raise ProfileError("local profile file is unsafe")
    return metadata


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("short profile write")
        remaining = remaining[written:]


def _write_new(root_fd: int, relative: str, payload: bytes) -> None:
    parent_fd, name = _open_parent(root_fd, relative)
    if parent_fd is None:
        raise ProfileError("local profile file is unavailable")
    try:
        if _file_metadata(parent_fd, name) is not None:
            if _read_file(root_fd, relative) != payload:
                raise ProfileError("local profile file conflicts")
            return
        try:
            descriptor = os.open(name, _FILE_CREATE_FLAGS, 0o600, dir_fd=parent_fd)
            try:
                _write_all(descriptor, payload)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.fsync(parent_fd)
        except FileExistsError:
            if _read_file(root_fd, relative) != payload:
                raise ProfileError("local profile file conflicts") from None
        except OSError as error:
            raise ProfileError("local profile file is unavailable") from error
    finally:
        os.close(parent_fd)


def _replace_file(root_fd: int, relative: str, payload: bytes) -> None:
    parent_fd, name = _open_parent(root_fd, relative)
    if parent_fd is None or _file_metadata(parent_fd, name) is None:
        if parent_fd is not None:
            os.close(parent_fd)
        raise ProfileError("local profile file is unavailable")
    temporary = f".{name}.{uuid.uuid4().hex}.tmp"
    descriptor = -1
    try:
        descriptor = os.open(temporary, _FILE_CREATE_FLAGS, 0o600, dir_fd=parent_fd)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    except OSError as error:
        raise ProfileError("local profile file is unavailable") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        except OSError as error:
            raise ProfileError("local profile file is unavailable") from error
        finally:
            os.close(parent_fd)


def _retire_legacy_identity(root_fd: int) -> None:
    parent_fd, name = _open_parent(root_fd, ".open-brain/identity.json")
    if parent_fd is None:
        return
    try:
        if _file_metadata(parent_fd, name) is None:
            return
        os.unlink(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except OSError as error:
        raise ProfileError("local profile file is unavailable") from error
    finally:
        os.close(parent_fd)


def _read_file(root_fd: int, relative: str) -> bytes | None:
    parent_fd, name = _open_parent(root_fd, relative)
    if parent_fd is None:
        return None
    try:
        metadata = _file_metadata(parent_fd, name)
        if metadata is None:
            return None
        try:
            descriptor = os.open(name, _FILE_READ_FLAGS, dir_fd=parent_fd)
            try:
                before = os.fstat(descriptor)
                if (before.st_dev, before.st_ino) != (
                    metadata.st_dev,
                    metadata.st_ino,
                ) or before.st_nlink != 1:
                    raise OSError("profile file changed")
                chunks: list[bytes] = []
                total = 0
                while total <= _MAX_IDENTITY_BYTES:
                    chunk = os.read(descriptor, _MAX_IDENTITY_BYTES + 1 - total)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                after = os.fstat(descriptor)
            finally:
                os.close(descriptor)
        except OSError as error:
            raise ProfileError("local profile file is unavailable") from error
        payload = b"".join(chunks)
        if (
            len(payload) > _MAX_IDENTITY_BYTES
            or len(payload) != metadata.st_size
            or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            != (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
            )
            or after.st_nlink != 1
        ):
            raise ProfileError("local profile file changed during validation")
        return payload
    finally:
        os.close(parent_fd)


def _portable_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4()}"


def _validate_identity(value: Mapping[str, object]) -> None:
    if set(value) != {"tenant_id", "owner_actor_id", "owner_role_claim"}:
        raise ProfileError("local identity is invalid")
    tenant_id = _string(value, "tenant_id")
    actor_id = _string(value, "owner_actor_id")
    role_claim = _mapping(value, "owner_role_claim")
    capabilities = role_claim.get("capabilities")
    if (
        set(role_claim) != {"role_claim_id", "tenant_id", "actor_id", "role_id", "capabilities"}
        or role_claim.get("tenant_id") != tenant_id
        or role_claim.get("actor_id") != actor_id
        or not isinstance(capabilities, list)
        or capabilities != sorted(set(capabilities))
        or any(not isinstance(item, str) or not item for item in capabilities)
    ):
        raise ProfileError("local identity is invalid")
    for prefix, identifier in (("tenant", tenant_id), ("actor", actor_id)):
        _validate_portable_id(identifier, prefix)
    for key, prefix in (("role_claim_id", "role_claim"), ("role_id", "role")):
        item = role_claim.get(key)
        if not isinstance(item, str):
            raise ProfileError("local identity is invalid")
        _validate_portable_id(item, prefix)


def _validate_portable_id(value: str, prefix: str) -> None:
    marker = prefix + "_"
    if not value.startswith(marker):
        raise ProfileError("local identity is invalid")
    try:
        parsed = uuid.UUID(value.removeprefix(marker))
    except ValueError as error:
        raise ProfileError("local identity is invalid") from error
    if value != f"{prefix}_{parsed}" or parsed.version != 4:
        raise ProfileError("local identity is invalid")


def _string(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise ProfileError("local identity is invalid")
    return item


def _mapping(value: Mapping[str, object], key: str) -> dict[str, object]:
    item = value.get(key)
    if not isinstance(item, dict) or any(not isinstance(name, str) for name in item):
        raise ProfileError("local identity is invalid")
    return item


def _starter_spaces(value: Sequence[str]) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ProfileError("starter spaces are invalid")
    result = tuple(item.strip() for item in value)
    if any(not item or len(item) > 120 for item in result) or len(set(result)) != len(result):
        raise ProfileError("starter spaces are invalid")
    return result

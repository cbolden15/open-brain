"""Open an existing local Brain from optional source commands."""

from __future__ import annotations

import os
import re
import stat
import tomllib
from pathlib import Path

from open_brain_engine.engine import LocalEngineContext, ProviderMode

from open_brain_connectors.runtime.connectors import ConnectorContractError


def existing_source_profile(root: Path) -> LocalEngineContext:
    """Read a bounded Portable identity without bootstrapping an implicit Brain."""
    directory_fd = -1
    try:
        if not root.is_absolute() or root.is_symlink():
            raise ValueError
        canonical = root.resolve(strict=True)
        directory_fd = os.open(canonical, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        directory = os.fstat(directory_fd)
        fd = os.open(
            "brain.toml", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd,
        )
        with os.fdopen(fd, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 16_384:
                raise ValueError
            payload = handle.read(16_385)
            after = os.fstat(handle.fileno())
        current = os.stat("brain.toml", dir_fd=directory_fd, follow_symlinks=False)
        current_root = canonical.stat(follow_symlinks=False)

        def identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
            return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)

        if (len(payload) > 16_384 or identity(before) != identity(after)
                or identity(after) != identity(current)
                or (directory.st_dev, directory.st_ino)
                != (current_root.st_dev, current_root.st_ino)):
            raise ValueError
        value = tomllib.loads(payload.decode("utf-8"))
        capabilities = ["canonical.publish", "capture.accept", "space.write"]
        if (value.get("layout_version") != 1 or value.get("profile") != "single-user-local"
                or value.get("owner_capabilities") != capabilities):
            raise ValueError

        def identifier(key: str, prefix: str) -> str:
            item = value.get(key)
            if not isinstance(item, str) or re.fullmatch(
                prefix + r"_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", item,
            ) is None:
                raise ValueError
            return item

        tenant, actor = identifier("tenant_id", "tenant"), identifier("owner_actor_id", "actor")
        return LocalEngineContext(
            root=canonical, root_identity=(directory.st_dev, directory.st_ino),
            tenant_id=tenant, owner_actor_id=actor,
            owner_role_claim={
                "actor_id": actor, "tenant_id": tenant, "capabilities": tuple(capabilities),
                "role_id": identifier("owner_role_id", "role"),
                "role_claim_id": identifier("owner_role_claim_id", "role_claim"),
            },
            provider_mode=ProviderMode.NONE, starter_spaces=(),
        )
    except (OSError, ValueError) as error:
        raise ConnectorContractError("source_brain_unavailable") from error
    finally:
        if directory_fd >= 0:
            os.close(directory_fd)

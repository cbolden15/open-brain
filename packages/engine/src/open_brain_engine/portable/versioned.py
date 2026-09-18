"""Version dispatch for Portable Brain snapshots."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.storage.filesystem import (
    RootConfinementError,
    RootIdentity,
    capture_root_identity,
    read_confined,
)

from .managed_v2 import validated_portable_snapshot_v2
from .v1 import (
    PortableSnapshot,
    PortableValidationError,
)
from .v1 import (
    validated_portable_snapshot as validated_portable_snapshot_v1,
)
from .v3 import validated_portable_snapshot_v3
from .v4 import validated_portable_snapshot_v4


def validated_portable_snapshot(
    root: Path, *, expected_root_identity: RootIdentity | None = None
) -> PortableSnapshot:
    try:
        identity = (
            capture_root_identity(root)
            if expected_root_identity is None
            else expected_root_identity
        )
        payload = read_confined(
            root=root,
            relative="portable-manifest.json",
            expected_root_identity=identity,
        )
    except (OSError, RootConfinementError) as error:
        raise PortableValidationError("Portable root must be a real directory") from error
    if payload is None:
        raise PortableValidationError("manifest is missing")
    try:
        value = json.loads(payload)
    except UnicodeDecodeError, json.JSONDecodeError:
        raise PortableValidationError("manifest is invalid") from None
    if (
        not isinstance(value, dict)
        or portable_canonical_json_bytes(value) != payload
        or type(value.get("schema_version")) is not int
    ):
        raise PortableValidationError("manifest is invalid")
    version = cast(int, value["schema_version"])
    if version == 1:
        return validated_portable_snapshot_v1(root, expected_root_identity=identity)
    if version == 2:
        return validated_portable_snapshot_v2(root, expected_root_identity=identity)
    if version == 4:
        return validated_portable_snapshot_v4(root, expected_root_identity=identity)
    if version == 3:
        return validated_portable_snapshot_v3(root, expected_root_identity=identity)
    raise PortableValidationError("unsupported Portable Brain schema")


def validate_portable_root(
    root: Path, *, expected_root_identity: RootIdentity | None = None
) -> dict[str, object]:
    return dict(
        validated_portable_snapshot(root, expected_root_identity=expected_root_identity).manifest
    )


__all__ = ["validate_portable_root", "validated_portable_snapshot"]

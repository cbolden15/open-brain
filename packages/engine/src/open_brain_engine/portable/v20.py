"""Portable20 inherits exact Portable19 bytes and adds saved-item admission evidence."""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import cast

from open_brain_engine.storage.filesystem import (
    RootConfinementError,
    RootIdentity,
    assert_root_identity,
    capture_root_identity,
    open_root_descriptor,
)

from .v1 import PortableSnapshot, PortableValidationError, _portable_record, _snapshot_directory
from .v5 import _manifest
from .v20_catalog import PORTABLE_V20_SCHEMA_CATALOG_DIGEST as PORTABLE_V20_SCHEMA_CATALOG_DIGEST


def manifest_v20(
    files: Mapping[str, bytes],
    *,
    tenant_id: str,
    export_id: str,
    created_at: str,
) -> dict[str, object]:
    """Build a v20 manifest over exact inherited and saved-item admission payload bytes."""
    return {
        "compatibility": {"maximum_contract_version": "20", "minimum_contract_version": "1"},
        "contract_version": "20",
        "created_at": created_at,
        "export_id": export_id,
        "files": [
            {"path": path, "sha256": sha256(payload).hexdigest()}
            for path, payload in sorted(files.items())
        ],
        "layout_version": 20,
        "schema_catalog_digest": PORTABLE_V20_SCHEMA_CATALOG_DIGEST,
        "schema_version": 20,
        "tenant_id": tenant_id,
    }


def validate_portable_file_set_v20(files: Mapping[str, bytes], *, tenant_id: str) -> None:
    from .saved_item_admission_v20 import SAVED_ITEM_ADMISSION_PATH, validate_saved_item_admission
    from .v19 import validate_portable_file_set_v19

    validate_portable_file_set_v19(
        {path: raw for path, raw in files.items() if path != SAVED_ITEM_ADMISSION_PATH},
        tenant_id=tenant_id,
    )
    validate_saved_item_admission(files)


def validated_portable_snapshot_v20(
    root: Path,
    *,
    expected_root_identity: RootIdentity | None = None,
) -> PortableSnapshot:
    """Capture one descriptor-confined byte snapshot and validate its v20 manifest."""
    try:
        identity = (
            capture_root_identity(root)
            if expected_root_identity is None
            else expected_root_identity
        )
        descriptor = open_root_descriptor(root, identity)
        files: dict[str, bytes] = {}
        try:
            _snapshot_directory(descriptor, (), files)
        finally:
            import os

            os.close(descriptor)
        manifest = _portable_record(files.get("portable-manifest.json", b""), "manifest")
        inventory = _manifest(manifest, version=20, catalog=PORTABLE_V20_SCHEMA_CATALOG_DIGEST)
        payloads = {
            path: payload for path, payload in files.items() if path != "portable-manifest.json"
        }
        if inventory != {path: sha256(payload).hexdigest() for path, payload in payloads.items()}:
            raise PortableValidationError("Portable v20 manifest checksum or inventory mismatch")
        validate_portable_file_set_v20(payloads, tenant_id=cast(str, manifest["tenant_id"]))
        assert_root_identity(root, identity)
    except PortableValidationError:
        raise
    except (OSError, RootConfinementError, KeyError, TypeError, ValueError) as error:
        raise PortableValidationError("Portable v20 root or manifest invalid") from error
    return PortableSnapshot(identity, manifest, MappingProxyType(files))


__all__ = ["manifest_v20", "validate_portable_file_set_v20", "validated_portable_snapshot_v20"]

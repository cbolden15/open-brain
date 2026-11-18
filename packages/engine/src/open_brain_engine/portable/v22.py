"""Portable22 inherits exact Portable21 bytes and adds browser revisit evidence."""

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
from .v22_catalog import PORTABLE_V22_SCHEMA_CATALOG_DIGEST as PORTABLE_V22_SCHEMA_CATALOG_DIGEST


def manifest_v22(
    files: Mapping[str, bytes],
    *,
    tenant_id: str,
    export_id: str,
    created_at: str,
) -> dict[str, object]:
    """Build a v22 manifest over exact inherited and saved-item revisit payload bytes."""
    return {
        "compatibility": {"maximum_contract_version": "22", "minimum_contract_version": "1"},
        "contract_version": "22",
        "created_at": created_at,
        "export_id": export_id,
        "files": [
            {"path": path, "sha256": sha256(payload).hexdigest()}
            for path, payload in sorted(files.items())
        ],
        "layout_version": 22,
        "schema_catalog_digest": PORTABLE_V22_SCHEMA_CATALOG_DIGEST,
        "schema_version": 22,
        "tenant_id": tenant_id,
    }


def validate_portable_file_set_v22(files: Mapping[str, bytes], *, tenant_id: str) -> None:
    from .browser_revisits_v22 import BROWSER_REVISITS_PATH, validate_browser_revisits
    from .v21 import validate_portable_file_set_v21

    validate_portable_file_set_v21(
        {path: raw for path, raw in files.items() if path != BROWSER_REVISITS_PATH},
        tenant_id=tenant_id,
    )
    validate_browser_revisits(files)


def validated_portable_snapshot_v22(
    root: Path,
    *,
    expected_root_identity: RootIdentity | None = None,
) -> PortableSnapshot:
    """Capture one descriptor-confined byte snapshot and validate its v22 manifest."""
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
        inventory = _manifest(manifest, version=22, catalog=PORTABLE_V22_SCHEMA_CATALOG_DIGEST)
        payloads = {
            path: payload for path, payload in files.items() if path != "portable-manifest.json"
        }
        if inventory != {path: sha256(payload).hexdigest() for path, payload in payloads.items()}:
            raise PortableValidationError("Portable v22 manifest checksum or inventory mismatch")
        validate_portable_file_set_v22(payloads, tenant_id=cast(str, manifest["tenant_id"]))
        assert_root_identity(root, identity)
    except PortableValidationError:
        raise
    except (OSError, RootConfinementError, KeyError, TypeError, ValueError) as error:
        raise PortableValidationError("Portable v22 root or manifest invalid") from error
    return PortableSnapshot(identity, manifest, MappingProxyType(files))


__all__ = ["manifest_v22", "validate_portable_file_set_v22", "validated_portable_snapshot_v22"]

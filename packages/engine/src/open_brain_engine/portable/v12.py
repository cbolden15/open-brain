"""Portable v12 snapshot composition over unchanged inherited evidence formats.

The coordinator owns the draft catalog and public version dispatcher. No v12
archive is relabeled or passed through a v5 manifest reader.
"""

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
from .v12_catalog import PORTABLE_V12_SCHEMA_CATALOG_DIGEST as PORTABLE_V12_SCHEMA_CATALOG_DIGEST


def manifest_v12(
    files: Mapping[str, bytes],
    *,
    tenant_id: str,
    export_id: str,
    created_at: str,
) -> dict[str, object]:
    """Build a v12 manifest over exact inherited and facet payload bytes."""
    return {
        "compatibility": {"maximum_contract_version": "12", "minimum_contract_version": "1"},
        "contract_version": "12",
        "created_at": created_at,
        "export_id": export_id,
        "files": [
            {"path": path, "sha256": sha256(payload).hexdigest()}
            for path, payload in sorted(files.items())
        ],
        "layout_version": 12,
        "schema_catalog_digest": PORTABLE_V12_SCHEMA_CATALOG_DIGEST,
        "schema_version": 12,
        "tenant_id": tenant_id,
    }


def validate_portable_file_set_v12(files: Mapping[str, bytes], *, tenant_id: str) -> None:
    from .slack_v12 import SELECTIONS_PATH, validate_selections, validate_sharing_authority_v12
    from .v11 import validate_portable_file_set_v11

    validate_selections(files)
    validate_portable_file_set_v11(
        {path: data for path, data in files.items() if path != SELECTIONS_PATH},
        tenant_id=tenant_id,
        _sharing_validator=lambda _base: validate_sharing_authority_v12(files),
    )


def validated_portable_snapshot_v12(
    root: Path,
    *,
    expected_root_identity: RootIdentity | None = None,
) -> PortableSnapshot:
    """Capture one descriptor-confined byte snapshot and validate its v12 manifest."""
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
        inventory = _manifest(manifest, version=12, catalog=PORTABLE_V12_SCHEMA_CATALOG_DIGEST)
        payloads = {
            path: payload for path, payload in files.items() if path != "portable-manifest.json"
        }
        if inventory != {path: sha256(payload).hexdigest() for path, payload in payloads.items()}:
            raise PortableValidationError("Portable v12 manifest checksum or inventory mismatch")
        validate_portable_file_set_v12(payloads, tenant_id=cast(str, manifest["tenant_id"]))
        assert_root_identity(root, identity)
    except PortableValidationError:
        raise
    except (OSError, RootConfinementError, KeyError, TypeError, ValueError) as error:
        raise PortableValidationError("Portable v12 root or manifest invalid") from error
    return PortableSnapshot(identity, manifest, MappingProxyType(files))


__all__ = ["manifest_v12", "validate_portable_file_set_v12", "validated_portable_snapshot_v12"]

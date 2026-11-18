"""Portable23 inherits exact Portable22 bytes and adds browser filing witnesses."""

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
from .v23_catalog import PORTABLE_V23_SCHEMA_CATALOG_DIGEST as PORTABLE_V23_SCHEMA_CATALOG_DIGEST


def manifest_v23(
    files: Mapping[str, bytes],
    *,
    tenant_id: str,
    export_id: str,
    created_at: str,
) -> dict[str, object]:
    """Build a v23 manifest over exact inherited and saved-item filing witness payload bytes."""
    return {
        "compatibility": {"maximum_contract_version": "23", "minimum_contract_version": "1"},
        "contract_version": "23",
        "created_at": created_at,
        "export_id": export_id,
        "files": [
            {"path": path, "sha256": sha256(payload).hexdigest()}
            for path, payload in sorted(files.items())
        ],
        "layout_version": 23,
        "schema_catalog_digest": PORTABLE_V23_SCHEMA_CATALOG_DIGEST,
        "schema_version": 23,
        "tenant_id": tenant_id,
    }


def validate_portable_file_set_v23(files: Mapping[str, bytes], *, tenant_id: str) -> None:
    from .browser_filing_v23 import BROWSER_FILING_PATH, validate_filing_witnesses
    from .v22 import validate_portable_file_set_v22

    validate_portable_file_set_v22(
        {path: raw for path, raw in files.items() if path != BROWSER_FILING_PATH},
        tenant_id=tenant_id,
    )
    validate_filing_witnesses(files)


def validated_portable_snapshot_v23(
    root: Path,
    *,
    expected_root_identity: RootIdentity | None = None,
) -> PortableSnapshot:
    """Capture one descriptor-confined byte snapshot and validate its v23 manifest."""
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
        inventory = _manifest(manifest, version=23, catalog=PORTABLE_V23_SCHEMA_CATALOG_DIGEST)
        payloads = {
            path: payload for path, payload in files.items() if path != "portable-manifest.json"
        }
        if inventory != {path: sha256(payload).hexdigest() for path, payload in payloads.items()}:
            raise PortableValidationError("Portable v23 manifest checksum or inventory mismatch")
        validate_portable_file_set_v23(payloads, tenant_id=cast(str, manifest["tenant_id"]))
        assert_root_identity(root, identity)
    except PortableValidationError:
        raise
    except (OSError, RootConfinementError, KeyError, TypeError, ValueError) as error:
        raise PortableValidationError("Portable v23 root or manifest invalid") from error
    return PortableSnapshot(identity, manifest, MappingProxyType(files))


__all__ = ["manifest_v23", "validate_portable_file_set_v23", "validated_portable_snapshot_v23"]

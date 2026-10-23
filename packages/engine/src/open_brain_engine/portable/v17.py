"""Portable17 inherits exact Portable16 bytes and adds owner egress policy evidence."""

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
from .v17_catalog import PORTABLE_V17_SCHEMA_CATALOG_DIGEST as PORTABLE_V17_SCHEMA_CATALOG_DIGEST


def manifest_v17(
    files: Mapping[str, bytes],
    *,
    tenant_id: str,
    export_id: str,
    created_at: str,
) -> dict[str, object]:
    """Build a v17 manifest over exact inherited and owner egress policy payload bytes."""
    return {
        "compatibility": {"maximum_contract_version": "17", "minimum_contract_version": "1"},
        "contract_version": "17",
        "created_at": created_at,
        "export_id": export_id,
        "files": [
            {"path": path, "sha256": sha256(payload).hexdigest()}
            for path, payload in sorted(files.items())
        ],
        "layout_version": 17,
        "schema_catalog_digest": PORTABLE_V17_SCHEMA_CATALOG_DIGEST,
        "schema_version": 17,
        "tenant_id": tenant_id,
    }


def validate_portable_file_set_v17(files: Mapping[str, bytes], *, tenant_id: str) -> None:
    from .egress_policy_v17 import (
        OWNER_EGRESS_POLICY_PATH,
        validate_owner_egress_policy_evidence,
    )
    from .v16 import validate_portable_file_set_v16

    validate_portable_file_set_v16(
        {path: raw for path, raw in files.items() if path != OWNER_EGRESS_POLICY_PATH},
        tenant_id=tenant_id,
    )
    validate_owner_egress_policy_evidence(files)


def validated_portable_snapshot_v17(
    root: Path,
    *,
    expected_root_identity: RootIdentity | None = None,
) -> PortableSnapshot:
    """Capture one descriptor-confined byte snapshot and validate its v17 manifest."""
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
        inventory = _manifest(manifest, version=17, catalog=PORTABLE_V17_SCHEMA_CATALOG_DIGEST)
        payloads = {
            path: payload for path, payload in files.items() if path != "portable-manifest.json"
        }
        if inventory != {path: sha256(payload).hexdigest() for path, payload in payloads.items()}:
            raise PortableValidationError("Portable v17 manifest checksum or inventory mismatch")
        validate_portable_file_set_v17(payloads, tenant_id=cast(str, manifest["tenant_id"]))
        assert_root_identity(root, identity)
    except PortableValidationError:
        raise
    except (OSError, RootConfinementError, KeyError, TypeError, ValueError) as error:
        raise PortableValidationError("Portable v17 root or manifest invalid") from error
    return PortableSnapshot(identity, manifest, MappingProxyType(files))


__all__ = ["manifest_v17", "validate_portable_file_set_v17", "validated_portable_snapshot_v17"]

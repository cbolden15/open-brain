"""Portable v10 snapshot composition over unchanged inherited evidence formats.

The coordinator owns the draft catalog and public version dispatcher. No v10
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

from .facets_v10 import (
    FACET_SIDECAR_PATHS,
    decode_portable_facets,
    references_from_portable_files,
    validate_facet_binding_record,
)
from .v1 import PortableSnapshot, PortableValidationError, _portable_record, _snapshot_directory
from .v3 import _BINDING_PATH, _records
from .v5 import ISSUER_MIGRATION_PATH, _manifest
from .v10_catalog import PORTABLE_V10_SCHEMA_CATALOG_DIGEST as PORTABLE_V10_SCHEMA_CATALOG_DIGEST


def manifest_v10(
    files: Mapping[str, bytes],
    *,
    tenant_id: str,
    export_id: str,
    created_at: str,
) -> dict[str, object]:
    """Build a v10 manifest over exact inherited and facet payload bytes."""
    return {
        "compatibility": {"maximum_contract_version": "10", "minimum_contract_version": "1"},
        "contract_version": "10",
        "created_at": created_at,
        "export_id": export_id,
        "files": [
            {"path": path, "sha256": sha256(payload).hexdigest()}
            for path, payload in sorted(files.items())
        ],
        "layout_version": 10,
        "schema_catalog_digest": PORTABLE_V10_SCHEMA_CATALOG_DIGEST,
        "schema_version": 10,
        "tenant_id": tenant_id,
    }


def validate_portable_file_set_v10(
    files: Mapping[str, bytes], *, tenant_id: str, allow_development_bindings: bool = False,
) -> None:
    """Validate base lossless evidence plus replay and v5 facet review bindings."""
    from .v5 import validate_portable_file_set_v5
    from .v6 import V6_SIDECAR_PATHS
    from .v7 import V7_SIDECAR_PATHS, validate_sharing_authority
    from .v8_capture_metadata import validate_capture_metadata
    from .v8_custody import validate_capture_custody
    from .v9 import V9_SIDECAR_PATHS, validate_source_authority_v9

    try:
        snapshot = dict(files)
        if not snapshot.keys() >= FACET_SIDECAR_PATHS:
            raise PortableValidationError("Portable v10 facet sidecars missing")
        base = {
            path: payload
            for path, payload in snapshot.items()
            if path
            not in FACET_SIDECAR_PATHS | V6_SIDECAR_PATHS | V7_SIDECAR_PATHS | V9_SIDECAR_PATHS
        }
        validate_portable_file_set_v5(
            base, tenant_id=tenant_id, allow_facet_bindings=True,
            allow_development_bindings=allow_development_bindings,
        )
        validate_source_authority_v9(snapshot)
        validate_sharing_authority(snapshot)
        validate_capture_metadata(snapshot)
        validate_capture_custody(snapshot)
        issuer = _portable_record(snapshot[ISSUER_MIGRATION_PATH], "issuer migration")
        references = references_from_portable_files(snapshot)
        decode_portable_facets(
            snapshot, brain_id=cast(str, issuer["brain_id"]), references=references
        )
        proposals = _records(snapshot, "history/proposals/", "proposal")
        for path, payload in snapshot.items():
            if _BINDING_PATH.fullmatch(path) is None:
                continue
            binding = _portable_record(payload, "review binding")
            if binding.get("base_schema_version", binding["schema_version"]) == 5:
                proposal = proposals[cast(str, binding["proposal_id"])]
                validate_facet_binding_record(
                    binding,
                    proposal=proposal,
                    files=snapshot,
                    references=references,
                    tenant_id=tenant_id,
                    allow_development_binding=allow_development_bindings,
                )
    except PortableValidationError:
        raise
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise PortableValidationError("Portable v10 evidence invalid") from error


def validated_portable_snapshot_v10(
    root: Path,
    *,
    expected_root_identity: RootIdentity | None = None,
) -> PortableSnapshot:
    """Capture one descriptor-confined byte snapshot and validate its v10 manifest."""
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
        inventory = _manifest(manifest, version=10, catalog=PORTABLE_V10_SCHEMA_CATALOG_DIGEST)
        payloads = {
            path: payload for path, payload in files.items() if path != "portable-manifest.json"
        }
        if inventory != {path: sha256(payload).hexdigest() for path, payload in payloads.items()}:
            raise PortableValidationError("Portable v10 manifest checksum or inventory mismatch")
        validate_portable_file_set_v10(payloads, tenant_id=cast(str, manifest["tenant_id"]))
        assert_root_identity(root, identity)
    except PortableValidationError:
        raise
    except (OSError, RootConfinementError, KeyError, TypeError, ValueError) as error:
        raise PortableValidationError("Portable v10 root or manifest invalid") from error
    return PortableSnapshot(identity, manifest, MappingProxyType(files))


__all__ = ["manifest_v10", "validate_portable_file_set_v10", "validated_portable_snapshot_v10"]

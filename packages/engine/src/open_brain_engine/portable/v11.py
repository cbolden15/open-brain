"""Portable v11 snapshot composition over unchanged inherited evidence formats.

The coordinator owns the draft catalog and public version dispatcher. No v11
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
from .v11_catalog import PORTABLE_V11_SCHEMA_CATALOG_DIGEST as PORTABLE_V11_SCHEMA_CATALOG_DIGEST


def manifest_v11(
    files: Mapping[str, bytes],
    *,
    tenant_id: str,
    export_id: str,
    created_at: str,
) -> dict[str, object]:
    """Build a v11 manifest over exact inherited and facet payload bytes."""
    return {
        "compatibility": {"maximum_contract_version": "11", "minimum_contract_version": "1"},
        "contract_version": "11",
        "created_at": created_at,
        "export_id": export_id,
        "files": [
            {"path": path, "sha256": sha256(payload).hexdigest()}
            for path, payload in sorted(files.items())
        ],
        "layout_version": 11,
        "schema_catalog_digest": PORTABLE_V11_SCHEMA_CATALOG_DIGEST,
        "schema_version": 11,
        "tenant_id": tenant_id,
    }


def validate_portable_file_set_v11(files: Mapping[str, bytes], *, tenant_id: str) -> None:
    """Validate base lossless evidence plus replay and v5 facet review bindings."""
    from .developments_v11 import (
        DEVELOPMENT_APPEND_PATH,
        DEVELOPMENT_EVIDENCE_PATH,
        validate_append_operations,
        validate_development_evidence,
    )
    from .v10 import validate_portable_file_set_v10

    try:
        snapshot = dict(files)
        if not snapshot.keys() >= FACET_SIDECAR_PATHS:
            raise PortableValidationError("Portable v11 facet sidecars missing")
        base = {
            path: payload
            for path, payload in snapshot.items()
            if path not in {DEVELOPMENT_EVIDENCE_PATH, DEVELOPMENT_APPEND_PATH}
        }
        validate_portable_file_set_v10(base, tenant_id=tenant_id)
        validate_development_evidence(snapshot)
        validate_append_operations(snapshot)
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
            if binding["schema_version"] == 5:
                proposal = proposals[cast(str, binding["proposal_id"])]
                validate_facet_binding_record(
                    binding,
                    proposal=proposal,
                    files=snapshot,
                    references=references,
                    tenant_id=tenant_id,
                )
    except PortableValidationError:
        raise
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise PortableValidationError("Portable v11 evidence invalid") from error


def validated_portable_snapshot_v11(
    root: Path,
    *,
    expected_root_identity: RootIdentity | None = None,
) -> PortableSnapshot:
    """Capture one descriptor-confined byte snapshot and validate its v11 manifest."""
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
        inventory = _manifest(manifest, version=11, catalog=PORTABLE_V11_SCHEMA_CATALOG_DIGEST)
        payloads = {
            path: payload for path, payload in files.items() if path != "portable-manifest.json"
        }
        if inventory != {path: sha256(payload).hexdigest() for path, payload in payloads.items()}:
            raise PortableValidationError("Portable v11 manifest checksum or inventory mismatch")
        validate_portable_file_set_v11(payloads, tenant_id=cast(str, manifest["tenant_id"]))
        assert_root_identity(root, identity)
    except PortableValidationError:
        raise
    except (OSError, RootConfinementError, KeyError, TypeError, ValueError) as error:
        raise PortableValidationError("Portable v11 root or manifest invalid") from error
    return PortableSnapshot(identity, manifest, MappingProxyType(files))


__all__ = ["manifest_v11", "validate_portable_file_set_v11", "validated_portable_snapshot_v11"]

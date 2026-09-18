"""Portable source-history metadata over exact retained v1-v3 evidence bytes."""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast
from uuid import NAMESPACE_URL, uuid5

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.storage.filesystem import (
    RootIdentity,
    assert_root_identity,
    capture_root_identity,
    open_root_descriptor,
)
from open_brain_engine.storage.markdown import parse_markdown

from .managed_v2 import validate_portable_file_set_v2
from .relationships_v1 import RELATIONSHIP_METADATA_PATH, validate_relationship_metadata
from .v1 import (
    PortableSnapshot,
    PortableValidationError,
    _identifier,
    _portable_record,
    _snapshot_directory,
    validate_portable_file_set,
)
from .v3 import PORTABLE_V3_SCHEMA_CATALOG_DIGEST, _require_manifest, validate_portable_file_set_v3

SOURCE_METADATA_PATH = "sources/logical-sources.json"
PORTABLE_V4_SCHEMA_CATALOG_DIGEST = sha256(
    portable_canonical_json_bytes(
        {
            "base": "portable-brain-v1-v3-exact-evidence",
            "logical_sources": 1,
            "canonical_revision_members": 1,
            "schema_version": 4,
        }
    )
).hexdigest()

PORTABLE_V4_RELATIONSHIPS_CATALOG_DIGEST = sha256(
    portable_canonical_json_bytes(
        {
            "base_catalog": PORTABLE_V4_SCHEMA_CATALOG_DIGEST,
            "relationship_evidence": 1,
        }
    )
).hexdigest()


def catalog_digest(files: Mapping[str, bytes]) -> str:
    return (
        PORTABLE_V4_RELATIONSHIPS_CATALOG_DIGEST
        if RELATIONSHIP_METADATA_PATH in files
        else PORTABLE_V4_SCHEMA_CATALOG_DIGEST
    )


def canonical_revision_id(publication_id: str) -> str:
    return "revision_" + str(uuid5(NAMESPACE_URL, "open-brain-publication:" + publication_id))


def manifest_v4(
    files: Mapping[str, bytes], *, tenant_id: str, export_id: str, created_at: str
) -> dict[str, object]:
    return {
        "compatibility": {"maximum_contract_version": "4", "minimum_contract_version": "1"},
        "contract_version": "4",
        "created_at": created_at,
        "export_id": export_id,
        "files": [
            {"path": path, "sha256": sha256(payload).hexdigest()}
            for path, payload in sorted(files.items())
        ],
        "layout_version": 4,
        "schema_catalog_digest": catalog_digest(files),
        "schema_version": 4,
        "tenant_id": tenant_id,
    }


def validate_portable_file_set_v4(files: Mapping[str, bytes], *, tenant_id: str) -> None:
    payload = files.get(SOURCE_METADATA_PATH)
    if payload is None:
        raise PortableValidationError("source metadata missing")
    metadata = cast(dict[str, Any], _portable_record(payload, "source metadata"))
    if (
        set(metadata) != {"schema_version", "sources", "revisions", "canonical_members"}
        or metadata["schema_version"] != 1
    ):
        raise PortableValidationError("source metadata invalid")
    base = {
        path: data
        for path, data in files.items()
        if path not in {SOURCE_METADATA_PATH, RELATIONSHIP_METADATA_PATH}
    }
    validator = (
        validate_portable_file_set_v3
        if any(p.startswith("history/review-bindings/") for p in base)
        else validate_portable_file_set_v2
        if any(p.startswith("history/managed-workspace/") for p in base)
        else validate_portable_file_set
    )
    validator(base, tenant_id=tenant_id)
    captures = {
        json.loads(data)["capture_id"]: (path, sha256(data).hexdigest())
        for path, data in base.items()
        if path.startswith("sources/captures/")
    }
    sources: dict[str, dict[str, Any]] = {}
    revisions: dict[str, dict[str, Any]] = {}
    try:
        if (
            type(metadata["schema_version"]) is not int
            or type(metadata["sources"]) is not list
            or type(metadata["revisions"]) is not list
        ):
            raise ValueError
        for source in metadata["sources"]:
            if set(source) != {
                "source_id",
                "head_capture_id",
                "space_id",
                "route_version",
                "head_version",
                "historical_only",
                "lifecycle",
                "availability",
            }:
                raise ValueError
            _identifier(source["source_id"], "source", "source identity")
            if source["source_id"] in sources or type(source["historical_only"]) is not bool:
                raise ValueError
            if source["space_id"] is not None:
                _identifier(source["space_id"], "space", "space identity")
            if (
                type(source["route_version"]) is not int
                or source["route_version"] < 0
                or type(source["head_version"]) is not int
                or source["head_version"] < 1
                or source["lifecycle"] not in {"active", "retired"}
                or source["availability"] not in {"available", "missing", "inaccessible", "unknown"}
            ):
                raise ValueError
            sources[source["source_id"]] = source
        sequences = set()
        for revision in metadata["revisions"]:
            if set(revision) != {
                "capture_id",
                "source_id",
                "sequence",
                "predecessor_capture_id",
                "source_path",
                "source_sha256",
                "recorded_at",
                "diagnostic",
            }:
                raise ValueError
            capture_id = revision["capture_id"]
            if capture_id in revisions or revision["source_id"] not in sources:
                raise ValueError
            if captures.get(capture_id) != (revision["source_path"], revision["source_sha256"]):
                raise ValueError
            capture = json.loads(base[revision["source_path"]])
            if revision["recorded_at"] != capture["accepted_at"] or revision["diagnostic"] not in {
                None,
                "ungrouped_legacy_history",
            }:
                raise ValueError
            sequence = revision["sequence"]
            if (
                type(sequence) is not int
                or sequence < 1
                or (revision["source_id"], sequence) in sequences
            ):
                raise ValueError
            sequences.add((revision["source_id"], sequence))
            revisions[capture_id] = revision
        if set(revisions) != set(captures):
            raise ValueError
        for source_id, source in sources.items():
            if revisions[source["head_capture_id"]]["source_id"] != source_id:
                raise ValueError
        for revision in revisions.values():
            predecessor = revision["predecessor_capture_id"]
            if predecessor is not None and (
                revisions[predecessor]["source_id"] != revision["source_id"]
                or revisions[predecessor]["sequence"] >= revision["sequence"]
            ):
                raise ValueError
        members = metadata["canonical_members"]
        if not isinstance(members, list):
            raise ValueError
        seen = set()
        for member in members:
            if set(member) != {"revision_id", "page_id", "publication_id", "ordinal", "capture_id"}:
                raise ValueError
            if (
                member["capture_id"] not in revisions
                or type(member["ordinal"]) is not int
                or member["ordinal"] < 0
            ):
                raise ValueError
            key = (member["revision_id"], member["ordinal"])
            if key in seen:
                raise ValueError
            seen.add(key)
        expected_members = []
        for path, data in sorted(base.items()):
            if not path.startswith("history/publications/"):
                continue
            publication = json.loads(data)
            page = parse_markdown(
                base64.b64decode(publication["published_bytes_base64"], validate=True)
            )
            for ordinal, capture_id in enumerate(cast(list[str], page.fields["provenance"])):
                expected_members.append(
                    {
                        "revision_id": canonical_revision_id(publication["publication_id"]),
                        "page_id": publication["page_id"],
                        "publication_id": publication["publication_id"],
                        "ordinal": ordinal,
                        "capture_id": capture_id,
                    }
                )
        if sorted(members, key=lambda x: (x["publication_id"], x["ordinal"])) != sorted(
            expected_members, key=lambda x: (x["publication_id"], x["ordinal"])
        ):
            raise ValueError
    except TypeError, KeyError, ValueError:
        raise PortableValidationError("source metadata invalid") from None
    relationships = files.get(RELATIONSHIP_METADATA_PATH)
    if relationships is not None:
        validate_relationship_metadata(relationships, metadata)


def validated_portable_snapshot_v4(
    root: Path, *, expected_root_identity: RootIdentity | None = None
) -> PortableSnapshot:
    identity = (
        capture_root_identity(root) if expected_root_identity is None else expected_root_identity
    )
    descriptor = open_root_descriptor(root, identity)
    files: dict[str, bytes] = {}
    try:
        _snapshot_directory(descriptor, (), files)
    finally:
        os.close(descriptor)
    manifest = _portable_record(files.get("portable-manifest.json", b""), "manifest")
    if (
        manifest.get("schema_version") != 4
        or manifest.get("layout_version") != 4
        or manifest.get("contract_version") != "4"
        or manifest.get("compatibility")
        != {"maximum_contract_version": "4", "minimum_contract_version": "1"}
        or manifest.get("schema_catalog_digest") != catalog_digest(files)
    ):
        raise PortableValidationError("unsupported Portable v4 manifest")
    projected = dict(
        manifest,
        schema_version=3,
        layout_version=3,
        contract_version="3",
        compatibility={"maximum_contract_version": "3", "minimum_contract_version": "1"},
        schema_catalog_digest=PORTABLE_V3_SCHEMA_CATALOG_DIGEST,
    )
    _require_manifest(projected)
    actual = [
        {"path": path, "sha256": sha256(payload).hexdigest()}
        for path, payload in sorted(files.items())
        if path != "portable-manifest.json"
    ]
    if manifest["files"] != actual:
        raise PortableValidationError("manifest checksum mismatch")
    validate_portable_file_set_v4(
        {path: data for path, data in files.items() if path != "portable-manifest.json"},
        tenant_id=cast(str, manifest["tenant_id"]),
    )
    assert_root_identity(root, identity)
    return PortableSnapshot(
        root_identity=identity, manifest=manifest, files=MappingProxyType(files)
    )

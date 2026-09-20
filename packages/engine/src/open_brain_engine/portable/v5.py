"""Frozen Portable v5 lossless privacy and issuer commitment evidence.

Validation consumes only portable bytes. Engine privacy primitives are imported
at call time to avoid the engine's existing Portable import cycle.
"""

from __future__ import annotations

import base64
import json
import os
import tomllib
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast
from uuid import NAMESPACE_URL, uuid5

from open_brain_engine.core.access_contracts import derive_brain_id, validate_issuer_epoch
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.storage.filesystem import (
    RootConfinementError,
    RootIdentity,
    assert_root_identity,
    capture_root_identity,
    open_root_descriptor,
)

from .relationships_v1 import RELATIONSHIP_METADATA_PATH
from .v1 import (
    PortableSnapshot,
    PortableValidationError,
    _digest,
    _portable_record,
    _snapshot_directory,
    _timestamp,
    _validate_relative_path,
)
from .v3 import PORTABLE_V3_SCHEMA_CATALOG_DIGEST, _require_manifest
from .v4 import (
    PORTABLE_V4_RELATIONSHIPS_CATALOG_DIGEST,
    PORTABLE_V4_SCHEMA_CATALOG_DIGEST,
    SOURCE_METADATA_PATH,
    canonical_revision_id,
    validate_portable_file_set_v4,
)

EFFECTIVE_PRIVACY_PATH = "history/privacy/effective-privacy-v1.json"
LEGACY_BINDINGS_PATH = "history/issuer/legacy-bindings-v1.json"
ISSUER_MIGRATION_PATH = "history/issuer/migration-v1.json"
V5_SIDECAR_PATHS = frozenset({EFFECTIVE_PRIVACY_PATH, LEGACY_BINDINGS_PATH, ISSUER_MIGRATION_PATH})
PORTABLE_V5_SCHEMA_CATALOG_DIGEST = sha256(
    portable_canonical_json_bytes(
        {
            "base": "portable-brain-v4-exact-evidence",
            "effective_privacy": 1,
            "issuer_migration": 1,
            "legacy_issuer_bindings": 1,
            "relationship_evidence": "optional-v1",
            "schema_version": 5,
        }
    )
).hexdigest()


def _keys(value: object, keys: set[str]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise PortableValidationError("Portable v5 object keys invalid")
    return cast(dict[str, Any], value)


def _equal(actual: object, expected: object) -> bool:
    # Python equality aliases bool/int; canonical bytes preserve strict JSON types.
    return portable_canonical_json_bytes(actual) == portable_canonical_json_bytes(expected)


def _base64(value: object) -> bytes:
    if not isinstance(value, str):
        raise PortableValidationError("Portable v5 base64 invalid")
    decoded = base64.b64decode(value, validate=True)
    if base64.b64encode(decoded).decode("ascii") != value:
        raise PortableValidationError("Portable v5 base64 is not canonical")
    return decoded


def encode_retained_privacy_value(value: object) -> dict[str, str]:
    """Encode an exact SQLite storage value without normalizing retained text."""
    if value is None:
        return {"storage_class": "null"}
    if type(value) is str:
        return {
            "storage_class": "text",
            "utf8_base64": base64.b64encode(value.encode("utf-8")).decode("ascii"),
        }
    if type(value) is bytes:
        return {"storage_class": "blob", "base64": base64.b64encode(value).decode("ascii")}
    if type(value) is int and -(2**63) <= value < 2**63:
        return {"storage_class": "integer", "decimal": str(value)}
    if type(value) is float and value == value:
        return {"storage_class": "real", "hex": value.hex()}
    raise PortableValidationError("unsupported retained SQLite privacy value")


def decode_retained_privacy_value(value: object) -> str | int | float | bytes | None:
    """Decode the closed tagged union, requiring an identical canonical re-encoding."""
    try:
        if type(value) is not dict:
            raise ValueError
        decoded: str | int | float | bytes | None
        tag = value.get("storage_class")
        if tag == "null":
            decoded = None
        elif tag == "text":
            decoded = _base64(value["utf8_base64"]).decode("utf-8")
        elif tag == "blob":
            decoded = _base64(value["base64"])
        elif tag == "integer" and type(value["decimal"]) is str:
            decoded = int(value["decimal"])
        elif tag == "real" and type(value["hex"]) is str:
            decoded = float.fromhex(value["hex"])
        else:
            raise ValueError
        if not _equal(value, encode_retained_privacy_value(decoded)):
            raise ValueError
        return decoded
    except KeyError, TypeError, ValueError, OverflowError:
        raise PortableValidationError("retained privacy encoding invalid") from None


def manifest_v5(
    files: Mapping[str, bytes], *, tenant_id: str, export_id: str, created_at: str
) -> dict[str, object]:
    """Build the sole v5 catalog manifest over exact payload bytes."""
    return {
        "compatibility": {"maximum_contract_version": "5", "minimum_contract_version": "1"},
        "contract_version": "5",
        "created_at": created_at,
        "export_id": export_id,
        "files": [
            {"path": path, "sha256": sha256(payload).hexdigest()}
            for path, payload in sorted(files.items())
        ],
        "layout_version": 5,
        "schema_catalog_digest": PORTABLE_V5_SCHEMA_CATALOG_DIGEST,
        "schema_version": 5,
        "tenant_id": tenant_id,
    }


def _manifest(value: dict[str, Any], *, version: int, catalog: str) -> dict[str, str]:
    if (
        type(value.get("schema_version")) is not int
        or value["schema_version"] != version
        or type(value.get("layout_version")) is not int
        or value["layout_version"] != version
        or value.get("contract_version") != str(version)
        or value.get("compatibility")
        != {"maximum_contract_version": str(version), "minimum_contract_version": "1"}
        or value.get("schema_catalog_digest") != catalog
    ):
        raise PortableValidationError("unsupported Portable v5 manifest or issuer manifest")
    # Reuse the unchanged closed manifest identity/time rules.
    # Cutover export IDs are UUIDv5 commitments, unlike ordinary UUIDv4 exports.
    # _issuer verifies their exact deterministic preimage after this shape check.
    projected = dict(value)
    if version == 4:
        projected["export_id"] = "export_00000000-0000-4000-8000-000000000000"
    _require_manifest(
        dict(
            projected,
            schema_version=3,
            layout_version=3,
            contract_version="3",
            compatibility={"maximum_contract_version": "3", "minimum_contract_version": "1"},
            schema_catalog_digest=PORTABLE_V3_SCHEMA_CATALOG_DIGEST,
        )
    )
    inventory: dict[str, str] = {}
    previous = ""
    for entry in value["files"]:
        row = _keys(entry, {"path", "sha256"})
        path = row["path"]
        if not isinstance(path, str):
            raise PortableValidationError("manifest path invalid")
        _validate_relative_path(path)
        if path <= previous or path == "portable-manifest.json":
            raise PortableValidationError("manifest paths must be sorted and unique")
        inventory[path] = _digest(row["sha256"], "manifest")
        previous = path
    return inventory


def _issuer(files: Mapping[str, bytes], tenant_id: str) -> dict[str, Any]:
    identity = _keys(
        _portable_record(files[ISSUER_MIGRATION_PATH], "issuer migration"),
        {
            "schema_version",
            "tenant_id",
            "brain_id",
            "current_issuer_epoch",
            "legacy_issuer_epoch",
            "identity_recorded_at",
            "migration_marker",
        },
    )
    bindings_record = _keys(
        _portable_record(files[LEGACY_BINDINGS_PATH], "legacy bindings"),
        {"schema_version", "bindings"},
    )
    if (
        type(identity["schema_version"]) is not int
        or identity["schema_version"] != 1
        or type(bindings_record["schema_version"]) is not int
        or bindings_record["schema_version"] != 1
        or type(bindings_record["bindings"]) is not list
        or identity["tenant_id"] != tenant_id
        or identity["brain_id"] != derive_brain_id(tenant_id)
    ):
        raise PortableValidationError("issuer identity invalid")
    current = validate_issuer_epoch(identity["current_issuer_epoch"])
    _timestamp(identity["identity_recorded_at"], "issuer identity")
    legacy = identity["legacy_issuer_epoch"]
    bindings = bindings_record["bindings"]
    marker = identity["migration_marker"]
    if marker is None:
        if legacy is not None or bindings:
            raise PortableValidationError("issuer migration marker missing")
        return identity
    legacy = validate_issuer_epoch(legacy)
    if legacy >= current:
        raise PortableValidationError("issuer legacy epoch must be lower")
    marker = _keys(
        marker,
        {
            "source_portable_manifest_bytes_base64",
            "source_portable_manifest_sha256",
            "brain_id",
            "designated_legacy_issuer_epoch",
            "current_issuer_epoch",
            "legacy_binding_manifest_sha256",
            "recorded_at",
        },
    )
    _timestamp(marker["recorded_at"], "issuer migration")
    if (
        marker["brain_id"] != identity["brain_id"]
        or type(marker["designated_legacy_issuer_epoch"]) is not int
        or marker["designated_legacy_issuer_epoch"] != legacy
        or type(marker["current_issuer_epoch"]) is not int
        or marker["current_issuer_epoch"] != current
    ):
        raise PortableValidationError("issuer marker identity mismatch")
    payload = _base64(marker["source_portable_manifest_bytes_base64"])
    if sha256(payload).hexdigest() != _digest(marker["source_portable_manifest_sha256"], "issuer"):
        raise PortableValidationError("issuer manifest digest mismatch")
    historical = _portable_record(payload, "issuer manifest")
    paths = {entry["path"] for entry in cast(list[dict[str, Any]], historical.get("files", []))}
    catalog = (
        PORTABLE_V4_RELATIONSHIPS_CATALOG_DIGEST
        if RELATIONSHIP_METADATA_PATH in paths
        else PORTABLE_V4_SCHEMA_CATALOG_DIGEST
    )
    inventory = _manifest(historical, version=4, catalog=catalog)
    export_id = "export_" + str(
        uuid5(
            NAMESPACE_URL,
            "open-brain-cutover-manifest-v1:"
            + tenant_id
            + ":"
            + sha256(portable_canonical_json_bytes(historical["files"])).hexdigest(),
        )
    )
    if (
        historical["tenant_id"] != tenant_id
        or historical["created_at"] != "1970-01-01T00:00:00Z"
        or historical["export_id"] != export_id
    ):
        raise PortableValidationError("issuer cutover manifest mismatch")
    previous = ("", -1)
    covered = set()
    for binding in bindings:
        row = _keys(binding, {"artifact_path", "jsonl_ordinal", "payload_sha256", "issuer_epoch"})
        path, ordinal = row["artifact_path"], row["jsonl_ordinal"]
        if not isinstance(path, str):
            raise PortableValidationError("legacy binding path invalid")
        _validate_relative_path(path)
        digest = _digest(row["payload_sha256"], "legacy binding")
        if (
            path not in inventory
            or type(row["issuer_epoch"]) is not int
            or row["issuer_epoch"] != legacy
        ):
            raise PortableValidationError("legacy binding inventory mismatch")
        if path.endswith(".jsonl"):
            if type(ordinal) is not int or ordinal < 0:
                raise PortableValidationError("legacy JSONL ordinal invalid")
        elif ordinal is not None or digest != inventory[path]:
            raise PortableValidationError("legacy file commitment mismatch")
        key = (path, -1 if ordinal is None else ordinal)
        if key <= previous:
            raise PortableValidationError("legacy bindings must be sorted and unique")
        previous = key
        covered.add(path)
    if {path for path in inventory if not path.endswith(".jsonl")} - covered:
        raise PortableValidationError("legacy binding coverage incomplete")
    if sha256(portable_canonical_json_bytes(bindings)).hexdigest() != _digest(
        marker["legacy_binding_manifest_sha256"], "legacy bindings"
    ):
        raise PortableValidationError("legacy binding manifest digest mismatch")
    return identity


def _ordered(values: object, fields: tuple[str, ...]) -> list[dict[str, Any]]:
    if type(values) is not list:
        raise PortableValidationError("Portable v5 rows must be arrays")
    keys = [tuple(row[field] for field in fields) for row in values]
    if keys != sorted(set(keys)):
        raise PortableValidationError("Portable v5 rows must be sorted and unique")
    return cast(list[dict[str, Any]], values)


def _privacy(files: Mapping[str, bytes], identity: dict[str, Any]) -> None:
    from open_brain_engine.engine.privacy_projection import (
        RepairedPrivacyEvidence,
        RetainedPrivacyEvidence,
        apply_privacy_repair,
        effective_privacy_json,
        project_retained_privacy_evidence,
    )
    from open_brain_engine.engine.privacy_repairs import (
        PrivacyRepairReceipt,
        PrivacyRepairRequest,
        privacy_repair_request_sha256,
    )

    value = _keys(
        _portable_record(files[EFFECTIVE_PRIVACY_PATH], "effective privacy"),
        {
            "schema_version",
            "retained_privacy",
            "base_projections",
            "invalid_evidence",
            "repairs",
            "resolved_revisions",
            "resolved_search",
        },
    )
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise PortableValidationError("effective privacy version invalid")
    metadata = json.loads(files[SOURCE_METADATA_PATH])
    revisions = {row["capture_id"]: row for row in metadata["revisions"]}
    retained = {}
    for row in _ordered(value["retained_privacy"], ("capture_id",)):
        _keys(row, {"capture_id", "source_sha256", "privacy_json"})
        if (
            row["capture_id"] not in revisions
            or row["source_sha256"] != revisions[row["capture_id"]]["source_sha256"]
        ):
            raise PortableValidationError("retained privacy source binding mismatch")
        raw = decode_retained_privacy_value(row["privacy_json"])
        if type(raw) in {int, float}:
            raise PortableValidationError("numeric privacy storage requires a future migration")
        retained[row["capture_id"]] = raw
    if set(retained) != set(revisions):
        raise PortableValidationError("retained privacy revision coverage mismatch")
    members: dict[str, list[dict[str, Any]]] = {}
    for member in sorted(metadata["canonical_members"], key=lambda row: row["ordinal"]):
        members.setdefault(member["revision_id"], []).append(member)
    bases = {
        ("source_revision", key): project_retained_privacy_evidence((raw,))
        for key, raw in retained.items()
    }
    bases.update(
        {
            ("canonical_revision", key): project_retained_privacy_evidence(
                tuple(retained[row["capture_id"]] for row in rows)
            )
            for key, rows in members.items()
        }
    )
    expected_bases = [
        dict(
            target_kind=kind,
            target_id=key,
            effective_privacy=json.loads(effective_privacy_json(evidence)),
        )
        for (kind, key), evidence in sorted(bases.items())
    ]
    if not _equal(value["base_projections"], expected_bases):
        raise PortableValidationError("base privacy projection mismatch")
    markers = {}
    search_ids = set(retained) | {row["page_id"] for row in metadata["canonical_members"]}
    for row in _ordered(
        value["invalid_evidence"], ("target_kind", "target_id", "invalid_evidence_sha256")
    ):
        _keys(row, {"target_kind", "target_id", "invalid_reason", "invalid_evidence_sha256"})
        key = (row["target_kind"], row["target_id"])
        _digest(row["invalid_evidence_sha256"], "invalid evidence")
        if row["invalid_reason"] not in {"missing", "malformed", "inconsistent"}:
            raise PortableValidationError("invalid evidence reason invalid")
        if key[0] == "search_document":
            if key[1] not in search_ids:
                raise PortableValidationError("invalid search evidence target")
        else:
            base = bases.get(key)
            if (
                base is None
                or base.valid
                or base.invalid_reason is None
                or row["invalid_reason"] != base.invalid_reason.value
                or row["invalid_evidence_sha256"] != base.invalid_evidence_sha256
            ):
                raise PortableValidationError("invalid revision evidence mismatch")
        markers[(*key, row["invalid_evidence_sha256"])] = row["invalid_reason"]
    for key, base in bases.items():
        if not base.valid and (*key, base.invalid_evidence_sha256) not in markers:
            raise PortableValidationError("invalid evidence coverage incomplete")
    active: dict[tuple[str, str], Any] = {}
    owner_actor_id = tomllib.loads(files["brain.toml"].decode())["owner_actor_id"]
    repair_ids, operation_ids = set(), set()
    previous_sequence = 0
    if type(value["repairs"]) is not list:
        raise PortableValidationError("repairs must be an array")
    for row in value["repairs"]:
        receipt = PrivacyRepairReceipt.decode(row)
        if (
            type(row["receipt_version"]) is not int
            or receipt.encode().encode() != portable_canonical_json_bytes(row)
            or receipt.issuer_epoch != identity["current_issuer_epoch"]
            or receipt.owner_actor_id != owner_actor_id
            or receipt.repair_id in repair_ids
            or receipt.operation_id in operation_ids
            or receipt.repair_sequence <= previous_sequence
        ):
            raise PortableValidationError("privacy repair receipt invalid")
        _timestamp(receipt.recorded_at, "privacy repair")
        request = PrivacyRepairRequest(
            target_kind=receipt.target_kind,
            target_id=receipt.target_id,
            invalid_evidence_sha256=receipt.invalid_evidence_sha256,
            replacement=receipt.replacement,
            operation_id=receipt.operation_id,
            supersedes_repair_id=receipt.supersedes_repair_id,
        )
        if privacy_repair_request_sha256(request) != receipt.request_sha256:
            raise PortableValidationError("privacy repair request digest mismatch")
        key = (receipt.target_kind, receipt.target_id)
        prior = active.get(key)
        if (
            (*key, receipt.invalid_evidence_sha256) not in markers
            or key not in bases
            or bases[key].valid
            or receipt.supersedes_repair_id != (None if prior is None else prior.repair_id)
        ):
            raise PortableValidationError("privacy repair target or supersession invalid")
        active[key] = receipt
        repair_ids.add(receipt.repair_id)
        operation_ids.add(receipt.operation_id)
        previous_sequence = receipt.repair_sequence

    def resolve(key: tuple[str, str]) -> dict[str, Any]:
        base, receipt = bases[key], active.get(key)
        if key[0] == "canonical_revision" and not base.valid:
            member_values = []
            for member in members[key[1]]:
                capture = member["capture_id"]
                repaired = active.get(("source_revision", capture))
                member_values.append(
                    retained[capture]
                    if repaired is None
                    else portable_canonical_json_bytes(repaired.replacement.to_dict()).decode()
                )
            healed = project_retained_privacy_evidence(tuple(member_values))
            if healed.valid:
                base, receipt = healed, None
        evidence: RetainedPrivacyEvidence | RepairedPrivacyEvidence = base
        if receipt is not None and not base.valid:
            evidence = apply_privacy_repair(
                base,
                receipt.replacement,
                applied_repair_id=receipt.repair_id,
                applied_repair_sequence=receipt.repair_sequence,
            )
        else:
            receipt = None
        return dict(
            # Both evidence records have the same six portable projection fields.
            effective_privacy=json.loads(
                effective_privacy_json(cast(RetainedPrivacyEvidence, evidence))
            ),
            applied_repair_id=None if receipt is None else receipt.repair_id,
            applied_repair_sequence=None if receipt is None else receipt.repair_sequence,
        )

    resolved = {key: resolve(key) for key in bases}
    expected = [
        dict(target_kind=kind, target_id=key, **row)
        for (kind, key), row in sorted(resolved.items())
    ]
    if not _equal(value["resolved_revisions"], expected):
        raise PortableValidationError("resolved revision privacy mismatch")
    expected_search = []
    for source in metadata["sources"]:
        if (
            not source["historical_only"]
            and source["lifecycle"] == "active"
            and source["availability"] == "available"
        ):
            capture = source["head_capture_id"]
            expected_search.append(
                dict(
                    result_id=capture,
                    capture_id=capture,
                    record_type="source",
                    **resolved[("source_revision", capture)],
                )
            )
    publications = {
        json.loads(payload)["publication_id"]: json.loads(payload)
        for path, payload in files.items()
        if path.startswith("history/publications/")
    }
    # Publications bind decisions; only decisions carry the proposal identity.
    published_decisions = {publication["decision_id"] for publication in publications.values()}
    published_proposals = {
        decision["proposal_id"]
        for path, payload in files.items()
        if path.startswith("history/decisions/")
        and (decision := json.loads(payload))["decision_id"] in published_decisions
    }
    superseded = {
        json.loads(payload)["expected_publication_id"]
        for path, payload in files.items()
        if path.startswith("history/review-bindings/")
        and json.loads(payload)["proposal_id"] in published_proposals
    }
    for publication_id, publication in publications.items():
        if publication_id in superseded:
            continue
        revision = canonical_revision_id(publication_id)
        expected_search.append(
            dict(
                result_id=publication["page_id"],
                capture_id=members[revision][0]["capture_id"],
                record_type="canonical",
                **resolved[("canonical_revision", revision)],
            )
        )
    expected_search.sort(key=lambda row: row["result_id"])
    if not _equal(value["resolved_search"], expected_search):
        raise PortableValidationError("resolved search privacy mismatch")
    for row in expected_search:
        effective = row["effective_privacy"]
        if effective["invalid_reason"] is not None and markers.get(
            ("search_document", row["result_id"], effective["invalid_evidence_sha256"])
        ) != effective["invalid_reason"]:
            raise PortableValidationError("current search invalid evidence marker mismatch")


def validate_portable_file_set_v5(files: Mapping[str, bytes], *, tenant_id: str) -> None:
    """Validate all v5 evidence without reading a Brain, database, or root path."""
    try:
        snapshot = dict(files)
        if not snapshot.keys() >= V5_SIDECAR_PATHS:
            raise PortableValidationError("Portable v5 required sidecars missing")
        validate_portable_file_set_v4(
            {path: payload for path, payload in snapshot.items() if path not in V5_SIDECAR_PATHS},
            tenant_id=tenant_id,
        )
        identity = _issuer(snapshot, tenant_id)
        _privacy(snapshot, identity)
    except PortableValidationError:
        raise
    except KeyError, TypeError, ValueError, OverflowError:
        raise PortableValidationError("Portable v5 evidence invalid") from None


def validated_portable_snapshot_v5(
    root: Path, *, expected_root_identity: RootIdentity | None = None
) -> PortableSnapshot:
    """Capture once through confined descriptors, then validate immutable bytes."""
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
            os.close(descriptor)
        manifest = _portable_record(files.get("portable-manifest.json", b""), "manifest")
        inventory = _manifest(manifest, version=5, catalog=PORTABLE_V5_SCHEMA_CATALOG_DIGEST)
        payloads = {
            path: payload for path, payload in files.items() if path != "portable-manifest.json"
        }
        if inventory != {path: sha256(payload).hexdigest() for path, payload in payloads.items()}:
            raise PortableValidationError("manifest checksum or inventory mismatch")
        validate_portable_file_set_v5(payloads, tenant_id=cast(str, manifest["tenant_id"]))
        assert_root_identity(root, identity)
    except PortableValidationError:
        raise
    except OSError, RootConfinementError, KeyError, TypeError, ValueError:
        raise PortableValidationError("Portable v5 root or manifest invalid") from None
    return PortableSnapshot(
        root_identity=identity, manifest=manifest, files=MappingProxyType(files)
    )


__all__ = [
    "EFFECTIVE_PRIVACY_PATH",
    "ISSUER_MIGRATION_PATH",
    "LEGACY_BINDINGS_PATH",
    "PORTABLE_V5_SCHEMA_CATALOG_DIGEST",
    "V5_SIDECAR_PATHS",
    "decode_retained_privacy_value",
    "encode_retained_privacy_value",
    "manifest_v5",
    "validate_portable_file_set_v5",
    "validated_portable_snapshot_v5",
]

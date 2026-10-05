"""Portable 9 required, lossless historical authority. No imported consent."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.storage.filesystem import (
    RootConfinementError,
    RootIdentity,
    assert_root_identity,
    capture_root_identity,
    open_root_descriptor,
)

from .v1 import PortableSnapshot, PortableValidationError, _portable_record, _snapshot_directory
from .v4 import SOURCE_METADATA_PATH
from .v5 import ISSUER_MIGRATION_PATH, _manifest, manifest_v5, validate_portable_file_set_v5
from .v6 import (
    SOURCE_ADMISSION_PATH,
    SOURCE_LIFECYCLE_PATH,
    V6_SIDECAR_PATHS,
    _validate_source_authority,
)
from .v7 import V7_SIDECAR_PATHS, validate_sharing_authority
from .v8_capture_metadata import CAPTURE_METADATA_PATH, validate_capture_metadata
from .v8_custody import CUSTODY_PATH, validate_capture_custody

if TYPE_CHECKING:
    from open_brain_engine.engine.historical_contracts import (
        HistoricalSourceCAS,
    )
    from open_brain_engine.engine.historical_dispatch import Transition as HistoricalTransition
    from open_brain_engine.engine.historical_registry import HistoricalClaimRegistry

HISTORICAL_AUTHORITY_PATH = "history/historical-authority/reconciliation-v2.json"
V9_SIDECAR_PATHS = frozenset({HISTORICAL_AUTHORITY_PATH, CAPTURE_METADATA_PATH, CUSTODY_PATH})
PORTABLE_V9_SCHEMA_CATALOG_DIGEST = sha256(
    canonical(
        {
            "base": "portable-brain-v8-frozen-capture-evidence",
            "historical_authority": 2,
            "original_capture_metadata": 2,
            "capture_custody": 1,
            "schema_version": 9,
        }
    )
).hexdigest()
MAX_HISTORICAL_SIDECAR_BYTES = 64 * 1024 * 1024


def _kinds(version: int = 1) -> dict[str, Any]:
    from open_brain_engine.engine import historical_contracts as v1
    from open_brain_engine.engine import historical_contracts_v2 as v2

    if type(version) is not int or version not in (1, 2):
        raise ValueError("historical operation version invalid")
    module = v1 if version == 1 else v2
    suffix = "" if version == 1 else "V2"
    return {
        kind: getattr(module, name + suffix)
        for kind, name in (
            ("baseline", "HistoricalBaselineRequest"),
            ("claim", "HistoricalClaimRequest"),
            ("relation", "HistoricalCopyRelationRequest"),
            ("revocation", "HistoricalRevocationRequest"),
        )
    }


@dataclass(frozen=True, slots=True)
class ValidatedHistoricalAuthority:
    registry: HistoricalClaimRegistry
    records: tuple[HistoricalTransition, ...]


def manifest_v9(
    files: Mapping[str, bytes], *, tenant_id: str, export_id: str, created_at: str
) -> dict[str, object]:
    result = manifest_v5(files, tenant_id=tenant_id, export_id=export_id, created_at=created_at)
    result.update(
        schema_version=9,
        layout_version=9,
        contract_version="9",
        compatibility={"maximum_contract_version": "9", "minimum_contract_version": "1"},
        schema_catalog_digest=PORTABLE_V9_SCHEMA_CATALOG_DIGEST,
    )
    return result


def historical_authority_bytes(
    registry: HistoricalClaimRegistry, records: tuple[HistoricalTransition, ...]
) -> bytes:
    """Compact lossless encoding avoids repeating every prior registry N times.

    All previous/proposed memberships reconstruct from typed operations. Their
    exact digests and each original transition digest must still match. This
    preserves complete immutable records, not merely the final membership set.
    """
    from open_brain_engine.engine.historical_projection import historical_projection_rows

    historical_projection_rows(registry, records)
    operations = []
    for record in records:
        kinds = _kinds(record.request.dto_version)
        kind = next(name for name, cls in kinds.items() if type(record.request) is cls)
        operations.append(
            {
                "kind": kind,
                "request": record.request.value(),
                "receipt": record.receipt.value(),
                "previous_registry_sha256": record.previous.registry_sha256,
                "proposed_registry_sha256": record.proposed.registry_sha256,
                "transition_sha256": record.transition_sha256,
            }
        )
    raw = canonical(
        {
            "schema_version": 2,
            "registry": json.loads(registry.canonical_bytes()),
            "operations": operations,
        }
    )
    if len(raw) > MAX_HISTORICAL_SIDECAR_BYTES:
        raise ValueError("historical sidecar exceeds the Portable9 bound")
    return raw


def _decode(raw: bytes) -> ValidatedHistoricalAuthority:
    from open_brain_engine.engine.historical_dispatch import (
        BASELINE_TYPES,
        CLAIM_TYPES,
        create_historical_transition,
        decode_receipt,
    )
    from open_brain_engine.engine.historical_projection import historical_projection_rows
    from open_brain_engine.engine.historical_registry import (
        HistoricalClaimMembership,
        HistoricalClaimRegistry,
    )

    if type(raw) is not bytes or not raw or len(raw) > MAX_HISTORICAL_SIDECAR_BYTES:
        raise ValueError("historical sidecar byte bound invalid")
    value = json.loads(raw)
    if (
        type(value) is not dict
        or canonical(value) != raw
        or set(value) != {"schema_version", "registry", "operations"}
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 2
        or type(value["operations"]) is not list
    ):
        raise ValueError("historical sidecar shape invalid")
    registry = HistoricalClaimRegistry.from_bytes(canonical(value["registry"]))
    if len(value["operations"]) != registry.generation:
        raise ValueError("historical sidecar generation coverage invalid")
    previous = HistoricalClaimRegistry.empty(registry.destination)
    records = []
    for operation in value["operations"]:
        if (
            type(operation) is not dict
            or set(operation)
            != {
                "kind",
                "request",
                "receipt",
                "previous_registry_sha256",
                "proposed_registry_sha256",
                "transition_sha256",
            }
            or type(operation["kind"]) is not str
            or operation["kind"] not in _kinds()
        ):
            raise ValueError("historical operation shape invalid")
        kinds = _kinds(operation["request"]["dto_version"])
        request = kinds[operation["kind"]].from_value(operation["request"])
        members = set(previous.memberships)
        if isinstance(request, BASELINE_TYPES):
            members.add(
                HistoricalClaimMembership(
                    capture_id=request.retained_original.capture_id,
                    source_id=request.source_cas.source_id,
                    capture_source_id=request.source_cas.source_id,
                    claim_role="baseline_original",
                )
            )
        elif isinstance(request, CLAIM_TYPES):
            members.add(
                HistoricalClaimMembership(
                    capture_id=request.retained_capture.capture_id,
                    source_id=request.source_cas.source_id,
                    capture_source_id=request.capture_source_cas.source_id,
                    claim_role=request.claim_role,
                )
            )
        proposed = HistoricalClaimRegistry._create(
            registry.destination,
            previous.generation + 1,
            tuple(sorted(members, key=lambda member: member.capture_id)),
        )
        if (
            previous.registry_sha256 != operation["previous_registry_sha256"]
            or proposed.registry_sha256 != operation["proposed_registry_sha256"]
        ):
            raise ValueError("historical operation registry binding invalid")
        receipt = decode_receipt(operation["receipt"], request.dto_version)
        record = create_historical_transition(
            request=request,
            previous=previous,
            proposed=proposed,
            receipt=receipt,
        )
        if record.transition_sha256 != operation["transition_sha256"]:
            raise ValueError("historical transition digest invalid")
        records.append(record)
        previous = proposed
    result = ValidatedHistoricalAuthority(registry, tuple(records))
    historical_projection_rows(registry, result.records)
    return result


def _index(rows: object, key: str) -> dict[str, Any]:
    if type(rows) is not list:
        raise ValueError("historical witness rows invalid")
    result = {}
    for row in rows:
        if type(row) is not dict or type(row.get(key)) is not str or row[key] in result:
            raise ValueError("historical witness identity invalid")
        result[row[key]] = row
    return result


def validate_historical_authority(files: Mapping[str, bytes]) -> ValidatedHistoricalAuthority:
    """Cross-check history against independently retained archive witnesses.

    Full v9 archive validation must also validate every inherited sidecar. This
    validator does not infer current CAS equality, provider consent, upstream
    file presence, installed selection, independent custody or owner approval.
    """
    from open_brain_engine.engine.historical_admission import (
        verify_historical_baseline_payload,
    )
    from open_brain_engine.engine.historical_admission import (
        verify_historical_relation_payload as relation_v1,
    )
    from open_brain_engine.engine.historical_admission_v2 import (
        verify_historical_relation_payload as relation_v2,
    )
    from open_brain_engine.engine.historical_admission_v2 import (
        verify_portable_import_projection,
    )
    from open_brain_engine.engine.historical_contracts_v2 import RetainedCaptureEvidenceV2
    from open_brain_engine.engine.historical_dispatch import (
        BASELINE_TYPES,
        CLAIM_TYPES,
        RELATION_TYPES,
        BaselineRequest,
    )
    from open_brain_engine.engine.sharing_contracts import SharingError

    try:
        authority = _decode(files[HISTORICAL_AUTHORITY_PATH])
        issuer = json.loads(files[ISSUER_MIGRATION_PATH])
        destination = authority.registry.destination
        if type(issuer["current_issuer_epoch"]) is not int or (
            issuer["brain_id"],
            issuer["current_issuer_epoch"],
        ) != (
            destination.brain_id,
            destination.issuer_epoch,
        ):
            raise ValueError("historical issuer mismatch")
        metadata = json.loads(files[SOURCE_METADATA_PATH])
        admission = json.loads(files[SOURCE_ADMISSION_PATH])
        lifecycle = json.loads(files[SOURCE_LIFECYCLE_PATH])
        sources = _index(metadata["sources"], "source_id")
        revisions = _index(metadata["revisions"], "capture_id")
        admissions = _index(admission["revision_admission"], "capture_id")
        aliases = _index(admission["source_aliases"], "delivery_id")
        captures = {row["capture_id"]: row for row in validate_capture_metadata(files)}
        namespaces = _index(admission["source_namespaces"], "namespace_sha256")
        lifecycles = _index(lifecycle["source_lifecycle_state"], "source_id")

        def retained(evidence: Any, source_id: str) -> dict[str, Any]:
            row = revisions[evidence.capture_id]
            raw = files[row["source_path"]]
            record = json.loads(raw)
            if isinstance(evidence, RetainedCaptureEvidenceV2) and (
                evidence.correspondence_scheme == "portable_import_projection_v1"
            ):
                revision = dict(row, **admissions[evidence.capture_id])
                return verify_portable_import_projection(
                    evidence,
                    captures[evidence.capture_id],
                    revision,
                    evidence.retained_delivery_id in aliases,
                    raw,
                    issuer["tenant_id"],
                    source_id,
                )
            alias = aliases[evidence.retained_delivery_id]
            v2 = isinstance(evidence, RetainedCaptureEvidenceV2)
            revision_digest = (
                evidence.revision_request_sha256 if v2 else evidence.retained_request_sha256
            )
            alias_digest = (
                evidence.alias_evidence_sha256 if v2 else evidence.retained_request_sha256
            )
            matching = [
                key
                for key, item in admissions.items()
                if revisions[key]["source_id"] == source_id
                and item["request_sha256"] == revision_digest
            ]
            if (
                row["source_id"] != source_id
                or alias["source_id"] != source_id
                or alias["evidence_sha256"] != alias_digest
                or matching != [evidence.capture_id]
                or row["source_sha256"] != evidence.source_sha256
                or sha256(raw).hexdigest() != evidence.source_sha256
                or canonical(record) != raw
                or record["capture_id"] != evidence.capture_id
                or sha256(canonical(record["privacy"])).hexdigest() != evidence.privacy_sha256
                or record["tenant_id"] != issuer["tenant_id"]
            ):
                raise ValueError("historical retained capture mismatch")
            if v2:
                if (
                    captures[evidence.capture_id]["request_sha256"]
                    != evidence.capture_request_sha256
                ):
                    raise ValueError("historical capture commitment mismatch")
                correspondence = (
                    alias_digest == revision_digest
                    if evidence.correspondence_scheme == "direct_revision_alias"
                    else evidence.capture_request_sha256 == revision_digest
                    and alias_digest == sha256(canonical(revision_digest)).hexdigest()
                )
                if not correspondence:
                    raise ValueError("historical commitment correspondence mismatch")
            return cast(dict[str, Any], record)

        def witness(cas: HistoricalSourceCAS) -> None:
            source = sources[cas.source_id]
            if (
                revisions[cas.expected_head]["source_id"] != cas.source_id
                or source["head_version"] < cas.expected_head_version
                or source["route_version"] < cas.expected_route_version
                or lifecycles[cas.source_id]["lifecycle_version"] < cas.expected_lifecycle_version
                or admission["control_epoch"] < cas.expected_control_epoch
                or (
                    source["head_capture_id"] == cas.expected_head
                    and source["head_version"] != cas.expected_head_version
                )
                or (
                    source["head_capture_id"] != cas.expected_head
                    and source["head_version"] <= cas.expected_head_version
                )
            ):
                raise ValueError("historical source witness mismatch")

        baselines: dict[str, BaselineRequest] = {}
        for transition in authority.records:
            request = transition.request
            witness(request.source_cas)
            if isinstance(request, BASELINE_TYPES):
                request.source_cas.require_active()
                record = retained(request.retained_original, request.source_cas.source_id)
                if request.dto_version == 2 and captures[request.retained_original.capture_id][
                    "submission_path"
                ] not in (None, "owner", "import"):
                    raise ValueError("historical original path invalid")
                verify_historical_baseline_payload(cast(Any, request), record)
                observed = request.observed_delivery
                namespace = sha256(observed.submission.namespace_bytes()).hexdigest()
                if (
                    admissions[request.retained_original.capture_id]["revision_key"] is not None
                    or observed.submission.capture.tenant_id != issuer["tenant_id"]
                    or namespace in namespaces
                    and namespaces[namespace]["source_id"] != request.source_cas.source_id
                ):
                    raise ValueError("historical baseline namespace/admission mismatch")
                baselines[request.operation_id] = request
            elif isinstance(request, CLAIM_TYPES):
                witness(request.capture_source_cas)
                retained(request.retained_capture, request.capture_source_cas.source_id)
            elif isinstance(request, RELATION_TYPES):
                request.source_cas.require_active()
                request.copy_source_cas.require_active()
                witness(request.copy_source_cas)
                record = retained(request.retained_copy, request.copy_source_cas.source_id)
                if request.dto_version == 2:
                    if (
                        captures[request.retained_copy.capture_id]["submission_path"]
                        != "public_job"
                    ):
                        raise ValueError("historical copy path invalid")
                    relation_v2(baselines[request.baseline_operation_id], record)
                else:
                    baseline = baselines[request.baseline_operation_id]
                    if baseline.dto_version != 1:
                        raise ValueError("V1 relation requires a V1 baseline")
                    relation_v1(cast(Any, baseline), record)
        return authority
    except PortableValidationError:
        raise
    except KeyError, TypeError, ValueError, UnicodeError, OverflowError, SharingError:
        raise PortableValidationError("Portable v9 historical authority invalid") from None


def validate_source_authority_v9(files: Mapping[str, bytes]) -> dict[str, Any]:
    """Prove old null-key predecessors without altering the v6 interpretation."""
    from open_brain_engine.engine.historical_dispatch import BASELINE_TYPES

    authority = validate_historical_authority(files)
    historical_admissions = {
        record.request.retained_original.capture_id: {
            "revision_key": record.request.observed_delivery.submission.revision_key,
            "ordering_json": canonical(
                dict(record.request.observed_delivery.submission.ordering)
            ).decode("utf-8"),
        }
        for record in authority.records
        if isinstance(record.request, BASELINE_TYPES)
    }
    return _validate_source_authority(files, historical_admissions=historical_admissions)


def validate_portable_file_set_v9(files: Mapping[str, bytes], *, tenant_id: str) -> None:
    inherited_sidecars = V6_SIDECAR_PATHS | V7_SIDECAR_PATHS | V9_SIDECAR_PATHS
    validate_portable_file_set_v5(
        {path: raw for path, raw in files.items() if path not in inherited_sidecars},
        tenant_id=tenant_id,
    )
    validate_source_authority_v9(files)
    validate_sharing_authority(files)
    validate_capture_metadata(files)
    validate_capture_custody(files)


def validated_portable_snapshot_v9(
    root: Path, *, expected_root_identity: RootIdentity | None = None
) -> PortableSnapshot:
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
        inventory = _manifest(manifest, version=9, catalog=PORTABLE_V9_SCHEMA_CATALOG_DIGEST)
        payloads = {path: raw for path, raw in files.items() if path != "portable-manifest.json"}
        if inventory != {path: sha256(raw).hexdigest() for path, raw in payloads.items()}:
            raise PortableValidationError("manifest checksum or inventory mismatch")
        validate_portable_file_set_v9(payloads, tenant_id=cast(str, manifest["tenant_id"]))
        assert_root_identity(root, identity)
        return PortableSnapshot(
            root_identity=identity, manifest=manifest, files=MappingProxyType(files)
        )
    except PortableValidationError:
        raise
    except OSError, RootConfinementError, KeyError, TypeError, ValueError:
        raise PortableValidationError("Portable v9 root or manifest invalid") from None

"""Portable v6 closed lifecycle and revision admission evidence.

V1–v5 records retain their original byte contracts. This required sidecar carries
the additional schema-eleven authority and cannot be read as a v5 archive.
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.core.models import PrivacyDecision
from open_brain_engine.storage.filesystem import (
    RootConfinementError,
    RootIdentity,
    assert_root_identity,
    capture_root_identity,
    open_root_descriptor,
)

from .v1 import PortableSnapshot, PortableValidationError, _portable_record, _snapshot_directory
from .v4 import SOURCE_METADATA_PATH
from .v5 import (
    ISSUER_MIGRATION_PATH,
    _manifest,
    manifest_v5,
    validate_portable_file_set_v5,
)

SOURCE_LIFECYCLE_PATH = "history/sources/lifecycle-v1.json"
SOURCE_ADMISSION_PATH = "history/sources/admission-v1.json"
V6_SIDECAR_PATHS = frozenset({SOURCE_LIFECYCLE_PATH, SOURCE_ADMISSION_PATH})
PORTABLE_V6_SCHEMA_CATALOG_DIGEST = sha256(
    canonical(
        {"base": "portable-brain-v5-exact-evidence", "source_authority": 1, "schema_version": 6}
    )
).hexdigest()

# A suffix denotes the only accepted SQLite/JSON type; ? means nullable.
AUTHORITY_COLUMNS: dict[str, tuple[str, ...]] = {
    "source_lifecycle_state": ("source_id:s", "lifecycle_version:i"),
    "source_lifecycle_operations": (
        "operation_id:s",
        "request_sha256:s",
        "source_id:s",
        "expected_head:s",
        "expected_lifecycle_version:i",
        "resulting_lifecycle_version:i",
        "reason_code:s",
        "absence_evidence_digest:s?",
        "sequence:i",
        "recorded_at:s",
        "receipt_json:s",
    ),
    "source_namespaces": ("namespace_sha256:s", "namespace_json:s", "source_id:s"),
    "source_aliases": ("delivery_id:s", "source_id:s", "evidence_sha256:s"),
    "source_operations": ("operation_id:s", "request_sha256:s", "source_id:s", "receipt_json:s"),
    "source_intakes": (
        "namespace_sha256:s",
        "revision_key:s",
        "source_id:s",
        "request_sha256:s",
        "delivery_id:s",
        "plan_json:s",
        "submission_json:b",
        "receipt_json:s",
    ),
    "managed_source_deliveries": (
        "delivery_id:s",
        "envelope_sha256:s",
        "source_id:s",
        "destination_brain_id:s",
        "issuer_epoch:i",
        "expected_head:s?",
        "expected_lifecycle_version:i",
        "source_delivery_id:s",
        "receipt_json:s",
        "envelope_bytes:b",
    ),
    "revision_admission": (
        "capture_id:s",
        "request_sha256:s?",
        "revision_key:s?",
        "ordering_json:s?",
    ),
}


def manifest_v6(
    files: Mapping[str, bytes], *, tenant_id: str, export_id: str, created_at: str
) -> dict[str, object]:
    result = manifest_v5(files, tenant_id=tenant_id, export_id=export_id, created_at=created_at)
    result.update(
        schema_version=6,
        layout_version=6,
        contract_version="6",
        compatibility={"maximum_contract_version": "6", "minimum_contract_version": "1"},
        schema_catalog_digest=PORTABLE_V6_SCHEMA_CATALOG_DIGEST,
    )
    return result


def _rows(value: Any, columns: tuple[str, ...]) -> list[dict[str, Any]]:
    if type(value) is not list:
        raise ValueError("authority rows invalid")
    previous = ""
    result = []
    for row in value:
        if type(row) is not dict or set(row) != {column.split(":")[0] for column in columns}:
            raise ValueError("authority row fields invalid")
        for column in columns:
            key, kind = column.split(":")
            item = row[key]
            if item is None and kind.endswith("?"):
                continue
            if kind.startswith("i"):
                if type(item) is not int or item < 0:
                    raise ValueError("authority integer invalid")
            elif type(item) is not str or not item or "\x00" in item:
                raise ValueError("authority text invalid")
            if kind == "b":
                raw = base64.b64decode(item, validate=True)
                if base64.b64encode(raw).decode("ascii") != item:
                    raise ValueError("authority bytes invalid")
            if (key.endswith("sha256") or key == "absence_evidence_digest") and (
                len(item) != 64 or any(c not in "0123456789abcdef" for c in item)
            ):
                raise ValueError("authority digest invalid")
            if key.endswith("_json") and kind != "b" and type(json.loads(item)) is not dict:
                raise ValueError("authority JSON invalid")
        identity = row[columns[0].split(":")[0]]
        # Intakes have a composite key; their portable ordering uses delivery ID.
        if "submission_json" in row:
            identity = row["delivery_id"]
        if identity <= previous:
            raise ValueError("authority identities must be sorted and unique")
        previous = identity
        result.append(row)
    return result


def _terminal_source_receipt(value: Any) -> dict[str, Any]:
    if (
        type(value) is not dict
        or set(value)
        not in (
            {"source_id", "capture_id", "outcome", "control_epoch"},
            {"source_id", "capture_id", "outcome", "control_epoch", "custody_id"},
        )
        or value["outcome"] not in {"captured", "history_only"}
        or type(value["control_epoch"]) is not int
        or value["control_epoch"] < 0
        or value.get("custody_id") is not None
        and (type(value["custody_id"]) is not str or not value["custody_id"])
    ):
        raise ValueError("terminal source receipt invalid")
    # V1 intake receipts omit the optional null custody field; managed receipts
    # retain it. Preserve both byte contracts while comparing their exact values.
    return dict(value, custody_id=value.get("custody_id"))


def _validate_intake_capture(
    files: Mapping[str, bytes], revision: Mapping[str, Any], submission: Mapping[str, Any]
) -> None:
    from open_brain_engine.engine.normalization import _optional_text
    from open_brain_engine.engine.privacy_projection import narrow_retained_privacy_decision

    claimed = submission["capture"]
    retained = json.loads(files[revision["source_path"]])
    if (
        type(claimed) is not dict
        or set(claimed)
        != {
            "schema_version",
            "payload",
            "source_origin",
            "source_reference",
            "provenance",
            "privacy",
            "tenant_id",
            "actor_id",
            "role_claim",
            "space_id",
            "intent",
            "capture_why",
            "capture_why_origin",
            "title",
        }
        or type(claimed["schema_version"]) is not int
        or claimed["schema_version"] != 1
        or claimed["source_origin"] not in {"third_party", "unknown"}
        or claimed["space_id"] is not None
        or claimed["capture_why"] is not None
        or claimed["capture_why_origin"] != "automation_absent"
        or any(
            canonical(claimed[key]) != canonical(retained[key])
            for key in (
                "schema_version",
                "payload",
                "tenant_id",
                "actor_id",
                "role_claim",
                "intent",
                "capture_why",
            )
        )
        or canonical(claimed["provenance"])
        != canonical(
            {
                "content_origin": claimed["source_origin"],
                "owner_context": "automation_absent",
                "source_ref": claimed["source_reference"],
            }
        )
        or canonical(retained["source"])
        != canonical(
            {
                "origin": "third_party",
                "reference": claimed["source_reference"],
            }
        )
        or canonical(retained["provenance"])
        != canonical(
            {
                **claimed["provenance"],
                "transformation_receipts": [],
            }
        )
    ):
        raise ValueError("intake contradicts retained capture")
    if _optional_text(claimed["title"], field="title", maximum=200) != claimed["title"]:
        raise ValueError("intake title is not normalized")
    requested = PrivacyDecision.from_dict(claimed["privacy"])
    admitted = PrivacyDecision.from_dict(retained["privacy"])
    if canonical(narrow_retained_privacy_decision(requested, admitted.tier).to_dict()) != canonical(
        retained["privacy"]
    ):
        raise ValueError("intake privacy contradicts admitted narrowing")
    if retained["payload"].get("kind") == "file":
        digest = retained["payload"]["blob_sha256"]
        if submission["file_bytes_base64"] != base64.b64encode(
            files[f"sources/blobs/sha256/{digest[:2]}/{digest}"]
        ).decode("ascii"):
            raise ValueError("intake file bytes contradict retained blob")
    elif submission["file_bytes_base64"] is not None:
        raise ValueError("non-file intake carries file bytes")
    # Supplied title and provider delivery ID are not fields in the frozen
    # capture record. Space may be routed later; neither is an equality witness.


def _validate_intake_order(
    submission: Mapping[str, Any],
    receipt: Mapping[str, Any],
    revisions: Mapping[str, Any],
    admissions: Mapping[str, Any],
    outcomes: Mapping[str, str],
    intake_delivery_id: str,
    historical_admissions: Mapping[str, Mapping[str, Any]],
) -> None:
    revision = revisions[receipt["capture_id"]]
    expected = submission["expected_head"]
    order = submission["ordering"]
    predecessor = revision["predecessor_capture_id"]
    if expected == receipt["capture_id"] and intake_delivery_id.startswith("adoption."):
        if (
            order["kind"] == "predecessor"
            or predecessor is not None
            or receipt["outcome"] != "captured"
        ):
            raise ValueError("adopted revision order invalid")
        return
    if expected is None:
        if (
            revision["sequence"] != 1
            or predecessor is not None
            or order["kind"] == "predecessor"
            or receipt["outcome"] != "captured"
        ):
            raise ValueError("initial revision order invalid")
        return
    head = revisions[expected]
    if (
        head["source_id"] != revision["source_id"]
        or head["sequence"] >= revision["sequence"]
        or outcomes.get(expected) == "history_only"
    ):
        raise ValueError("expected head is not earlier source membership")
    head_admission = admissions[expected]
    if head_admission["revision_key"] is None and expected in historical_admissions:
        # Only v8 supplies proven baseline evidence, without rewriting old bytes.
        head_admission = historical_admissions[expected]
    if order["kind"] == "predecessor":
        if (
            predecessor != expected
            or head_admission["revision_key"] != order["revision_key"]
            or receipt["outcome"] != "captured"
        ):
            raise ValueError("predecessor admission contradicts retained chain")
    elif order["kind"] == "monotonic":
        previous = json.loads(head_admission["ordering_json"] or "null")
        if (
            type(previous) is not dict
            or any(
                order[key] != previous.get(key) for key in ("kind", "provider_namespace", "epoch")
            )
            or order["sequence"] == previous["sequence"]
            or predecessor is not None
            or receipt["outcome"]
            != ("captured" if order["sequence"] > previous["sequence"] else "history_only")
        ):
            raise ValueError("monotonic admission contradicts retained order")
    else:
        raise ValueError("unordered revision cannot advance an existing source")


def validate_source_authority(files: Mapping[str, bytes]) -> dict[str, Any]:
    """Frozen v6 interpretation requires an ordinary predecessor intake key."""
    return _validate_source_authority(files, historical_admissions={})


def _validate_source_authority(
    files: Mapping[str, bytes], *, historical_admissions: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    """Validate closed rows, hashes, receipts and references using archive bytes."""
    try:
        lifecycle = _portable_record(files.get(SOURCE_LIFECYCLE_PATH, b""), "source lifecycle")
        admission = _portable_record(files.get(SOURCE_ADMISSION_PATH, b""), "source admission")
        if (
            set(lifecycle)
            != {"schema_version", "source_lifecycle_state", "source_lifecycle_operations"}
            or set(admission)
            != {
                "schema_version",
                "control_epoch",
                *(
                    set(AUTHORITY_COLUMNS)
                    - {"source_lifecycle_state", "source_lifecycle_operations"}
                ),
            }
            or type(lifecycle["schema_version"]) is not int
            or lifecycle["schema_version"] != 1
        ):
            raise ValueError("lifecycle or admission fields invalid")
        value = dict(
            admission,
            source_lifecycle_state=lifecycle["source_lifecycle_state"],
            source_lifecycle_operations=lifecycle["source_lifecycle_operations"],
        )
        if (
            set(value) != {"schema_version", "control_epoch", *AUTHORITY_COLUMNS}
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or type(value["control_epoch"]) is not int
            or value["control_epoch"] < 0
        ):
            raise ValueError("source authority invalid")
        rows = {table: _rows(value[table], columns) for table, columns in AUTHORITY_COLUMNS.items()}
        metadata = json.loads(files[SOURCE_METADATA_PATH])
        sources = {row["source_id"]: row for row in metadata["sources"]}
        revisions = {row["capture_id"]: row for row in metadata["revisions"]}
        issuer = json.loads(files[ISSUER_MIGRATION_PATH])
        versions = {
            row["source_id"]: row["lifecycle_version"] for row in rows["source_lifecycle_state"]
        }
        if set(versions) != set(sources):
            raise ValueError("lifecycle coverage invalid")
        operations: dict[str, list[dict[str, Any]]] = {}
        sequences = set()
        for row in rows["source_lifecycle_operations"]:
            if (
                row["source_id"] not in sources
                or revisions[row["expected_head"]]["source_id"] != row["source_id"]
                or row["resulting_lifecycle_version"] != row["expected_lifecycle_version"] + 1
                or row["sequence"] < 1
                or row["sequence"] in sequences
            ):
                raise ValueError("lifecycle operation invalid")
            sequences.add(row["sequence"])
            request = {
                "dto_version": 1,
                "operation_id": row["operation_id"],
                "source_id": row["source_id"],
                "expected_head": row["expected_head"],
                "expected_lifecycle_version": row["expected_lifecycle_version"],
                "brain_id": issuer["brain_id"],
                "issuer_epoch": issuer["current_issuer_epoch"],
                "reason_code": row["reason_code"],
                "absence_evidence_digest": row["absence_evidence_digest"],
            }
            if sha256(canonical(request)).hexdigest() != row["request_sha256"]:
                raise ValueError("withdrawal request digest invalid")
            receipt = {
                "operation_id": row["operation_id"],
                "request_sha256": row["request_sha256"],
                "source_id": row["source_id"],
                "head_capture_id": row["expected_head"],
                "lifecycle_version": row["resulting_lifecycle_version"],
                "lifecycle": "retired",
            }
            receipt["receipt_sha256"] = sha256(canonical(receipt)).hexdigest()
            if canonical(json.loads(row["receipt_json"])) != canonical(receipt):
                raise ValueError("withdrawal receipt invalid")
            operations.setdefault(row["source_id"], []).append(row)
        for source_id, version in versions.items():
            observed = sorted(
                row["resulting_lifecycle_version"] for row in operations.get(source_id, [])
            )
            if observed != list(range(1, version + 1)) or (
                version
                and (
                    sources[source_id]["lifecycle"] != "retired"
                    or sources[source_id]["availability"] != "missing"
                )
            ):
                raise ValueError("lifecycle state invalid")
        namespaces: dict[str, str] = {}
        for row in rows["source_namespaces"]:
            namespace = json.loads(row["namespace_json"])
            if (
                set(namespace) != {"connector_name", "connection_id", "resource_id", "external_id"}
                or any(type(item) is not str or not item for item in namespace.values())
                or canonical(namespace).decode() != row["namespace_json"]
                or sha256(canonical(namespace)).hexdigest() != row["namespace_sha256"]
                or row["source_id"] not in sources
                or row["source_id"] in namespaces.values()
            ):
                raise ValueError("namespace invalid")
            namespaces[row["namespace_sha256"]] = row["source_id"]
        if {row["capture_id"] for row in rows["revision_admission"]} != set(revisions):
            raise ValueError("revision admission coverage invalid")
        admissions = {row["capture_id"]: row for row in rows["revision_admission"]}
        for table in ("source_aliases", "source_operations"):
            if any(row["source_id"] not in sources for row in rows[table]):
                raise ValueError("source authority reference invalid")
        receipts = {
            row["delivery_id"]: _terminal_source_receipt(json.loads(row["receipt_json"]))
            for row in rows["source_intakes"]
        }
        outcomes: dict[str, str] = {}
        for receipt in receipts.values():
            if outcomes.setdefault(receipt["capture_id"], receipt["outcome"]) != receipt["outcome"]:
                raise ValueError("revision intake outcomes disagree")
        intakes: dict[tuple[str, str], tuple[bytes, dict[str, Any]]] = {}
        for row in rows["source_intakes"]:
            raw = base64.b64decode(row["submission_json"], validate=True)
            submission = json.loads(raw)
            receipt = receipts[row["delivery_id"]]
            if (
                type(submission) is not dict
                or set(submission)
                != {
                    "dto_version",
                    "namespace",
                    "revision_key",
                    "canonical_sha256",
                    "expected_head",
                    "ordering",
                    "expected_control_epoch",
                    "capture",
                    "delivery_id",
                    "file_bytes_base64",
                }
                or type(submission["dto_version"]) is not int
                or submission["dto_version"] != 1
                or type(submission["expected_control_epoch"]) is not int
                or not 0 <= submission["expected_control_epoch"] <= value["control_epoch"]
                or sha256(canonical(submission["capture"])).hexdigest() != row["request_sha256"]
            ):
                raise ValueError("source submission or receipt invalid")
            ordering = submission["ordering"]
            if type(ordering) is not dict:
                raise ValueError("source order invalid")
            kind = ordering.get("kind")
            if kind == "unordered":
                if set(ordering) != {"kind"}:
                    raise ValueError("source order invalid")
            elif kind == "predecessor":
                if (
                    set(ordering) != {"kind", "revision_key"}
                    or type(ordering["revision_key"]) is not str
                    or not ordering["revision_key"]
                ):
                    raise ValueError("source order invalid")
            elif kind == "monotonic":
                if (
                    set(ordering) != {"kind", "provider_namespace", "epoch", "sequence"}
                    or type(ordering["sequence"]) is not int
                    or ordering["sequence"] < 0
                    or any(
                        type(ordering[key]) is not str or not ordering[key]
                        for key in ("provider_namespace", "epoch")
                    )
                ):
                    raise ValueError("source order invalid")
            else:
                raise ValueError("source order invalid")
            if (
                canonical(submission) != raw
                or namespaces.get(row["namespace_sha256"]) != row["source_id"]
                or submission["revision_key"] != row["revision_key"]
                or submission["canonical_sha256"] != row["request_sha256"]
                or sha256(canonical(submission["namespace"])).hexdigest() != row["namespace_sha256"]
                or receipt["source_id"] != row["source_id"]
                or receipt["capture_id"] not in revisions
                or revisions[receipt["capture_id"]]["source_id"] != row["source_id"]
                or receipt["control_epoch"] > value["control_epoch"]
            ):
                raise ValueError("terminal source intake invalid")
            admission = admissions[receipt["capture_id"]]
            if (
                admission["revision_key"] != row["revision_key"]
                or admission["request_sha256"] != row["request_sha256"]
                or admission["ordering_json"] is None
                or canonical(json.loads(cast(str, admission["ordering_json"])))
                != canonical(submission["ordering"])
            ):
                raise ValueError("intake revision evidence invalid")
            _validate_intake_capture(files, revisions[receipt["capture_id"]], submission)
            _validate_intake_order(
                submission,
                receipt,
                revisions,
                admissions,
                outcomes,
                row["delivery_id"],
                historical_admissions,
            )
            key = (row["source_id"], row["revision_key"])
            if key in intakes:
                raise ValueError("source intake identity duplicated")
            intakes[key] = raw, receipt
        for row in rows["managed_source_deliveries"]:
            retained = json.loads(row["receipt_json"])
            if set(retained) != {"source_receipt"}:
                raise ValueError("managed source receipt fields invalid")
            receipt = _terminal_source_receipt(retained["source_receipt"])
            raw = base64.b64decode(row["envelope_bytes"], validate=True)
            envelope = json.loads(raw)
            if (
                type(envelope) is not dict
                or set(envelope)
                != {
                    "dto_version",
                    "binding",
                    "submission",
                    "expected_lifecycle_version",
                    "delivery_id",
                }
                | ({"observation"} if envelope.get("dto_version") == 2 else set())
                or canonical(envelope) != raw
                or sha256(raw).hexdigest() != row["envelope_sha256"]
                or type(envelope["dto_version"]) is not int
                or envelope["dto_version"] not in {1, 2}
                or envelope["delivery_id"] != row["delivery_id"]
                or type(envelope["expected_lifecycle_version"]) is not int
                or envelope["expected_lifecycle_version"] < 0
                or envelope["expected_lifecycle_version"] != row["expected_lifecycle_version"]
                or type(envelope["submission"]) is not dict
                or envelope["submission"]["expected_head"] != row["expected_head"]
                or type(envelope["binding"]) is not dict
                or set(envelope["binding"])
                != {
                    "destination_brain_id",
                    "issuer_epoch",
                    "root_fingerprint",
                    "accepted_source_id",
                    "namespace",
                }
                or any(
                    type(envelope["binding"][key]) is not str or not envelope["binding"][key]
                    for key in ("destination_brain_id", "root_fingerprint", "accepted_source_id")
                )
                or type(envelope["binding"]["issuer_epoch"]) is not int
                or envelope["binding"]["issuer_epoch"] < 1
                or canonical(envelope["binding"]["namespace"])
                != canonical(envelope["submission"]["namespace"])
                or envelope["binding"]["destination_brain_id"] != row["destination_brain_id"]
                or envelope["binding"]["issuer_epoch"] != row["issuer_epoch"]
            ):
                raise ValueError("managed source envelope invalid")
            if (
                row["source_id"] not in sources
                or receipt["source_id"] != row["source_id"]
                or receipt["capture_id"] not in revisions
                or revisions[receipt["capture_id"]]["source_id"] != row["source_id"]
                or row["destination_brain_id"] != issuer["brain_id"]
                or row["issuer_epoch"] != issuer["current_issuer_epoch"]
                or row["expected_lifecycle_version"] > versions[row["source_id"]]
            ):
                raise ValueError("managed source receipt invalid")
            intake = intakes.get((row["source_id"], envelope["submission"]["revision_key"]))
            if (
                intake is None
                or canonical(envelope["submission"]) != intake[0]
                or envelope["submission"]["delivery_id"] != row["source_delivery_id"]
                or canonical(receipt) != canonical(intake[1])
            ):
                raise ValueError("managed receipt intake linkage invalid")
            if envelope["dto_version"] == 2:
                from open_brain_engine.engine.source_observation import SourceRevisionObservation

                observation = SourceRevisionObservation.from_value(envelope["observation"])
                observation.validate_capture(envelope["submission"]["capture"])
        return cast(dict[str, Any], value)
    except PortableValidationError:
        raise
    except KeyError, TypeError, ValueError, OverflowError:
        raise PortableValidationError("Portable v6 source authority invalid") from None


def validate_portable_file_set_v6(files: Mapping[str, bytes], *, tenant_id: str) -> None:
    validate_portable_file_set_v5(
        {path: payload for path, payload in files.items() if path not in V6_SIDECAR_PATHS},
        tenant_id=tenant_id,
    )
    validate_source_authority(files)


def validated_portable_snapshot_v6(
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
        inventory = _manifest(manifest, version=6, catalog=PORTABLE_V6_SCHEMA_CATALOG_DIGEST)
        payloads = {path: data for path, data in files.items() if path != "portable-manifest.json"}
        if inventory != {path: sha256(data).hexdigest() for path, data in payloads.items()}:
            raise PortableValidationError("manifest checksum or inventory mismatch")
        validate_portable_file_set_v6(payloads, tenant_id=cast(str, manifest["tenant_id"]))
        assert_root_identity(root, identity)
        return PortableSnapshot(
            root_identity=identity, manifest=manifest, files=MappingProxyType(files)
        )
    except PortableValidationError:
        raise
    except OSError, RootConfinementError, KeyError, TypeError, ValueError:
        raise PortableValidationError("Portable v6 root or manifest invalid") from None

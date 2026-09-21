"""Frozen Portable v5 evidence, standalone validation, and snapshot contract."""

from __future__ import annotations

import base64
import json
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

import pytest
from open_brain_engine.core.access_contracts import derive_brain_id
from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.engine.privacy_projection import (
    effective_privacy_json,
    project_retained_privacy_evidence,
)
from open_brain_engine.portable import v5
from open_brain_engine.portable.v1 import PortableValidationError
from open_brain_engine.portable.v4 import SOURCE_METADATA_PATH, canonical_revision_id
from open_brain_engine.portable.versioned import validated_portable_snapshot
from open_brain_engine.storage.markdown import parse_markdown

TENANT = "tenant_123e4567-e89b-42d3-a456-426614174000"
EXPORT = "export_123e4567-e89b-42d3-a456-426614174030"
NOW = "2026-09-20T12:00:00Z"
FIXTURE = Path(__file__).parents[2] / "src/open_brain_engine/portable/conformance/v1/brain-root"


def _fixture() -> dict[str, bytes]:
    files = {
        str(path.relative_to(FIXTURE)): path.read_bytes()
        for path in FIXTURE.rglob("*")
        if path.is_file() and path.name != "portable-manifest.json"
    }
    captures = sorted(
        (json.loads(payload)["capture_id"], path, payload)
        for path, payload in files.items()
        if path.startswith("sources/captures/")
    )
    sources, revisions, members = [], [], []
    retained, bases, resolved, search = [], [], [], []
    values = {}
    for index, (capture_id, path, payload) in enumerate(captures):
        capture = json.loads(payload)
        source_id = f"source_00000000-0000-4000-8000-{index:012d}"
        sources.append(
            dict(
                source_id=source_id,
                head_capture_id=capture_id,
                space_id=capture["space_id"],
                route_version=0,
                head_version=1,
                historical_only=False,
                lifecycle="active",
                availability="available",
            )
        )
        revisions.append(
            dict(
                capture_id=capture_id,
                source_id=source_id,
                sequence=1,
                predecessor_capture_id=None,
                source_path=path,
                source_sha256=sha256(payload).hexdigest(),
                recorded_at=capture["accepted_at"],
                diagnostic=None,
            )
        )
        values[capture_id] = canonical(capture["privacy"]).decode()
        retained.append(
            dict(
                capture_id=capture_id,
                source_sha256=sha256(payload).hexdigest(),
                privacy_json=v5.encode_retained_privacy_value(values[capture_id]),
            )
        )
        effective = json.loads(
            effective_privacy_json(project_retained_privacy_evidence(values[capture_id]))
        )
        row = dict(target_kind="source_revision", target_id=capture_id, effective_privacy=effective)
        bases.append(row)
        resolved.append(dict(row, applied_repair_id=None, applied_repair_sequence=None))
        search.append(
            dict(
                result_id=capture_id,
                capture_id=capture_id,
                record_type="source",
                effective_privacy=effective,
                applied_repair_id=None,
                applied_repair_sequence=None,
            )
        )
    for path, payload in list(files.items()):
        if not path.startswith("history/publications/"):
            continue
        publication = json.loads(payload)
        page = parse_markdown(base64.b64decode(publication["published_bytes_base64"]))
        provenance = cast(list[str], page.fields["provenance"])
        revision_id = canonical_revision_id(publication["publication_id"])
        for ordinal, capture_id in enumerate(provenance):
            members.append(
                dict(
                    revision_id=revision_id,
                    page_id=publication["page_id"],
                    publication_id=publication["publication_id"],
                    ordinal=ordinal,
                    capture_id=capture_id,
                )
            )
        effective = json.loads(
            effective_privacy_json(
                project_retained_privacy_evidence(tuple(values[item] for item in provenance))
            )
        )
        row = dict(
            target_kind="canonical_revision", target_id=revision_id, effective_privacy=effective
        )
        bases.append(row)
        resolved.append(dict(row, applied_repair_id=None, applied_repair_sequence=None))
        search.append(
            dict(
                result_id=publication["page_id"],
                capture_id=provenance[0],
                record_type="canonical",
                effective_privacy=effective,
                applied_repair_id=None,
                applied_repair_sequence=None,
            )
        )
    files[SOURCE_METADATA_PATH] = canonical(
        dict(schema_version=1, sources=sources, revisions=revisions, canonical_members=members)
    )

    def key(row: dict[str, Any]) -> tuple[str, str]:
        return row["target_kind"], row["target_id"]

    files[v5.EFFECTIVE_PRIVACY_PATH] = canonical(
        dict(
            schema_version=1,
            retained_privacy=retained,
            base_projections=sorted(bases, key=key),
            invalid_evidence=[],
            repairs=[],
            resolved_revisions=sorted(resolved, key=key),
            resolved_search=sorted(search, key=lambda row: row["result_id"]),
        )
    )
    files[v5.LEGACY_BINDINGS_PATH] = canonical(dict(schema_version=1, bindings=[]))
    files[v5.ISSUER_MIGRATION_PATH] = canonical(
        dict(
            schema_version=1,
            tenant_id=TENANT,
            brain_id=derive_brain_id(TENANT),
            current_issuer_epoch=1,
            legacy_issuer_epoch=None,
            identity_recorded_at=NOW,
            migration_marker=None,
        )
    )
    return files


def _write(root: Path, files: dict[str, bytes]) -> None:
    for path, payload in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    (root / "portable-manifest.json").write_bytes(
        canonical(v5.manifest_v5(files, tenant_id=TENANT, export_id=EXPORT, created_at=NOW))
    )


def test_fresh_v5_dispatch_and_catalog(tmp_path: Path) -> None:
    files = _fixture()
    v5.validate_portable_file_set_v5(files, tenant_id=TENANT)
    _write(tmp_path, files)
    snapshot = validated_portable_snapshot(tmp_path)
    assert snapshot.manifest["schema_version"] == 5
    assert (
        snapshot.manifest["schema_catalog_digest"]
        == sha256(
            canonical(
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
    )
    assert {p: b for p, b in snapshot.files.items() if p != "portable-manifest.json"} == files


@pytest.mark.parametrize(
    "value",
    [None, "", "e\u0301\x00", -9223372036854775808, 9223372036854775807, -0.0, 1.25, b"\x00\xff"],
)
def test_lossless_retained_values(value: Any) -> None:
    encoded = v5.encode_retained_privacy_value(value)
    decoded = v5.decode_retained_privacy_value(encoded)
    assert type(decoded) is type(value)
    assert v5.encode_retained_privacy_value(decoded) == encoded


@pytest.mark.parametrize(
    "value",
    [
        {"storage_class": "null", "extra": None},
        {"storage_class": "boolean"},
        {"storage_class": "integer", "decimal": True},
        {"storage_class": "integer", "decimal": "01"},
        {"storage_class": "real", "hex": "1.25"},
        {"storage_class": "blob", "base64": "YQ"},
        {"storage_class": "text", "utf8_base64": "/w=="},
    ],
)
def test_reject_noncanonical_retained_tags(value: Any) -> None:
    with pytest.raises(PortableValidationError):
        v5.decode_retained_privacy_value(value)


def _upgraded(files: dict[str, bytes]) -> None:
    from open_brain_engine.engine.issuer_state import (
        derive_legacy_bindings,
        synthetic_cutover_manifest,
    )

    historical = {
        path: payload for path, payload in files.items() if path not in v5.V5_SIDECAR_PATHS
    }
    # Historical JSONL commitments preserve sparse physical ordinals. The
    # historical bytes need not be the mutable files in today's archive.
    batch = next(path for path in historical if path.endswith(".jsonl"))
    historical[batch] = b'{"old":1}\n\n{"old":2}\n'
    historical["brain.toml"] = b"historical root configuration\n"
    manifest = canonical(synthetic_cutover_manifest(historical, tenant_id=TENANT))
    bindings = [
        dict(artifact_path=path, jsonl_ordinal=ordinal, payload_sha256=digest, issuer_epoch=1)
        for path, ordinal, digest in derive_legacy_bindings(historical)
    ]
    files[v5.LEGACY_BINDINGS_PATH] = canonical(dict(schema_version=1, bindings=bindings))
    identity = json.loads(files[v5.ISSUER_MIGRATION_PATH])
    identity.update(
        current_issuer_epoch=2,
        legacy_issuer_epoch=1,
        migration_marker=dict(
            source_portable_manifest_bytes_base64=base64.b64encode(manifest).decode(),
            source_portable_manifest_sha256=sha256(manifest).hexdigest(),
            brain_id=identity["brain_id"],
            designated_legacy_issuer_epoch=1,
            current_issuer_epoch=2,
            legacy_binding_manifest_sha256=sha256(canonical(bindings)).hexdigest(),
            recorded_at=NOW,
        ),
    )
    files[v5.ISSUER_MIGRATION_PATH] = canonical(identity)


def _privacy_state(files: dict[str, bytes], replacement: Any = None) -> dict[str, Any]:
    state: dict[str, Any] = json.loads(files[v5.EFFECTIVE_PRIVACY_PATH])
    state["retained_privacy"][0]["privacy_json"] = v5.encode_retained_privacy_value(replacement)
    values = {
        row["capture_id"]: v5.decode_retained_privacy_value(row["privacy_json"])
        for row in state["retained_privacy"]
    }
    metadata = json.loads(files[SOURCE_METADATA_PATH])
    state["invalid_evidence"] = []
    for row in state["base_projections"]:
        raw = (
            [values[row["target_id"]]]
            if row["target_kind"] == "source_revision"
            else [
                values[member["capture_id"]]
                for member in sorted(
                    metadata["canonical_members"], key=lambda item: item["ordinal"]
                )
                if member["revision_id"] == row["target_id"]
            ]
        )
        effective = json.loads(effective_privacy_json(project_retained_privacy_evidence(raw)))
        row["effective_privacy"] = effective
        if effective["invalid_reason"] is not None:
            state["invalid_evidence"].append(
                dict(
                    target_kind=row["target_kind"],
                    target_id=row["target_id"],
                    invalid_reason=effective["invalid_reason"],
                    invalid_evidence_sha256=effective["invalid_evidence_sha256"],
                )
            )
    state["resolved_revisions"] = [
        dict(row, applied_repair_id=None, applied_repair_sequence=None)
        for row in state["base_projections"]
    ]
    for row in state["resolved_search"]:
        row.update(applied_repair_id=None, applied_repair_sequence=None)
        target = (
            row["capture_id"]
            if row["record_type"] == "source"
            else next(
                member["revision_id"]
                for member in metadata["canonical_members"]
                if member["page_id"] == row["result_id"]
            )
        )
        row["effective_privacy"] = next(
            item["effective_privacy"]
            for item in state["base_projections"]
            if item["target_id"] == target
        )
        if row["effective_privacy"]["invalid_reason"] is not None:
            state["invalid_evidence"].append(
                dict(
                    target_kind="search_document",
                    target_id=row["result_id"],
                    invalid_reason=row["effective_privacy"]["invalid_reason"],
                    invalid_evidence_sha256=row["effective_privacy"]["invalid_evidence_sha256"],
                )
            )
    state["invalid_evidence"].sort(
        key=lambda row: (row["target_kind"], row["target_id"], row["invalid_evidence_sha256"])
    )
    files[v5.EFFECTIVE_PRIVACY_PATH] = canonical(state)
    return state


def _repair_state(files: dict[str, bytes]) -> dict[str, Any]:
    from open_brain_engine.core.access_contracts import validate_stored_privacy_decision
    from open_brain_engine.engine.privacy_projection import apply_privacy_repair
    from open_brain_engine.engine.privacy_repairs import (
        PrivacyRepairReceipt,
        PrivacyRepairRequest,
        privacy_repair_request_sha256,
    )

    state = _privacy_state(files)
    capture = state["retained_privacy"][0]["capture_id"]
    base = project_retained_privacy_evidence(None)
    assert base.invalid_evidence_sha256 is not None
    replacement = validate_stored_privacy_decision(
        json.loads(
            next(payload for path, payload in files.items() if path.startswith("sources/captures/"))
        )["privacy"]
    )
    previous = None
    for sequence in (2, 7):
        request = PrivacyRepairRequest(
            "source_revision",
            capture,
            base.invalid_evidence_sha256,
            replacement,
            f"operation.{sequence}",
            previous,
        )
        receipt = PrivacyRepairReceipt(
            f"repair.{sequence}",
            sequence,
            "source_revision",
            capture,
            base.invalid_evidence_sha256,
            "actor_123e4567-e89b-42d3-a456-426614174001",
            1,
            replacement,
            request.operation_id,
            privacy_repair_request_sha256(request),
            previous,
            NOW,
        )
        state["repairs"].append(json.loads(receipt.encode()))
        previous = receipt.repair_id
    applied = dict(applied_repair_id=receipt.repair_id, applied_repair_sequence=7)
    effective = json.loads(
        effective_privacy_json(
            cast(
                Any,
                apply_privacy_repair(
                    base,
                    replacement,
                    applied_repair_id=receipt.repair_id,
                    applied_repair_sequence=7,
                ),
            )
        )
    )
    for row in state["resolved_revisions"]:
        if row["target_id"] == capture:
            row.update(effective_privacy=effective, **applied)
    for row in state["resolved_search"]:
        if row["result_id"] == capture:
            row.update(effective_privacy=effective, **applied)
    # The canonical aggregate heals through the member's replacement.
    metadata = json.loads(files[SOURCE_METADATA_PATH])
    values = {
        row["capture_id"]: v5.decode_retained_privacy_value(row["privacy_json"])
        for row in state["retained_privacy"]
    }
    values[capture] = canonical(replacement.to_dict()).decode()
    for row in state["resolved_revisions"]:
        if row["target_kind"] != "canonical_revision":
            continue
        members = sorted(
            (
                member
                for member in metadata["canonical_members"]
                if member["revision_id"] == row["target_id"]
            ),
            key=lambda member: member["ordinal"],
        )
        effective = json.loads(
            effective_privacy_json(
                project_retained_privacy_evidence(
                    [values[member["capture_id"]] for member in members]
                )
            )
        )
        row.update(effective_privacy=effective)
        for search in state["resolved_search"]:
            if search["result_id"] == members[0]["page_id"]:
                search.update(effective_privacy=effective)
    files[v5.EFFECTIVE_PRIVACY_PATH] = canonical(state)
    return state


def test_upgraded_commitments_sparse_ordinals_and_changed_files() -> None:
    files = _fixture()
    _upgraded(files)
    v5.validate_portable_file_set_v5(files, tenant_id=TENANT)
    bindings = json.loads(files[v5.LEGACY_BINDINGS_PATH])["bindings"]
    assert any(row["jsonl_ordinal"] == 2 for row in bindings)


@pytest.mark.parametrize("value", [None, "malformed e\u0301", b"\x00\xff"])
def test_archive_recomputes_lossless_base_and_resolved_privacy(value: Any) -> None:
    files = _fixture()
    _privacy_state(files, value)
    v5.validate_portable_file_set_v5(files, tenant_id=TENANT)


@pytest.mark.parametrize("value", [1, 1.25])
def test_archive_rejects_numeric_storage_reserved_for_future_migration(value: Any) -> None:
    files = _fixture()
    _privacy_state(files, value)
    with pytest.raises(PortableValidationError, match="storage"):
        v5.validate_portable_file_set_v5(files, tenant_id=TENANT)


def test_repair_gaps_linear_supersession_and_healed_aggregate() -> None:
    files = _fixture()
    state = _repair_state(files)
    v5.validate_portable_file_set_v5(files, tenant_id=TENANT)
    assert [row["repair_sequence"] for row in state["repairs"]] == [2, 7]
    assert all(
        row["applied_repair_id"] is None
        for row in state["resolved_revisions"]
        if row["target_kind"] == "canonical_revision"
    )


@pytest.mark.parametrize(
    "section,mutation",
    [
        ("retained_privacy", lambda rows: rows.pop()),
        ("retained_privacy", lambda rows: rows.append(rows[0])),
        ("retained_privacy", lambda rows: rows[0].update(source_sha256="0" * 64)),
        ("base_projections", lambda rows: rows.pop()),
        ("base_projections", lambda rows: rows[0]["effective_privacy"].update(tier="public")),
        (
            "base_projections",
            lambda rows: rows[0]["effective_privacy"]["authority"].update(cloud=0),
        ),
        ("invalid_evidence", lambda rows: rows.clear()),
        ("repairs", lambda rows: rows[0].update(receipt_version=True)),
        ("repairs", lambda rows: rows[1].update(repair_sequence=2)),
        ("repairs", lambda rows: rows[0].update(repair_sequence=0)),
        ("repairs", lambda rows: rows.reverse()),
        ("repairs", lambda rows: rows[1].update(repair_id=rows[0]["repair_id"])),
        ("repairs", lambda rows: rows[1].update(operation_id=rows[0]["operation_id"])),
        ("repairs", lambda rows: rows[1].update(request_sha256="0" * 64)),
        ("repairs", lambda rows: rows[1].update(supersedes_repair_id=None)),
        ("repairs", lambda rows: rows[1].update(issuer_epoch=2)),
        ("repairs", lambda rows: rows[1].update(owner_actor_id="actor_other")),
        ("repairs", lambda rows: rows[1].update(extra=True)),
        ("resolved_revisions", lambda rows: rows.pop()),
        ("resolved_revisions", lambda rows: rows[0].update(applied_repair_id="unbound")),
        ("resolved_search", lambda rows: rows.pop()),
        ("resolved_search", lambda rows: rows[0].update(capture_id="capture_unknown")),
        ("resolved_search", lambda rows: rows.reverse()),
    ],
)
def test_rejects_privacy_drift(section: str, mutation: Any) -> None:
    files = _fixture()
    state = _repair_state(files)
    mutation(state[section])
    files[v5.EFFECTIVE_PRIVACY_PATH] = canonical(state)
    with pytest.raises(PortableValidationError):
        v5.validate_portable_file_set_v5(files, tenant_id=TENANT)


def test_snapshot_retains_bytes_after_root_files_change(tmp_path: Path) -> None:
    files = _fixture()
    _write(tmp_path, files)
    snapshot = validated_portable_snapshot(tmp_path)
    (tmp_path / v5.EFFECTIVE_PRIVACY_PATH).write_bytes(b"changed")
    assert snapshot.files[v5.EFFECTIVE_PRIVACY_PATH] == files[v5.EFFECTIVE_PRIVACY_PATH]
    with pytest.raises(TypeError):
        cast(dict[str, bytes], snapshot.files)[v5.EFFECTIVE_PRIVACY_PATH] = b"replacement"
    with pytest.raises(PortableValidationError):
        validated_portable_snapshot(tmp_path)


@pytest.mark.parametrize("presence", ["absent", "empty", "nonempty"])
def test_relationship_presence_has_one_v5_catalog(tmp_path: Path, presence: str) -> None:
    from open_brain_engine.portable.relationships_v1 import RELATIONSHIP_METADATA_PATH

    files = _fixture()
    if presence != "absent":
        relationships, decisions = [], []
        if presence == "nonempty":
            captures = [
                row["capture_id"] for row in json.loads(files[SOURCE_METADATA_PATH])["revisions"]
            ]
            identity = "relationship_00000000-0000-4000-8000-000000000001"
            relationships.append(
                dict(
                    relationship_id=identity,
                    left=dict(record_id=captures[0], revision_id=captures[0]),
                    right=dict(record_id=captures[1], revision_id=captures[1]),
                    kind="duplicate_of",
                    status="accepted",
                    version=1,
                )
            )
            decisions.append(
                dict(
                    decision_id="decision_00000000-0000-4000-8000-000000000001",
                    relationship_id=identity,
                    sequence=1,
                    decision="accept",
                    version=1,
                    recorded_at=NOW,
                    actor_id="actor_123e4567-e89b-42d3-a456-426614174001",
                )
            )
        files[RELATIONSHIP_METADATA_PATH] = canonical(
            dict(schema_version=1, relationships=relationships, decisions=decisions)
        )
    _write(tmp_path, files)
    snapshot = validated_portable_snapshot(tmp_path)
    assert snapshot.manifest["schema_catalog_digest"] == v5.PORTABLE_V5_SCHEMA_CATALOG_DIGEST
    assert (RELATIONSHIP_METADATA_PATH in snapshot.files) == (presence != "absent")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda row: row.update(schema_version=True),
        lambda row: row.update(extra=None),
        lambda row: row.update(brain_id="brain_wrong"),
        lambda row: row.update(current_issuer_epoch=True),
        lambda row: row.update(legacy_issuer_epoch=2),
        lambda row: row.update(migration_marker=None),
        lambda row: row["migration_marker"].update(source_portable_manifest_sha256="0" * 64),
        lambda row: row["migration_marker"].update(legacy_binding_manifest_sha256="0" * 64),
        lambda row: row["migration_marker"].update(designated_legacy_issuer_epoch=True),
        lambda row: row["migration_marker"].update(recorded_at="yesterday"),
    ],
)
def test_issuer_identity_and_marker_are_strict(mutation: Any) -> None:
    files = _fixture()
    _upgraded(files)
    identity = json.loads(files[v5.ISSUER_MIGRATION_PATH])
    mutation(identity)
    files[v5.ISSUER_MIGRATION_PATH] = canonical(identity)
    with pytest.raises(PortableValidationError):
        v5.validate_portable_file_set_v5(files, tenant_id=TENANT)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda rows: rows.pop(0),
        lambda rows: rows.append(rows[0]),
        lambda rows: rows.reverse(),
        lambda rows: rows[0].update(artifact_path="../escape"),
        lambda rows: rows[0].update(issuer_epoch=True),
        lambda rows: rows[0].update(payload_sha256="0" * 64),
        lambda rows: rows[0].update(jsonl_ordinal=0),
        lambda rows: next(row for row in rows if row["jsonl_ordinal"] is not None).update(
            jsonl_ordinal=True
        ),
        lambda rows: next(row for row in rows if row["jsonl_ordinal"] is not None).update(
            jsonl_ordinal=-1
        ),
    ],
)
def test_binding_tamper_rejected_even_with_recomputed_commitment(mutation: Any) -> None:
    files = _fixture()
    _upgraded(files)
    bindings = json.loads(files[v5.LEGACY_BINDINGS_PATH])
    mutation(bindings["bindings"])
    files[v5.LEGACY_BINDINGS_PATH] = canonical(bindings)
    identity = json.loads(files[v5.ISSUER_MIGRATION_PATH])
    identity["migration_marker"]["legacy_binding_manifest_sha256"] = sha256(
        canonical(bindings["bindings"])
    ).hexdigest()
    files[v5.ISSUER_MIGRATION_PATH] = canonical(identity)
    with pytest.raises(PortableValidationError):
        v5.validate_portable_file_set_v5(files, tenant_id=TENANT)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda row: row.update(layout_version=True),
        lambda row: row.update(schema_version=True),
        lambda row: row.update(extra=None),
        lambda row: row.update(schema_catalog_digest="0" * 64),
        lambda row: row["files"][0].update(extra=None),
        lambda row: row["files"][0].update(path="../escape"),
        lambda row: row["files"].reverse(),
        lambda row: row["files"].pop(),
    ],
)
def test_manifest_strictness(tmp_path: Path, mutation: Any) -> None:
    files = _fixture()
    _write(tmp_path, files)
    path = tmp_path / "portable-manifest.json"
    manifest = json.loads(path.read_bytes())
    mutation(manifest)
    path.write_bytes(canonical(manifest))
    with pytest.raises(PortableValidationError):
        validated_portable_snapshot(tmp_path)


@pytest.mark.parametrize("path", list(v5.V5_SIDECAR_PATHS))
def test_sidecars_are_required_canonical_and_closed(path: str) -> None:
    files = _fixture()
    original = files.pop(path)
    with pytest.raises(PortableValidationError):
        v5.validate_portable_file_set_v5(files, tenant_id=TENANT)
    files[path] = original + b"\n"
    with pytest.raises(PortableValidationError):
        v5.validate_portable_file_set_v5(files, tenant_id=TENANT)
    files[path] = canonical(dict(json.loads(original), extra=True))
    with pytest.raises(PortableValidationError):
        v5.validate_portable_file_set_v5(files, tenant_id=TENANT)


def test_snapshot_rejects_symlink_and_root_replacement(tmp_path: Path) -> None:
    files = _fixture()
    root = tmp_path / "root"
    _write(root, files)
    snapshot = validated_portable_snapshot(root)
    root.rename(tmp_path / "previous")
    _write(root, files)
    with pytest.raises(PortableValidationError):
        v5.validated_portable_snapshot_v5(root, expected_root_identity=snapshot.root_identity)
    path = root / v5.EFFECTIVE_PRIVACY_PATH
    path.unlink()
    path.symlink_to(tmp_path / "previous" / v5.EFFECTIVE_PRIVACY_PATH)
    with pytest.raises(PortableValidationError):
        validated_portable_snapshot(root)


def _redigest_repair(row: dict[str, Any]) -> None:
    from open_brain_engine.engine.privacy_repairs import (
        PrivacyRepairRequest,
        privacy_repair_request_sha256,
    )

    row["request_sha256"] = privacy_repair_request_sha256(
        PrivacyRepairRequest(
            target_kind=row["target_kind"],
            target_id=row["target_id"],
            invalid_evidence_sha256=row["invalid_evidence_sha256"],
            replacement=row["replacement_privacy"],
            operation_id=row["operation_id"],
            supersedes_repair_id=row["supersedes_repair_id"],
        )
    )


@pytest.mark.parametrize("supersedes", [None, "repair.missing", "repair.2"])
def test_supersession_rejected_after_request_digest_is_recomputed(supersedes: str | None) -> None:
    files = _fixture()
    state = _repair_state(files)
    third = dict(
        state["repairs"][-1],
        repair_id="repair.9",
        repair_sequence=9,
        operation_id="operation.9",
        supersedes_repair_id=supersedes,
    )
    _redigest_repair(third)
    state["repairs"].append(third)
    files[v5.EFFECTIVE_PRIVACY_PATH] = canonical(state)
    with pytest.raises(PortableValidationError, match="supersession"):
        v5.validate_portable_file_set_v5(files, tenant_id=TENANT)


def test_cross_target_supersession_rejected_with_valid_request_digest() -> None:
    files = _fixture()
    state = _repair_state(files)
    canonical_base = next(
        row
        for row in state["base_projections"]
        if row["target_kind"] == "canonical_revision"
        and row["effective_privacy"]["invalid_reason"] is not None
    )
    third = dict(
        state["repairs"][-1],
        repair_id="repair.canonical",
        repair_sequence=9,
        operation_id="operation.canonical",
        target_kind="canonical_revision",
        target_id=canonical_base["target_id"],
        invalid_evidence_sha256=canonical_base["effective_privacy"]["invalid_evidence_sha256"],
        supersedes_repair_id="repair.7",
    )
    _redigest_repair(third)
    state["repairs"].append(third)
    files[v5.EFFECTIVE_PRIVACY_PATH] = canonical(state)
    with pytest.raises(PortableValidationError, match="supersession"):
        v5.validate_portable_file_set_v5(files, tenant_id=TENANT)


def test_historical_search_markers_survive_without_current_search_result() -> None:
    files = _fixture()
    state = _privacy_state(files)
    capture = state["retained_privacy"][0]["capture_id"]
    metadata = json.loads(files[SOURCE_METADATA_PATH])
    next(row for row in metadata["sources"] if row["head_capture_id"] == capture)["lifecycle"] = (
        "retired"
    )
    files[SOURCE_METADATA_PATH] = canonical(metadata)
    state["resolved_search"] = [
        row for row in state["resolved_search"] if row["result_id"] != capture
    ]
    files[v5.EFFECTIVE_PRIVACY_PATH] = canonical(state)
    v5.validate_portable_file_set_v5(files, tenant_id=TENANT)
    assert any(
        row["target_kind"] == "search_document" and row["target_id"] == capture
        for row in state["invalid_evidence"]
    )


def test_direct_canonical_repair_is_recomputed() -> None:
    from open_brain_engine.engine.privacy_projection import apply_privacy_repair
    from open_brain_engine.engine.privacy_repairs import PrivacyRepairReceipt

    files = _fixture()
    state = _repair_state(files)
    # Use an unrepaired source base; the canonical receipt supplies the overlay.
    canonical_base = next(
        row
        for row in state["base_projections"]
        if row["target_kind"] == "canonical_revision"
        and row["effective_privacy"]["invalid_reason"] is not None
    )
    receipt_row = dict(
        state["repairs"][0],
        target_kind="canonical_revision",
        target_id=canonical_base["target_id"],
        invalid_evidence_sha256=canonical_base["effective_privacy"]["invalid_evidence_sha256"],
    )
    _redigest_repair(receipt_row)
    receipt = PrivacyRepairReceipt.decode(receipt_row)
    state = _privacy_state(files)
    state["repairs"] = [receipt_row]
    metadata = json.loads(files[SOURCE_METADATA_PATH])
    values = {
        row["capture_id"]: v5.decode_retained_privacy_value(row["privacy_json"])
        for row in state["retained_privacy"]
    }
    members = sorted(
        (row for row in metadata["canonical_members"] if row["revision_id"] == receipt.target_id),
        key=lambda row: row["ordinal"],
    )
    base = project_retained_privacy_evidence([values[row["capture_id"]] for row in members])
    effective = json.loads(
        effective_privacy_json(
            cast(
                Any,
                apply_privacy_repair(
                    base,
                    receipt.replacement,
                    applied_repair_id=receipt.repair_id,
                    applied_repair_sequence=receipt.repair_sequence,
                ),
            )
        )
    )
    for row in state["resolved_revisions"]:
        if row["target_id"] == receipt.target_id:
            row.update(
                effective_privacy=effective,
                applied_repair_id=receipt.repair_id,
                applied_repair_sequence=receipt.repair_sequence,
            )
    for row in state["resolved_search"]:
        if row["result_id"] == members[0]["page_id"]:
            row.update(
                effective_privacy=effective,
                applied_repair_id=receipt.repair_id,
                applied_repair_sequence=receipt.repair_sequence,
            )
    files[v5.EFFECTIVE_PRIVACY_PATH] = canonical(state)
    v5.validate_portable_file_set_v5(files, tenant_id=TENANT)


def test_snapshot_validation_uses_captured_payloads_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = _fixture()
    _write(tmp_path, files)
    validate = v5.validate_portable_file_set_v5

    def mutate_after_snapshot(payloads: Any, *, tenant_id: str) -> None:
        (tmp_path / v5.EFFECTIVE_PRIVACY_PATH).write_bytes(b"later mutation")
        validate(payloads, tenant_id=tenant_id)

    monkeypatch.setattr(v5, "validate_portable_file_set_v5", mutate_after_snapshot)
    snapshot = validated_portable_snapshot(tmp_path)
    assert snapshot.files[v5.EFFECTIVE_PRIVACY_PATH] == files[v5.EFFECTIVE_PRIVACY_PATH]


@pytest.mark.parametrize("record_type,repaired", [("source", False), ("source", True),
                                                 ("canonical", False)])
@pytest.mark.parametrize("mutation", ["missing", "wrong_reason"])
def test_current_search_invalid_lineage_requires_agreeing_marker(
    record_type: str, repaired: bool, mutation: str,
) -> None:
    files = _fixture()
    state = _repair_state(files) if repaired else _privacy_state(files)
    current = next(row for row in state["resolved_search"]
                   if row["record_type"] == record_type
                   and row["effective_privacy"]["invalid_reason"] is not None)
    assert (current["applied_repair_id"] is not None) == repaired
    marker = next(row for row in state["invalid_evidence"]
                  if row["target_kind"] == "search_document"
                  and row["target_id"] == current["result_id"]
                  and row["invalid_evidence_sha256"] == current[
                      "effective_privacy"]["invalid_evidence_sha256"])
    if mutation == "missing":
        state["invalid_evidence"].remove(marker)
    else:
        marker["invalid_reason"] = "malformed"
    files[v5.EFFECTIVE_PRIVACY_PATH] = canonical(state)
    with pytest.raises(PortableValidationError, match="search.*marker"):
        v5.validate_portable_file_set_v5(files, tenant_id=TENANT)


def test_current_search_accepts_extra_historical_invalid_marker() -> None:
    files = _fixture()
    state = _repair_state(files)
    state["invalid_evidence"].append(dict(
        target_kind="search_document", target_id=state["retained_privacy"][0]["capture_id"],
        invalid_reason="malformed", invalid_evidence_sha256="0" * 64))
    state["invalid_evidence"].sort(key=lambda row: (
        row["target_kind"], row["target_id"], row["invalid_evidence_sha256"]))
    files[v5.EFFECTIVE_PRIVACY_PATH] = canonical(state)
    v5.validate_portable_file_set_v5(files, tenant_id=TENANT)


@pytest.mark.parametrize("outcome", ["approved", "rejected", "pending"])
def test_automatic_publication_and_review_head_do_not_require_proposal_id(outcome: str) -> None:
    from open_brain_engine.engine import DecisionOutcome, ProposalDraft
    from open_brain_engine.engine.local import BrainEngine
    from open_brain_engine.portable.v4 import validate_portable_file_set_v4

    from packages.engine.tests.unit.engine.test_portable_v5_restore import _fixture as live_fixture

    def review_automatic_page(engine: BrainEngine) -> None:
        page = engine.retrieval.search("Synthetic canonical source", record_type="canonical")[0]
        with engine._store.connect() as connection:
            capture_id = connection.execute(
                "SELECT capture_id FROM search_documents WHERE result_id=?", (page.result_id,)
            ).fetchone()[0]
        proposal = engine.review.propose(
            (capture_id,), (ProposalDraft("Updated automatic page", "Successor body"),),
            delivery_id="v5.publication.proposal", target_page_id=page.result_id,
        )[0]
        if outcome != "pending":
            engine.review.decide(
                proposal.proposal_id, DecisionOutcome(outcome),
                delivery_id="v5.publication.decision",
                expected_review_digest=proposal.review_digest,
            )

    files = live_fixture(review_automatic_page)
    publications = [json.loads(payload) for path, payload in files.items()
                    if path.startswith("history/publications/")]
    assert len(publications) == (2 if outcome == "approved" else 1)
    assert all("proposal_id" not in publication for publication in publications)
    assert any(path.startswith("history/review-bindings/") for path in files)
    validate_portable_file_set_v4(
        {path: payload for path, payload in files.items() if path not in v5.V5_SIDECAR_PATHS},
        tenant_id=TENANT,
    )
    v5.validate_portable_file_set_v5(files, tenant_id=TENANT)
    state = json.loads(files[v5.EFFECTIVE_PRIVACY_PATH])
    canonical_rows = [row for row in state["resolved_search"] if row["record_type"] == "canonical"]
    assert len(canonical_rows) == 1
    state["resolved_search"].remove(canonical_rows[0])
    files[v5.EFFECTIVE_PRIVACY_PATH] = canonical(state)
    with pytest.raises(PortableValidationError, match="resolved search"):
        v5.validate_portable_file_set_v5(files, tenant_id=TENANT)

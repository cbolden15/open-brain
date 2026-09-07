from __future__ import annotations

import base64
import json
import shutil
import uuid
from dataclasses import replace
from hashlib import sha256
from importlib.resources import as_file, files
from pathlib import Path
from types import MappingProxyType
from typing import cast

import pytest
from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]
from open_brain_engine.ledger.model import Record
from open_brain_engine.portability import (
    ImportEnvelopeContext,
    PortabilityMappingError,
    SharedBlob,
    batch_document,
    derive_import_record_id,
    load_conformance_cases,
    load_shared_envelope_schema,
    plan_secure_node_import,
    record_document,
    reencode_portable_identifier,
    shared_brain_from_snapshot,
)
from open_brain_engine.portable import portable_canonical_json_bytes
from open_brain_engine.portable.v1 import PortableSnapshot, validated_portable_snapshot
from open_brain_engine.protocol import (
    RESOURCE_LIMITS,
    canonical_sha256,
    load_schema,
    schema_catalog,
    validate_protocol_semantics,
)
from referencing import Registry, Resource

TENANT_ID = "tenant_123e4567-e89b-42d3-a456-426614174000"
POLICY_DIGEST = "a" * 64
OBSERVED_AT = "2026-09-07T12:00:00.123Z"
DELIVERY_A = "dlv_aaaaaaaaaaaaaaaaaaaaaaaaaa"
DELIVERY_B = "dlv_bbbbbbbbbbbbbbbbbbbbbbbbbb"
_FORMAT_CHECKER = FormatChecker()


def _fixture_root(tmp_path: Path) -> Path:
    destination = tmp_path / "brain-root"
    resource = files("open_brain_engine.portable").joinpath(
        "conformance", "v1", "brain-root"
    )
    with as_file(resource) as source:
        shutil.copytree(source, destination)
    return destination


def _rewrite_manifest(root: Path) -> None:
    manifest_path = root / "portable-manifest.json"
    manifest = cast(dict[str, object], json.loads(manifest_path.read_bytes()))
    manifest["files"] = [
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": sha256(path.read_bytes()).hexdigest(),
        }
        for path in sorted(root.rglob("*"))
        if path.is_file() and path != manifest_path
    ]
    manifest_path.write_bytes(portable_canonical_json_bytes(manifest))


def _context(*delivery_ids: str) -> ImportEnvelopeContext:
    return ImportEnvelopeContext(
        observed_at=OBSERVED_AT,
        compartments=("private",),
        policy_digest=POLICY_DIGEST,
        issuer_epoch=1,
        sequencer_epoch=1,
        delivery_ids=delivery_ids,
    )


def _protocol_validator(name: str) -> Draft202012Validator:
    schemas = {schema_name: load_schema(schema_name) for schema_name in schema_catalog()}
    registry = Registry().with_resources(
        (str(schema["$id"]), Resource.from_contents(schema)) for schema in schemas.values()
    )
    return Draft202012Validator(
        schemas[name], registry=registry, format_checker=_FORMAT_CHECKER
    )


def test_complete_fixture_maps_losslessly_through_the_secure_node_kernel(
    tmp_path: Path,
) -> None:
    source = validated_portable_snapshot(_fixture_root(tmp_path))
    shared = shared_brain_from_snapshot(source)
    plan = plan_secure_node_import(shared, _context(DELIVERY_A))

    assert len(shared.records) == 18
    assert {record.family for record in shared.records} == {
        "action",
        "brain",
        "capture",
        "decision",
        "event",
        "measurement",
        "page",
        "proposal",
        "publication",
        "route",
        "space",
    }
    assert shared.reconstruct_file_set() == {
        path: payload
        for path, payload in source.files.items()
        if path != "portable-manifest.json"
    }
    assert plan.reconstruct_file_set() == shared.reconstruct_file_set()
    assert len(plan.batches) == 1
    assert all(record.ciphertext_state == "pending" for record in plan.records)
    assert all(record.ciphertext_digest is None for record in plan.records)
    shared_by_id = {record.semantic_id: record for record in shared.records}
    for record in plan.records:
        shared_source_record = shared_by_id[cast(str, record.origin_id)]
        content_derived_id = "rec_" + base64.b32encode(
            bytes.fromhex(shared_source_record.source_sha256)[:16]
        ).decode("ascii").rstrip("=").lower()
        assert record.record_id != content_derived_id

    body_validator = Draft202012Validator(
        load_shared_envelope_schema(), format_checker=_FORMAT_CHECKER
    )
    record_validator = _protocol_validator("record")
    batch_validator = _protocol_validator("commit-batch")
    for record in plan.records:
        document = record_document(record)
        assert body_validator.is_valid(document["body"])
        assert record_validator.is_valid(document)
        validate_protocol_semantics("record", document)
    for batch in plan.batches:
        document = batch_document(batch)
        assert batch_validator.is_valid(document)
        validate_protocol_semantics("commit-batch", document)
        assert batch.digest == canonical_sha256(document)


def test_packaged_conformance_vector_freezes_ids_and_digests(tmp_path: Path) -> None:
    cases = load_conformance_cases()
    expected = cast(dict[str, object], cases["expected"])
    context = cast(dict[str, object], cases["context"])
    source = validated_portable_snapshot(_fixture_root(tmp_path))
    shared = shared_brain_from_snapshot(source)
    plan = plan_secure_node_import(
        shared,
        ImportEnvelopeContext(
            observed_at=cast(str, context["observed_at"]),
            compartments=cast(list[str], context["compartments"]),
            policy_digest=cast(str, context["policy_digest"]),
            issuer_epoch=cast(int, context["issuer_epoch"]),
            sequencer_epoch=cast(int, context["sequencer_epoch"]),
            delivery_ids=cast(list[str], context["delivery_ids"]),
        ),
    )

    assert plan.brain_id == expected["brain_id"]
    assert plan.full_plan_digest == expected["full_plan_digest"]
    assert [batch.digest for batch in plan.batches] == expected["batch_digests"]
    record_keys = [
        f"{record.record_type.removeprefix('portable_brain_v1.')}:{record.origin_id}"
        for record in plan.records
    ]
    assert record_keys == expected["record_order"]
    assert shared.evidence.manifest_sha256 == expected["source_manifest_sha256"]
    assert {record.content_schema.sha256 for record in plan.records} == {
        expected["shared_envelope_schema_sha256"]
    }
    assert {
        family: sum(record.family == family for record in shared.records)
        for family in sorted({record.family for record in shared.records})
    } == expected["family_counts"]
    assert dict(zip(record_keys, (record.captured_at for record in plan.records), strict=True)) == (
        expected["captured_at"]
    )
    assert dict(
        zip(
            record_keys,
            (list(record.provenance.source_record_ids) for record in plan.records),
            strict=True,
        )
    ) == expected["provenance_record_ids"]
    assert [
        {
            "kind": "source_blob",
            "path": blob.path,
            "sha256": blob.sha256,
            "size": len(blob.data),
        }
        for blob in shared.blobs
    ] == expected["attachments"]
    assert {
        f"{record.record_type.removeprefix('portable_brain_v1.')}:{record.origin_id}": (
            record.record_id
        )
        for record in plan.records
    } == expected["record_ids"]


def test_identity_mapping_is_role_distinct_deterministic_and_not_content_derived() -> None:
    suffix = "123e4567-e89b-42d3-a456-426614174100"
    capture_id = f"capture_{suffix}"
    page_id = f"page_{suffix}"

    assert reencode_portable_identifier(TENANT_ID, "tenant", "brn").startswith("brn_")
    assert derive_import_record_id(TENANT_ID, "capture", capture_id) == derive_import_record_id(
        TENANT_ID, "capture", capture_id
    )
    assert derive_import_record_id(TENANT_ID, "capture", capture_id) != derive_import_record_id(
        TENANT_ID, "page", page_id
    )
    assert derive_import_record_id(TENANT_ID, "capture", capture_id) != f"rec_{'a' * 26}"


def test_high_precision_timestamp_stays_exact_only_in_the_protected_body(
    tmp_path: Path,
) -> None:
    root = _fixture_root(tmp_path)
    capture_path = next((root / "sources/captures").rglob("capture_*.json"))
    capture = cast(dict[str, object], json.loads(capture_path.read_bytes()))
    capture["accepted_at"] = "2026-08-30T12:00:00.1234567Z"
    capture_path.write_bytes(portable_canonical_json_bytes(capture))
    _rewrite_manifest(root)

    shared = shared_brain_from_snapshot(validated_portable_snapshot(root))
    plan = plan_secure_node_import(shared, _context(DELIVERY_A))
    mapped = next(record for record in plan.records if record.origin_id == capture["capture_id"])
    body = mapped.body

    assert mapped.captured_at is None
    assert body["source_timestamp"] == capture["accepted_at"]
    assert base64.b64decode(cast(str, body["source_bytes_base64"])) == capture_path.read_bytes()
    assert mapped.observed_at == OBSERVED_AT


def test_more_than_128_records_validate_with_cross_batch_lineage(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    batch_path = next((root / "sources/batches").rglob("*006.jsonl"))
    rows = [
        cast(dict[str, object], json.loads(line))
        for line in batch_path.read_bytes().splitlines()
    ]
    previous_id = cast(str, rows[-1]["record_id"])
    template = rows[-1]
    for number in range(127):
        row = dict(template)
        identifier = f"event_{uuid.UUID(int=number + 4096, version=4)}"
        row["record_id"] = identifier
        row["supersedes"] = previous_id
        rows.append(row)
        previous_id = identifier
    batch_path.write_bytes(
        b"".join(portable_canonical_json_bytes(row) + b"\n" for row in rows)
    )
    _rewrite_manifest(root)

    shared = shared_brain_from_snapshot(validated_portable_snapshot(root))
    plan = plan_secure_node_import(shared, _context(DELIVERY_A, DELIVERY_B))

    assert len(shared.records) > RESOURCE_LIMITS.commit_batch_items
    assert [len(tuple(batch.items)) for batch in plan.batches] == [
        128,
        len(shared.records) - 128,
    ]
    first_ids = {
        record.record_id
        for record in plan.batches[0].items
        if isinstance(record, Record)
    }
    assert any(
        first_ids.intersection(record.provenance.source_record_ids)
        for record in plan.batches[1].items
        if isinstance(record, Record)
    )


def test_owner_markdown_without_a_page_id_is_an_exact_attachment(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    owner_document = root / "content/spaces/studio/state/notes.md"
    owner_document.parent.mkdir(parents=True)
    owner_document.write_bytes(b"Owner-maintained state.\n")
    _rewrite_manifest(root)

    shared = shared_brain_from_snapshot(validated_portable_snapshot(root))
    plan = plan_secure_node_import(shared, _context(DELIVERY_A))

    assert [attachment.path for attachment in shared.attachments] == [
        "content/spaces/studio/state/notes.md"
    ]
    assert shared.attachments[0].data == owner_document.read_bytes()
    assert all(record.source_path != shared.attachments[0].path for record in shared.records)
    assert plan.reconstruct_file_set()[shared.attachments[0].path] == owner_document.read_bytes()


def test_mapping_rejects_tampering_missing_lineage_and_derived_id_collisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = validated_portable_snapshot(_fixture_root(tmp_path))
    tampered_files = dict(snapshot.files)
    tampered_files["brain.toml"] += b"# tampered\n"
    tampered = PortableSnapshot(
        snapshot.root_identity,
        snapshot.manifest,
        MappingProxyType(tampered_files),
    )
    with pytest.raises(PortabilityMappingError, match="manifest|digest"):
        shared_brain_from_snapshot(tampered)

    shared = shared_brain_from_snapshot(snapshot)
    proposal_index = next(
        index for index, record in enumerate(shared.records) if record.family == "proposal"
    )
    invalid_records = tuple(
        record
        for record in shared.records
        if record.semantic_id != "capture_123e4567-e89b-42d3-a456-426614174100"
    )
    with pytest.raises(PortabilityMappingError, match="provenance"):
        replace(shared, records=invalid_records)

    with pytest.raises(PortabilityMappingError, match="metadata"):
        replace(
            shared.records[proposal_index],
            provenance_ids=("capture_123e4567-e89b-42d3-a456-426614174101",),
        )

    monkeypatch.setattr(
        "open_brain_engine.portability.secure_node.derive_import_record_id",
        lambda *_args: "rec_aaaaaaaaaaaaaaaaaaaaaaaaaa",
    )
    with pytest.raises(PortabilityMappingError, match="collision"):
        plan_secure_node_import(shared, _context(DELIVERY_A))


def test_context_and_delivery_count_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="compartments"):
        ImportEnvelopeContext(
            observed_at=OBSERVED_AT,
            compartments=(),
            policy_digest=POLICY_DIGEST,
            issuer_epoch=1,
            sequencer_epoch=1,
            delivery_ids=(DELIVERY_A,),
        )

    shared = shared_brain_from_snapshot(validated_portable_snapshot(_fixture_root(tmp_path)))
    with pytest.raises(PortabilityMappingError, match="delivery"):
        plan_secure_node_import(shared, _context(DELIVERY_A, DELIVERY_B))


def test_blob_has_no_public_identity_and_streams_below_the_frozen_limit() -> None:
    payload = b"x" * (RESOURCE_LIMITS.blob_staging_bytes + 1)
    digest = sha256(payload).hexdigest()
    blob = SharedBlob(
        path=f"sources/blobs/sha256/{digest[:2]}/{digest}",
        sha256=digest,
        data=payload,
    )
    chunks = blob.iter_chunks()

    assert len(next(chunks)) == RESOURCE_LIMITS.blob_staging_bytes
    assert next(chunks) == b"x"
    with pytest.raises(StopIteration):
        next(chunks)
    assert not hasattr(blob, "record_id")

    with pytest.raises(PortabilityMappingError, match="blob binding"):
        SharedBlob(path=blob.path, sha256=blob.sha256, data=payload + b"tampered")


def test_shared_values_reject_unsafe_paths_duplicates_and_jsonl_drift(
    tmp_path: Path,
) -> None:
    shared = shared_brain_from_snapshot(validated_portable_snapshot(_fixture_root(tmp_path)))
    brain = next(record for record in shared.records if record.family == "brain")
    for unsafe_path in ("../brain.toml", ".open-brain/brain.toml"):
        with pytest.raises(PortabilityMappingError, match="unsafe|operational"):
            replace(brain, source_path=unsafe_path)

    with pytest.raises(PortabilityMappingError, match="duplicate"):
        replace(shared, records=(shared.records[0], *shared.records))

    event_index = next(
        index for index, record in enumerate(shared.records) if record.family == "event"
    )
    invalid_records = list(shared.records)
    event = invalid_records[event_index]
    invalid_records[event_index] = replace(
        event, source_ordinal=cast(int, event.source_ordinal) + 2
    )
    with pytest.raises(PortabilityMappingError, match="ordinals"):
        replace(shared, records=tuple(invalid_records))


def test_unreferenced_blob_inventory_is_exact_and_order_bound(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    baseline = shared_brain_from_snapshot(validated_portable_snapshot(root))
    baseline_digest = plan_secure_node_import(
        baseline, _context(DELIVERY_A)
    ).full_plan_digest
    added_digests: list[str] = []
    for payload in (b"unreferenced attachment one", b"unreferenced attachment two"):
        digest = sha256(payload).hexdigest()
        added_digests.append(digest)
        target = root / f"sources/blobs/sha256/{digest[:2]}/{digest}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    _rewrite_manifest(root)

    shared = shared_brain_from_snapshot(validated_portable_snapshot(root))
    plan = plan_secure_node_import(shared, _context(DELIVERY_A))
    assert plan.full_plan_digest != baseline_digest
    assert all(
        digest.encode("ascii") not in record.source_bytes
        for digest in added_digests
        for record in shared.records
    )

    reordered = replace(shared, blobs=tuple(reversed(shared.blobs)))
    assert (
        plan_secure_node_import(reordered, _context(DELIVERY_A)).full_plan_digest
        != plan.full_plan_digest
    )
    with pytest.raises(PortabilityMappingError, match="inventory"):
        replace(shared, blobs=shared.blobs[:-1])

    extra_payload = b"not declared by the manifest"
    extra_digest = sha256(extra_payload).hexdigest()
    extra = SharedBlob(
        path=f"sources/blobs/sha256/{extra_digest[:2]}/{extra_digest}",
        sha256=extra_digest,
        data=extra_payload,
    )
    with pytest.raises(PortabilityMappingError, match="inventory"):
        replace(shared, blobs=(*shared.blobs, extra))


def test_portable_privacy_is_source_data_not_secure_node_authority(tmp_path: Path) -> None:
    shared = shared_brain_from_snapshot(validated_portable_snapshot(_fixture_root(tmp_path)))
    plan = plan_secure_node_import(shared, _context(DELIVERY_A))
    source = next(record for record in shared.records if record.family == "capture")
    mapped = next(record for record in plan.records if record.origin_id == source.semantic_id)
    source_document = cast(dict[str, object], json.loads(source.source_bytes))
    protected_source = cast(
        dict[str, object],
        json.loads(base64.b64decode(cast(str, mapped.body["source_bytes_base64"]))),
    )

    assert protected_source["privacy"] == source_document["privacy"]
    assert tuple(mapped.compartments) == ("private",)
    assert mapped.ciphertext_state == "pending"
    assert mapped.ciphertext_digest is None
    assert set(record_document(mapped)).isdisjoint(
        {"grant_id", "key_id", "nonce", "receipt_id", "job_id", "service_state"}
    )


def test_batch_and_full_plan_digests_bind_order_and_context(tmp_path: Path) -> None:
    shared = shared_brain_from_snapshot(validated_portable_snapshot(_fixture_root(tmp_path)))
    plan = plan_secure_node_import(shared, _context(DELIVERY_A))
    document = batch_document(plan.batches[0])
    reordered = dict(document)
    reordered["items"] = list(reversed(cast(list[object], document["items"])))

    assert canonical_sha256(reordered) != plan.batches[0].digest
    changed_context = replace(_context(DELIVERY_A), observed_at="2026-09-07T12:00:01Z")
    assert (
        plan_secure_node_import(shared, changed_context).full_plan_digest
        != plan.full_plan_digest
    )

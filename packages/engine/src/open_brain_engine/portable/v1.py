from __future__ import annotations

import base64
import binascii
import json
import os
import re
import stat
import tomllib
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import cast

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.storage.filesystem import (
    RootConfinementError,
    RootIdentity,
    assert_root_identity,
    capture_root_identity,
    open_root_descriptor,
)
from open_brain_engine.storage.markdown import MarkdownFormatError, parse_markdown, render_markdown


class PortableValidationError(ValueError):
    """A Portable Brain v1 export is malformed, unsafe, or incomplete."""


# Whole-file sidecars (capture metadata, capture custody, historical authority) are
# read into memory, so one shared bound caps them for writers and readers alike.
MAX_PORTABLE_SIDECAR_BYTES = 256 * 1024 * 1024


class PortableSidecarTooLarge(ValueError):
    """An export would write a whole-file sidecar over the Portable byte bound."""

    def __init__(self, path: str, size: int, limit: int) -> None:
        super().__init__(f"Portable sidecar {path} would be {size} bytes; the limit is {limit}")
        self.path = path
        self.size = size
        self.limit = limit


@dataclass(frozen=True, slots=True)
class PortableSnapshot:
    """Immutable, descriptor-confined Portable bytes used for validation/materialization."""

    root_identity: RootIdentity
    manifest: dict[str, object]
    files: Mapping[str, bytes]


PORTABLE_V1_SCHEMA_CATALOG_DIGEST = (
    "16f06dcbf13864187ce0e3dc7fc4a0c067634eef717c58a7ee576505c10ae11b"
)
_OPERATIONAL_DIRECTORY = ".open-brain"
_OWNER_CAPABILITIES = ["canonical.publish", "capture.accept", "space.write"]
_IDENTIFIER = re.compile(
    r"^(?P<prefix>[a-z][a-z0-9_]*)_"
    r"(?P<uuid>[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$"
)
_TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z$")
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_DATED_DIRECTORY = r"(?P<year>[0-9]{4})/(?P<month>0[1-9]|1[0-2])"
_OWNER_MARKDOWN_PATH = re.compile(
    r"content/spaces/[a-z0-9]+(?:-[a-z0-9]+)*/(?:notes|state)/"
    r"[a-z0-9]+(?:[-_][a-z0-9]+)*\.md$"
)
_SPACE_MARKDOWN_PATH = re.compile(r"content/spaces/[a-z0-9]+(?:-[a-z0-9]+)*/_space\.md$")


def validate_portable_write(relative: str, payload: bytes, tenant_id: str) -> None:
    """Validate one canonical Portable v1 family record before persistence."""
    _validate_relative_path(relative)
    if not isinstance(payload, bytes):
        raise PortableValidationError("Portable write payload is invalid")
    if relative.startswith("sources/blobs/sha256/"):
        parts = PurePosixPath(relative).parts
        if (
            len(parts) != 5
            or parts[-2] != parts[-1][:2]
            or sha256(payload).hexdigest() != parts[-1]
        ):
            raise PortableValidationError("Portable blob is invalid")
        return
    if relative.endswith(".md"):
        try:
            parsed = parse_markdown(payload)
            if render_markdown(fields=parsed.fields, body=parsed.body).encode() != payload:
                raise MarkdownFormatError("noncanonical Portable Markdown")
        except MarkdownFormatError as error:
            raise PortableValidationError("Portable Markdown is invalid") from error
        fields = dict(parsed.fields)
        if relative.endswith("/_space.md"):
            _require_exact_keys(
                fields,
                {
                    "actor_id",
                    "name",
                    "role_claim",
                    "schema_version",
                    "slug",
                    "space_id",
                    "tenant_id",
                },
                "space",
            )
            _validate_role_binding(fields, tenant_id, "space")
            _identifier(fields.get("space_id"), "space", "space")
            slug = _required_string(fields, "slug", "space")
            if (
                fields.get("schema_version") != 1
                or _SLUG.fullmatch(slug) is None
                or not _required_string(fields, "name", "space")
                or relative != f"content/spaces/{slug}/_space.md"
            ):
                raise PortableValidationError("space is malformed")
            return
        _require_exact_keys(
            fields,
            {
                "actor_id",
                "modified_at",
                "page_id",
                "privacy",
                "provenance",
                "role_claim",
                "schema_version",
                "space_id",
                "status",
                "tenant_id",
                "title",
                "trust",
            },
            "canonical page",
        )
        _validate_role_binding(fields, tenant_id, "canonical page")
        _validate_privacy(fields, "canonical page")
        page_id = _identifier(fields.get("page_id"), "page", "canonical page")
        _identifier(fields.get("space_id"), "space", "canonical page")
        _timestamp(fields.get("modified_at"), "canonical page")
        provenance = _required_list(fields, "provenance", "canonical page")
        if (
            fields.get("schema_version") != 1
            or fields.get("status") not in {"active", "archived"}
            or fields.get("trust") not in {"owner", "reviewed"}
            or not _required_string(fields, "title", "canonical page")
            or not provenance
            or len(provenance) != len(set(cast(list[str], provenance)))
            or any(
                _identifier(item, "capture", "canonical page provenance") != item
                for item in provenance
            )
            or re.fullmatch(
                rf"content/spaces/[a-z0-9]+(?:-[a-z0-9]+)*/notes/{re.escape(page_id)}\.md",
                relative,
            )
            is None
        ):
            raise PortableValidationError("canonical page is malformed")
        return
    if relative.endswith(".jsonl"):
        if not payload or not payload.endswith(b"\n"):
            raise PortableValidationError("Portable batch is invalid")
        file_batch_id: str | None = None
        file_partition: str | None = None
        seen_records: dict[str, str] = {}
        for line in payload.splitlines():
            record = _portable_record(line, "batch")
            _require_exact_keys(
                record,
                {
                    "actor_id",
                    "batch_id",
                    "payload",
                    "record_id",
                    "recorded_at",
                    "role_claim",
                    "schema_version",
                    "supersedes",
                    "tenant_id",
                },
                "batch",
            )
            _validate_role_binding(record, tenant_id, "batch")
            batch_id = _identifier(record.get("batch_id"), "batch", "batch")
            partition = _timestamp(record.get("recorded_at"), "batch")[:7].replace("-", "/")
            family = _validate_payload(
                _object(record.get("payload"), "batch payload"), "batch payload"
            )
            if family not in {"event", "measurement"}:
                raise PortableValidationError("batch record family binding mismatch")
            record_id = _identifier(record.get("record_id"), family, "batch")
            supersedes = record.get("supersedes")
            if supersedes is not None:
                supersedes_id = _identifier(supersedes, family, "batch")
                if seen_records.get(supersedes_id) != family:
                    raise PortableValidationError(
                        "batch supersedes must reference an earlier same-family row"
                    )
            if (
                record.get("schema_version") != 1
                or record_id in seen_records
                or (file_batch_id is not None and file_batch_id != batch_id)
                or (file_partition is not None and file_partition != partition)
            ):
                raise PortableValidationError("batch is malformed")
            file_batch_id = batch_id
            file_partition = partition
            seen_records[record_id] = family
        if (
            file_batch_id is None
            or file_partition is None
            or relative != f"sources/batches/{file_partition}/{file_batch_id}.jsonl"
        ):
            raise PortableValidationError("batch path is malformed")
        return
    record = _portable_record(payload, "Portable record")
    specifications: dict[str, tuple[str, set[str], str, str]] = {
        "sources/captures/": (
            "capture",
            {
                "accepted_at",
                "actor_id",
                "capture_id",
                "capture_why",
                "intent",
                "original_payload",
                "payload",
                "payload_binding",
                "payload_schema_version",
                "privacy",
                "provenance",
                "receipt_refs",
                "role_claim",
                "schema_version",
                "source",
                "space_id",
                "tenant_id",
                "trust",
            },
            "capture_id",
            "capture",
        ),
        "history/proposals/": (
            "proposal",
            {
                "actor_id",
                "capture_ids",
                "evidence",
                "expected_receipt",
                "privacy",
                "proposal_id",
                "proposed_content",
                "proposed_kind",
                "recorded_at",
                "role_claim",
                "schema_version",
                "sibling_context",
                "space_id",
                "status",
                "supplied_reason",
                "tenant_id",
                "trust",
            },
            "proposal_id",
            "proposal",
        ),
        "history/routes/": (
            "routing",
            {
                "actor_id",
                "capture_id",
                "receipt",
                "recorded_at",
                "role_claim",
                "route_id",
                "schema_version",
                "space_id",
                "supersedes",
                "tenant_id",
            },
            "route_id",
            "route",
        ),
        "history/decisions/": (
            "decision",
            {
                "actor_id",
                "decision_id",
                "edited_content",
                "expected_receipt",
                "expected_state_digest",
                "outcome",
                "proposal_id",
                "recorded_at",
                "role_claim",
                "schema_version",
                "tenant_id",
                "terminal_digest",
            },
            "decision_id",
            "decision",
        ),
        "history/publications/": (
            "publication",
            {
                "actor_id",
                "decision_id",
                "page_id",
                "publication_id",
                "published_bytes_base64",
                "published_path",
                "published_sha256",
                "recorded_at",
                "role_claim",
                "schema_version",
                "tenant_id",
            },
            "publication_id",
            "publication",
        ),
        "history/actions/": (
            "action",
            {
                "action_id",
                "action_request",
                "action_request_sha256",
                "action_result",
                "action_result_sha256",
                "actor_id",
                "approval_receipt",
                "decision_id",
                "outcome",
                "proposal_id",
                "recorded_at",
                "role_claim",
                "schema_version",
                "tenant_id",
            },
            "action_id",
            "action",
        ),
    }
    prefix = next((value for value in specifications if relative.startswith(value)), None)
    if prefix is None:
        raise PortableValidationError("Portable write family is invalid")
    label, keys, identifier_key, identifier_prefix = specifications[prefix]
    _require_exact_keys(record, keys, label)
    _validate_role_binding(record, tenant_id, label)
    identifier = _identifier(record.get(identifier_key), identifier_prefix, label)
    recorded_at = _timestamp(
        record.get("accepted_at") if label == "capture" else record.get("recorded_at"), label
    )
    if (
        record.get("schema_version") != 1
        or relative != f"{prefix}{recorded_at[:4]}/{recorded_at[5:7]}/{identifier}.json"
    ):
        raise PortableValidationError(f"{label} is malformed")
    if label == "capture":
        _validate_capture_write(record)
    elif label == "proposal":
        _validate_proposal_write(record)
    elif label == "decision":
        _validate_decision_write(record)
    elif label == "publication":
        _validate_publication_write(record)
    elif label == "routing":
        _validate_routing_write(record)
    else:
        _validate_action_write(record)


def _unique_portable_values(values: list[object], label: str) -> None:
    try:
        encoded = [portable_canonical_json_bytes(value) for value in values]
    except (TypeError, ValueError) as error:
        raise PortableValidationError(f"{label} is not strict Portable JSON") from error
    if len(encoded) != len(set(encoded)):
        raise PortableValidationError(f"{label} must be unique")


def _validate_capture_write(capture: Mapping[str, object]) -> None:
    _validate_privacy(capture, "capture")
    capture_id = _identifier(capture.get("capture_id"), "capture", "capture")
    payload = _object(capture.get("payload"), "capture payload")
    family = _validate_payload(payload, "capture payload")
    if (
        capture.get("payload_schema_version") != 1
        or not (
            isinstance(capture.get("capture_why"), str) or capture.get("capture_why") is None
        )
        or not (isinstance(capture.get("intent"), str) or capture.get("intent") is None)
    ):
        raise PortableValidationError("capture is malformed")
    source = _object(capture.get("source"), "capture source")
    _require_exact_keys(source, {"origin", "reference"}, "capture source")
    if (
        source.get("origin") not in {"owner", "third_party"}
        or not isinstance(source.get("reference"), str)
        or not source["reference"]
    ):
        raise PortableValidationError("capture source is malformed")
    if capture.get("space_id") is not None:
        _identifier(capture.get("space_id"), "space", "capture")
    provenance = _validate_provenance(capture.get("provenance"), "capture")
    _validate_trust(capture.get("trust"), "capture")
    payload_digest = _canonical_digest(payload, "capture payload")
    original = _object(capture.get("original_payload"), "capture original payload")
    original_kind = _required_string(original, "kind", "capture original payload")
    if original_kind == "inline":
        _require_exact_keys(
            original, {"bytes_base64", "kind", "sha256"}, "capture original payload"
        )
        original_digest = sha256(
            _verify_encoded_digest(
                original, "bytes_base64", "sha256", "capture original payload"
            )
        ).hexdigest()
    elif original_kind == "blob":
        _require_exact_keys(original, {"blob_sha256", "kind"}, "capture original payload")
        original_digest = _digest(original.get("blob_sha256"), "capture original payload")
    else:
        raise PortableValidationError("capture original payload kind is unsupported")
    blob_digest = payload.get("blob_sha256")
    if blob_digest is not None:
        _digest(blob_digest, "capture payload")
    binding = _object(capture.get("payload_binding"), "capture payload binding")
    binding_kind = _required_string(binding, "kind", "capture payload binding")
    if binding_kind == "inline":
        _require_exact_keys(binding, {"kind", "payload_sha256"}, "capture payload binding")
        if _digest(binding.get("payload_sha256"), "capture payload binding") != payload_digest:
            raise PortableValidationError("capture payload binding digest mismatch")
    elif binding_kind == "batch":
        _require_exact_keys(
            binding, {"batch_id", "kind", "record_id"}, "capture payload binding"
        )
        _identifier(binding.get("batch_id"), "batch", "capture payload binding")
        if family not in {"event", "measurement"}:
            raise PortableValidationError("capture batch family is unsupported")
        _identifier(binding.get("record_id"), family, "capture payload binding")
    else:
        raise PortableValidationError("capture payload binding kind is unsupported")
    if source.get("reference") != provenance.get("source_ref"):
        raise PortableValidationError("capture provenance source binding mismatch")
    receipts = _required_list(capture, "receipt_refs", "capture")
    _unique_portable_values(receipts, "capture receipts")
    receipt_registry: dict[str, bytes] = {}
    expected_receipt_payload = {
        "capture_id": capture_id,
        "original_payload_sha256": original_digest,
        "payload_sha256": payload_digest,
    }
    accepted = 0
    for value in receipts:
        receipt = _validate_receipt(
            value,
            subject_id=capture_id,
            receipts=receipt_registry,
            label="capture receipt",
        )
        if receipt.get("kind") == "capture_accepted":
            if receipt.get("payload") != expected_receipt_payload:
                raise PortableValidationError("capture receipt payload binding mismatch")
            accepted += 1
    if accepted != 1:
        raise PortableValidationError("capture must have exactly one acceptance receipt")
    transformations = _required_list(
        provenance, "transformation_receipts", "capture provenance"
    )
    _unique_portable_values(transformations, "capture transformation receipts")
    for value in transformations:
        _validate_receipt(
            value,
            subject_id=capture_id,
            receipts=receipt_registry,
            label="transformation receipt",
        )


def _validate_proposal_write(proposal: Mapping[str, object]) -> None:
    _validate_privacy(proposal, "proposal")
    _validate_trust(proposal.get("trust"), "proposal")
    proposal_id = _identifier(proposal.get("proposal_id"), "proposal", "proposal")
    content = _object(proposal.get("proposed_content"), "proposal content")
    _require_exact_keys(content, {"bytes_base64", "media_type", "sha256"}, "proposal content")
    _verify_encoded_digest(content, "bytes_base64", "sha256", "proposal content")
    if (
        proposal.get("proposed_kind") not in {"page_update", "event", "measurement", "action"}
        or proposal.get("status") != "pending"
        or not isinstance(content.get("media_type"), str)
        or not content["media_type"]
        or not (
            isinstance(proposal.get("supplied_reason"), str)
            or proposal.get("supplied_reason") is None
        )
    ):
        raise PortableValidationError("proposal is malformed")
    if proposal.get("space_id") is not None:
        _identifier(proposal.get("space_id"), "space", "proposal")
    capture_ids = _required_list(proposal, "capture_ids", "proposal")
    if (
        not capture_ids
        or len(capture_ids) != len(set(cast(list[str], capture_ids)))
        or any(_identifier(item, "capture", "proposal") != item for item in capture_ids)
    ):
        raise PortableValidationError("proposal capture binding is malformed")
    evidence = _required_list(proposal, "evidence", "proposal")
    if not evidence:
        raise PortableValidationError("proposal evidence is malformed")
    for value in evidence:
        item = _object(value, "proposal evidence")
        _require_exact_keys(item, {"capture_id", "excerpt", "sha256"}, "proposal evidence")
        capture_id = _identifier(item.get("capture_id"), "capture", "proposal evidence")
        excerpt = _required_string(item, "excerpt", "proposal evidence")
        evidence_digest = _digest(item.get("sha256"), "proposal evidence")
        if capture_id not in capture_ids or evidence_digest != sha256(
            excerpt.encode("utf-8")
        ).hexdigest():
            raise PortableValidationError("proposal evidence binding is malformed")
    siblings = _object(proposal.get("sibling_context"), "proposal sibling context")
    _require_exact_keys(siblings, {"proposal_ids"}, "proposal sibling context")
    sibling_ids = _required_list(siblings, "proposal_ids", "proposal sibling context")
    if (
        not sibling_ids
        or any(
            _identifier(item, "proposal", "proposal sibling context") != item
            for item in sibling_ids
        )
        or proposal_id not in sibling_ids
        or cast(list[str], sibling_ids) != sorted(set(cast(list[str], sibling_ids)))
    ):
        raise PortableValidationError("proposal sibling binding mismatch")
    _validate_receipt(
        proposal.get("expected_receipt"),
        subject_id=proposal_id,
        receipts={},
        label="proposal expected receipt",
        expected_kind="proposal_created",
        expected_payload={
            "proposal_id": proposal_id,
            "proposed_content_sha256": content["sha256"],
        },
    )


def _validate_decision_write(decision: Mapping[str, object]) -> None:
    proposal_id = _identifier(decision.get("proposal_id"), "proposal", "decision")
    _digest(decision.get("expected_state_digest"), "decision expected state")
    _validate_receipt(
        decision.get("expected_receipt"),
        subject_id=proposal_id,
        receipts={},
        label="decision expected receipt",
        expected_kind="proposal_created",
    )
    outcome = _required_string(decision, "outcome", "decision")
    edited_value = decision.get("edited_content")
    edited_digest: str | None = None
    if outcome == "edited":
        edited = _object(edited_value, "decision edited content")
        _require_exact_keys(edited, {"bytes_base64", "sha256"}, "decision edited content")
        _verify_encoded_digest(edited, "bytes_base64", "sha256", "decision edited content")
        edited_digest = _digest(edited.get("sha256"), "decision edited content")
    elif outcome in {"approved", "rejected"}:
        if edited_value is not None:
            raise PortableValidationError(f"{outcome} decision has edited content")
    else:
        raise PortableValidationError("decision outcome is unsupported")
    if decision.get("terminal_digest") != _canonical_digest(
        _decision_terminal_payload(decision, edited_digest), "decision terminal state"
    ):
        raise PortableValidationError("decision terminal digest mismatch")


def _validate_publication_write(publication: Mapping[str, object]) -> None:
    _identifier(publication.get("decision_id"), "decision", "publication")
    page_id = _identifier(publication.get("page_id"), "page", "publication")
    _verify_encoded_digest(
        publication, "published_bytes_base64", "published_sha256", "publication bytes"
    )
    path = _required_string(publication, "published_path", "publication")
    _validate_relative_path(path)
    if re.fullmatch(
        rf"content/spaces/[a-z0-9]+(?:-[a-z0-9]+)*/notes/{re.escape(page_id)}\.md", path
    ) is None:
        raise PortableValidationError("publication path is malformed")


def _validate_routing_write(routing: Mapping[str, object]) -> None:
    capture_id = _identifier(routing.get("capture_id"), "capture", "routing")
    _identifier(routing.get("route_id"), "route", "routing")
    _identifier(routing.get("space_id"), "space", "routing")
    supersedes = routing.get("supersedes")
    if supersedes is not None:
        _identifier(supersedes, "route", "routing")
    receipt = _validate_receipt(
        routing.get("receipt"),
        subject_id=capture_id,
        receipts={},
        label="routing receipt",
        expected_kind="routing",
        expected_payload={
            "capture_id": capture_id,
            "space_id": routing["space_id"],
        },
    )
    if receipt.get("recorded_at") != routing.get("recorded_at"):
        raise PortableValidationError("routing receipt timestamp binding mismatch")


def _validate_action_write(action: Mapping[str, object]) -> None:
    action_id = _identifier(action.get("action_id"), "action", "action")
    decision_id = _identifier(action.get("decision_id"), "decision", "action")
    _identifier(action.get("proposal_id"), "proposal", "action")
    if action.get("outcome") not in {"completed", "not_executed", "failed"}:
        raise PortableValidationError("action is malformed")
    request = _object(action.get("action_request"), "action request")
    result = _object(action.get("action_result"), "action result")
    if not request or not result:
        raise PortableValidationError("action request or result is malformed")
    request_digest = _canonical_digest(request, "action request")
    result_digest = _canonical_digest(result, "action result")
    if action.get("action_request_sha256") != request_digest:
        raise PortableValidationError("action request digest mismatch")
    if action.get("action_result_sha256") != result_digest:
        raise PortableValidationError("action result digest mismatch")
    _validate_receipt(
        action.get("approval_receipt"),
        subject_id=action_id,
        receipts={},
        label="action approval receipt",
        expected_kind="external_action",
        expected_payload={
            "action_id": action_id,
            "action_request_sha256": request_digest,
            "decision_id": decision_id,
        },
    )


def _portable_record(payload: bytes, label: str) -> dict[str, object]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PortableValidationError("invalid Portable JSON") from error
    if portable_canonical_json_bytes(value) != payload:
        raise PortableValidationError("Portable JSON is not canonical")
    return _object(value, label)


def _metadata_identity(metadata: os.stat_result) -> tuple[int, int]:
    return (metadata.st_dev, metadata.st_ino)


def _read_snapshot_file(directory_fd: int, name: str, expected: os.stat_result) -> bytes:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
    except OSError as error:
        raise PortableValidationError("Portable file cannot be read safely") from error
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or _metadata_identity(before) != _metadata_identity(expected)
        ):
            raise PortableValidationError("Portable exports must contain unique regular files")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 64 * 1024):
            chunks.append(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            (
                after.st_dev,
                after.st_ino,
                after.st_nlink,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            != (
                before.st_dev,
                before.st_ino,
                before.st_nlink,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            or len(payload) != before.st_size
        ):
            raise PortableValidationError("Portable file changed during validation")
        return payload
    except OSError as error:
        raise PortableValidationError("Portable file cannot be read safely") from error
    finally:
        os.close(descriptor)


def _snapshot_directory(
    directory_fd: int,
    parts: tuple[str, ...],
    files: dict[str, bytes] | None,
) -> None:
    try:
        with os.scandir(directory_fd) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name)
    except OSError as error:
        raise PortableValidationError("Portable directory cannot be read safely") from error
    for entry in entries:
        try:
            metadata = entry.stat(follow_symlinks=False)
        except OSError as error:
            raise PortableValidationError("Portable directory cannot be read safely") from error
        if stat.S_ISDIR(metadata.st_mode):
            try:
                child_fd = os.open(
                    entry.name,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory_fd,
                )
            except OSError as error:
                raise PortableValidationError("Portable directory cannot be read safely") from error
            try:
                if _metadata_identity(os.fstat(child_fd)) != _metadata_identity(metadata):
                    raise PortableValidationError("Portable directory changed during validation")
                child_files = None if not parts and entry.name == _OPERATIONAL_DIRECTORY else files
                _snapshot_directory(child_fd, (*parts, entry.name), child_files)
            finally:
                os.close(child_fd)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise PortableValidationError(
                "Portable exports must not contain symlinks or special files"
            )
        payload = _read_snapshot_file(directory_fd, entry.name, metadata)
        if files is not None:
            files["/".join((*parts, entry.name))] = payload


def validated_portable_snapshot(
    root: Path, *, expected_root_identity: RootIdentity | None = None
) -> PortableSnapshot:
    """Read once through descriptors, then validate only the immutable byte snapshot."""
    try:
        root_metadata = os.lstat(root)
    except OSError as error:
        raise PortableValidationError("Portable root must be a real directory") from error
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise PortableValidationError("Portable root must be a real directory")
    try:
        root_identity = (
            capture_root_identity(root)
            if expected_root_identity is None
            else expected_root_identity
        )
        root_fd = open_root_descriptor(root, root_identity)
    except RootConfinementError as error:
        raise PortableValidationError("Portable root identity changed") from error
    files: dict[str, bytes] = {}
    try:
        _snapshot_directory(root_fd, (), files)
    finally:
        os.close(root_fd)
    manifest_payload = files.get("portable-manifest.json")
    if manifest_payload is None:
        raise PortableValidationError("manifest is missing")
    manifest = _portable_record(manifest_payload, "manifest")
    _require_manifest_shape(manifest)
    entries = cast(list[object], manifest["files"])
    previous_path: str | None = None
    declared: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise PortableValidationError("manifest entry must be an object")
        path = entry.get("path")
        digest = entry.get("sha256")
        if not isinstance(path, str):
            raise PortableValidationError("manifest entry is malformed")
        digest = _digest(digest, "manifest entry")
        _validate_relative_path(path)
        if previous_path is not None and path <= previous_path:
            raise PortableValidationError("manifest paths must be sorted and unique")
        previous_path = path
        declared.add(path)
        payload = files.get(path)
        if payload is None or sha256(payload).hexdigest() != digest:
            raise PortableValidationError("manifest checksum mismatch")
        _validate_blob_address(path, digest)
    actual = set(files) - {"portable-manifest.json"}
    if declared != actual:
        raise PortableValidationError("manifest does not describe the portable root exactly")
    _validate_file_inventory(declared)
    _validate_semantics(files, manifest)
    try:
        assert_root_identity(root, root_identity)
    except RootConfinementError as error:
        raise PortableValidationError("Portable root identity changed") from error
    return PortableSnapshot(
        root_identity=root_identity,
        manifest=manifest,
        files=MappingProxyType(files),
    )


def validate_portable_root(
    root: Path, *, expected_root_identity: RootIdentity | None = None
) -> dict[str, object]:
    """Validate a strict, root-confined Portable Brain v1 export."""
    return dict(
        validated_portable_snapshot(
            root,
            expected_root_identity=expected_root_identity,
        ).manifest
    )


def validate_portable_file_set(files: Mapping[str, bytes], *, tenant_id: str) -> None:
    """Validate exact Portable v1 payload bytes without requiring an export manifest."""

    if (
        not isinstance(files, Mapping)
        or not isinstance(tenant_id, str)
        or not tenant_id
        or any(
            not isinstance(path, str) or not isinstance(payload, bytes)
            for path, payload in files.items()
        )
    ):
        raise PortableValidationError("Portable file set is invalid")
    snapshot = dict(files)
    if "portable-manifest.json" in snapshot:
        raise PortableValidationError("Portable file set must not contain an export manifest")
    _validate_file_inventory(snapshot)
    _validate_semantics(snapshot, {"tenant_id": tenant_id})


def export_portable_tree(source: Path, destination: Path) -> None:
    """Copy a validated export exactly, excluding all operational state."""
    snapshot = validated_portable_snapshot(source)
    manifest = snapshot.manifest
    if destination.exists():
        raise PortableValidationError("export destination already exists")
    destination.mkdir(parents=True)
    entries = cast(list[Mapping[str, object]], manifest["files"])
    for entry in entries:
        path = cast(str, entry["path"])
        target = destination.joinpath(*PurePosixPath(path).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(snapshot.files[path])
    (destination / "portable-manifest.json").write_bytes(
        snapshot.files["portable-manifest.json"]
    )
    validate_portable_root(destination)


def _require_manifest_shape(manifest: Mapping[str, object]) -> None:
    required = {
        "compatibility",
        "contract_version",
        "created_at",
        "export_id",
        "files",
        "layout_version",
        "schema_catalog_digest",
        "schema_version",
        "tenant_id",
    }
    if (
        set(manifest) != required
        or manifest["layout_version"] != 1
        or manifest["schema_version"] != 1
    ):
        raise PortableValidationError("unsupported manifest shape")
    if manifest["contract_version"] != "1":
        raise PortableValidationError("unsupported Portable contract version")
    compatibility = manifest["compatibility"]
    if not isinstance(compatibility, dict) or compatibility != {
        "maximum_contract_version": "1",
        "minimum_contract_version": "1",
    }:
        raise PortableValidationError("incompatible Portable contract range")
    if not isinstance(manifest["files"], list) or not manifest["files"]:
        raise PortableValidationError("manifest files are required")
    _identifier(manifest["export_id"], "export", "manifest export")
    _identifier(manifest["tenant_id"], "tenant", "manifest tenant")
    _timestamp(manifest["created_at"], "manifest created")
    if manifest["schema_catalog_digest"] != PORTABLE_V1_SCHEMA_CATALOG_DIGEST:
        raise PortableValidationError("Portable schema catalog digest mismatch")


def _validate_relative_path(value: str) -> None:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or "\\" in value
        or any(part in {"", ".", ".."} or part == _OPERATIONAL_DIRECTORY for part in path.parts)
        or path.as_posix() != value
    ):
        raise PortableValidationError("manifest path escapes the Portable root")


def _identifier(value: object, prefix: str, label: str) -> str:
    if not isinstance(value, str):
        raise PortableValidationError(f"{label} identifier is malformed")
    matched = _IDENTIFIER.fullmatch(value)
    if matched is None or matched["prefix"] != prefix:
        raise PortableValidationError(f"{label} identifier is malformed")
    try:
        parsed = uuid.UUID(matched["uuid"])
    except ValueError as error:
        raise PortableValidationError(f"{label} identifier is malformed") from error
    if parsed.version != 4 or value != f"{prefix}_{parsed}":
        raise PortableValidationError(f"{label} identifier is malformed")
    return value


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise PortableValidationError(f"{label} digest is malformed")
    return value


def _timestamp(value: object, label: str) -> str:
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        raise PortableValidationError(f"{label} timestamp is malformed")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as error:
        raise PortableValidationError(f"{label} timestamp is malformed") from error
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise PortableValidationError(f"{label} timestamp is malformed")
    return value


def _require_exact_keys(value: Mapping[str, object], keys: set[str], label: str) -> None:
    if set(value) != keys:
        raise PortableValidationError(f"{label} shape is malformed")


def _validate_dated_record_path(
    path: PurePosixPath,
    directory: PurePosixPath,
    identifier: str,
    timestamp: str,
    suffix: str,
    label: str,
) -> None:
    relative = path.relative_to(directory).as_posix()
    expected = f"{timestamp[:4]}/{timestamp[5:7]}/{identifier}{suffix}"
    if relative != expected:
        raise PortableValidationError(f"{label} filename or date path is malformed")


def _validate_blob_address(path: str, digest: str) -> None:
    parts = PurePosixPath(path).parts
    if parts[:3] != ("sources", "blobs", "sha256"):
        return
    if len(parts) != 5 or parts[3] != digest[:2] or parts[4] != digest:
        raise PortableValidationError("content-addressed blob path mismatch")


def _validate_file_inventory(paths: Iterable[str]) -> None:
    for path in paths:
        if path == "brain.toml":
            continue
        if re.fullmatch(r"sources/blobs/sha256/[0-9a-f]{2}/[0-9a-f]{64}", path) is not None:
            continue
        if (
            re.fullmatch(
                rf"sources/captures/{_DATED_DIRECTORY}/capture_[0-9a-f-]{{36}}\.json", path
            )
            is not None
        ):
            continue
        if (
            re.fullmatch(rf"sources/batches/{_DATED_DIRECTORY}/batch_[0-9a-f-]{{36}}\.jsonl", path)
            is not None
        ):
            continue
        if (
            re.fullmatch(
                rf"history/(?:proposals|decisions|publications|actions|routes)/{_DATED_DIRECTORY}/"
                rf"(?:proposal|decision|publication|action|route)_[0-9a-f-]{{36}}\.json",
                path,
            )
            is not None
        ):
            family, filename = path.split("/")[1], path.rsplit("/", 1)[1]
            expected_prefix = {
                "proposals": "proposal_",
                "decisions": "decision_",
                "publications": "publication_",
                "actions": "action_",
                "routes": "route_",
            }[family]
            if filename.startswith(expected_prefix):
                continue
        if (
            _SPACE_MARKDOWN_PATH.fullmatch(path) is not None
            or _OWNER_MARKDOWN_PATH.fullmatch(path) is not None
        ):
            continue
        raise PortableValidationError("Portable file is outside the v1 inventory")


def _validate_semantics(files: Mapping[str, bytes], manifest: Mapping[str, object]) -> None:
    tenant_id = _required_string(manifest, "tenant_id", "manifest")
    _validate_brain_profile(files, tenant_id)
    spaces = _validate_spaces(files, tenant_id)
    pages = _validate_pages(files, tenant_id, spaces)
    receipts: dict[str, bytes] = {}
    batches = _validate_batches(files, tenant_id)
    captures = _validate_captures(files, tenant_id, batches, spaces, receipts)
    _validate_routes(files, tenant_id, captures, spaces, receipts)
    proposals, proposal_bytes = _validate_proposals(files, tenant_id, captures, receipts)
    decisions = _validate_decisions(files, tenant_id, proposals, proposal_bytes, receipts)
    _validate_publications(files, tenant_id, proposals, decisions, pages)
    _validate_actions(files, tenant_id, proposals, decisions, receipts)


def _validate_brain_profile(files: Mapping[str, bytes], tenant_id: str) -> None:
    try:
        payload = files["brain.toml"]
        value = tomllib.loads(payload.decode("utf-8"))
    except (KeyError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise PortableValidationError("Portable profile is invalid") from error
    if not isinstance(value, dict):
        raise PortableValidationError("Portable profile is invalid")
    _require_exact_keys(
        value,
        {
            "layout_version",
            "profile",
            "tenant_id",
            "owner_actor_id",
            "owner_role_id",
            "owner_role_claim_id",
            "owner_capabilities",
        },
        "Portable profile",
    )
    if (
        value.get("layout_version") != 1
        or value.get("profile") != "single-user-local"
        or value.get("tenant_id") != tenant_id
        or value.get("owner_capabilities") != _OWNER_CAPABILITIES
    ):
        raise PortableValidationError("Portable profile is invalid")
    _identifier(value.get("tenant_id"), "tenant", "Portable profile tenant")
    _identifier(value.get("owner_actor_id"), "actor", "Portable profile owner")
    _identifier(value.get("owner_role_id"), "role", "Portable profile role")
    _identifier(value.get("owner_role_claim_id"), "role_claim", "Portable profile role claim")
    expected = (
        "layout_version = 1\n"
        'profile = "single-user-local"\n'
        f'tenant_id = "{value["tenant_id"]}"\n'
        f'owner_actor_id = "{value["owner_actor_id"]}"\n'
        f'owner_role_id = "{value["owner_role_id"]}"\n'
        f'owner_role_claim_id = "{value["owner_role_claim_id"]}"\n'
        'owner_capabilities = ["canonical.publish", "capture.accept", "space.write"]\n'
    ).encode()
    if payload != expected:
        raise PortableValidationError("Portable profile is not canonical")


def _canonical_markdown_fields(payload: bytes, label: str) -> tuple[dict[str, object], bytes]:
    try:
        parsed = parse_markdown(payload)
        if render_markdown(fields=parsed.fields, body=parsed.body).encode("utf-8") != payload:
            raise MarkdownFormatError("noncanonical Markdown")
    except MarkdownFormatError as error:
        raise PortableValidationError(f"{label} is not canonical Markdown") from error
    return dict(parsed.fields), payload


def _validate_spaces(files: Mapping[str, bytes], tenant_id: str) -> dict[str, str]:
    directory = PurePosixPath("content/spaces")
    spaces: dict[str, str] = {}
    for relative, payload in sorted(files.items()):
        if _SPACE_MARKDOWN_PATH.fullmatch(relative) is None:
            continue
        path = PurePosixPath(relative)
        fields, _ = _canonical_markdown_fields(payload, "space")
        _require_exact_keys(
            fields,
            {"actor_id", "name", "role_claim", "schema_version", "slug", "space_id", "tenant_id"},
            "space",
        )
        _validate_role_binding(fields, tenant_id, "space")
        space_id = _identifier(fields.get("space_id"), "space", "space")
        slug = _required_string(fields, "slug", "space")
        name = fields.get("name")
        if _SLUG.fullmatch(slug) is None or not isinstance(name, str) or not name:
            raise PortableValidationError("space is malformed")
        expected = directory / slug / "_space.md"
        if path != expected or space_id in spaces:
            raise PortableValidationError("space filename or identity is malformed")
        spaces[space_id] = slug
    return spaces


def _validate_pages(
    files: Mapping[str, bytes], tenant_id: str, spaces: Mapping[str, str]
) -> dict[str, tuple[str, bytes]]:
    directory = PurePosixPath("content/spaces")
    pages: dict[str, tuple[str, bytes]] = {}
    for relative, source_payload in sorted(files.items()):
        path = PurePosixPath(relative)
        if (
            len(path.parts) != 5
            or path.parts[:2] != ("content", "spaces")
            or path.parts[3] != "notes"
            or not path.name.startswith("page_")
            or path.suffix != ".md"
        ):
            continue
        fields, payload = _canonical_markdown_fields(source_payload, "canonical page")
        _require_exact_keys(
            fields,
            {
                "actor_id",
                "modified_at",
                "page_id",
                "privacy",
                "provenance",
                "role_claim",
                "schema_version",
                "space_id",
                "status",
                "tenant_id",
                "title",
                "trust",
            },
            "canonical page",
        )
        _validate_role_binding(fields, tenant_id, "canonical page")
        _validate_privacy(fields, "canonical page")
        page_id = _identifier(fields.get("page_id"), "page", "canonical page")
        space_id = _identifier(fields.get("space_id"), "space", "canonical page")
        _timestamp(fields.get("modified_at"), "canonical page")
        provenance = _required_list(fields, "provenance", "canonical page")
        if (
            not provenance
            or any(
                _identifier(item, "capture", "canonical page provenance") != item
                for item in provenance
            )
            or not isinstance(fields.get("title"), str)
            or not fields["title"]
            or fields.get("schema_version") != 1
            or fields.get("status") not in {"active", "archived"}
            or fields.get("trust") not in {"owner", "reviewed"}
        ):
            raise PortableValidationError("canonical page is malformed")
        slug = spaces.get(space_id)
        expected = directory / (slug or "") / "notes" / f"{page_id}.md"
        if slug is None or path != expected or page_id in pages:
            raise PortableValidationError("canonical page filename or space binding is malformed")
        pages[page_id] = (path.as_posix(), payload)
    return pages


def _json_records(
    files: Mapping[str, bytes], relative: str, label: str
) -> list[tuple[PurePosixPath, dict[str, object]]]:
    prefix = relative + "/"
    return [
        (PurePosixPath(path), _portable_record(payload, label))
        for path, payload in sorted(files.items())
        if path.startswith(prefix) and path.endswith(".json")
    ]


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise PortableValidationError(f"{label} must be an object")
    return cast(dict[str, object], value)


def _required_string(value: Mapping[str, object], key: str, label: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise PortableValidationError(f"{label} {key} is malformed")
    return item


def _required_list(value: Mapping[str, object], key: str, label: str) -> list[object]:
    item = value.get(key)
    if not isinstance(item, list):
        raise PortableValidationError(f"{label} {key} is malformed")
    return cast(list[object], item)


def _canonical_digest(value: object, label: str) -> str:
    try:
        return sha256(portable_canonical_json_bytes(value)).hexdigest()
    except (TypeError, ValueError) as error:
        raise PortableValidationError(f"{label} is not strict Portable JSON") from error


def _decode_base64(value: object, label: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise PortableValidationError(f"{label} bytes are malformed")
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as error:
        raise PortableValidationError(f"{label} base64 is malformed") from error


def _verify_encoded_digest(
    value: Mapping[str, object], bytes_key: str, digest_key: str, label: str
) -> bytes:
    data = _decode_base64(value.get(bytes_key), label)
    if sha256(data).hexdigest() != value.get(digest_key):
        raise PortableValidationError(f"{label} digest mismatch")
    return data


def _validate_role_binding(record: Mapping[str, object], tenant_id: str, label: str) -> None:
    actor_id = _identifier(record.get("actor_id"), "actor", label)
    if record.get("tenant_id") != tenant_id:
        raise PortableValidationError(f"{label} tenant binding mismatch")
    _identifier(tenant_id, "tenant", label)
    claim = _object(record.get("role_claim"), f"{label} role claim")
    _require_exact_keys(
        claim,
        {"actor_id", "capabilities", "role_claim_id", "role_id", "tenant_id"},
        f"{label} role claim",
    )
    if claim.get("tenant_id") != tenant_id or claim.get("actor_id") != actor_id:
        raise PortableValidationError(f"{label} role claim binding mismatch")
    _identifier(claim.get("role_claim_id"), "role_claim", f"{label} role claim")
    _identifier(claim.get("role_id"), "role", f"{label} role claim")
    capabilities = claim.get("capabilities")
    if not isinstance(capabilities, list) or any(
        not isinstance(capability, str)
        or re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", capability) is None
        for capability in capabilities
    ):
        raise PortableValidationError(f"{label} role claim capabilities are malformed")
    capability_names = cast(list[str], capabilities)
    if capability_names != sorted(set(capability_names)):
        raise PortableValidationError(f"{label} role claim capabilities must be sorted and unique")


def _validate_privacy(record: Mapping[str, object], label: str) -> None:
    privacy = _object(record.get("privacy"), f"{label} privacy")
    _require_exact_keys(
        privacy,
        {"authority", "confirmation_ref", "policy_version", "reason", "tier"},
        f"{label} privacy",
    )
    authority = _object(privacy.get("authority"), f"{label} privacy authority")
    _require_exact_keys(authority, {"cloud", "external_egress"}, f"{label} privacy authority")
    if (
        not isinstance(authority.get("cloud"), bool)
        or not isinstance(authority.get("external_egress"), bool)
        or not isinstance(privacy.get("policy_version"), str)
        or not privacy["policy_version"]
        or not isinstance(privacy.get("reason"), str)
        or not privacy["reason"]
        or not (
            isinstance(privacy.get("confirmation_ref"), str)
            or privacy.get("confirmation_ref") is None
        )
        or privacy.get("tier") not in {"public", "work", "personal", "secret", "unknown"}
    ):
        raise PortableValidationError(f"{label} privacy is malformed")
    if privacy.get("tier") in {"secret", "unknown"} and (
        authority.get("cloud") is True or authority.get("external_egress") is True
    ):
        raise PortableValidationError(f"{label} privacy authority exceeds its tier")


def _validate_attributes(value: object, label: str) -> None:
    if not isinstance(value, list):
        raise PortableValidationError(f"{label} attributes are malformed")
    for item in value:
        attribute = _object(item, f"{label} attribute")
        _require_exact_keys(attribute, {"name", "value"}, f"{label} attribute")
        if (
            not isinstance(attribute.get("name"), str)
            or not attribute["name"]
            or not isinstance(attribute.get("value"), str)
            or not attribute["value"]
        ):
            raise PortableValidationError(f"{label} attribute is malformed")


def _validate_payload(payload: Mapping[str, object], label: str) -> str:
    family = _required_string(payload, "family", label)
    if family == "text":
        _require_exact_keys(payload, {"family", "text"}, label)
        if not isinstance(payload.get("text"), str) or not payload["text"]:
            raise PortableValidationError(f"{label} text is malformed")
    elif family == "reference_or_file":
        allowed = {
            "blob_sha256",
            "family",
            "file_name",
            "kind",
            "media_type",
            "supplied_text",
            "url",
        }
        if set(payload) - allowed or payload.get("kind") not in {"reference", "file"}:
            raise PortableValidationError(f"{label} reference is malformed")
        for key in ("file_name", "media_type", "supplied_text", "url"):
            if key in payload and (not isinstance(payload[key], str) or not payload[key]):
                raise PortableValidationError(f"{label} reference is malformed")
        if payload.get("kind") == "reference":
            url = payload.get("url")
            if not isinstance(url, str) or not re.match(r"https?://", url):
                raise PortableValidationError(f"{label} reference is malformed")
        else:
            _digest(payload.get("blob_sha256"), label)
            if not all(
                isinstance(payload.get(key), str) and payload[key]
                for key in ("file_name", "media_type")
            ):
                raise PortableValidationError(f"{label} file is malformed")
    elif family == "event":
        _require_exact_keys(payload, {"attributes", "event_type", "family", "occurrence_at"}, label)
        event_type = payload.get("event_type")
        if (
            not isinstance(event_type, str)
            or re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", event_type) is None
        ):
            raise PortableValidationError(f"{label} event type is malformed")
        occurrence_at = payload.get("occurrence_at")
        if occurrence_at is not None:
            _timestamp(occurrence_at, label)
        _validate_attributes(payload.get("attributes"), label)
    elif family == "measurement":
        _require_exact_keys(
            payload,
            {"dimensions", "family", "occurrence_at", "unit", "value"},
            label,
        )
        unit = payload.get("unit")
        measurement_value = payload.get("value")
        if (
            not isinstance(unit, str)
            or re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_./-]{0,63}", unit) is None
            or not isinstance(measurement_value, str)
            or re.fullmatch(
                r"(?=.{1,130}$)(?=(?:[^0-9]*[0-9]){1,128}$)(?:0|-?(?:(?:[1-9][0-9]*)(?:\.[0-9]*[1-9])?|0\.[0-9]*[1-9]))",
                measurement_value,
            )
            is None
        ):
            raise PortableValidationError(f"{label} measurement is malformed")
        occurrence_at = payload.get("occurrence_at")
        if occurrence_at is not None:
            _timestamp(occurrence_at, label)
        _validate_attributes(payload.get("dimensions"), label)
    else:
        raise PortableValidationError(f"{label} family is unsupported")
    return family


def _validate_trust(value: object, label: str) -> None:
    trust = _object(value, f"{label} trust")
    _require_exact_keys(
        trust,
        {"assessed_at", "assessor_actor_id", "label", "reason"},
        f"{label} trust",
    )
    _timestamp(trust.get("assessed_at"), f"{label} trust")
    _identifier(trust.get("assessor_actor_id"), "actor", f"{label} trust")
    reason = trust.get("reason")
    if (
        trust.get("label") not in {"owner", "third_party", "unverified", "reviewed"}
        or not isinstance(reason, str)
        or not reason
    ):
        raise PortableValidationError(f"{label} trust is malformed")


def _validate_provenance(value: object, label: str) -> dict[str, object]:
    provenance = _object(value, f"{label} provenance")
    _require_exact_keys(
        provenance,
        {"content_origin", "owner_context", "source_ref", "transformation_receipts"},
        f"{label} provenance",
    )
    if (
        provenance.get("content_origin")
        not in {"owner_authored", "third_party", "mixed", "unknown"}
        or provenance.get("owner_context") not in {"owner_authored", "automation_absent"}
        or not isinstance(provenance.get("source_ref"), str)
        or not provenance["source_ref"]
        or not isinstance(provenance.get("transformation_receipts"), list)
    ):
        raise PortableValidationError(f"{label} provenance is malformed")
    return provenance


def _validate_receipt(
    value: object,
    *,
    subject_id: str,
    receipts: dict[str, bytes],
    label: str,
    expected_kind: str | None = None,
    expected_payload: Mapping[str, object] | None = None,
) -> dict[str, object]:
    receipt = _object(value, label)
    _require_exact_keys(
        receipt,
        {"kind", "payload", "receipt_id", "recorded_at", "sha256", "subject_id"},
        label,
    )
    receipt_id = _identifier(receipt.get("receipt_id"), "receipt", label)
    if receipt.get("subject_id") != subject_id:
        raise PortableValidationError(f"{label} subject binding mismatch")
    if receipt.get("kind") not in {
        "capture_accepted",
        "routing",
        "privacy_change",
        "proposal_created",
        "decision",
        "publication",
        "external_action",
    }:
        raise PortableValidationError(f"{label} kind binding mismatch")
    _timestamp(receipt.get("recorded_at"), label)
    _digest(receipt.get("sha256"), label)
    if expected_kind is not None and receipt.get("kind") != expected_kind:
        raise PortableValidationError(f"{label} kind binding mismatch")
    payload = _object(receipt.get("payload"), f"{label} payload")
    if receipt.get("sha256") != _canonical_digest(payload, f"{label} payload"):
        raise PortableValidationError(f"{label} receipt digest mismatch")
    if expected_payload is not None and payload != dict(expected_payload):
        raise PortableValidationError(f"{label} payload binding mismatch")
    try:
        receipt_bytes = portable_canonical_json_bytes(receipt)
    except (TypeError, ValueError) as error:
        raise PortableValidationError(f"{label} is not strict Portable JSON") from error
    previous = receipts.get(receipt_id)
    if previous is not None and previous != receipt_bytes:
        raise PortableValidationError("receipt ID is reused with different content")
    receipts[receipt_id] = receipt_bytes
    return receipt


def _require_blob(files: Mapping[str, bytes], digest: str, label: str) -> None:
    path = f"sources/blobs/sha256/{digest[:2]}/{digest}"
    payload = files.get(path)
    if payload is None or sha256(payload).hexdigest() != digest:
        raise PortableValidationError(f"{label} blob binding mismatch")


def _validate_batches(
    files: Mapping[str, bytes], tenant_id: str
) -> dict[tuple[str, str], dict[str, object]]:
    directory = PurePosixPath("sources/batches")
    rows: dict[tuple[str, str], dict[str, object]] = {}
    seen_records: dict[str, tuple[str, str]] = {}
    for relative, data in sorted(files.items()):
        if not relative.startswith("sources/batches/") or not relative.endswith(".jsonl"):
            continue
        path = PurePosixPath(relative)
        if not data or not data.endswith(b"\n"):
            raise PortableValidationError("batch JSONL must be non-empty and end in LF")
        file_batch_id: str | None = None
        file_timestamp: str | None = None
        for raw_line in data.splitlines(keepends=True):
            try:
                value = json.loads(raw_line[:-1])
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise PortableValidationError("batch row is invalid JSON") from error
            row = _object(value, "batch row")
            try:
                canonical_line = portable_canonical_json_bytes(row) + b"\n"
            except (TypeError, ValueError) as error:
                raise PortableValidationError("batch row is not strict Portable JSON") from error
            if canonical_line != raw_line:
                raise PortableValidationError("batch row is not canonical JSONL")
            _require_exact_keys(
                row,
                {
                    "actor_id",
                    "batch_id",
                    "payload",
                    "record_id",
                    "recorded_at",
                    "role_claim",
                    "schema_version",
                    "supersedes",
                    "tenant_id",
                },
                "batch row",
            )
            _validate_role_binding(row, tenant_id, "batch row")
            batch_id = _identifier(row.get("batch_id"), "batch", "batch row")
            record_id = _required_string(row, "record_id", "batch row")
            recorded_at = _timestamp(row.get("recorded_at"), "batch row")
            if row.get("schema_version") != 1:
                raise PortableValidationError("batch row schema version is malformed")
            payload = _object(row.get("payload"), "batch row payload")
            family = _validate_payload(payload, "batch row payload")
            if family not in {"event", "measurement"}:
                raise PortableValidationError("batch record family binding mismatch")
            _identifier(record_id, family, "batch row")
            if file_batch_id is None:
                file_batch_id = batch_id
            elif file_batch_id != batch_id:
                raise PortableValidationError("batch file contains multiple batch IDs")
            if file_timestamp is None:
                file_timestamp = recorded_at
            elif file_timestamp[:7] != recorded_at[:7]:
                raise PortableValidationError("batch file contains multiple month partitions")
            if record_id in seen_records:
                raise PortableValidationError("batch record ID is duplicated")
            supersedes = row.get("supersedes")
            if supersedes is not None:
                previous = seen_records.get(cast(str, supersedes))
                if previous is None or previous != (batch_id, family):
                    raise PortableValidationError(
                        "batch supersedes must reference an earlier same-family row"
                    )
            seen_records[record_id] = (batch_id, family)
            rows[(batch_id, record_id)] = payload
        if file_batch_id is None or file_timestamp is None:
            raise PortableValidationError("batch file is empty")
        _validate_dated_record_path(
            path,
            directory,
            file_batch_id,
            file_timestamp,
            ".jsonl",
            "batch",
        )
    return rows


def _validate_captures(
    files: Mapping[str, bytes],
    tenant_id: str,
    batches: Mapping[tuple[str, str], dict[str, object]],
    spaces: Mapping[str, str],
    receipts: dict[str, bytes],
) -> dict[str, dict[str, object]]:
    captures: dict[str, dict[str, object]] = {}
    directory = PurePosixPath("sources/captures")
    for path, capture in _json_records(files, "sources/captures", "capture"):
        _require_exact_keys(
            capture,
            {
                "accepted_at",
                "actor_id",
                "capture_id",
                "capture_why",
                "intent",
                "original_payload",
                "payload",
                "payload_binding",
                "payload_schema_version",
                "privacy",
                "provenance",
                "receipt_refs",
                "role_claim",
                "schema_version",
                "source",
                "space_id",
                "tenant_id",
                "trust",
            },
            "capture",
        )
        _validate_role_binding(capture, tenant_id, "capture")
        _validate_privacy(capture, "capture")
        capture_id = _identifier(capture.get("capture_id"), "capture", "capture")
        accepted_at = _timestamp(capture.get("accepted_at"), "capture")
        _validate_dated_record_path(path, directory, capture_id, accepted_at, ".json", "capture")
        if capture_id in captures:
            raise PortableValidationError("capture ID is duplicated")
        payload = _object(capture.get("payload"), "capture payload")
        _validate_payload(payload, "capture payload")
        if (
            capture.get("schema_version") != 1
            or capture.get("payload_schema_version") != 1
            or not (
                isinstance(capture.get("capture_why"), str) or capture.get("capture_why") is None
            )
            or not (isinstance(capture.get("intent"), str) or capture.get("intent") is None)
        ):
            raise PortableValidationError("capture is malformed")
        source = _object(capture.get("source"), "capture source")
        _require_exact_keys(source, {"origin", "reference"}, "capture source")
        if (
            source.get("origin") not in {"owner", "third_party"}
            or not isinstance(source.get("reference"), str)
            or not source["reference"]
        ):
            raise PortableValidationError("capture source is malformed")
        space_id = capture.get("space_id")
        if space_id is not None and _identifier(space_id, "space", "capture") not in spaces:
            raise PortableValidationError("capture space binding mismatch")
        provenance = _validate_provenance(capture.get("provenance"), "capture")
        _validate_trust(capture.get("trust"), "capture")
        payload_digest = _canonical_digest(payload, "capture payload")
        original = _object(capture.get("original_payload"), "capture original payload")
        original_kind = _required_string(original, "kind", "capture original payload")
        if original_kind == "inline":
            _require_exact_keys(
                original,
                {"bytes_base64", "kind", "sha256"},
                "capture original payload",
            )
            original_bytes = _verify_encoded_digest(
                original, "bytes_base64", "sha256", "capture original payload"
            )
            original_digest = sha256(original_bytes).hexdigest()
        elif original_kind == "blob":
            _require_exact_keys(original, {"blob_sha256", "kind"}, "capture original payload")
            original_digest = _required_string(original, "blob_sha256", "capture original payload")
            _digest(original_digest, "capture original payload")
            _require_blob(files, original_digest, "capture original payload")
        else:
            raise PortableValidationError("capture original payload kind is unsupported")
        blob_digest = payload.get("blob_sha256")
        if blob_digest is not None:
            if not isinstance(blob_digest, str):
                raise PortableValidationError("capture payload blob digest is malformed")
            _require_blob(files, blob_digest, "capture payload")
        binding = _object(capture.get("payload_binding"), "capture payload binding")
        binding_kind = _required_string(binding, "kind", "capture payload binding")
        if binding_kind == "inline":
            _require_exact_keys(binding, {"kind", "payload_sha256"}, "capture payload binding")
            _digest(binding.get("payload_sha256"), "capture payload binding")
            if binding.get("payload_sha256") != payload_digest:
                raise PortableValidationError("capture payload binding digest mismatch")
        elif binding_kind == "batch":
            _require_exact_keys(
                binding,
                {"batch_id", "kind", "record_id"},
                "capture payload binding",
            )
            batch_id = _required_string(binding, "batch_id", "capture payload binding")
            record_id = _required_string(binding, "record_id", "capture payload binding")
            _identifier(batch_id, "batch", "capture payload binding")
            payload_family = _required_string(payload, "family", "capture payload")
            _identifier(record_id, payload_family, "capture payload binding")
            batch_payload = batches.get((batch_id, record_id))
            if batch_payload is None:
                raise PortableValidationError("capture batch record binding is missing")
            if _canonical_digest(batch_payload, "batch payload") != payload_digest:
                raise PortableValidationError("capture batch payload binding mismatch")
        else:
            raise PortableValidationError("capture payload binding kind is unsupported")
        if source.get("reference") != provenance.get("source_ref"):
            raise PortableValidationError("capture provenance source binding mismatch")
        expected_receipt_payload = {
            "capture_id": capture_id,
            "original_payload_sha256": original_digest,
            "payload_sha256": payload_digest,
        }
        accepted = 0
        for receipt_value in _required_list(capture, "receipt_refs", "capture"):
            receipt = _validate_receipt(
                receipt_value,
                subject_id=capture_id,
                receipts=receipts,
                label="capture receipt",
            )
            if receipt.get("kind") == "capture_accepted":
                if receipt.get("payload") != expected_receipt_payload:
                    raise PortableValidationError("capture receipt payload binding mismatch")
                accepted += 1
        if accepted != 1:
            raise PortableValidationError("capture must have exactly one acceptance receipt")
        for receipt_value in _required_list(
            provenance, "transformation_receipts", "capture provenance"
        ):
            _validate_receipt(
                receipt_value,
                subject_id=capture_id,
                receipts=receipts,
                label="transformation receipt",
            )
        captures[capture_id] = capture
    return captures


def _validate_routes(
    files: Mapping[str, bytes],
    tenant_id: str,
    captures: Mapping[str, dict[str, object]],
    spaces: Mapping[str, str],
    receipts: dict[str, bytes],
) -> dict[str, dict[str, object]]:
    directory = PurePosixPath("history/routes")
    routes: dict[str, dict[str, object]] = {}
    by_capture: dict[str, set[str]] = {}
    for path, routing in _json_records(files, "history/routes", "routing"):
        _require_exact_keys(
            routing,
            {
                "actor_id",
                "capture_id",
                "receipt",
                "recorded_at",
                "role_claim",
                "route_id",
                "schema_version",
                "space_id",
                "supersedes",
                "tenant_id",
            },
            "routing",
        )
        _validate_role_binding(routing, tenant_id, "routing")
        route_id = _identifier(routing.get("route_id"), "route", "routing")
        capture_id = _identifier(routing.get("capture_id"), "capture", "routing")
        space_id = _identifier(routing.get("space_id"), "space", "routing")
        recorded_at = _timestamp(routing.get("recorded_at"), "routing")
        _validate_dated_record_path(
            path,
            directory,
            route_id,
            recorded_at,
            ".json",
            "routing",
        )
        supersedes = routing.get("supersedes")
        if supersedes is not None:
            _identifier(supersedes, "route", "routing")
        receipt = _validate_receipt(
            routing.get("receipt"),
            subject_id=capture_id,
            receipts=receipts,
            label="routing receipt",
            expected_kind="routing",
            expected_payload={"capture_id": capture_id, "space_id": space_id},
        )
        if (
            routing.get("schema_version") != 1
            or route_id in routes
            or capture_id not in captures
            or space_id not in spaces
            or receipt.get("recorded_at") != recorded_at
            or datetime.fromisoformat(recorded_at.removesuffix("Z") + "+00:00")
            < datetime.fromisoformat(
                cast(str, captures[capture_id]["accepted_at"]).removesuffix("Z") + "+00:00"
            )
        ):
            raise PortableValidationError("routing binding is malformed")
        routes[route_id] = routing
        by_capture.setdefault(capture_id, set()).add(route_id)

    superseded: set[str] = set()
    roots: dict[str, set[str]] = {capture_id: set() for capture_id in by_capture}
    for route_id, routing in routes.items():
        capture_id = cast(str, routing["capture_id"])
        supersedes = cast(str | None, routing["supersedes"])
        if supersedes is None:
            roots[capture_id].add(route_id)
            continue
        previous = routes.get(supersedes)
        previous_at = (
            None
            if previous is None
            else datetime.fromisoformat(
                cast(str, previous["recorded_at"]).removesuffix("Z") + "+00:00"
            )
        )
        current_at = datetime.fromisoformat(
            cast(str, routing["recorded_at"]).removesuffix("Z") + "+00:00"
        )
        if (
            previous is None
            or previous["capture_id"] != capture_id
            or supersedes in superseded
            or previous_at is not None
            and previous_at > current_at
        ):
            raise PortableValidationError("routing supersession is malformed")
        superseded.add(supersedes)

    for capture_id, route_ids in by_capture.items():
        terminals = route_ids - superseded
        if len(roots[capture_id]) != 1 or len(terminals) != 1:
            raise PortableValidationError("routing chain is malformed")
        visited: set[str] = set()
        current: str | None = next(iter(terminals))
        while current is not None and current not in visited:
            visited.add(current)
            current = cast(str | None, routes[current]["supersedes"])
        if current is not None or visited != route_ids:
            raise PortableValidationError("routing chain is malformed")
    embedded_routing_receipts = {
        cast(str, receipt["receipt_id"])
        for capture in captures.values()
        for receipt in cast(list[dict[str, object]], capture["receipt_refs"])
        if receipt["kind"] == "routing"
    }
    route_receipts = {
        cast(str, cast(dict[str, object], routing["receipt"])["receipt_id"])
        for routing in routes.values()
    }
    if not embedded_routing_receipts <= route_receipts:
        raise PortableValidationError("capture routing receipt has no route record")
    return routes


def _validate_proposals(
    files: Mapping[str, bytes],
    tenant_id: str,
    captures: Mapping[str, dict[str, object]],
    receipts: dict[str, bytes],
) -> tuple[dict[str, dict[str, object]], dict[str, bytes]]:
    proposals: dict[str, dict[str, object]] = {}
    proposed_bytes: dict[str, bytes] = {}
    directory = PurePosixPath("history/proposals")
    for path, proposal in _json_records(files, "history/proposals", "proposal"):
        _require_exact_keys(
            proposal,
            {
                "actor_id",
                "capture_ids",
                "evidence",
                "expected_receipt",
                "privacy",
                "proposal_id",
                "proposed_content",
                "proposed_kind",
                "recorded_at",
                "role_claim",
                "schema_version",
                "sibling_context",
                "space_id",
                "status",
                "supplied_reason",
                "tenant_id",
                "trust",
            },
            "proposal",
        )
        _validate_role_binding(proposal, tenant_id, "proposal")
        _validate_privacy(proposal, "proposal")
        proposal_id = _identifier(proposal.get("proposal_id"), "proposal", "proposal")
        recorded_at = _timestamp(proposal.get("recorded_at"), "proposal")
        _validate_dated_record_path(path, directory, proposal_id, recorded_at, ".json", "proposal")
        _validate_trust(proposal.get("trust"), "proposal")
        if (
            proposal.get("schema_version") != 1
            or proposal.get("proposed_kind")
            not in {"page_update", "event", "measurement", "action"}
            or proposal.get("status") != "pending"
            or not (
                isinstance(proposal.get("supplied_reason"), str)
                or proposal.get("supplied_reason") is None
            )
        ):
            raise PortableValidationError("proposal is malformed")
        if proposal.get("space_id") is not None:
            _identifier(proposal.get("space_id"), "space", "proposal")
        if proposal_id in proposals:
            raise PortableValidationError("proposal ID is duplicated")
        content = _object(proposal.get("proposed_content"), "proposal content")
        _require_exact_keys(content, {"bytes_base64", "media_type", "sha256"}, "proposal content")
        if not isinstance(content.get("media_type"), str) or not content["media_type"]:
            raise PortableValidationError("proposal content is malformed")
        content_bytes = _verify_encoded_digest(
            content, "bytes_base64", "sha256", "proposal content"
        )
        expected_payload = {
            "proposal_id": proposal_id,
            "proposed_content_sha256": content["sha256"],
        }
        _validate_receipt(
            proposal.get("expected_receipt"),
            subject_id=proposal_id,
            receipts=receipts,
            label="proposal expected receipt",
            expected_kind="proposal_created",
            expected_payload=expected_payload,
        )
        capture_ids = _required_list(proposal, "capture_ids", "proposal")
        if (
            not capture_ids
            or len(capture_ids) != len(set(capture_ids))
            or any(
                _identifier(item, "capture", "proposal") != item or item not in captures
                for item in capture_ids
            )
        ):
            raise PortableValidationError("proposal capture binding is missing")
        for evidence_value in _required_list(proposal, "evidence", "proposal"):
            evidence = _object(evidence_value, "proposal evidence")
            _require_exact_keys(evidence, {"capture_id", "excerpt", "sha256"}, "proposal evidence")
            capture_id = _identifier(evidence.get("capture_id"), "capture", "proposal evidence")
            excerpt = _required_string(evidence, "excerpt", "proposal evidence")
            if capture_id not in capture_ids or capture_id not in captures:
                raise PortableValidationError("proposal evidence capture binding is missing")
            evidence_digest = _digest(evidence.get("sha256"), "proposal evidence")
            if evidence_digest != sha256(excerpt.encode("utf-8")).hexdigest():
                raise PortableValidationError("proposal evidence digest mismatch")
        proposals[proposal_id] = proposal
        proposed_bytes[proposal_id] = content_bytes
    for proposal_id, proposal in proposals.items():
        sibling_context = _object(proposal.get("sibling_context"), "proposal sibling context")
        siblings = _required_list(sibling_context, "proposal_ids", "proposal sibling context")
        if (
            any(not isinstance(item, str) or item not in proposals for item in siblings)
            or proposal_id not in siblings
            or cast(list[str], siblings) != sorted(set(cast(list[str], siblings)))
        ):
            raise PortableValidationError("proposal sibling binding mismatch")
        sibling_set = set(cast(list[str], siblings))
        for sibling_id in sibling_set:
            sibling = proposals[sibling_id]
            other_context = _object(sibling.get("sibling_context"), "proposal sibling context")
            if (
                set(
                    cast(
                        list[str],
                        _required_list(other_context, "proposal_ids", "proposal sibling context"),
                    )
                )
                != sibling_set
            ):
                raise PortableValidationError("proposal sibling binding is asymmetric")
    return proposals, proposed_bytes


def _decision_terminal_payload(
    decision: Mapping[str, object], edited_content_sha256: str | None
) -> dict[str, object]:
    return {
        "decision_id": decision.get("decision_id"),
        "edited_content_sha256": edited_content_sha256,
        "expected_state_digest": decision.get("expected_state_digest"),
        "outcome": decision.get("outcome"),
        "proposal_id": decision.get("proposal_id"),
    }


def _validate_decisions(
    files: Mapping[str, bytes],
    tenant_id: str,
    proposals: Mapping[str, dict[str, object]],
    proposed_bytes: Mapping[str, bytes],
    receipts: dict[str, bytes],
) -> dict[str, tuple[dict[str, object], bytes | None]]:
    decisions: dict[str, tuple[dict[str, object], bytes | None]] = {}
    decided_proposals: set[str] = set()
    directory = PurePosixPath("history/decisions")
    for path, decision in _json_records(files, "history/decisions", "decision"):
        _require_exact_keys(
            decision,
            {
                "actor_id",
                "decision_id",
                "edited_content",
                "expected_receipt",
                "expected_state_digest",
                "outcome",
                "proposal_id",
                "recorded_at",
                "role_claim",
                "schema_version",
                "terminal_digest",
                "tenant_id",
            },
            "decision",
        )
        _validate_role_binding(decision, tenant_id, "decision")
        decision_id = _identifier(decision.get("decision_id"), "decision", "decision")
        proposal_id = _identifier(decision.get("proposal_id"), "proposal", "decision")
        recorded_at = _timestamp(decision.get("recorded_at"), "decision")
        _validate_dated_record_path(path, directory, decision_id, recorded_at, ".json", "decision")
        if decision.get("schema_version") != 1:
            raise PortableValidationError("decision schema version is malformed")
        if decision_id in decisions:
            raise PortableValidationError("decision ID is duplicated")
        if proposal_id in decided_proposals:
            raise PortableValidationError("proposal has multiple terminal decisions")
        proposal = proposals.get(proposal_id)
        if proposal is None:
            raise PortableValidationError("decision proposal binding is missing")
        expected_receipt = _validate_receipt(
            decision.get("expected_receipt"),
            subject_id=proposal_id,
            receipts=receipts,
            label="decision expected receipt",
            expected_kind="proposal_created",
        )
        if expected_receipt != proposal.get("expected_receipt"):
            raise PortableValidationError("decision expected receipt binding mismatch")
        expected_state_digest = _canonical_digest(proposal, "proposal state")
        if decision.get("expected_state_digest") != expected_state_digest:
            raise PortableValidationError("decision proposal state digest mismatch")
        outcome = _required_string(decision, "outcome", "decision")
        edited_value = decision.get("edited_content")
        edited_digest: str | None = None
        if outcome == "edited":
            edited = _object(edited_value, "decision edited content")
            _require_exact_keys(edited, {"bytes_base64", "sha256"}, "decision edited content")
            effective_bytes = _verify_encoded_digest(
                edited, "bytes_base64", "sha256", "decision edited content"
            )
            edited_digest = _required_string(edited, "sha256", "decision edited content")
        elif outcome == "approved":
            if edited_value is not None:
                raise PortableValidationError("approved decision has edited content")
            effective_bytes = proposed_bytes[proposal_id]
        elif outcome == "rejected":
            if edited_value is not None:
                raise PortableValidationError("rejected decision has edited content")
            effective_bytes = None
        else:
            raise PortableValidationError("decision outcome is unsupported")
        terminal_payload = _decision_terminal_payload(decision, edited_digest)
        if decision.get("terminal_digest") != _canonical_digest(
            terminal_payload, "decision terminal state"
        ):
            raise PortableValidationError("decision terminal digest mismatch")
        decisions[decision_id] = (decision, effective_bytes)
        decided_proposals.add(proposal_id)
    return decisions


def _validate_publications(
    files: Mapping[str, bytes],
    tenant_id: str,
    proposals: Mapping[str, dict[str, object]],
    decisions: Mapping[str, tuple[dict[str, object], bytes | None]],
    pages: Mapping[str, tuple[str, bytes]],
) -> None:
    publication_ids: set[str] = set()
    published_decisions: set[str] = set()
    directory = PurePosixPath("history/publications")
    for path, publication in _json_records(files, "history/publications", "publication"):
        _require_exact_keys(
            publication,
            {
                "actor_id",
                "decision_id",
                "page_id",
                "publication_id",
                "published_bytes_base64",
                "published_path",
                "published_sha256",
                "recorded_at",
                "role_claim",
                "schema_version",
                "tenant_id",
            },
            "publication",
        )
        _validate_role_binding(publication, tenant_id, "publication")
        publication_id = _identifier(
            publication.get("publication_id"), "publication", "publication"
        )
        decision_id = _identifier(publication.get("decision_id"), "decision", "publication")
        page_id = _identifier(publication.get("page_id"), "page", "publication")
        recorded_at = _timestamp(publication.get("recorded_at"), "publication")
        _validate_dated_record_path(
            path, directory, publication_id, recorded_at, ".json", "publication"
        )
        if publication.get("schema_version") != 1:
            raise PortableValidationError("publication schema version is malformed")
        if publication_id in publication_ids or decision_id in published_decisions:
            raise PortableValidationError("publication identity is duplicated")
        decision_entry = decisions.get(decision_id)
        if decision_entry is None:
            raise PortableValidationError("publication decision binding is missing")
        decision, effective_bytes = decision_entry
        proposal_id = _required_string(decision, "proposal_id", "decision")
        proposal = proposals[proposal_id]
        if proposal.get("proposed_kind") != "page_update" or effective_bytes is None:
            raise PortableValidationError("publication decision is not publishable")
        published_bytes = _verify_encoded_digest(
            publication,
            "published_bytes_base64",
            "published_sha256",
            "publication bytes",
        )
        if published_bytes != effective_bytes:
            raise PortableValidationError("publication decision content binding mismatch")
        published_path = _required_string(publication, "published_path", "publication")
        _validate_relative_path(published_path)
        if not published_path.startswith("content/"):
            raise PortableValidationError("publication path is outside content")
        current_page = pages.get(page_id)
        if current_page is None or current_page[0] != published_path:
            raise PortableValidationError("publication path page binding mismatch")
        publication_ids.add(publication_id)
        published_decisions.add(decision_id)


def _validate_actions(
    files: Mapping[str, bytes],
    tenant_id: str,
    proposals: Mapping[str, dict[str, object]],
    decisions: Mapping[str, tuple[dict[str, object], bytes | None]],
    receipts: dict[str, bytes],
) -> None:
    action_ids: set[str] = set()
    action_decisions: set[str] = set()
    directory = PurePosixPath("history/actions")
    for path, action in _json_records(files, "history/actions", "action"):
        _require_exact_keys(
            action,
            {
                "action_id",
                "action_request",
                "action_request_sha256",
                "action_result",
                "action_result_sha256",
                "actor_id",
                "approval_receipt",
                "decision_id",
                "outcome",
                "proposal_id",
                "recorded_at",
                "role_claim",
                "schema_version",
                "tenant_id",
            },
            "action",
        )
        _validate_role_binding(action, tenant_id, "action")
        action_id = _identifier(action.get("action_id"), "action", "action")
        decision_id = _identifier(action.get("decision_id"), "decision", "action")
        proposal_id = _identifier(action.get("proposal_id"), "proposal", "action")
        recorded_at = _timestamp(action.get("recorded_at"), "action")
        _validate_dated_record_path(path, directory, action_id, recorded_at, ".json", "action")
        if action.get("schema_version") != 1 or action.get("outcome") not in {
            "completed",
            "not_executed",
            "failed",
        }:
            raise PortableValidationError("action is malformed")
        if action_id in action_ids or decision_id in action_decisions:
            raise PortableValidationError("action identity is duplicated")
        decision_entry = decisions.get(decision_id)
        proposal = proposals.get(proposal_id)
        if decision_entry is None or proposal is None:
            raise PortableValidationError("action review-chain binding is missing")
        decision, effective_bytes = decision_entry
        if (
            decision.get("proposal_id") != proposal_id
            or proposal.get("proposed_kind") != "action"
            or effective_bytes is None
        ):
            raise PortableValidationError("action decision binding mismatch")
        request = _object(action.get("action_request"), "action request")
        result = _object(action.get("action_result"), "action result")
        request_digest = _canonical_digest(request, "action request")
        result_digest = _canonical_digest(result, "action result")
        if action.get("action_request_sha256") != request_digest:
            raise PortableValidationError("action request digest mismatch")
        if action.get("action_result_sha256") != result_digest:
            raise PortableValidationError("action result digest mismatch")
        try:
            request_bytes = portable_canonical_json_bytes(request)
        except (TypeError, ValueError) as error:
            raise PortableValidationError("action request is not strict Portable JSON") from error
        if request_bytes != effective_bytes:
            raise PortableValidationError("action decision request binding mismatch")
        approval_payload = {
            "action_id": action_id,
            "action_request_sha256": request_digest,
            "decision_id": decision_id,
        }
        _validate_receipt(
            action.get("approval_receipt"),
            subject_id=action_id,
            receipts=receipts,
            label="action approval receipt",
            expected_kind="external_action",
            expected_payload=approval_payload,
        )
        action_ids.add(action_id)
        action_decisions.add(decision_id)

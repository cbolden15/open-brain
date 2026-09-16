"""Portable Brain v2 managed-workspace extension over unchanged v1 file bytes."""

from __future__ import annotations

import base64
import binascii
import os
import re
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import cast

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.core.models import PrivacyDecision, ValidationError
from open_brain_engine.storage.filesystem import (
    RootConfinementError,
    RootIdentity,
    assert_root_identity,
    capture_root_identity,
    open_root_descriptor,
)
from open_brain_engine.storage.markdown import MarkdownFormatError, parse_markdown

from .v1 import (
    PortableSnapshot,
    PortableValidationError,
    _digest,
    _identifier,
    _portable_record,
    _snapshot_directory,
    _timestamp,
    _validate_blob_address,
    _validate_relative_path,
    validate_portable_file_set,
)

_MANAGED_PATH = re.compile(
    r"history/managed-workspace/"
    r"(?P<workspace>workspace_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12})\.json"
)
PORTABLE_V2_SCHEMA_CATALOG_DIGEST = sha256(
    portable_canonical_json_bytes(
        {
            "base": "portable-brain-v1-exact-files",
            "managed_workspace_record": 1,
            "schema_version": 2,
        }
    )
).hexdigest()


def managed_workspace_path(workspace_id: str) -> str:
    _identifier(workspace_id, "workspace", "managed workspace")
    return f"history/managed-workspace/{workspace_id}.json"


def validate_portable_file_set_v2(
    files: Mapping[str, bytes], *, tenant_id: str
) -> None:
    """Validate exact Portable v2 payload bytes without an export manifest."""
    managed_paths = [path for path in files if _MANAGED_PATH.fullmatch(path)]
    if len(managed_paths) != 1:
        raise PortableValidationError("Portable v2 requires one managed workspace record")
    base = {path: payload for path, payload in files.items() if path != managed_paths[0]}
    validate_portable_file_set(base, tenant_id=tenant_id)
    page_ids = {
        cast(str, parse_markdown(payload).fields["page_id"])
        for path, payload in base.items()
        if path.startswith("content/spaces/") and "/notes/" in path and path.endswith(".md")
    }
    record = validate_managed_workspace_record(
        files[managed_paths[0]], tenant_id=tenant_id, page_ids=page_ids
    )
    match = _MANAGED_PATH.fullmatch(managed_paths[0])
    if match is None or record["workspace_id"] != match.group("workspace"):
        raise PortableValidationError("managed workspace path is invalid")


def validated_portable_snapshot_v2(
    root: Path, *, expected_root_identity: RootIdentity | None = None
) -> PortableSnapshot:
    """Validate v2 while delegating every unchanged base byte to the v1 validator."""
    try:
        root_identity = (
            capture_root_identity(root)
            if expected_root_identity is None
            else expected_root_identity
        )
        root_fd = open_root_descriptor(root, root_identity)
    except (OSError, RootConfinementError) as error:
        raise PortableValidationError("Portable root must be a real directory") from error
    files: dict[str, bytes] = {}
    try:
        _snapshot_directory(root_fd, (), files)
    finally:
        os.close(root_fd)
    manifest_payload = files.get("portable-manifest.json")
    if manifest_payload is None:
        raise PortableValidationError("manifest is missing")
    manifest = _portable_record(manifest_payload, "manifest")
    _require_manifest(manifest)
    entries = cast(list[object], manifest["files"])
    previous: str | None = None
    declared: set[str] = set()
    managed_paths: list[str] = []
    for raw_entry in entries:
        if not isinstance(raw_entry, dict):
            raise PortableValidationError("manifest entry must be an object")
        path = raw_entry.get("path")
        if not isinstance(path, str):
            raise PortableValidationError("manifest entry is malformed")
        digest = _digest(raw_entry.get("sha256"), "manifest entry")
        _validate_relative_path(path)
        if previous is not None and path <= previous:
            raise PortableValidationError("manifest paths must be sorted and unique")
        previous = path
        declared.add(path)
        payload = files.get(path)
        if payload is None or sha256(payload).hexdigest() != digest:
            raise PortableValidationError("manifest checksum mismatch")
        _validate_blob_address(path, digest)
        if _MANAGED_PATH.fullmatch(path):
            managed_paths.append(path)
    if declared != set(files) - {"portable-manifest.json"}:
        raise PortableValidationError("manifest does not describe the portable root exactly")
    if len(managed_paths) != 1:
        raise PortableValidationError("Portable v2 requires one managed workspace record")
    base = {
        path: payload
        for path, payload in files.items()
        if path not in {"portable-manifest.json", managed_paths[0]}
    }
    tenant_id = cast(str, manifest["tenant_id"])
    validate_portable_file_set(base, tenant_id=tenant_id)
    page_ids = {
        cast(str, parse_markdown(payload).fields["page_id"])
        for path, payload in base.items()
        if path.startswith("content/spaces/") and "/notes/" in path and path.endswith(".md")
    }
    record = validate_managed_workspace_record(
        files[managed_paths[0]], tenant_id=tenant_id, page_ids=page_ids
    )
    match = _MANAGED_PATH.fullmatch(managed_paths[0])
    if match is None or record["workspace_id"] != match.group("workspace"):
        raise PortableValidationError("managed workspace path is invalid")
    try:
        assert_root_identity(root, root_identity)
    except RootConfinementError as error:
        raise PortableValidationError("Portable root identity changed") from error
    return PortableSnapshot(
        root_identity=root_identity,
        manifest=manifest,
        files=MappingProxyType(files),
    )


def validate_managed_workspace_record(
    payload: bytes, *, tenant_id: str, page_ids: set[str]
) -> dict[str, object]:
    record = _portable_record(payload, "managed workspace")
    _keys(
        record,
        {
            "budgets",
            "conflicts",
            "consents",
            "created_at",
            "exclusions",
            "links",
            "notes",
            "observation_generation",
            "origin_owner_actor_id",
            "policy_generation",
            "schema_version",
            "tenant_id",
            "workspace_id",
        },
        "managed workspace",
    )
    if record["schema_version"] != 1 or record["tenant_id"] != tenant_id:
        raise PortableValidationError("managed workspace identity is invalid")
    _identifier(record["workspace_id"], "workspace", "managed workspace")
    _identifier(record["origin_owner_actor_id"], "actor", "managed workspace owner")
    _timestamp(record["created_at"], "managed workspace created")
    _nonnegative(record["observation_generation"], "observation generation")
    _nonnegative(record["policy_generation"], "policy generation")
    notes = _list(record["notes"], "managed notes")
    revision_ids: dict[str, str] = {}
    accepted: dict[str, str] = {}
    seen_notes: set[str] = set()
    for raw_note in notes:
        note = _object(raw_note, "managed note")
        _keys(note, {"accepted_revision_id", "active", "note_id", "revisions"}, "managed note")
        note_id = _identifier(note["note_id"], "page", "managed note")
        if note_id in seen_notes or note_id not in page_ids or type(note["active"]) is not bool:
            raise PortableValidationError("managed note identity is invalid")
        seen_notes.add(note_id)
        revisions = _list(note["revisions"], "managed revisions")
        if not revisions:
            raise PortableValidationError("managed revisions are required")
        local_ids: set[str] = set()
        for raw_revision in revisions:
            revision = _object(raw_revision, "managed revision")
            _keys(
                revision,
                {
                    "accepted_by_actor_id",
                    "body_base64",
                    "body_sha256",
                    "kind",
                    "operation_id",
                    "parent_revision_id",
                    "privacy",
                    "provenance",
                    "recorded_at",
                    "revision_id",
                },
                "managed revision",
            )
            revision_id = _identifier(revision["revision_id"], "revision", "managed revision")
            if revision_id in revision_ids:
                raise PortableValidationError("duplicate managed revision")
            parent = revision["parent_revision_id"]
            if parent is not None and (
                not isinstance(parent, str) or parent not in local_ids
            ):
                raise PortableValidationError("managed revision chain is invalid")
            if revision["kind"] not in {"setup", "edit", "link", "merge", "restore"}:
                raise PortableValidationError("managed revision kind is invalid")
            _identifier(revision["accepted_by_actor_id"], "actor", "managed revision actor")
            _timestamp(revision["recorded_at"], "managed revision recorded")
            operation_id = revision["operation_id"]
            if not isinstance(operation_id, str) or not operation_id:
                raise PortableValidationError("managed revision operation is invalid")
            body = _base64(revision["body_base64"], "managed revision body")
            if sha256(body).hexdigest() != _digest(
                revision["body_sha256"], "managed revision body"
            ):
                raise PortableValidationError("managed revision body digest mismatch")
            try:
                parsed = parse_markdown(body)
                PrivacyDecision.from_dict(_object(revision["privacy"], "managed privacy"))
            except (MarkdownFormatError, ValidationError):
                raise PortableValidationError("managed revision body is invalid") from None
            if parsed.fields.get("page_id") != note_id:
                raise PortableValidationError("managed revision note binding is invalid")
            _object(revision["provenance"], "managed provenance")
            local_ids.add(revision_id)
            revision_ids[revision_id] = note_id
        accepted_id = _identifier(
            note["accepted_revision_id"], "revision", "managed accepted revision"
        )
        if accepted_id not in local_ids:
            raise PortableValidationError("managed accepted revision is invalid")
        accepted[note_id] = accepted_id
    if seen_notes != page_ids:
        raise PortableValidationError("managed notes do not match canonical pages")
    _validate_links(_list(record["links"], "managed links"), revision_ids)
    _validate_conflicts(_list(record["conflicts"], "managed conflicts"), revision_ids)
    _validate_consents(_list(record["consents"], "managed consents"))
    _validate_exclusions(_list(record["exclusions"], "managed exclusions"), seen_notes)
    _validate_budgets(_list(record["budgets"], "managed budgets"))
    return record


def _validate_links(values: list[object], revisions: Mapping[str, str]) -> None:
    seen: set[str] = set()
    for raw in values:
        link = _object(raw, "managed link")
        _keys(
            link,
            {
                "accepted_at",
                "active",
                "link_id",
                "provenance",
                "source_note_id",
                "source_quote",
                "source_revision_id",
                "target_note_id",
                "target_quote",
                "target_revision_id",
            },
            "managed link",
        )
        link_id = _identifier(link["link_id"], "link", "managed link")
        source_revision = _identifier(
            link["source_revision_id"], "revision", "managed link source"
        )
        target_revision = _identifier(
            link["target_revision_id"], "revision", "managed link target"
        )
        source_note = _identifier(link["source_note_id"], "page", "managed link source")
        target_note = _identifier(link["target_note_id"], "page", "managed link target")
        if (
            link_id in seen
            or source_note == target_note
            or revisions.get(source_revision) != source_note
            or revisions.get(target_revision) != target_note
            or type(link["active"]) is not bool
        ):
            raise PortableValidationError("managed link binding is invalid")
        _text(link["source_quote"], "managed source quote")
        _text(link["target_quote"], "managed target quote")
        _object(link["provenance"], "managed link provenance")
        _timestamp(link["accepted_at"], "managed link accepted")
        seen.add(link_id)


def _validate_conflicts(values: list[object], revisions: Mapping[str, str]) -> None:
    seen: set[str] = set()
    for raw in values:
        conflict = _object(raw, "managed conflict")
        _keys(
            conflict,
            {
                "accepted_revision_id",
                "base_revision_id",
                "candidate_body_base64",
                "candidate_sha256",
                "conflict_id",
                "detected_at",
                "note_id",
                "resolution_revision_id",
                "resolved_at",
            },
            "managed conflict",
        )
        conflict_id = _identifier(conflict["conflict_id"], "conflict", "managed conflict")
        note_id = _identifier(conflict["note_id"], "page", "managed conflict")
        for key in ("base_revision_id", "accepted_revision_id", "resolution_revision_id"):
            revision = _identifier(conflict[key], "revision", "managed conflict revision")
            if revisions.get(revision) != note_id:
                raise PortableValidationError("managed conflict binding is invalid")
        candidate = _base64(conflict["candidate_body_base64"], "managed conflict candidate")
        if (
            conflict_id in seen
            or sha256(candidate).hexdigest()
            != _digest(conflict["candidate_sha256"], "managed conflict candidate")
        ):
            raise PortableValidationError("managed conflict is invalid")
        _timestamp(conflict["detected_at"], "managed conflict detected")
        _timestamp(conflict["resolved_at"], "managed conflict resolved")
        seen.add(conflict_id)


def _validate_consents(values: list[object]) -> None:
    seen: set[str] = set()
    for raw in values:
        consent = _object(raw, "managed consent")
        _keys(
            consent,
            {
                "access_mode",
                "active_at_export",
                "consent_id",
                "granted_at",
                "granted_generation",
                "note_scope",
                "operation",
                "owner_actor_id",
                "provider",
                "revoked_at",
            },
            "managed consent",
        )
        consent_id = _identifier(consent["consent_id"], "consent", "managed consent")
        if consent_id in seen or type(consent["active_at_export"]) is not bool:
            raise PortableValidationError("managed consent is invalid")
        _identifier(consent["owner_actor_id"], "actor", "managed consent owner")
        _text(consent["provider"], "managed consent provider")
        _text(consent["access_mode"], "managed consent access")
        if consent["operation"] != "semantic_graph" or consent["note_scope"] != "*":
            raise PortableValidationError("managed consent scope is invalid")
        _nonnegative(consent["granted_generation"], "managed consent generation")
        _timestamp(consent["granted_at"], "managed consent granted")
        if consent["revoked_at"] is not None:
            _timestamp(consent["revoked_at"], "managed consent revoked")
        seen.add(consent_id)


def _validate_exclusions(values: list[object], note_ids: set[str]) -> None:
    seen: set[str] = set()
    for raw in values:
        exclusion = _object(raw, "managed exclusion")
        _keys(
            exclusion,
            {"active", "exclusion_id", "note_ids", "policy_generation", "recorded_at"},
            "managed exclusion",
        )
        exclusion_id = _identifier(
            exclusion["exclusion_id"], "exclusion", "managed exclusion"
        )
        subjects = _list(exclusion["note_ids"], "managed exclusion notes")
        normalized = [
            _identifier(note_id, "page", "managed exclusion") for note_id in subjects
        ]
        if (
            exclusion_id in seen
            or not normalized
            or normalized != sorted(set(normalized))
            or not set(normalized).issubset(note_ids)
            or type(exclusion["active"]) is not bool
        ):
            raise PortableValidationError("managed exclusion is invalid")
        _nonnegative(exclusion["policy_generation"], "managed exclusion generation")
        _timestamp(exclusion["recorded_at"], "managed exclusion recorded")
        seen.add(exclusion_id)


def _validate_budgets(values: list[object]) -> None:
    seen: set[tuple[str, str]] = set()
    for raw in values:
        budget = _object(raw, "managed budget")
        keys = {
            "byte_limit",
            "provider",
            "request_limit",
            "uncertain_bytes",
            "uncertain_requests",
            "updated_at",
            "used_bytes",
            "used_requests",
            "window_key",
        }
        _keys(budget, keys, "managed budget")
        provider = _text(budget["provider"], "managed budget provider")
        window = _text(budget["window_key"], "managed budget window")
        identity = (provider, window)
        numbers = [
            _nonnegative(budget[key], f"managed budget {key}")
            for key in keys
            if key.endswith("bytes") or key.endswith("requests") or key.endswith("limit")
        ]
        if identity in seen or len(numbers) != 6:
            raise PortableValidationError("managed budget is invalid")
        if (
            cast(int, budget["used_requests"]) + cast(int, budget["uncertain_requests"])
            > cast(int, budget["request_limit"])
            or cast(int, budget["used_bytes"]) + cast(int, budget["uncertain_bytes"])
            > cast(int, budget["byte_limit"])
        ):
            raise PortableValidationError("managed budget is exhausted beyond its limit")
        _timestamp(budget["updated_at"], "managed budget updated")
        seen.add(identity)


def _require_manifest(manifest: Mapping[str, object]) -> None:
    _keys(
        manifest,
        {
            "compatibility",
            "contract_version",
            "created_at",
            "export_id",
            "files",
            "layout_version",
            "schema_catalog_digest",
            "schema_version",
            "tenant_id",
        },
        "manifest",
    )
    if (
        manifest["layout_version"] != 2
        or manifest["schema_version"] != 2
        or manifest["contract_version"] != "2"
        or manifest["compatibility"]
        != {"maximum_contract_version": "2", "minimum_contract_version": "1"}
        or manifest["schema_catalog_digest"] != PORTABLE_V2_SCHEMA_CATALOG_DIGEST
        or not isinstance(manifest["files"], list)
        or not manifest["files"]
    ):
        raise PortableValidationError("unsupported Portable v2 manifest")
    _identifier(manifest["export_id"], "export", "manifest export")
    _identifier(manifest["tenant_id"], "tenant", "manifest tenant")
    _timestamp(manifest["created_at"], "manifest created")


def _keys(value: Mapping[str, object], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise PortableValidationError(f"{label} fields are invalid")


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise PortableValidationError(f"{label} is invalid")
    return cast(dict[str, object], value)


def _list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise PortableValidationError(f"{label} are invalid")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 4096:
        raise PortableValidationError(f"{label} is invalid")
    return value


def _nonnegative(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise PortableValidationError(f"{label} is invalid")
    return value


def _base64(value: object, label: str) -> bytes:
    if not isinstance(value, str):
        raise PortableValidationError(f"{label} is invalid")
    try:
        result = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise PortableValidationError(f"{label} is invalid") from None
    if base64.b64encode(result).decode("ascii") != value:
        raise PortableValidationError(f"{label} is invalid")
    return result


__all__ = [
    "PORTABLE_V2_SCHEMA_CATALOG_DIGEST",
    "managed_workspace_path",
    "validate_managed_workspace_record",
    "validate_portable_file_set_v2",
    "validated_portable_snapshot_v2",
]

"""Closed consumer intent custody and independently rebuilt Portable15 projections."""

from __future__ import annotations

import base64
import json
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.engine.capture_lifecycle_contracts import canonical_bytes, validate_digest
from open_brain_engine.engine.consumer_journal import (
    LifecycleJournalV2,
    consumer_items,
    restore_lifecycle_journal_v2_value,
)
from open_brain_engine.engine.consumer_operations import PreparedConsumerOperation
from open_brain_engine.engine.consumer_sources import consumer_namespace_bytes

from .lifecycle_v15 import lifecycle_evidence_value
from .v1 import PortableSnapshot, PortableValidationError, _portable_record
from .v4 import SOURCE_METADATA_PATH
from .v8_capture_metadata import validate_capture_metadata
from .v9 import validate_source_authority_v9

CONSUMER_PATH = "history/consumer-intake/evidence-v1.json"
CONSUMER_OBJECT_PREFIX = "history/consumer-intake/objects/"
CONSUMER_CONTRACT = "ob.consumer-intake-evidence.v1"
PROJECTION_COLUMNS = {
    "consumer_scopes": ("scope_id", "provider", "label", "namespace_id", "created_operation_id"),
    "consumer_archive_bindings": ("provider", "archive_sha256", "scope_id", "operation_id"),
    "consumer_segment_bindings": (
        "capture_id",
        "operation_id",
        "group_key",
        "revision_sha256",
        "segment_index",
        "source_id",
        "namespace_bytes",
        "expected_source_bytes",
        "normalized_sha256",
        "envelope_sha256",
    ),
    "consumer_outcomes": (
        "operation_id",
        "group_key",
        "ordinal",
        "outcome",
        "receipt_bytes",
        "receipt_sha256",
    ),
}
BLOB_COLUMNS = frozenset({"namespace_bytes", "expected_source_bytes", "receipt_bytes"})


def is_consumer_path(path: str) -> bool:
    if path == CONSUMER_PATH:
        return True
    if not path.startswith(CONSUMER_OBJECT_PREFIX):
        return False
    try:
        validate_digest(path.removeprefix(CONSUMER_OBJECT_PREFIX))
        return True
    except ValueError:
        return False


def projection_row(value: Mapping[str, object]) -> dict[str, object]:
    return {
        key: base64.b64encode(cast(bytes, item)).decode("ascii") if key in BLOB_COLUMNS else item
        for key, item in value.items()
    }


def rebuild_projections(
    operations: Mapping[str, PreparedConsumerOperation],
    journal: LifecycleJournalV2,
) -> dict[str, list[dict[str, object]]]:
    """Derive scope/archive/membership/outcome facts solely from accepted proofs and events."""
    state = journal._state()
    scopes: dict[str, dict[str, object]] = {}
    archives: dict[tuple[str, str], dict[str, object]] = {}
    result: dict[str, list[dict[str, object]]] = {name: [] for name in PROJECTION_COLUMNS}
    for operation, prepared in sorted(operations.items()):
        for item in prepared.items:
            if item["planned_outcome"] not in {"new", "replace_current"}:
                continue
            conversation = prepared.conversation(item)
            assert conversation is not None
            intended = state.intended_groups(operation)[cast(str, item["group_key"])]
            if state.staged.get((operation, intended.group_key)) != intended:
                raise ValueError("accepted consumer manifest missing")
            for segment in conversation.segments:
                result["consumer_segment_bindings"].append(
                    projection_row(
                        {
                            "capture_id": segment.identities.capture_id,
                            "operation_id": operation,
                            "group_key": segment.conversation_key,
                            "revision_sha256": segment.conversation_revision_sha256,
                            "segment_index": segment.segment_index,
                            "source_id": segment.source.source_id,
                            "namespace_bytes": consumer_namespace_bytes(segment.source),
                            "expected_source_bytes": canonical_bytes(segment.source.value()),
                            "normalized_sha256": segment.normalized_sha256,
                            "envelope_sha256": sha256(segment.to_bytes()).hexdigest(),
                        }
                    )
                )
        for ordinal, outcome in enumerate(journal.conversation_outcomes(operation)):
            raw = canonical_bytes({"operation_id": operation, **outcome})
            result["consumer_outcomes"].append(
                projection_row(
                    {
                        "operation_id": operation,
                        "group_key": outcome["group_key"],
                        "ordinal": ordinal,
                        "outcome": outcome["outcome"],
                        "receipt_bytes": raw,
                        "receipt_sha256": sha256(raw).hexdigest(),
                    }
                )
            )
    for operation, kind, raw in journal.connection.execute(
        "SELECT operation_id,kind,payload_bytes FROM lifecycle_journal_events ORDER BY sequence"
    ):
        selected = operations.get(operation)
        if selected is None:
            continue
        prepared = selected
        payload = json.loads(raw)
        admission = kind == "group_activated"
        if not admission:
            if kind != "consumer_outcome":
                continue
            item = next(
                item for item in prepared.items if item["group_key"] == payload["group_key"]
            )
            if item["planned_outcome"] in {"new", "replace_current"}:
                continue
        scope, plan = prepared.scope, prepared.plan
        scope_id = cast(str, scope["scope_id"])
        existing = scopes.get(scope_id)
        archive_key = (plan.provider, plan.archive_sha256)
        if archive_key in archives and archives[archive_key]["scope_id"] != scope_id:
            raise ValueError("consumer archive rebound")
        if existing is None:
            if not scope["proposed"]:
                raise ValueError("consumer registered scope lacks authority")
            if any(
                (row["provider"], row["label"]) == (plan.provider, scope["label"])
                or row["namespace_id"] == scope["namespace_id"]
                for row in scopes.values()
            ):
                raise ValueError("consumer scope collision")
            if not admission:
                continue
            existing = {
                "scope_id": scope_id,
                "provider": plan.provider,
                "label": scope["label"],
                "namespace_id": scope["namespace_id"],
                "created_operation_id": operation,
            }
            if any(
                row["namespace_id"] == existing["namespace_id"]
                or (row["provider"], row["label"]) == (existing["provider"], existing["label"])
                for row in scopes.values()
            ):
                raise ValueError("consumer scope collision")
            scopes[scope_id] = existing
        elif any(
            existing[key] != expected
            for key, expected in (
                ("provider", plan.provider),
                ("label", scope["label"]),
                ("namespace_id", scope["namespace_id"]),
            )
        ):
            raise ValueError("consumer scope rebound")
        key = (plan.provider, plan.archive_sha256)
        if key in archives and archives[key]["scope_id"] != scope_id:
            raise ValueError("consumer archive rebound")
        archives.setdefault(
            key,
            {
                "provider": plan.provider,
                "archive_sha256": plan.archive_sha256,
                "scope_id": scope_id,
                "operation_id": operation,
            },
        )
    result["consumer_scopes"] = list(scopes.values())
    result["consumer_archive_bindings"] = list(archives.values())
    for table, rows in result.items():
        rows.sort(
            key=lambda row: tuple(
                row[column]
                for column in PROJECTION_COLUMNS[table][
                    : 2 if table in {"consumer_archive_bindings", "consumer_outcomes"} else 1
                ]
            )
        )
    return result


def _validate_sources(
    files: Mapping[str, bytes],
    operations: Mapping[str, PreparedConsumerOperation],
    journal: LifecycleJournalV2,
) -> None:
    state = journal._state()
    metadata = json.loads(files[SOURCE_METADATA_PATH])
    sources = {row["source_id"]: row for row in metadata["sources"]}
    revisions = {row["capture_id"]: row for row in metadata["revisions"]}
    captures = {row["capture_id"]: row for row in validate_capture_metadata(files)}
    authority = validate_source_authority_v9(files)
    namespaces = {row["source_id"]: row["namespace_json"] for row in authority["source_namespaces"]}
    admissions = {row["capture_id"]: row for row in authority["revision_admission"]}
    segments: dict[str, Any] = {}
    expected_heads: dict[str, str] = {}
    expected_head_versions: dict[str, int] = {}
    for prepared in operations.values():
        for item in prepared.items:
            conversation = prepared.conversation(item)
            if conversation is None or item["planned_outcome"] not in {"new", "replace_current"}:
                continue
            for segment in conversation.segments:
                capture = segment.identities.capture_id
                segments[capture] = segment
                if capture not in captures:
                    continue
                source = segment.source.source_id
                revision = revisions[capture]
                if namespaces.get(source) != consumer_namespace_bytes(segment.source).decode() or (
                    revision["source_id"] != source
                    or revision["predecessor_capture_id"] != segment.source.expected_head
                    or admissions[capture]["request_sha256"]
                    != segment.envelope.submission.request_sha256()
                    or admissions[capture]["revision_key"] != segment.conversation_revision_sha256
                    or admissions[capture]["ordering_json"]
                    != canonical({"kind": "unordered"}).decode()
                ):
                    raise ValueError("consumer source identity or lineage mismatch")
    for operation, kind, raw in journal.connection.execute(
        "SELECT operation_id,kind,payload_bytes FROM lifecycle_journal_events ORDER BY sequence"
    ):
        if operation not in operations or kind != "group_activated":
            continue
        group = json.loads(raw)["group_key"]
        for member in state.staged[operation, group].segments:
            segment = segments[member.capture_id]
            expected_heads[segment.source.source_id] = member.capture_id
            expected_head_versions[segment.source.source_id] = (
                1 if segment.source.expected_head is None else segment.source.head_version + 1
            )
    current = {
        segment.capture_id
        for group, revision in state.current.items()
        for segment in state.revisions[group, revision].segments
    }
    for capture, segment in segments.items():
        if capture not in captures:
            continue
        source = segment.source.source_id
        expected = expected_heads.get(source)
        if expected is None:
            first = min(
                (row for row in metadata["revisions"] if row["source_id"] == source),
                key=lambda row: row["sequence"],
            )
            expected = first["capture_id"]
        if (
            sources[source]["head_capture_id"] != expected
            or sources[source]["head_version"] != expected_head_versions.get(source, 1)
            or sources[source]["historical_only"] != (expected not in current)
        ):
            raise ValueError("consumer source head or current membership mismatch")
    owned_sources = {
        segment.source.source_id for capture, segment in segments.items() if capture in captures
    }
    if any(
        json.loads(row["namespace_json"])["connector_name"] == "consumer_chat"
        and row["source_id"] not in owned_sources
        for row in authority["source_namespaces"]
    ):
        raise ValueError("orphan consumer source namespace")


def validate_consumer_evidence(files: Mapping[str, bytes]) -> dict[str, Any]:
    from open_brain_engine.engine.materializer import _profile

    try:
        metadata = _portable_record(files[CONSUMER_PATH], "consumer intake evidence")
        if set(metadata) != {"contract", "objects", "approvals", *PROJECTION_COLUMNS} or (
            metadata["contract"] != CONSUMER_CONTRACT
            or type(metadata["objects"]) is not list
            or type(metadata["approvals"]) is not list
        ):
            raise ValueError()
        objects: dict[str, tuple[str, bytes]] = {}
        for descriptor in metadata["objects"]:
            if type(descriptor) is not dict or set(descriptor) != {"sha256", "kind"}:
                raise ValueError()
            key = validate_digest(descriptor["sha256"])
            raw = files[CONSUMER_OBJECT_PREFIX + key]
            if (
                not 0 < len(raw) <= 32 * 1024 * 1024
                or sha256(raw).hexdigest() != key
                or key in objects
            ):
                raise ValueError()
            objects[key] = (descriptor["kind"], raw)
        if list(objects) != sorted(objects) or {
            path for path in files if is_consumer_path(path)
        } != {
            CONSUMER_PATH,
            *(CONSUMER_OBJECT_PREFIX + key for key in objects),
        }:
            raise ValueError("consumer object inventory mismatch")
        profile = _profile(
            Path("/synthetic/portable15-validation"), PortableSnapshot((1, 1), {}, files)
        )
        evidence = lifecycle_evidence_value(files)
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.execute("BEGIN")
            journal = restore_lifecycle_journal_v2_value(
                connection,
                evidence,
                brain_id=cast(str, evidence["brain_id"]),
                issuer_epoch=1,
            )
            state = journal._state()
            typed = {
                key
                for key, request in state.requests.items()
                if consumer_items(request) is not None
            }
            operations: dict[str, PreparedConsumerOperation] = {}
            inventory: dict[str, tuple[str, bytes]] = {}
            capture_ids: set[str] = set()
            receipts: set[str] = set()
            deliveries: set[str] = set()
            for approval in metadata["approvals"]:
                if type(approval) is not dict or set(approval) != {
                    "operation_id",
                    "root_sha256",
                    "request_sha256",
                }:
                    raise ValueError()
                operation = approval["operation_id"]
                prepared = PreparedConsumerOperation.from_loader(
                    objects[approval["root_sha256"]][1], lambda key: objects[key][1]
                )
                prepared.validate_profile(profile)
                if (
                    operation in operations
                    or prepared.request != state.requests.get(operation)
                    or (
                        prepared.intent_sha256 != approval["root_sha256"]
                        or prepared.request.request_sha256 != approval["request_sha256"]
                        or state.sessions.get(operation, prepared.value()["session_id"])
                        != prepared.value()["session_id"]
                    )
                ):
                    raise ValueError("consumer accepted approval mismatch")
                operations[operation] = prepared
                for key, value in prepared.inventory().items():
                    if key in inventory and inventory[key] != value:
                        raise ValueError()
                    inventory[key] = value
                for item in prepared.items:
                    conversation = prepared.conversation(item)
                    if conversation is None:
                        continue
                    for segment in conversation.segments:
                        for seen, identity in (
                            (capture_ids, segment.identities.capture_id),
                            (receipts, segment.identities.accepted_receipt_id),
                            (deliveries, segment.envelope.submission.delivery_id),
                        ):
                            if identity in seen:
                                raise ValueError("consumer cross-operation identity reuse")
                            seen.add(identity)
            if (
                set(operations) != typed
                or list(operations) != sorted(operations)
                or inventory != objects
            ):
                raise ValueError("consumer accepted object closure mismatch")
            expected = rebuild_projections(operations, journal)
            if any(metadata[table] != rows for table, rows in expected.items()):
                raise ValueError("consumer projection mismatch")
            _validate_sources(files, operations, journal)
        return metadata
    except PortableValidationError:
        raise
    except (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        sqlite3.Error,
        RecursionError,
    ) as error:
        raise PortableValidationError("Portable15 consumer evidence invalid") from error

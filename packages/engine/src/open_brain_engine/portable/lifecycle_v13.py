"""Closed lifecycle evidence with real retained capture payload bindings."""

from __future__ import annotations

import base64
import json
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import asdict
from hashlib import sha256
from typing import Any

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.engine.capture_lifecycle_contracts import GroupIntent, validate_digest
from open_brain_engine.engine.capture_lifecycle_journal import restore_lifecycle_journal_value
from open_brain_engine.engine.contracts import (
    CaptureAction,
    CaptureSubmissionPath,
    JournalEnvelope,
    TextPayload,
)

from .v1 import PortableValidationError, _portable_record
from .v5 import ISSUER_MIGRATION_PATH
from .v8_capture_metadata import validate_capture_metadata

LIFECYCLE_PATH = "history/capture-lifecycle/journal-v1.json"
LIFECYCLE_PAYLOAD_PREFIX = "history/capture-lifecycle/payloads/"


def is_lifecycle_path(path: str) -> bool:
    """Only the exact metadata path and digest-named raw files belong to v13."""
    if path == LIFECYCLE_PATH:
        return True
    if not path.startswith(LIFECYCLE_PAYLOAD_PREFIX):
        return False
    try:
        validate_digest(path.removeprefix(LIFECYCLE_PAYLOAD_PREFIX))
        return True
    except ValueError:
        return False


def lifecycle_evidence_sidecars(evidence: dict[str, object]) -> dict[str, bytes]:
    """Split payload custody from the metadata index without an aggregate cap."""
    payloads = evidence["payloads"]
    if not isinstance(payloads, dict):
        raise ValueError("invalid lifecycle payloads")
    files = {}
    for key, encoded in payloads.items():
        validate_digest(key)
        if not isinstance(encoded, str):
            raise ValueError("invalid lifecycle payload")
        raw = base64.b64decode(encoded, validate=True)
        if base64.b64encode(raw).decode("ascii") != encoded or sha256(raw).hexdigest() != key:
            raise ValueError("invalid lifecycle payload")
        files[LIFECYCLE_PAYLOAD_PREFIX + key] = raw
    files[LIFECYCLE_PATH] = canonical({**evidence, "payloads": sorted(payloads)})
    return files


def lifecycle_evidence_value(files: Mapping[str, bytes]) -> dict[str, Any]:
    """Assemble only the exact indexed payload inventory for journal validation."""
    metadata = _portable_record(files[LIFECYCLE_PATH], "lifecycle metadata")
    keys = metadata["payloads"]
    if not isinstance(keys, list):
        raise ValueError("invalid lifecycle payload inventory")
    for key in keys:
        validate_digest(key)
    if keys != sorted(set(keys)):
        raise ValueError("invalid lifecycle payload inventory")
    expected = {LIFECYCLE_PATH} | {LIFECYCLE_PAYLOAD_PREFIX + key for key in keys}
    if {path for path in files if is_lifecycle_path(path)} != expected:
        raise ValueError("lifecycle payload inventory mismatch")
    payloads = {}
    for key in keys:
        raw = files[LIFECYCLE_PAYLOAD_PREFIX + key]
        if sha256(raw).hexdigest() != key:
            raise ValueError("lifecycle payload hash mismatch")
        payloads[key] = base64.b64encode(raw).decode("ascii")
    return {**metadata, "payloads": payloads}


def _require_capture(
    row: dict[str, Any], envelope: JournalEnvelope, *, identities: dict[str, object]
) -> None:
    submission = envelope.submission
    expected = {
        **{key: value for key, value in identities.items() if key != "canonical"},
        "delivery_id": submission.delivery_id,
        "request_sha256": submission.request_sha256(),
        "payload_family": submission.payload.family,
        "payload_json": canonical(submission.payload.to_dict()),
        "search_text": submission.payload.search_text(),
        "file_bytes": None,
        "source_origin": submission.durable_source_origin(),
        "source_reference": submission.source_reference,
        "intent": None if submission.intent is None else submission.intent.value,
        "capture_why": submission.capture_why,
        "action": submission.action.value,
        "title": submission.title,
        "actor_id": submission.actor_id,
        "submission_path": submission.submission_path.value,
        "role_claim_json": canonical(dict(submission.role_claim)).decode(),
        "privacy_json": canonical(envelope.admitted_privacy.to_dict()).decode(),
        "provenance_json": canonical(submission.provenance.to_dict()).decode(),
    }
    # Routing is mutable owner metadata. Original payload, privacy, provenance
    # and admission request remain immutable even after later routing changes.
    if any(row[key] != value for key, value in expected.items()):
        raise ValueError("lifecycle retained capture mismatch")


def validate_lifecycle_evidence(files: Mapping[str, bytes]) -> dict[str, Any]:
    """Replay isolated evidence before proving every activated capture exists."""
    from open_brain_engine.engine.capture_lifecycle import LifecycleSegment

    try:
        evidence = lifecycle_evidence_value(files)
        issuer = json.loads(files[ISSUER_MIGRATION_PATH])
        with closing(sqlite3.connect(":memory:")) as staging:
            staging.execute("PRAGMA foreign_keys=ON")
            staging.execute("BEGIN IMMEDIATE")
            restore_lifecycle_journal_value(
                staging, evidence,
                brain_id=issuer["brain_id"], issuer_epoch=issuer["current_issuer_epoch"],
            )
        captures = {row["capture_id"]: row for row in validate_capture_metadata(files)}
        payloads = {
            key: base64.b64decode(value, validate=True)
            for key, value in evidence["payloads"].items()
        }
        staged: dict[tuple[str, str], GroupIntent] = {}
        custodied: set[tuple[str, str, str]] = set()
        activated: set[tuple[str, str]] = set()
        for event in evidence["events"]:
            operation, payload = event["operation_id"], event["payload"]
            if event["kind"] == "group_staged":
                group = GroupIntent.from_value(payload["manifest"])
                staged[operation, group.group_key] = group
            elif event["kind"] == "segment_custodied":
                custodied.add((operation, payload["group_key"], payload["capture_id"]))
            elif event["kind"] == "group_activated":
                activated.add((operation, payload["group_key"]))
            elif event["kind"] == "capture_retracted" and payload["capture_id"] not in captures:
                raise ValueError("lifecycle retracted capture missing")
        deliveries: set[str] = set()
        for (operation, group_key), group in staged.items():
            for segment in group.segments:
                row = captures.get(segment.capture_id)
                custody = (operation, group_key, segment.capture_id) in custodied
                if not custody:
                    if row is not None:
                        raise ValueError("lifecycle capture lacks retained custody")
                    continue
                retained = LifecycleSegment.from_bytes(payloads[segment.request_sha256])
                envelope = retained.envelope
                submission = envelope.submission
                if (
                    retained.binding != segment
                    or not isinstance(submission.payload, TextPayload)
                    or submission.submission_path is not CaptureSubmissionPath.OWNER
                    or submission.action is not CaptureAction.QUICK
                    or submission.tenant_id != issuer["tenant_id"]
                    or submission.payload.text.encode("utf-8") != payloads[segment.body_sha256]
                    or submission.delivery_id in deliveries
                ):
                    raise ValueError("lifecycle envelope mismatch")
                deliveries.add(submission.delivery_id)
                if row is None:
                    if (operation, group_key) in activated:
                        raise ValueError("lifecycle activated capture missing")
                else:
                    _require_capture(row, envelope, identities=asdict(retained.identities))
        return evidence
    except PortableValidationError:
        raise
    except (ValueError, TypeError, KeyError, sqlite3.Error, OverflowError, RecursionError) as error:
        raise PortableValidationError("Portable v13 lifecycle evidence invalid") from error

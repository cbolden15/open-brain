"""Closed capture-ingress custody for unreleased Portable8, not fresh authority."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from hashlib import sha256
from typing import Any

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical

from .v1 import PortableValidationError
from .v5 import ISSUER_MIGRATION_PATH
from .v8_capture_metadata import validate_capture_metadata

CUSTODY_PATH = "history/capture-custody/journal-v1.json"
MAX_CUSTODY_BYTES = 64 * 1024 * 1024
JOURNAL_TABLES = {
    "capture_ingestion_items": (
        "journal_sequence",
        "ingestion_id",
        "delivery_id",
        "request_sha256",
        "envelope_sha256",
        "submission_path",
        "byte_count",
        "queued_at",
    ),
    "capture_ingestion_payloads": ("delivery_id", "envelope_bytes"),
    "capture_ingestion_events": (
        "event_sequence",
        "delivery_id",
        "event_kind",
        "attempt_number",
        "receipt_json",
        "recorded_at",
    ),
    "capture_ingestion_tombstones": ("delivery_id", "request_sha256", "result_json", "decided_at"),
}
SEQUENCE_TABLES = frozenset({"capture_ingestion_items", "capture_ingestion_events"})
_INTEGER_FIELDS = frozenset({"journal_sequence", "event_sequence", "attempt_number", "byte_count"})


def custody_bytes(state: dict[str, Any]) -> bytes:
    raw = canonical(state)
    if len(raw) > MAX_CUSTODY_BYTES:
        raise ValueError("Portable8 capture custody exceeds bounds")
    return raw


def _canonical_object(raw: str) -> dict[str, Any]:
    value = json.loads(raw)
    if type(value) is not dict or canonical(value).decode() != raw:
        raise ValueError
    return value


def _tables(value: dict[str, Any]) -> None:
    from open_brain_engine.engine.normalization import _delivery_id, _optional_timestamp

    for table, columns in JOURNAL_TABLES.items():
        rows = value[table]
        if type(rows) is not list or len(rows) > 100000:
            raise ValueError
        previous: str | int | None = None
        for row in rows:
            if type(row) is not dict or set(row) != set(columns):
                raise ValueError
            for name, item in row.items():
                if name in _INTEGER_FIELDS:
                    if (
                        type(item) is not int
                        or item < (0 if name == "attempt_number" else 1)
                        or item > 2**63 - 1
                    ):
                        raise ValueError
                elif type(item) is not str:
                    raise ValueError
            key = row[columns[0]]
            if previous is not None and key <= previous:
                raise ValueError
            previous = key
            _delivery_id(row["delivery_id"])
            for name in ("request_sha256", "envelope_sha256"):
                if name in row and (
                    len(row[name]) != 64
                    or any(character not in "0123456789abcdef" for character in row[name])
                ):
                    raise ValueError
            if "submission_path" in row and row["submission_path"] not in {
                "owner",
                "public_job",
                "destination_bound",
            }:
                raise ValueError
            for name in ("queued_at", "recorded_at", "decided_at"):
                if name in row and _optional_timestamp(row[name]) != row[name]:
                    raise ValueError
        if table == "capture_ingestion_payloads":
            for row in rows:
                encoded = row["envelope_bytes"]
                decoded = base64.b64decode(encoded, validate=True)
                if (
                    not 0 < len(decoded) <= 16 * 1024 * 1024
                    or base64.b64encode(decoded).decode("ascii") != encoded
                ):
                    raise ValueError
                row["envelope_bytes"] = decoded
    sequences = value["sequences"]
    if type(sequences) is not dict or not set(sequences) <= SEQUENCE_TABLES:
        raise ValueError
    for table in SEQUENCE_TABLES:
        high = sequences.get(table, 0)
        if type(high) is not int or not 0 <= high <= 2**63 - 1:
            raise ValueError
        column = JOURNAL_TABLES[table][0]
        if max((row[column] for row in value[table]), default=0) > high:
            raise ValueError


def validate_capture_custody(files: Mapping[str, bytes]) -> dict[str, Any]:
    from open_brain_engine.engine.contracts import CaptureCustodyReceipt, JournalEnvelope
    from open_brain_engine.engine.ingestion import _capture_receipt, _receipt_json
    from open_brain_engine.engine.normalization import _portable_id

    try:
        raw = files[CUSTODY_PATH]
        if not 0 < len(raw) <= MAX_CUSTODY_BYTES:
            raise ValueError
        value = json.loads(raw)
        if (
            type(value) is not dict
            or set(value) != {"schema_version", "sequences", *JOURNAL_TABLES}
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or canonical(value) != raw
        ):
            raise ValueError
        _tables(value)
        issuer = json.loads(files[ISSUER_MIGRATION_PATH])
        captures = {row["delivery_id"]: row for row in validate_capture_metadata(files)}
        items = {row["delivery_id"]: row for row in value["capture_ingestion_items"]}
        if len(items) != len(value["capture_ingestion_items"]):
            raise ValueError
        if len({row["ingestion_id"] for row in items.values()}) != len(items):
            raise ValueError
        payloads = {
            row["delivery_id"]: row["envelope_bytes"] for row in value["capture_ingestion_payloads"]
        }
        if not set(payloads) <= set(items):
            raise ValueError
        events: dict[str, list[dict[str, Any]]] = {}
        for event in value["capture_ingestion_events"]:
            if event["delivery_id"] not in items:
                raise ValueError
            events.setdefault(event["delivery_id"], []).append(event)
        for delivery, item in items.items():
            _portable_id(item["ingestion_id"], "ingestion")
            history = events[delivery]
            first = history[0]
            wrapped = _canonical_object(first["receipt_json"])
            if (
                first["event_kind"] != "queued"
                or first["attempt_number"] != 0
                or set(wrapped) != {"kind", "receipt"}
                or wrapped["kind"] != "custody"
            ):
                raise ValueError
            receipt = CaptureCustodyReceipt(**wrapped["receipt"])
            if (
                _receipt_json(receipt) != first["receipt_json"]
                or receipt.protection_acknowledgement is not None
            ):
                raise ValueError
            if (
                receipt.ingestion_id != item["ingestion_id"]
                or receipt.delivery_id != delivery
                or receipt.request_sha256 != item["request_sha256"]
                or receipt.queued_at != item["queued_at"]
                or receipt.brain_id != issuer["brain_id"]
                or receipt.issuer_epoch != issuer["current_issuer_epoch"]
            ):
                raise ValueError
            envelope = None
            if delivery in payloads:
                body = payloads[delivery]
                envelope = JournalEnvelope.from_bytes(body)
                submission = envelope.submission
                if (
                    sha256(body).hexdigest() != item["envelope_sha256"]
                    or len(body) != item["byte_count"]
                    or submission.delivery_id != delivery
                    or submission.request_sha256() != item["request_sha256"]
                    or submission.tenant_id != issuer["tenant_id"]
                    or submission.submission_path.value != item["submission_path"]
                    or receipt.requested_tier != submission.requested_tier
                    or receipt.final_admitted_tier != envelope.admitted_privacy.tier
                    or submission.destination_brain_id is not None
                    and (
                        submission.destination_brain_id != receipt.brain_id
                        or submission.issuer_epoch != receipt.issuer_epoch
                    )
                ):
                    raise ValueError
            previous = "queued"
            for event in history[1:]:
                kind = event["event_kind"]
                data = _canonical_object(event["receipt_json"])
                if previous in {"accepted", "duplicate", "discarded"}:
                    raise ValueError
                if kind == "queued":
                    if (
                        previous != "quarantined"
                        or data != {"status": "queued"}
                        or event["attempt_number"] != 0
                    ):
                        raise ValueError
                elif kind in {"attempt_failed", "quarantined"}:
                    if (
                        previous == "quarantined"
                        or set(data) != {"status", "reason"}
                        or data["status"] != kind
                        or data["reason"] not in {"invalid", "retryable"}
                    ):
                        raise ValueError
                elif kind in {"accepted", "duplicate"}:
                    terminal = _capture_receipt(event["receipt_json"])
                    capture = captures[delivery]
                    if (
                        terminal is None
                        or previous == "quarantined"
                        or _receipt_json(terminal) != event["receipt_json"]
                        or terminal.capture_id != capture["capture_id"]
                        or capture["request_sha256"] != item["request_sha256"]
                    ):
                        raise ValueError
                    if (
                        type(terminal.duplicate) is not bool
                        or terminal.duplicate != (kind == "duplicate")
                        or terminal.payload_family != capture["payload_family"]
                        or terminal.state not in {"inbox", "published"}
                    ):
                        raise ValueError
                    if (
                        terminal.requested_tier != receipt.requested_tier
                        or terminal.final_admitted_tier != receipt.final_admitted_tier
                    ):
                        raise ValueError
                    if terminal.destination_brain_id is not None and (
                        terminal.destination_brain_id != receipt.brain_id
                        or terminal.issuer_epoch != receipt.issuer_epoch
                        or terminal.delivery_id != delivery
                        or terminal.request_sha256 != item["request_sha256"]
                    ):
                        raise ValueError
                else:
                    raise ValueError
                previous = kind
            if delivery not in payloads and previous not in {"accepted", "duplicate"}:
                raise ValueError
            if (
                delivery in captures
                and captures[delivery]["request_sha256"] != item["request_sha256"]
            ):
                raise ValueError
        for tombstone in value["capture_ingestion_tombstones"]:
            if tombstone["delivery_id"] in items or tombstone["delivery_id"] in captures:
                raise ValueError
            data = _canonical_object(tombstone["result_json"])
            if (
                set(data) != {"status", "reason"}
                or data["status"] != "discarded"
                or type(data["reason"]) is not str
                or not 1 <= len(data["reason"]) <= 128
            ):
                raise ValueError
            if len(tombstone["request_sha256"]) != 64 or any(
                c not in "0123456789abcdef" for c in tombstone["request_sha256"]
            ):
                raise ValueError
        return value
    except KeyError, TypeError, ValueError, StopIteration, RecursionError:
        raise PortableValidationError("invalid Portable8 capture custody") from None

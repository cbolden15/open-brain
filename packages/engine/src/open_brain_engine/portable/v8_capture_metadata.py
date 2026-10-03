"""Closed original capture projection for the unreleased lossless v8 format."""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping
from hashlib import sha256
from typing import Any

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical

from .v1 import PortableValidationError
from .v5 import EFFECTIVE_PRIVACY_PATH, decode_retained_privacy_value

CAPTURE_METADATA_PATH = "history/capture-metadata/original-v1.json"
MAX_CAPTURE_METADATA_BYTES = 64 * 1024 * 1024
CAPTURE_COLUMNS = (
    "delivery_id",
    "request_sha256",
    "capture_id",
    "accepted_receipt_id",
    "payload_family",
    "payload_json",
    "search_text",
    "file_bytes",
    "source_origin",
    "source_reference",
    "space_id",
    "intent",
    "capture_why",
    "action",
    "title",
    "accepted_at",
    "stage",
    "source_path",
    "canonical_path",
    "auto_proposal_id",
    "auto_proposal_receipt_id",
    "auto_decision_id",
    "auto_decision_receipt_id",
    "page_id",
    "publication_id",
    "publication_path",
    "enrichment_state",
    "actor_id",
    "role_claim_json",
    "privacy_json",
    "provenance_json",
    "submission_path",
)
_NULLABLE = frozenset(
    {
        "file_bytes",
        "space_id",
        "intent",
        "capture_why",
        "title",
        "canonical_path",
        "auto_proposal_id",
        "auto_proposal_receipt_id",
        "auto_decision_id",
        "auto_decision_receipt_id",
        "page_id",
        "publication_id",
        "publication_path",
        "actor_id",
        "role_claim_json",
        "privacy_json",
        "provenance_json",
        "submission_path",
    }
)


def capture_metadata_bytes(rows: list[dict[str, Any]]) -> bytes:
    encoded = []
    for row in rows:
        value = dict(row)
        for name in ("payload_json", "file_bytes"):
            if value[name] is not None:
                value[name] = base64.b64encode(value[name]).decode("ascii")
        encoded.append(value)
    raw = canonical({"schema_version": 1, "captures": encoded})
    if len(raw) > MAX_CAPTURE_METADATA_BYTES:
        raise ValueError("Portable8 capture metadata exceeds bounds")
    return raw


def validate_capture_metadata(files: Mapping[str, bytes]) -> tuple[dict[str, Any], ...]:
    """Original metadata is archive evidence, never a new submission or grant."""
    from open_brain_engine.engine.contracts import FilePayload
    from open_brain_engine.engine.materializer import _payload_search_text
    from open_brain_engine.engine.normalization import _delivery_id

    try:
        raw = files[CAPTURE_METADATA_PATH]
        if not 0 < len(raw) <= MAX_CAPTURE_METADATA_BYTES:
            raise ValueError
        value = json.loads(raw)
        if (
            type(value) is not dict
            or set(value) != {"schema_version", "captures"}
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or type(value["captures"]) is not list
            or len(value["captures"]) > 100000
            or canonical(value) != raw
        ):
            raise ValueError
        captures = {
            record["capture_id"]: (path, record)
            for path, data in files.items()
            if path.startswith("sources/captures/")
            for record in (json.loads(data),)
        }
        privacy = {
            row["capture_id"]: decode_retained_privacy_value(row["privacy_json"])
            for row in json.loads(files[EFFECTIVE_PRIVACY_PATH])["retained_privacy"]
        }
        decoded = []
        previous = ""
        deliveries: set[str] = set()
        for row in value["captures"]:
            if type(row) is not dict or set(row) != set(CAPTURE_COLUMNS):
                raise ValueError
            for key, item in row.items():
                if key == "stage":
                    if type(item) is not int or item != 3:
                        raise ValueError
                elif item is None:
                    if key not in _NULLABLE:
                        raise ValueError
                elif type(item) is not str:
                    raise ValueError
            if row["action"] not in {"quick", "canonical_note"} or row["submission_path"] not in {
                None,
                "owner",
                "public_job",
                "destination_bound",
                "import",
            }:
                raise ValueError
            capture_id = row["capture_id"]
            _delivery_id(row["delivery_id"])
            if re.fullmatch(r"[0-9a-f]{64}", row["request_sha256"]) is None:
                raise ValueError
            if capture_id <= previous or row["delivery_id"] in deliveries:
                raise ValueError
            previous = capture_id
            deliveries.add(row["delivery_id"])
            path, record = captures[capture_id]
            for key in (
                "capture_id",
                "accepted_at",
                "payload_family",
                "source_origin",
                "source_reference",
            ):
                expected = {
                    "payload_family": record["payload"]["family"],
                    "source_origin": record["source"]["origin"],
                    "source_reference": record["source"]["reference"],
                }.get(key, record.get(key))
                if row[key] != expected:
                    raise ValueError
            if row["source_path"] != path or row["privacy_json"] != privacy[capture_id]:
                raise ValueError
            receipt = next(
                item for item in record["receipt_refs"] if item["kind"] == "capture_accepted"
            )
            if row["accepted_receipt_id"] != receipt["receipt_id"]:
                raise ValueError
            for name in ("canonical_path", "publication_path"):
                if row[name] is not None and row[name] not in files:
                    raise ValueError
            for name in ("payload_json", "file_bytes"):
                if row[name] is not None:
                    data = base64.b64decode(row[name], validate=True)
                    if base64.b64encode(data).decode("ascii") != row[name]:
                        raise ValueError
                    row[name] = data
            if row["payload_json"] != canonical(record["payload"]):
                raise ValueError
            payload = record["payload"]
            if row["file_bytes"] is not None and (
                record["payload"].get("kind") != "file"
                or sha256(row["file_bytes"]).hexdigest() != record["payload"]["blob_sha256"]
            ):
                raise ValueError
            expected_search = (
                FilePayload(
                    payload["file_name"], payload["media_type"], row["file_bytes"]
                ).search_text()
                if row["file_bytes"] is not None
                else _payload_search_text(payload)
            )
            if row["search_text"] != expected_search:
                raise ValueError
            if row["actor_id"] is not None and row["actor_id"] != record["actor_id"]:
                raise ValueError
            if (
                row["role_claim_json"] is not None
                and json.loads(row["role_claim_json"]) != record["role_claim"]
            ):
                raise ValueError
            decoded.append(row)
        if {row["capture_id"] for row in decoded} != set(captures):
            raise ValueError
        return tuple(decoded)
    except KeyError, TypeError, ValueError, StopIteration, RecursionError:
        raise PortableValidationError("invalid Portable8 original capture metadata") from None

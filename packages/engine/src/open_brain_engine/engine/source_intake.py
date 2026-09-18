"""Explicit provider identity and order admission; private values never enter read DTOs."""

from __future__ import annotations

import base64
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from open_brain_engine.core.ids import portable_canonical_json_bytes

from .contracts import CaptureSubmission, CaptureSubmissionPath, FilePayload
from .t03_contracts import T03Error, _freeze, _thaw


def _identity(value: object) -> str:
    if not isinstance(value, str) or not 0 < len(value) <= 1024 or "\x00" in value:
        raise T03Error("invalid_arguments")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise T03Error("invalid_arguments") from None
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceRevisionSubmission:
    capture: CaptureSubmission
    namespace: Mapping[str, str]
    revision_key: str
    canonical_sha256: str
    expected_head: str | None
    ordering: Mapping[str, Any]
    expected_control_epoch: int
    dto_version: int = 1

    def __post_init__(self) -> None:
        if (
            type(self.dto_version) is not int
            or self.dto_version != 1
            or not isinstance(self.capture, CaptureSubmission)
            or self.capture.submission_path is not CaptureSubmissionPath.PUBLIC_JOB
            or self.capture.request_sha256() != self.canonical_sha256
            or type(self.expected_control_epoch) is not int
            or not 0 <= self.expected_control_epoch <= 9007199254740991
        ):
            raise T03Error("invalid_arguments")
        if set(self.namespace) != {"connector_name", "connection_id", "resource_id", "external_id"}:
            raise T03Error("invalid_arguments")
        namespace = {key: _identity(value) for key, value in self.namespace.items()}
        _identity(self.revision_key)
        if self.expected_head is not None:
            from .normalization import _portable_id

            _portable_id(self.expected_head, "capture")
        order = dict(self.ordering)
        kind = order.get("kind")
        if kind == "predecessor":
            if set(order) != {"kind", "revision_key"}:
                raise T03Error("invalid_arguments")
            _identity(order["revision_key"])
        elif kind == "monotonic":
            if set(order) != {"kind", "provider_namespace", "epoch", "sequence"}:
                raise T03Error("invalid_arguments")
            _identity(order["provider_namespace"])
            _identity(order["epoch"])
            if type(order["sequence"]) is not int or not 0 <= order["sequence"] <= 9007199254740991:
                raise T03Error("invalid_arguments")
        elif kind != "unordered" or set(order) != {"kind"}:
            raise T03Error("invalid_arguments")
        object.__setattr__(self, "namespace", _freeze(namespace))
        object.__setattr__(self, "ordering", _freeze(order))

    def namespace_bytes(self) -> bytes:
        return portable_canonical_json_bytes(_thaw(self.namespace))

    def custody_bytes(self) -> bytes:
        return portable_canonical_json_bytes(
            {
                "dto_version": 1,
                "namespace": _thaw(self.namespace),
                "revision_key": self.revision_key,
                "canonical_sha256": self.canonical_sha256,
                "expected_head": self.expected_head,
                "ordering": _thaw(self.ordering),
                "expected_control_epoch": self.expected_control_epoch,
                "capture": self.capture.request_value(),
                "delivery_id": self.capture.delivery_id,
                "file_bytes_base64": (
                    base64.b64encode(self.capture.payload.data).decode("ascii")
                    if isinstance(self.capture.payload, FilePayload)
                    else None
                ),
            }
        )


@dataclass(frozen=True, slots=True)
class SourceRevisionReceipt:
    source_id: str
    capture_id: str
    outcome: str
    control_epoch: int


def register_intake(
    connection: sqlite3.Connection, capture: sqlite3.Row, payload: bytes, intake: sqlite3.Row
) -> None:
    """Complete the source linkage in the same commit as capture stage three."""
    plan = json.loads(intake["plan_json"])
    epoch = connection.execute("SELECT control_epoch FROM engine_generations").fetchone()[0]
    if epoch != plan["control_epoch"]:
        raise T03Error("operation_pending")
    source_id = intake["source_id"]
    source = connection.execute(
        "SELECT * FROM logical_sources WHERE source_id=?", (source_id,)
    ).fetchone()
    if source is None:
        connection.execute(
            "INSERT INTO logical_sources VALUES(?,?,0,?,0,1,'active','available')",
            (source_id, capture["capture_id"], capture["space_id"]),
        )
        connection.execute(
            "INSERT INTO source_namespaces VALUES(?,?,?)",
            (intake["namespace_sha256"], plan["namespace_json"], source_id),
        )
    elif source["head_capture_id"] != plan["expected_head"]:
        raise T03Error("revision_changed")
    sequence = connection.execute(
        "SELECT coalesce(max(sequence),0)+1 FROM source_revisions WHERE source_id=?", (source_id,)
    ).fetchone()[0]
    connection.execute(
        "INSERT INTO source_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL)",
        (
            capture["capture_id"],
            source_id,
            sequence,
            plan["predecessor_capture_id"],
            capture["source_path"],
            sha256(payload).hexdigest(),
            payload,
            intake["request_sha256"],
            intake["revision_key"],
            json.dumps(plan["ordering"], sort_keys=True),
            capture["accepted_at"],
        ),
    )
    if source is not None:
        # A new revision inherits the current route; captured evidence remains unchanged.
        connection.execute(
            "UPDATE captures SET space_id=? WHERE capture_id=?",
            (source["space_id"], capture["capture_id"]),
        )
        connection.execute(
            "UPDATE search_documents SET space_id=? WHERE record_type='source' AND capture_id=?",
            (source["space_id"], capture["capture_id"]),
        )
        if plan["promote"]:
            connection.execute(
                "UPDATE logical_sources SET head_capture_id=?,head_version=head_version+1 "
                "WHERE source_id=?",
                (capture["capture_id"], source_id),
            )
    connection.execute(
        "INSERT OR IGNORE INTO source_aliases VALUES(?,?,?)",
        (plan["legacy_delivery_id"], source_id, intake["request_sha256"]),
    )
    receipt = {
        "source_id": source_id,
        "capture_id": capture["capture_id"],
        "outcome": "captured" if plan["promote"] else "history_only",
        "control_epoch": epoch,
    }
    connection.execute(
        "UPDATE source_intakes SET receipt_json=? WHERE delivery_id=?",
        (json.dumps(receipt, sort_keys=True), intake["delivery_id"]),
    )

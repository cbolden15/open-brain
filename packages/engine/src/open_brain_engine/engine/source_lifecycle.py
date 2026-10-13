"""Durable B withdrawal decisions.  Retired sources retain immutable evidence."""
# ruff: noqa: E501

from __future__ import annotations

import json
from hashlib import sha256
from typing import TYPE_CHECKING, cast

from open_brain_engine.core.ids import portable_canonical_json_bytes

from .consent_contracts import EgressMode
from .normalization import _timestamp
from .source_lifecycle_contracts import (
    SourceInspection,
    SourceInspectRequest,
    SourceWithdrawReceipt,
    SourceWithdrawRequest,
)
from .t03_contracts import EffectiveAuthority, T03Error

if TYPE_CHECKING:
    from .local import BrainEngine


def _owner_local(authority: EffectiveAuthority) -> None:
    if not authority.owner or authority.egress_mode is not EgressMode.OWNER_LOCAL:
        raise T03Error("unsupported_capability")


class SourceLifecycleTasks:
    def __init__(self, engine: BrainEngine) -> None:
        self._engine = engine

    def inspect(
        self, request: SourceInspectRequest, *, authority: EffectiveAuthority
    ) -> SourceInspection:
        _owner_local(authority)
        if not isinstance(request, SourceInspectRequest):
            raise T03Error("invalid_arguments")
        connection = self._engine._store.connect()
        try:
            row = connection.execute(
                "SELECT s.*, l.lifecycle_version, b.brain_id, b.issuer_epoch FROM logical_sources s "
                "JOIN source_lifecycle_state l USING(source_id) "
                "JOIN brain_identity b ON b.singleton=1 WHERE s.source_id=?", (request.source_id,)
            ).fetchone()
            if row is None:
                raise T03Error("not_found")
            event = connection.execute(
                "SELECT receipt_json FROM source_lifecycle_operations WHERE source_id=? "
                "ORDER BY sequence DESC LIMIT 1", (request.source_id,)
            ).fetchone()
        finally:
            connection.close()
        return SourceInspection(
            source_id=row["source_id"], head_capture_id=row["head_capture_id"],
            head_version=row["head_version"], route_version=row["route_version"],
            lifecycle_version=row["lifecycle_version"], lifecycle=row["lifecycle"],
            availability=row["availability"], destination_brain_id=row["brain_id"],
            issuer_epoch=row["issuer_epoch"],
            withdrawal_receipt=None if event is None else json.loads(event["receipt_json"]),
        )

    def withdraw(
        self, request: SourceWithdrawRequest, *, authority: EffectiveAuthority
    ) -> SourceWithdrawReceipt:
        _owner_local(authority)
        if not isinstance(request, SourceWithdrawRequest):
            raise T03Error("invalid_arguments")
        with (
            self._engine._writer_lease.acquire_shared_writer(),
            self._engine._store.transaction() as connection,
        ):
            identity = connection.execute(
                "SELECT brain_id,issuer_epoch FROM brain_identity WHERE singleton=1"
            ).fetchone()
            if identity is None or (identity["brain_id"], identity["issuer_epoch"]) != (
                request.brain_id,
                request.issuer_epoch,
            ):
                raise T03Error("revision_changed")
            previous = connection.execute(
                "SELECT request_sha256,receipt_json FROM source_lifecycle_operations "
                "WHERE operation_id=?",
                (request.operation_id,),
            ).fetchone()
            if previous is not None:
                if previous["request_sha256"] != request.request_sha256:
                    raise T03Error("invalid_arguments")
                retained = cast(dict[str, object], json.loads(previous["receipt_json"]))
                try:
                    return SourceWithdrawReceipt(
                        operation_id=cast(str, retained["operation_id"]),
                        request_sha256=cast(str, retained["request_sha256"]),
                        source_id=cast(str, retained["source_id"]),
                        head_capture_id=cast(str, retained["head_capture_id"]),
                        lifecycle_version=cast(int, retained["lifecycle_version"]),
                        lifecycle=cast(str, retained["lifecycle"]),
                        receipt_sha256=cast(str, retained["receipt_sha256"]),
                    )
                except (KeyError, TypeError):
                    raise T03Error("operation_pending") from None
            source = connection.execute(
                "SELECT s.*,l.lifecycle_version FROM logical_sources s "
                "JOIN source_lifecycle_state l USING(source_id) WHERE s.source_id=?",
                (request.source_id,),
            ).fetchone()
            if source is None:
                raise T03Error("not_found")
            if (
                source["head_capture_id"] != request.expected_head
                or source["lifecycle_version"] != request.expected_lifecycle_version
                or source["lifecycle"] != "active"
            ):
                raise T03Error("revision_changed")
            if connection.execute(
                "SELECT 1 FROM source_intakes WHERE source_id=? AND receipt_json IS NULL",
                (request.source_id,),
            ).fetchone() is not None:
                raise T03Error("operation_pending")
            if connection.execute(
                "SELECT 1 FROM managed_source_deliveries "
                "WHERE source_id=? AND receipt_json IS NULL",
                (request.source_id,),
            ).fetchone() is not None:
                raise T03Error("operation_pending")
            result_version = request.expected_lifecycle_version + 1
            result = {
                "operation_id": request.operation_id,
                "request_sha256": request.request_sha256,
                "source_id": request.source_id,
                "head_capture_id": request.expected_head,
                "lifecycle_version": result_version,
                "lifecycle": "retired",
            }
            result["receipt_sha256"] = sha256(portable_canonical_json_bytes(result)).hexdigest()
            sequence = connection.execute(
                "SELECT coalesce(max(sequence),0)+1 FROM source_lifecycle_operations"
            ).fetchone()[0]
            connection.execute(
                "UPDATE logical_sources SET lifecycle='retired', availability='missing' WHERE source_id=?",
                (request.source_id,),
            )
            connection.execute(
                "UPDATE source_lifecycle_state SET lifecycle_version=? WHERE source_id=?",
                (result_version, request.source_id),
            )
            connection.execute(
                "INSERT INTO source_lifecycle_operations "
                "(operation_id,request_sha256,source_id,expected_head,expected_lifecycle_version,"
                "resulting_lifecycle_version,reason_code,absence_evidence_digest,sequence,recorded_at,receipt_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (request.operation_id, request.request_sha256, request.source_id,
                 request.expected_head, request.expected_lifecycle_version, result_version,
                 request.reason_code, request.absence_evidence_digest, sequence,
                 _timestamp(self._engine._clock()), json.dumps(result, sort_keys=True)),
            )
            return SourceWithdrawReceipt(
                operation_id=request.operation_id,
                request_sha256=request.request_sha256,
                source_id=request.source_id,
                head_capture_id=request.expected_head,
                lifecycle_version=result_version,
                lifecycle="retired",
                receipt_sha256=cast(str, result["receipt_sha256"]),
            )

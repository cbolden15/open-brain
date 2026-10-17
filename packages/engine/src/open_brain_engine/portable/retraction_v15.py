"""Preserve approval1 receipt checks against the explicit successor shared replay."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from contextlib import closing
from typing import cast

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.engine.consumer_journal import restore_lifecycle_journal_v2_value
from open_brain_engine.engine.owner_retraction_contracts import approval_record, lifecycle_request

from .lifecycle_v15 import lifecycle_evidence_value
from .retraction_v14 import APPROVALS_CONTRACT, APPROVALS_PATH
from .v1 import PortableValidationError, _portable_record


def validate_approval_records(
    records: object, evidence: dict[str, object]
) -> list[dict[str, object]]:
    if type(records) is not list:
        raise PortableValidationError("Portable15 approval evidence invalid")
    try:
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.execute("BEGIN")
            journal = restore_lifecycle_journal_v2_value(
                connection,
                evidence,
                brain_id=cast(str, evidence["brain_id"]),
                issuer_epoch=1,
            )
            state = journal._state()
            validated, operations = [], []
            for raw in records:
                if type(raw) is not dict or set(raw) != {"request", "receipt"}:
                    raise ValueError()
                record = approval_record(raw["request"], raw["receipt"])
                request, receipt = (
                    cast(dict[str, object], record["request"]),
                    cast(dict[str, object], record["receipt"]),
                )
                operation = cast(str, request["operation_id"])
                expected = lifecycle_request(request)
                status = journal.status(operation)
                if (
                    state.requests.get(operation) != expected
                    or status["status"] != "committed"
                    or (receipt["journal_receipt_sha256"] != status["receipt_sha256"])
                ):
                    raise ValueError()
                impact = cast(dict[str, object], request["impact"])
                if expected.kind == "session_retract" and impact[
                    "capture_ids"
                ] != state.members.get(cast(str, expected.payload["session_id"])):
                    raise ValueError()
                operations.append(operation)
                validated.append(record)
            if operations != sorted(set(operations)):
                raise ValueError()
            return validated
    except (ValueError, TypeError, KeyError, sqlite3.Error, RecursionError) as error:
        raise PortableValidationError("Portable15 owner retraction evidence invalid") from error


def retraction_evidence_sidecar(records: object, journal: dict[str, object]) -> bytes:
    return canonical(
        {"contract": APPROVALS_CONTRACT, "records": validate_approval_records(records, journal)}
    )


def validate_retraction_evidence(files: Mapping[str, bytes]) -> list[dict[str, object]]:
    value = _portable_record(files[APPROVALS_PATH], "owner retraction evidence")
    if set(value) != {"contract", "records"} or value["contract"] != APPROVALS_CONTRACT:
        raise PortableValidationError("Portable15 owner retraction evidence invalid")
    return validate_approval_records(value["records"], lifecycle_evidence_value(files))

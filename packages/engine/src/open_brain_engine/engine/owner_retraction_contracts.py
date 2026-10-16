"""Versioned owner approval evidence, separate from the frozen lifecycle journal."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from typing import cast

from .capture_lifecycle_contracts import (
    LifecycleError,
    LifecycleRequest,
    _prefixed_uuid4,
    _uuid4,
    canonical_bytes,
    digest,
    validate_capture_id,
    validate_digest,
    validate_session_id,
)

COUNTS = (
    "selected_captures",
    "already_retracted",
    "current_groups_suppressed",
    "newer_heads_preserved",
    "dependent_pages",
    "approved_copies",
)
ERROR_CODES = frozenset(
    {
        "invalid_arguments",
        "unsupported_capability",
        "not_found",
        "preview_not_found",
        "preview_expired",
        "preview_changed",
        "revision_changed",
        "operation_conflict",
        "operation_not_found",
        "operation_pending",
        "local_operation_busy",
        "local_operation_failed",
        "invalid_evidence",
    }
)


class RetractionError(ValueError):
    def __init__(self, code: str = "invalid_arguments") -> None:
        if code not in ERROR_CODES:
            raise ValueError("invalid retraction error code")
        self.code = code
        super().__init__(code)


def preview_identifier(value: object) -> str:
    if type(value) is not str or re.fullmatch(r"retprev_[A-Za-z0-9_-]{43}", value) is None:
        raise RetractionError("invalid_arguments")
    # The last base64 sextet has two padding bits; refuse alternate encodings.
    import base64

    if (
        base64.urlsafe_b64encode(base64.urlsafe_b64decode(value[8:] + "=")).decode().rstrip("=")
        != value[8:]
    ):
        raise RetractionError("invalid_arguments")
    return value


def operation_identifier(value: object) -> str:
    try:
        return _uuid4(value)
    except LifecycleError:
        raise RetractionError("invalid_arguments") from None


def _fields(value: object, keys: set[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        raise RetractionError("invalid_evidence")
    return cast(dict[str, object], value)


def impact_value(impact: object) -> dict[str, object]:
    """Validate the complete retained impact, including objects on every page."""
    value = _fields(
        impact,
        {
            "target",
            "capture_ids",
            "membership_version",
            "membership_sha256",
            "items",
            "counts",
            "preimage_sha256",
        },
    )
    target = _fields(value["target"], {"kind", "id"})
    captures = value["capture_ids"]
    if type(captures) is not list or not 1 <= len(captures) <= 20_000:
        raise RetractionError("invalid_evidence")
    try:
        for capture in captures:
            validate_capture_id(capture)
        if len(set(captures)) != len(captures) or digest(captures) != value["membership_sha256"]:
            raise RetractionError("invalid_evidence")
        validate_digest(value["preimage_sha256"])
        if target["kind"] == "capture":
            validate_capture_id(target["id"])
            if captures != [target["id"]] or value["membership_version"] is not None:
                raise RetractionError("invalid_evidence")
        elif target["kind"] == "session":
            validate_session_id(target["id"])
            if type(value["membership_version"]) is not int or value["membership_version"] <= 0:
                raise RetractionError("invalid_evidence")
        else:
            raise RetractionError("invalid_evidence")
        counts = _fields(value["counts"], set(COUNTS))
        if any(type(count) is not int or count < 0 for count in counts.values()):
            raise RetractionError("invalid_evidence")
        items = value["items"]
        if type(items) is not list:
            raise RetractionError("invalid_evidence")
        ordered: list[tuple[str, str, str]] = []
        effects: dict[str, int] = {}
        capture_items: set[str] = set()
        for raw in items:
            item = _fields(raw, {"kind", "id", "effect"})
            kind, identity, effect = item["kind"], item["id"], item["effect"]
            if type(kind) is not str or type(identity) is not str or type(effect) is not str:
                raise RetractionError("invalid_evidence")
            if kind == "capture":
                validate_capture_id(identity)
                if effect not in {"retract", "already_retracted", "historical_only"}:
                    raise RetractionError("invalid_evidence")
                capture_items.add(identity)
            elif kind == "current_group":
                validate_digest(identity)
                if effect != "suppress_current":
                    raise RetractionError("invalid_evidence")
            elif kind in {"newer_head", "approved_copy"}:
                validate_capture_id(identity)
                if effect != ("preserve" if kind == "newer_head" else "dependency_retracted"):
                    raise RetractionError("invalid_evidence")
            elif kind == "dependent_page":
                _prefixed_uuid4(identity, "page_")
                if effect != "dependency_retracted":
                    raise RetractionError("invalid_evidence")
            else:
                raise RetractionError("invalid_evidence")
            ordered.append((kind, identity, effect))
            effects[kind] = effects.get(kind, 0) + 1
            if kind == "capture" and effect == "already_retracted":
                effects["already_retracted"] = effects.get("already_retracted", 0) + 1
        if ordered != sorted(set(ordered)) or len({item[:2] for item in ordered}) != len(ordered):
            raise RetractionError("invalid_evidence")
        expected = dict(
            zip(
                COUNTS,
                (
                    len(captures),
                    effects.get("already_retracted", 0),
                    effects.get("current_group", 0),
                    effects.get("newer_head", 0),
                    effects.get("dependent_page", 0),
                    effects.get("approved_copy", 0),
                ),
                strict=True,
            )
        )
        if capture_items != set(captures) or counts != expected:
            raise RetractionError("invalid_evidence")
        canonical_bytes(value)
        return value
    except LifecycleError, TypeError, ValueError:
        raise RetractionError("invalid_evidence") from None


def lifecycle_request(request: Mapping[str, object]) -> LifecycleRequest:
    impact = cast(dict[str, object], request["impact"])
    target = cast(dict[str, object], impact["target"])
    payload = (
        {"capture_id": target["id"]}
        if target["kind"] == "capture"
        else {
            "session_id": target["id"],
            "membership_sha256": impact["membership_sha256"],
            "membership_version": impact["membership_version"],
        }
    )
    return LifecycleRequest(
        cast(str, request["operation_id"]),
        "capture_retract" if target["kind"] == "capture" else "session_retract",
        cast(str, request["brain_id"]),
        cast(int, request["issuer_epoch"]),
        payload,
    )


def approval_request(value: object) -> dict[str, object]:
    request = _fields(
        value,
        {
            "contract",
            "operation_id",
            "brain_id",
            "issuer_epoch",
            "principal_id",
            "preview_binding_sha256",
            "lifecycle_request_sha256",
            "impact",
        },
    )
    try:
        if request["contract"] != "ob.owner-retraction-approval.v1":
            raise RetractionError("invalid_evidence")
        principal = request["principal_id"]
        if (
            type(principal) is not str
            or not 1 <= len(principal) <= 256
            or any(ord(char) < 32 or ord(char) == 127 for char in principal)
        ):
            raise RetractionError("invalid_evidence")
        validate_digest(request["preview_binding_sha256"])
        impact_value(request["impact"])
        journal_request = lifecycle_request(request)
        if request["lifecycle_request_sha256"] != journal_request.request_sha256:
            raise RetractionError("invalid_evidence")
        canonical_bytes(request)
        return request
    except LifecycleError, TypeError, ValueError, KeyError:
        raise RetractionError("invalid_evidence") from None


def make_receipt(request: dict[str, object], journal_receipt_sha256: str) -> dict[str, object]:
    approval_request(request)
    validate_digest(journal_receipt_sha256)
    receipt: dict[str, object] = {
        "contract": "ob.owner-retraction-receipt.v1",
        "operation_id": request["operation_id"],
        "approval_sha256": digest(request),
        "journal_receipt_sha256": journal_receipt_sha256,
        "counts": cast(dict[str, object], request["impact"])["counts"],
    }
    receipt["receipt_id"] = "retreceipt_" + digest(receipt)
    return receipt


def approval_record(request: object, receipt: object) -> dict[str, object]:
    request = approval_request(request)
    receipt = _fields(
        receipt,
        {
            "contract",
            "operation_id",
            "approval_sha256",
            "journal_receipt_sha256",
            "counts",
            "receipt_id",
        },
    )
    try:
        if canonical_bytes(receipt) != canonical_bytes(
            make_receipt(request, cast(str, receipt["journal_receipt_sha256"]))
        ):
            raise RetractionError("invalid_evidence")
        canonical_bytes(receipt)
        return {"request": request, "receipt": receipt}
    except LifecycleError, TypeError, ValueError:
        raise RetractionError("invalid_evidence") from None


def validate_approval_records(
    records: object,
    journal_evidence: dict[str, object],
) -> list[dict[str, object]]:
    from .capture_lifecycle_journal import restore_lifecycle_journal_value

    if type(records) is not list:
        raise RetractionError("invalid_evidence")
    try:
        with closing(sqlite3.connect(":memory:")) as connection:
            try:
                connection.execute("BEGIN")
                journal = restore_lifecycle_journal_value(
                    connection,
                    journal_evidence,
                    brain_id=cast(str, journal_evidence["brain_id"]),
                    issuer_epoch=1,
                )
                state = journal._state()
                validated = []
                operations = []
                for raw in records:
                    value = _fields(raw, {"request", "receipt"})
                    record = approval_record(value["request"], value["receipt"])
                    request = cast(dict[str, object], record["request"])
                    receipt = cast(dict[str, object], record["receipt"])
                    operation = cast(str, request["operation_id"])
                    expected = lifecycle_request(request)
                    actual = state.requests.get(operation)
                    if actual != expected or journal.status(operation)["status"] != "committed":
                        raise RetractionError("invalid_evidence")
                    if (
                        receipt["journal_receipt_sha256"]
                        != journal.status(operation)["receipt_sha256"]
                    ):
                        raise RetractionError("invalid_evidence")
                    impact = cast(dict[str, object], request["impact"])
                    if expected.kind == "session_retract" and (
                        impact["capture_ids"]
                        != state.members.get(cast(str, expected.payload["session_id"]))
                    ):
                        raise RetractionError("invalid_evidence")
                    operations.append(operation)
                    validated.append(record)
                if operations != sorted(set(operations)):
                    raise RetractionError("invalid_evidence")
                return validated
            finally:
                connection.close()
    except LifecycleError, TypeError, ValueError, KeyError, sqlite3.Error:
        raise RetractionError("invalid_evidence") from None

"""Shared destination mapping for the ``outbox.v1`` transports.

One module maps the OSS-G4 destination-bound surfaces onto the outbox's
terminal values so the in-process synthetic transport and the foreground
stdio adapter cannot drift. An engine ``CaptureReceipt`` or a
``capture-submit --json`` result document becomes a ``TerminalReceipt``; an
engine ``CaptureAdmissionError`` becomes a ``DeliveryFailure`` carrying the
stable result value and its retryable flag unchanged; a delivery conflict
is one terminal code. Nothing here verifies a receipt against an envelope:
the drain does that before any body removal, so a wrong mapping still
cannot remove a body.
"""

from __future__ import annotations

from typing import cast

from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import (
    CaptureAdmissionError,
    CaptureCustodyReceipt,
    CaptureOutcome,
    ProtectionAcknowledgement,
    verify_capture_custody_receipt,
)

from .contracts import OutboxContractError, TerminalReceipt, TerminalReceiptStatus
from .drain import DeliveryFailure

__all__ = [
    "DELIVERY_CONFLICT",
    "POLICY_MISMATCH",
    "RECEIPT_MALFORMED",
    "RECOVERY_PENDING",
    "TRANSPORT_ERROR",
    "TRANSPORT_MISUSE",
    "delivery_conflict_failure",
    "failure_from_admission_error",
    "receipt_malformed_failure",
    "terminal_receipt_from_capture_receipt",
    "terminal_receipt_from_result_document",
]

DELIVERY_CONFLICT = "delivery_conflict"
POLICY_MISMATCH = "policy_mismatch"
RECEIPT_MALFORMED = "receipt_malformed"
RECOVERY_PENDING = "recovery_pending"
TRANSPORT_ERROR = "transport_error"
TRANSPORT_MISUSE = "transport_misuse"

_RESULT_DOCUMENT_KEYS = frozenset(
    {
        "capture_id",
        "delivery_id",
        "destination_brain_id",
        "duplicate",
        "final_admitted_tier",
        "issuer_epoch",
        "payload_family",
        "protection_acknowledgement",
        "request_sha256",
        "requested_tier",
        "state",
        "status",
    }
)
_LEGACY_RESULT_DOCUMENT_KEYS = _RESULT_DOCUMENT_KEYS - {"protection_acknowledgement"}


def failure_from_admission_error(error: CaptureAdmissionError) -> DeliveryFailure:
    """One admission refusal with its stable code and retryable flag unchanged."""
    return DeliveryFailure(code=error.result.value, retryable=error.retryable)


def receipt_malformed_failure() -> DeliveryFailure:
    """One receipt document that cannot parse into the terminal contract."""
    return DeliveryFailure(code=RECEIPT_MALFORMED, retryable=False)


def delivery_conflict_failure() -> DeliveryFailure:
    """A delivery key already bound to different bytes; never retried."""
    return DeliveryFailure(code=DELIVERY_CONFLICT, retryable=False)


def terminal_receipt_from_capture_receipt(receipt: CaptureOutcome) -> TerminalReceipt:
    """Map an engine destination-bound receipt onto the outbox terminal contract."""
    if isinstance(receipt, CaptureCustodyReceipt):
        return TerminalReceipt(
            status=TerminalReceiptStatus.QUEUED,
            brain_id=receipt.brain_id,
            issuer_epoch=receipt.issuer_epoch,
            delivery_id=receipt.delivery_id,
            request_digest=receipt.request_sha256,
            final_admitted_tier=receipt.final_admitted_tier,
            protection_acknowledgement=receipt.protection_acknowledgement,
        )
    if (
        receipt.destination_brain_id is None
        or receipt.issuer_epoch is None
        or receipt.delivery_id is None
        or receipt.request_sha256 is None
    ):
        raise OutboxContractError(RECEIPT_MALFORMED)
    status = (
        TerminalReceiptStatus.DUPLICATE if receipt.duplicate else TerminalReceiptStatus.ACCEPTED
    )
    return TerminalReceipt(
        status=status,
        brain_id=receipt.destination_brain_id,
        issuer_epoch=receipt.issuer_epoch,
        delivery_id=receipt.delivery_id,
        request_digest=receipt.request_sha256,
        final_admitted_tier=receipt.final_admitted_tier,
        protection_acknowledgement=receipt.protection_acknowledgement,
    )


def terminal_receipt_from_result_document(document: object) -> TerminalReceipt:
    """Map one ``capture-submit --json`` success document onto the terminal contract."""
    if isinstance(document, dict) and document.get("status") == "queued":
        try:
            custody = verify_capture_custody_receipt(document)
        except ValueError as error:
            raise OutboxContractError(RECEIPT_MALFORMED) from error
        return TerminalReceipt(
            status=TerminalReceiptStatus.QUEUED,
            brain_id=custody.brain_id,
            issuer_epoch=custody.issuer_epoch,
            delivery_id=custody.delivery_id,
            request_digest=custody.request_sha256,
            final_admitted_tier=custody.final_admitted_tier,
            protection_acknowledgement=custody.protection_acknowledgement,
        )
    if not isinstance(document, dict) or set(document) not in {
        _RESULT_DOCUMENT_KEYS,
        _LEGACY_RESULT_DOCUMENT_KEYS,
    }:
        raise OutboxContractError(RECEIPT_MALFORMED)
    duplicate = document["duplicate"]
    if type(duplicate) is not bool or document["status"] != "captured":
        raise OutboxContractError(RECEIPT_MALFORMED)
    status = TerminalReceiptStatus.DUPLICATE if duplicate else TerminalReceiptStatus.ACCEPTED
    raw_acknowledgement = document.get("protection_acknowledgement")
    try:
        acknowledgement = (
            None
            if raw_acknowledgement is None
            else ProtectionAcknowledgement.from_dict(raw_acknowledgement)
        )
    except ValueError as error:
        raise OutboxContractError(RECEIPT_MALFORMED) from error
    return TerminalReceipt(
        status=status,
        brain_id=cast(str, document["destination_brain_id"]),
        issuer_epoch=cast(int, document["issuer_epoch"]),
        delivery_id=cast(str, document["delivery_id"]),
        request_digest=cast(str, document["request_sha256"]),
        final_admitted_tier=cast(PrivacyTier, document["final_admitted_tier"]),
        protection_acknowledgement=acknowledgement,
    )
